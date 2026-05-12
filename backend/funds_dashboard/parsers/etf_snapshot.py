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
    """

    wind_fetch_audit_id: int
    data_source_version: str
    windcode: str
    trade_date: str
    name: str | None
    fund_size_yuan: float | None
    nav: float | None
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
    "基金规模",
)


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


def _extract_analytics_name_and_size(
    size: WindResult,
) -> tuple[str | None, float | None]:
    """Pull `(name, fund_size_yuan)` from a Wind analytics_data response.

    The probe shape:
        columns = ["Wind代码", "基金简称_中文", "最新基金规模"]
        rows = [["510300.SH", "华泰柏瑞沪深300ETF", 1686.5965]]
    Fund size is in 亿元 — multiply by 1e8 to normalize to 元 (the
    canonical unit `EtfDailySnapshot.fund_size_yuan` uses).

    Column-name probing uses the fallback lists declared at module
    top so the parser keeps working when Wind's NL router picks a
    different header form for the same logical field.
    """
    column_index = {name: i for i, name in enumerate(size.columns)}
    if not size.rows:
        return None, None
    row = size.rows[0]
    name_idx = _first_present_index(column_index, _ANALYTICS_NAME_COLS)
    size_idx = _first_present_index(column_index, _ANALYTICS_FUND_SIZE_COLS)
    name: str | None = None
    if name_idx is not None and name_idx < len(row):
        raw_name = row[name_idx]
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
    fund_size_yuan: float | None = None
    if size_idx is not None and size_idx < len(row):
        raw_size = row[size_idx]
        if _classify_marker(raw_size) is None:
            yi = _coerce_float(raw_size)
            if yi is not None:
                fund_size_yuan = yi * 1e8
    return name, fund_size_yuan


def parse_etf_snapshot_from_multi_tool(
    payload: EtfSnapshotMultiInput,
) -> ParsedEtfSnapshot:
    """Merge two Wind responses into one ETF snapshot row.

    Returns exactly one `ParsedEtfSnapshot` (vs the original
    single-tool parser which returned one-per-Wind-row, because
    `get_fund_price_indicators` could return multiple windcodes
    in one call). Here the caller passes one windcode at a time so
    the output is always a single row.

    Unobtainable fields (`shares`, `iopv`, `forward_discount`,
    `cumulative_nav`, `change_range`) come back as `None` with
    `missing_reason="not_returned"`. Linda's no-coerce-to-0 rule
    (msg=91b45123) extends to those: never 0, never fabricated.
    """
    nav = _extract_last_match_price(payload.quote_result)
    name, fund_size_yuan = _extract_analytics_name_and_size(payload.size_result)
    return ParsedEtfSnapshot(
        wind_fetch_audit_id=payload.quote_audit_id,
        data_source_version=payload.data_source_version,
        windcode=payload.windcode,
        trade_date=payload.trade_date,
        name=name,
        fund_size_yuan=fund_size_yuan,
        nav=nav,
        cumulative_nav=None,
        change_range=None,
        iopv=None,
        forward_discount=None,
        shares=None,
        shares_status="MISSING",
        missing_reason="not_returned",
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
                nav=numeric("NETVALUE"),
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
