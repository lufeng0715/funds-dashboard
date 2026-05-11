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
            )
        )
    return parsed
