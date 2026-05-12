"""Wind `get_fund_price_indicators` → `EtfDailySnapshot` parser.

The hard contract (Linda + Vera + Nova): a Wind cell of
`"INVALID"` / `"MISSING"` / `"NOT_APPLICABLE"` / `None` for any
numeric column propagates as the corresponding enum status on the
output row — and the numeric value is None (→ SQL NULL), never 0.

That contract is what makes downstream consumers (daily-report
markdown, dashboard frontend, RAG transformer) trustworthy: a
"shares = 0" cell unambiguously means zero shares, never "Wind
didn't have the data".

This module is pure. Persistence happens in
`funds_dashboard.db.audit.record_etf_snapshot(...)` (added in a
follow-up) which writes the parser's output rows under the same
session as the originating `wind_fetch_audit`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..wind import WindResult


# --- markers Wind uses for "this number is not usable" --------------------

# These are the literal strings Wind returns when a column has no
# numeric value. The parser strips them out of numeric fields and
# records the matching enum status. `None` and empty string are
# treated as MISSING (column omitted / cell null).
_INVALID_MARKERS = frozenset({"INVALID", "invalid", "Invalid"})
_NOT_APPLICABLE_MARKERS = frozenset({"NOT_APPLICABLE", "not_applicable", "N/A", "n/a", "NA"})
_MISSING_MARKERS = frozenset({"MISSING", "missing", ""})

# Column name from the Wind `get_fund_price_indicators` response that
# anchors `shares_status` semantics. Linda v3 SSOT field dictionary
# names this `SHARES`.
_SHARES_COLUMN = "SHARES"


SharesStatus = Literal["VALID", "INVALID", "MISSING", "NOT_APPLICABLE"]
MissingReason = Literal[
    "invalid_value",
    "not_returned",
    "not_applicable",
    "non_trading_day",
    "suspended_or_abnormal",
    "wind_field_uncovered",
    "parse_error",
]

# Mapping locked by Linda msg=5520b860 — see field dictionary v3.
# `VALID` rows have `missing_reason=None`; everything else carries a
# distinct reason code so the frontend can render a faithful "why is
# this number missing" instead of a generic "no data".
_REASON_FOR_STATUS: dict[SharesStatus, MissingReason | None] = {
    "VALID": None,
    "INVALID": "invalid_value",
    "MISSING": "not_returned",
    "NOT_APPLICABLE": "not_applicable",
}


@dataclass(frozen=True)
class EtfSnapshotInput:
    """One Wind fetch worth of input to the parser.

    Caller supplies the trade_date / version / audit FK that the
    parser stamps onto every derived row — those don't come from
    Wind, they come from the scheduler's bookkeeping.
    """

    wind_result: WindResult
    trade_date: str  # ISO 8601; persistence layer converts to date
    data_source_version: str
    wind_fetch_audit_id: int


@dataclass(frozen=True)
class ParsedEtfSnapshot:
    """One derived row, ready for `EtfDailySnapshot.__init__(**asdict)`-style
    persistence. Field names line up with the SQLAlchemy model.

    `market_price` = intraday last trade (was `nav` pre-PR e — Linda
    msg=ed1d62dc rename so the column name actually matches its
    semantics). `unit_nav` = basis NAV per share (`最新单位净值` from
    `analytics_data:get_financial_data`).
    """

    wind_fetch_audit_id: int
    data_source_version: str
    windcode: str
    trade_date: str
    name: str | None
    fund_size_yuan: float | None
    market_price: float | None
    unit_nav: float | None
    cumulative_nav: float | None
    change_range: float | None
    iopv: float | None
    forward_discount: float | None
    shares: float | None
    shares_status: SharesStatus
    missing_reason: MissingReason | None


# --- internal helpers ------------------------------------------------------


def _classify_marker(value: object) -> SharesStatus | None:
    """Return the matching enum status if `value` is a Wind null marker.

    `None` is the explicit "column missing in this cell" case; treat
    as MISSING.
    """
    if value is None:
        return "MISSING"
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in _INVALID_MARKERS:
            return "INVALID"
        if stripped in _NOT_APPLICABLE_MARKERS:
            return "NOT_APPLICABLE"
        if stripped in _MISSING_MARKERS:
            return "MISSING"
    return None


def _coerce_float(value: object) -> float | None:
    """Best-effort float conversion. Returns None when value is a
    marker (caller has already detected the marker case) OR when
    conversion fails (defensive — should be unreachable in practice).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _read_cell(
    row: list[object], column_index: dict[str, int], name: str
) -> object | None:
    """Read a cell by column name; None when the column isn't in the
    response at all.
    """
    idx = column_index.get(name)
    if idx is None or idx >= len(row):
        return None
    return row[idx]


# --- the entry point ------------------------------------------------------


# --- multi-tool fallback parser (PR (d) runner-fund-data-tool-switch) ----
#
# Background: feng-lu msg=22188ff4 + Linda msg=744a40f9 + Alex msg=293ecab0
# captured that the original probe tool `fund_data:get_fund_price_indicators`
# is currently returning `TOOL_ERROR: 服务暂时不可用` for every call
# regardless of API key validity. The runner therefore can't land any
# real ETF data via that single-tool path.
#
# Switch the runner to a TWO-tool flow that uses currently-working
# Wind tools:
#   * `fund_data:get_fund_quote {"windcode": <code>}` — returns
#     intraday minute-level rows. Last row's `MATCH` (last quoted
#     price) is the best available proxy for `nav` during trading
#     hours. The same response carries no name + no fund_size +
#     no shares.
#   * `analytics_data:get_financial_data {"question":
#     "<code> 最新基金规模 中文简称"}` — Wind NL query that returns
#     a structured single-row response with columns `Wind代码` /
#     `证券简称` / `最新基金规模` (亿元). The NL routing happens
#     server-side; we consume the structured JSON it returns.
#
# Linda's no-NL-to-core-table rule (msg=64b7d14d) is satisfied
# because the analytics_data response IS structured (columns +
# typed cells), and the raw Wind response is preserved into
# `wind_fetch_audit.wind_raw_response` exactly like every other
# tool call (Vera consistency_checks.md §5 + §11).
#
# Fields the two-call flow CAN populate today:
#   - name (from analytics_data 证券简称)
#   - fund_size_yuan (from analytics_data 最新基金规模, 亿元 → 元 ×1e8)
#   - nav (from get_fund_quote last MATCH — intraday proxy, not
#     formal end-of-day NAV; see EtfDailySnapshot field note)
#
# Fields that STAY MISSING/not_returned because no currently-working
# tool surfaces them:
#   - shares (outstanding-shares-of-fund — not VOLUME)
#   - iopv, forward_discount (`get_fund_price_indicators` exclusive)
#   - cumulative_nav, change_range (formal NAV-derived fields)
#
# Linda's no-coerce-to-0 rule extended to these fields too: every
# unreached field lands None + `missing_reason="not_returned"`,
# never 0 or a fabricated value (msg=91b45123).

_QUOTE_MATCH_COL = "MATCH"

# Wind `analytics_data:get_financial_data` returns column headers that
# vary slightly depending on the question phrasing — Vera msg=e1c92857
# caught that the real Wind backend uses `基金简称_中文` even when our
# NL question asked for `中文简称`. We accept BOTH variants in priority
# order so the parser works regardless of which header form the NL
# router picked. New variants can be appended without other code changes.
_ANALYTICS_NAME_COLS: tuple[str, ...] = (
    "基金简称_中文",  # real-Wind probe shape (Vera msg=e1c92857)
    "证券简称",        # original spec / mock test shape
    "中文简称",        # historical alternate phrasing
)
_ANALYTICS_FUND_SIZE_COLS: tuple[str, ...] = (
    "最新基金规模",
    "最新规模",
    "基金规模",
    "基金规模合计",
)
# Extended NL question's new columns (real probe shape).
# `analytics_data` returns one of these aliases depending on NL routing.
_ANALYTICS_SHARES_COLS: tuple[str, ...] = (
    "最新总份额",      # 510300 probe
    "最新份额",        # 510500 / 588200 probe
    "基金份额_合计",   # alternate NL phrasing
    "总份额",          # bare-form alternative
)
_ANALYTICS_UNIT_NAV_COLS: tuple[str, ...] = (
    "最新单位净值",    # real probe shape
    "单位净值",        # bare-form
)
_ANALYTICS_IOPV_COLS: tuple[str, ...] = (
    "最新IOPV",
    "IOPV",
)
# Wind returns 总份额 in 万份 (10000-unit blocks); normalise to raw 份
# count so consumers (daily-report markdown, dashboard frontend) can
# scale to 亿份 / 万份 themselves without guessing the source unit.
_SHARES_WAN_TO_FEN = 10000.0
# Fund size from `analytics_data` arrives in 亿元; convert to 元 for
# canonical storage (matches the existing `fund_size_yuan` contract).
_YI_TO_YUAN = 1e8


@dataclass(frozen=True)
class EtfSnapshotMultiInput:
    """Input bundle for the two-call ETF snapshot parser.

    Caller (the scheduler) provides both Wind responses + the audit
    bookkeeping. The parser derives one `ParsedEtfSnapshot` per
    windcode merging the two sources.

    `quote_audit_id` is the wind_fetch_audit FK we stamp onto the
    derived row. `size_audit_id` is recorded separately in the audit
    table by the runner; the JOIN to recover both sources for a
    derived row goes through `(trade_date, data_source_version)`.
    """

    windcode: str
    quote_result: WindResult
    size_result: WindResult
    trade_date: str
    data_source_version: str
    quote_audit_id: int


def _extract_last_match_price(quote: WindResult) -> float | None:
    """Pull the last quoted price from a `get_fund_quote` response.

    The CLI returns rows in chronological order; the last row holds
    the most recent minute bar. Returns None if the column isn't
    present or no rows came back (e.g. non-trading day).
    """
    column_index = {name: i for i, name in enumerate(quote.columns)}
    idx = column_index.get(_QUOTE_MATCH_COL)
    if idx is None or not quote.rows:
        return None
    last_row = quote.rows[-1]
    if idx >= len(last_row):
        return None
    raw = last_row[idx]
    if _classify_marker(raw) is not None:
        return None
    return _coerce_float(raw)


def _first_present_index(
    column_index: dict[str, int], candidates: tuple[str, ...]
) -> int | None:
    """Return the index of the first column from `candidates` present
    in `column_index`, or None if none of them match.

    Used to handle Wind's varying NL response column names (Vera
    msg=e1c92857: real backend used `基金简称_中文` while our spec /
    mock used `证券简称`).
    """
    for name in candidates:
        idx = column_index.get(name)
        if idx is not None:
            return idx
    return None


@dataclass(frozen=True)
class _AnalyticsFields:
    """Per-windcode fields extracted from one `analytics_data` response.

    Grouped so `_extract_analytics_fields` returns a typed bundle the
    caller can deconstruct cleanly, rather than a 5-tuple of `Optional`s.
    """

    name: str | None
    fund_size_yuan: float | None
    shares: float | None
    unit_nav: float | None
    iopv: float | None


def _extract_analytics_fields(size: WindResult) -> _AnalyticsFields:
    """Pull all 5 fund fields from a Wind `analytics_data` response.

    The extended NL question (per feng-lu msg=440b79ea finding) is:
        "{windcode} 总份额 基金规模 单位净值 实时IOPV 中文简称"

    Probe shapes seen on real Wind:
        510300: ["Wind代码", "基金简称_中文", "最新总份额", "最新基金规模",
                 "最新单位净值", "单位净值币种", "证券简称", "最新IOPV", "交易时间"]
        510500: ["Wind代码", "证券简称", "最新份额", "最新规模",
                 "最新单位净值", "单位净值币种", "最新IOPV", ...]
        588200: same shape as 510500.

    Column-name probing uses fallback lists declared at module top
    so the parser stays robust when Wind's NL router picks alternate
    header forms. Unit normalisations:
      - `fund_size_yuan`: yi → 元 (×1e8)
      - `shares`:         万份 → 份 (×10000)
      - `unit_nav` / `iopv`: stored as-is (元 per share)
    """
    column_index = {name: i for i, name in enumerate(size.columns)}
    if not size.rows:
        return _AnalyticsFields(
            name=None,
            fund_size_yuan=None,
            shares=None,
            unit_nav=None,
            iopv=None,
        )
    row = size.rows[0]

    name = _extract_str_by_candidates(row, column_index, _ANALYTICS_NAME_COLS)
    fund_size_yuan = _extract_scaled_numeric(
        row, column_index, _ANALYTICS_FUND_SIZE_COLS, scale=_YI_TO_YUAN,
    )
    shares = _extract_scaled_numeric(
        row, column_index, _ANALYTICS_SHARES_COLS, scale=_SHARES_WAN_TO_FEN,
    )
    unit_nav = _extract_scaled_numeric(
        row, column_index, _ANALYTICS_UNIT_NAV_COLS, scale=1.0,
    )
    iopv = _extract_scaled_numeric(
        row, column_index, _ANALYTICS_IOPV_COLS, scale=1.0,
    )
    return _AnalyticsFields(
        name=name,
        fund_size_yuan=fund_size_yuan,
        shares=shares,
        unit_nav=unit_nav,
        iopv=iopv,
    )


def _extract_str_by_candidates(
    row: list[object],
    column_index: dict[str, int],
    candidates: tuple[str, ...],
) -> str | None:
    """Trimmed non-empty string from the first matching candidate column."""
    idx = _first_present_index(column_index, candidates)
    if idx is None or idx >= len(row):
        return None
    raw = row[idx]
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _extract_scaled_numeric(
    row: list[object],
    column_index: dict[str, int],
    candidates: tuple[str, ...],
    *,
    scale: float,
) -> float | None:
    """Float × scale from the first matching candidate column.

    Returns None when the column is missing, the cell is a Wind null
    marker (INVALID/MISSING/N/A), or coercion fails. Markers are NOT
    coerced to 0 — Linda's no-coerce-to-0 rule (msg=91b45123).
    """
    idx = _first_present_index(column_index, candidates)
    if idx is None or idx >= len(row):
        return None
    raw = row[idx]
    if _classify_marker(raw) is not None:
        return None
    numeric = _coerce_float(raw)
    if numeric is None:
        return None
    return numeric * scale


# Back-compat wrapper preserved so existing call sites (and any
# fixture-based tests) that destructure `(name, fund_size_yuan)`
# keep working. New code should use `_extract_analytics_fields()`.
def _extract_analytics_name_and_size(
    size: WindResult,
) -> tuple[str | None, float | None]:
    """Two-field subset of `_extract_analytics_fields()`. Deprecated —
    new code should use the full bundle so shares/IOPV/unit_nav can be
    surfaced from the same response. Kept as a thin alias until the
    last callers migrate.
    """
    bundle = _extract_analytics_fields(size)
    return bundle.name, bundle.fund_size_yuan


def parse_etf_snapshot_from_multi_tool(
    payload: EtfSnapshotMultiInput,
) -> ParsedEtfSnapshot:
    """Merge two Wind responses into one ETF snapshot row.

    Returns exactly one `ParsedEtfSnapshot` per windcode. The two
    sources cover complementary fields:

      * `quote_result` (`fund_data:get_fund_quote`): provides
        `market_price` = MATCH (intraday last trade). Linda hardline
        #1: this is NOT the basis NAV — that's in `unit_nav`.

      * `size_result` (`analytics_data:get_financial_data`): provides
        `name` / `fund_size_yuan` / `shares` / `unit_nav` / `iopv`
        in a single NL call when the question explicitly asks for
        those fields (`{code} 总份额 基金规模 单位净值 实时IOPV 中文简称` —
        per feng-lu msg=440b79ea Wind alt-tool finding).

    Unobtainable fields stay `None` with `missing_reason="not_returned"`
    (Linda msg=91b45123 no-coerce-to-0 rule):
      - `cumulative_nav` — analytics_data doesn't expose累计净值 today
      - `change_range`   — needs separate price-history call
      - `forward_discount` — `get_fund_price_indicators` exclusive
        (still TOOL_ERROR / schema-incompatible on the Wind backend)

    `shares_status` flips to `VALID` when shares is populated; else
    stays `MISSING/not_returned`.
    """
    market_price = _extract_last_match_price(payload.quote_result)
    fields = _extract_analytics_fields(payload.size_result)
    shares_status: SharesStatus = "VALID" if fields.shares is not None else "MISSING"
    missing_reason: MissingReason | None = (
        None if shares_status == "VALID" else "not_returned"
    )
    return ParsedEtfSnapshot(
        wind_fetch_audit_id=payload.quote_audit_id,
        data_source_version=payload.data_source_version,
        windcode=payload.windcode,
        trade_date=payload.trade_date,
        name=fields.name,
        fund_size_yuan=fields.fund_size_yuan,
        market_price=market_price,
        unit_nav=fields.unit_nav,
        cumulative_nav=None,
        change_range=None,
        iopv=fields.iopv,
        forward_discount=None,
        shares=fields.shares,
        shares_status=shares_status,
        missing_reason=missing_reason,
    )


def parse_etf_snapshot_rows(payload: EtfSnapshotInput) -> list[ParsedEtfSnapshot]:
    """Convert every row of `payload.wind_result.rows` into a
    `ParsedEtfSnapshot`.

    The parser is per-row resilient: an INVALID cell in one row does
    not derail the rest of the batch, and an INVALID cell in one
    column does not contaminate the others on the same row.
    """
    columns = payload.wind_result.columns
    column_index = {name: i for i, name in enumerate(columns)}
    shares_column_present = _SHARES_COLUMN in column_index

    parsed: list[ParsedEtfSnapshot] = []
    for row in payload.wind_result.rows:
        # --- shares status (the headline contract) --------------------
        if shares_column_present:
            shares_raw = _read_cell(row, column_index, _SHARES_COLUMN)
            marker = _classify_marker(shares_raw)
            if marker is not None:
                shares_value: float | None = None
                shares_status: SharesStatus = marker
            else:
                numeric = _coerce_float(shares_raw)
                if numeric is None:
                    shares_value = None
                    shares_status = "MISSING"
                else:
                    shares_value = numeric
                    shares_status = "VALID"
        else:
            shares_value = None
            shares_status = "MISSING"

        # --- other numerics ------------------------------------------
        # Pattern is the same for every numeric column: detect a
        # marker → leave None, else coerce. We don't surface separate
        # status fields for these in Phase 0 — the schema only carries
        # `shares_status`. When (if) the field dictionary adds
        # per-field status enums, this is where they'd land.
        def numeric(col: str) -> float | None:
            raw = _read_cell(row, column_index, col)
            if _classify_marker(raw) is not None:
                return None
            return _coerce_float(raw)

        def name_value() -> str | None:
            raw = _read_cell(row, column_index, "NAME")
            return raw if isinstance(raw, str) and raw else None

        def windcode_value() -> str:
            raw = _read_cell(row, column_index, "windcode")
            if isinstance(raw, str) and raw:
                return raw
            # Defensive: a missing windcode is a hard error — the row
            # cannot be persisted without it. Surface as ValueError so
            # the scheduler catches and audits rather than silently
            # dropping.
            raise ValueError(
                "Wind row missing required `windcode` column; cannot "
                "produce ETF snapshot. Raw row: %r" % (row,)
            )

        parsed.append(
            ParsedEtfSnapshot(
                wind_fetch_audit_id=payload.wind_fetch_audit_id,
                data_source_version=payload.data_source_version,
                windcode=windcode_value(),
                trade_date=payload.trade_date,
                name=name_value(),
                fund_size_yuan=numeric("FUNDSIZE"),
                # `MATCH` is the intraday last trade (secondary
                # market price). Maps to the renamed `market_price`.
                market_price=numeric("MATCH"),
                # `NETVALUE` (when present) is the basis unit NAV.
                # Maps to `unit_nav`.
                unit_nav=numeric("NETVALUE"),
                cumulative_nav=numeric("ACCUMULATEDNETVALUE"),
                change_range=numeric("CHANGERANGE"),
                iopv=numeric("IOPV"),
                forward_discount=numeric("FORWARDDISCOUNT"),
                shares=shares_value,
                shares_status=shares_status,
                missing_reason=_REASON_FOR_STATUS[shares_status],
            )
        )
    return parsed
