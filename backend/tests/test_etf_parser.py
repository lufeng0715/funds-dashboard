"""ETF snapshot parser — test-first contract.

Linda hard rule + Vera consistency_checks §1: a Wind response with
`"INVALID"` / `"MISSING"` / `"NOT_APPLICABLE"` / `None` for any
numeric column **must** propagate as the corresponding enum status
value on the derived row — and the numeric field itself **must**
land as SQL NULL, never as 0.

These tests are the RED side of the test-first cycle: they pin every
boundary the parser implementation must hit, including the
particular column name variants the Wind CLI returns
(`fund_data:get_fund_price_indicators` schema per Linda v3 SSOT
field dictionary). The first commit makes them RED; the
implementation commit makes them GREEN. No "INVALID→0 coercion bug"
can land without this test breaking first.
"""

from __future__ import annotations

import pytest

from funds_dashboard.parsers.etf_snapshot import (
    EtfSnapshotInput,
    ParsedEtfSnapshot,
    parse_etf_snapshot_rows,
)
from funds_dashboard.wind import WindResult


WINDCODE = "588200.SH"
TRADE_DATE = "2026-05-11"
DATA_SOURCE_VERSION = "20260510#20260511T090000Z#1"
WIND_FETCH_AUDIT_ID = 42


def _wind_result(rows: list[list[object]]) -> WindResult:
    """Build a minimal WindResult for the get_fund_price_indicators schema."""
    return WindResult(
        tool_name="fund_data:get_fund_price_indicators",
        request_payload={"windcode": WINDCODE},
        columns=[
            "NAME",
            "MATCH",
            "SHARES",
            "FUNDSIZE",
            "NETVALUE",
            "ACCUMULATEDNETVALUE",
            "CHANGERANGE",
            "IOPV",
            "FORWARDDISCOUNT",
            "windcode",
        ],
        rows=rows,
        raw_stdout="{}",
    )


def _input(result: WindResult) -> EtfSnapshotInput:
    return EtfSnapshotInput(
        wind_result=result,
        trade_date=TRADE_DATE,
        data_source_version=DATA_SOURCE_VERSION,
        wind_fetch_audit_id=WIND_FETCH_AUDIT_ID,
    )


# --- happy path ------------------------------------------------------------


def test_parse_well_formed_row_yields_full_numeric_snapshot() -> None:
    result = _wind_result(
        rows=[
            [
                "科创50ETF",
                1.234,    # MATCH
                5_000_000.0,  # SHARES
                6_180_000.0,  # FUNDSIZE
                1.236,    # NETVALUE
                1.245,    # ACCUMULATEDNETVALUE
                0.0123,   # CHANGERANGE
                1.234,    # IOPV
                -0.0005,  # FORWARDDISCOUNT
                WINDCODE,
            ]
        ]
    )
    parsed = parse_etf_snapshot_rows(_input(result))
    assert len(parsed) == 1
    snap: ParsedEtfSnapshot = parsed[0]
    assert snap.windcode == WINDCODE
    assert snap.name == "科创50ETF"
    assert snap.shares == 5_000_000.0
    assert snap.shares_status == "VALID"
    assert snap.missing_reason is None  # VALID rows carry no reason
    assert snap.fund_size_yuan == 6_180_000.0
    # NETVALUE column → `unit_nav` (PR e rename + new column);
    # MATCH column → `market_price`. Both populated when the
    # single-tool response carries them.
    assert snap.unit_nav == 1.236
    assert snap.market_price == 1.234
    assert snap.cumulative_nav == 1.245
    assert snap.change_range == pytest.approx(0.0123)
    assert snap.iopv == 1.234
    assert snap.forward_discount == pytest.approx(-0.0005)
    assert snap.wind_fetch_audit_id == WIND_FETCH_AUDIT_ID
    assert snap.data_source_version == DATA_SOURCE_VERSION


# --- INVALID shares column (the canonical regression) ---------------------


def test_invalid_shares_does_not_coerce_to_zero() -> None:
    """The HARD rule (Linda + Vera + Nova). If shares == "INVALID":
    * `shares` MUST be None (→ SQL NULL)
    * `shares_status` MUST be `"INVALID"`
    * other numeric fields keep their valid values
    """
    result = _wind_result(
        rows=[
            [
                "黄金ETF",
                1.5,
                "INVALID",  # SHARES literally returned as "INVALID"
                3_000_000.0,
                1.5,
                1.5,
                0.01,
                1.5,
                0.0,
                WINDCODE,
            ]
        ]
    )
    parsed = parse_etf_snapshot_rows(_input(result))
    snap = parsed[0]
    assert snap.shares is None, (
        "INVALID shares must NOT coerce to 0 — that's the bug this "
        "whole test file exists to prevent."
    )
    assert snap.shares != 0, "explicit guard against the 0-coercion bug"
    assert snap.shares_status == "INVALID"
    assert snap.missing_reason == "invalid_value"  # Linda v3 mapping
    assert snap.fund_size_yuan == 3_000_000.0  # other fields unaffected


def test_missing_shares_column_marks_status_missing() -> None:
    """If the Wind response simply omits the SHARES column from
    `result.columns` (rare but allowed), parser flags it as MISSING."""
    result = WindResult(
        tool_name="fund_data:get_fund_price_indicators",
        request_payload={"windcode": WINDCODE},
        columns=[
            "NAME",
            "MATCH",
            # SHARES intentionally absent
            "FUNDSIZE",
            "NETVALUE",
            "ACCUMULATEDNETVALUE",
            "CHANGERANGE",
            "IOPV",
            "FORWARDDISCOUNT",
            "windcode",
        ],
        rows=[
            ["银行ETF", 1.0, 9_000_000.0, 1.0, 1.0, 0.0, 1.0, 0.0, WINDCODE]
        ],
        raw_stdout="{}",
    )
    parsed = parse_etf_snapshot_rows(_input(result))
    snap = parsed[0]
    assert snap.shares is None
    assert snap.shares_status == "MISSING"
    assert snap.missing_reason == "not_returned"  # Linda v3 mapping


def test_none_shares_value_marks_status_missing() -> None:
    """A null returned in the row (not omitted in columns) → MISSING."""
    result = _wind_result(
        rows=[
            ["白酒ETF", 2.0, None, 4_000_000.0, 2.0, 2.0, 0.02, 2.0, 0.0, WINDCODE]
        ]
    )
    parsed = parse_etf_snapshot_rows(_input(result))
    snap = parsed[0]
    assert snap.shares is None
    assert snap.shares_status == "MISSING"
    assert snap.missing_reason == "not_returned"


def test_not_applicable_marker_propagates() -> None:
    """Wind sometimes uses the explicit string `"NOT_APPLICABLE"`
    when a field doesn't make sense for the product type (e.g.
    money-market funds have no NAV-style fields). Parser must
    preserve the marker."""
    result = _wind_result(
        rows=[
            [
                "货币基金A",
                "NOT_APPLICABLE",
                "NOT_APPLICABLE",
                1_000_000.0,
                "NOT_APPLICABLE",
                "NOT_APPLICABLE",
                0.0,
                "NOT_APPLICABLE",
                0.0,
                WINDCODE,
            ]
        ]
    )
    parsed = parse_etf_snapshot_rows(_input(result))
    snap = parsed[0]
    assert snap.shares is None
    assert snap.shares_status == "NOT_APPLICABLE"
    assert snap.missing_reason == "not_applicable"  # Linda v3 mapping
    assert snap.unit_nav is None  # NETVALUE marker also propagates


# --- numeric robustness ----------------------------------------------------


def test_numeric_zero_stays_zero_and_status_valid() -> None:
    """0.0 is a VALID number (e.g. CHANGERANGE = 0 on a flat day)
    — the parser must NOT confuse 0 with INVALID."""
    result = _wind_result(
        rows=[
            [
                "稳定ETF",
                1.0,
                1_000_000.0,  # shares = 1M, valid
                1_000_000.0,
                1.0,
                1.0,
                0.0,    # CHANGERANGE 0 — flat day, valid
                1.0,
                0.0,
                WINDCODE,
            ]
        ]
    )
    parsed = parse_etf_snapshot_rows(_input(result))
    snap = parsed[0]
    assert snap.change_range == 0.0
    assert snap.shares == 1_000_000.0
    assert snap.shares_status == "VALID"
    assert snap.missing_reason is None  # 0.0 is a real value, not missing


def test_numeric_string_parses_to_float() -> None:
    """Wind sometimes returns numbers as strings (`"1.234"`). Parser
    must accept and convert; this is NOT an INVALID case."""
    result = _wind_result(
        rows=[
            [
                "字符串-数字ETF",
                "1.234",
                "5000000.0",  # shares as string
                "6000000.0",
                "1.236",
                "1.245",
                "0.012",
                "1.234",
                "-0.0005",
                WINDCODE,
            ]
        ]
    )
    parsed = parse_etf_snapshot_rows(_input(result))
    snap = parsed[0]
    assert snap.shares == 5_000_000.0
    assert snap.shares_status == "VALID"
    assert snap.unit_nav == 1.236  # NETVALUE column → unit_nav (PR e)


# --- multi-row + edge cases -----------------------------------------------


def test_parses_multiple_rows() -> None:
    """Wind returns a batch when multiple windcodes are queried."""
    result = _wind_result(
        rows=[
            ["ETF-A", 1.0, 100.0, 100.0, 1.0, 1.0, 0.0, 1.0, 0.0, "111.SH"],
            ["ETF-B", 2.0, "INVALID", 200.0, 2.0, 2.0, 0.0, 2.0, 0.0, "222.SH"],
        ]
    )
    parsed = parse_etf_snapshot_rows(_input(result))
    assert len(parsed) == 2
    assert parsed[0].windcode == "111.SH"
    assert parsed[0].shares_status == "VALID"
    assert parsed[1].windcode == "222.SH"
    assert parsed[1].shares is None
    assert parsed[1].shares_status == "INVALID"


def test_empty_rows_yields_empty_list() -> None:
    """No rows = no parsed snapshots, not an error."""
    result = _wind_result(rows=[])
    parsed = parse_etf_snapshot_rows(_input(result))
    assert parsed == []


def test_invalid_fund_size_marks_status_but_does_not_crash() -> None:
    """INVALID on a non-shares column still creates a row with the
    other valid fields preserved — the parser is per-row resilient."""
    result = _wind_result(
        rows=[
            [
                "FundSize-Invalid",
                1.0,
                100.0,
                "INVALID",  # FUNDSIZE invalid
                1.0,
                1.0,
                0.0,
                1.0,
                0.0,
                WINDCODE,
            ]
        ]
    )
    parsed = parse_etf_snapshot_rows(_input(result))
    snap = parsed[0]
    assert snap.fund_size_yuan is None
    # shares wasn't invalid → still VALID
    assert snap.shares == 100.0
    assert snap.shares_status == "VALID"


# --- multi-tool parser regression (Vera msg=e1c92857 HIGH#2) --------------


def _quote_result_for_parser(match_last: float = 4.951) -> "WindResult":
    """Fake `fund_data:get_fund_quote` response — last row's MATCH is
    what `parse_etf_snapshot_from_multi_tool` reads."""
    from funds_dashboard.wind import WindResult
    return WindResult(
        tool_name="fund_data:get_fund_quote",
        request_payload={"windcode": "510300.SH"},
        columns=["MATCH", "AVGPRICE", "TIME"],
        rows=[[1.0, 1.0, "09:30:00"], [match_last, 1.0, "14:59:00"]],
        raw_stdout='{}',
    )


def _size_result_for_parser(
    *, name_col: str = "基金简称_中文", name: str = "华泰柏瑞沪深300ETF",
    size_yi: float = 1686.5965,
) -> "WindResult":
    """Fake `analytics_data:get_financial_data` response with
    configurable name-column header so we can test the fallback."""
    from funds_dashboard.wind import WindResult
    return WindResult(
        tool_name="analytics_data:get_financial_data",
        request_payload={"question": "510300.SH 最新基金规模"},
        columns=["Wind代码", name_col, "最新基金规模"],
        rows=[["510300.SH", name, size_yi]],
        raw_stdout='{}',
    )


def test_multi_tool_parser_accepts_real_wind_column_name() -> None:
    """Real-Wind column name `基金简称_中文` (Vera msg=e1c92857) must
    be parsed correctly. Previously the parser only looked up
    `证券简称` so `name` came back None despite valid data being
    present."""
    from funds_dashboard.parsers.etf_snapshot import (
        EtfSnapshotMultiInput,
        parse_etf_snapshot_from_multi_tool,
    )
    snap = parse_etf_snapshot_from_multi_tool(EtfSnapshotMultiInput(
        windcode="510300.SH",
        quote_result=_quote_result_for_parser(match_last=4.951),
        size_result=_size_result_for_parser(name_col="基金简称_中文"),
        trade_date="2026-05-11",
        data_source_version="v1",
        quote_audit_id=1,
    ))
    assert snap.name == "华泰柏瑞沪深300ETF"
    assert snap.fund_size_yuan == 1686.5965 * 1e8
    # MATCH from `fund_data:get_fund_quote` → `market_price` (PR e
    # rename — Linda hardline #1: this is NOT the basis NAV).
    assert snap.market_price == 4.951


def test_multi_tool_parser_falls_back_to_legacy_column_name() -> None:
    """Backwards compat: if Wind's NL router still emits the
    original `证券简称` header on some queries, the parser's fallback
    list catches it."""
    from funds_dashboard.parsers.etf_snapshot import (
        EtfSnapshotMultiInput,
        parse_etf_snapshot_from_multi_tool,
    )
    snap = parse_etf_snapshot_from_multi_tool(EtfSnapshotMultiInput(
        windcode="510300.SH",
        quote_result=_quote_result_for_parser(),
        size_result=_size_result_for_parser(name_col="证券简称"),
        trade_date="2026-05-11",
        data_source_version="v1",
        quote_audit_id=1,
    ))
    assert snap.name == "华泰柏瑞沪深300ETF"


def test_multi_tool_parser_no_match_returns_none() -> None:
    """Neither `基金简称_中文` nor `证券简称` nor `中文简称` present →
    name stays None. (Defensive: parser doesn't fabricate from
    arbitrary columns.)"""
    from funds_dashboard.parsers.etf_snapshot import (
        EtfSnapshotMultiInput,
        parse_etf_snapshot_from_multi_tool,
    )
    snap = parse_etf_snapshot_from_multi_tool(EtfSnapshotMultiInput(
        windcode="510300.SH",
        quote_result=_quote_result_for_parser(),
        size_result=_size_result_for_parser(name_col="其他列名"),
        trade_date="2026-05-11",
        data_source_version="v1",
        quote_audit_id=1,
    ))
    assert snap.name is None


# --- PR e: extended NL question (shares + unit_nav + iopv) -----------------


def _extended_nl_size_result(
    *,
    windcode: str = "510300.SH",
    name: str = "华泰柏瑞沪深300ETF",
    shares_wan_fen: float = 4_482_858.769,
    size_yi: float = 1686.5965,
    unit_nav: float = 4.9685,
    iopv: float = 4.966,
    shares_col: str = "最新总份额",
    unit_nav_col: str = "最新单位净值",
    iopv_col: str = "最新IOPV",
) -> "WindResult":
    """Real-probe shape from the extended NL question
    `"{code} 总份额 基金规模 单位净值 实时IOPV 中文简称"` — Alex
    msg=2e6f71dc verified this returns ALL 5 fields in one call."""
    from funds_dashboard.wind import WindResult
    return WindResult(
        tool_name="analytics_data:get_financial_data",
        request_payload={"question": f"{windcode} 总份额 基金规模 单位净值 实时IOPV 中文简称"},
        columns=[
            "Wind代码", "基金简称_中文", shares_col, "最新基金规模",
            unit_nav_col, "单位净值币种", "证券简称", iopv_col, "交易时间",
        ],
        rows=[[windcode, name, shares_wan_fen, size_yi, unit_nav, "人民币元",
               name, iopv, "20260512 15:00:22"]],
        raw_stdout='{}',
    )


def test_multi_tool_parser_extracts_all_five_fields_from_extended_nl() -> None:
    """PR e core invariant — extended NL question fills shares /
    unit_nav / iopv on top of name + fund_size_yuan from the SAME
    analytics_data call. `market_price` still comes from MATCH on
    the separate quote call."""
    from funds_dashboard.parsers.etf_snapshot import (
        EtfSnapshotMultiInput,
        parse_etf_snapshot_from_multi_tool,
    )
    snap = parse_etf_snapshot_from_multi_tool(EtfSnapshotMultiInput(
        windcode="510300.SH",
        quote_result=_quote_result_for_parser(match_last=4.951),
        size_result=_extended_nl_size_result(),
        trade_date="2026-05-12",
        data_source_version="v1",
        quote_audit_id=1,
    ))
    assert snap.name == "华泰柏瑞沪深300ETF"
    assert snap.fund_size_yuan == pytest.approx(1686.5965 * 1e8)
    # 万份 → 份 normalisation × 10000
    assert snap.shares == pytest.approx(4_482_858.769 * 10000)
    # unit_nav stored as-is (元 per share)
    assert snap.unit_nav == 4.9685
    assert snap.iopv == 4.966
    # quote.MATCH → market_price
    assert snap.market_price == 4.951
    # Headline contract: with valid shares the status flips to VALID
    assert snap.shares_status == "VALID"
    assert snap.missing_reason is None


def test_multi_tool_parser_handles_alternate_shares_column_name() -> None:
    """510500 / 588200 probes used `最新份额` instead of `最新总份额`.
    Parser falls through the candidate list."""
    from funds_dashboard.parsers.etf_snapshot import (
        EtfSnapshotMultiInput,
        parse_etf_snapshot_from_multi_tool,
    )
    snap = parse_etf_snapshot_from_multi_tool(EtfSnapshotMultiInput(
        windcode="510500.SH",
        quote_result=_quote_result_for_parser(),
        size_result=_extended_nl_size_result(
            windcode="510500.SH",
            shares_col="最新份额",
            shares_wan_fen=943_656.8617,
        ),
        trade_date="2026-05-12",
        data_source_version="v1",
        quote_audit_id=1,
    ))
    assert snap.shares == pytest.approx(943_656.8617 * 10000)
    assert snap.shares_status == "VALID"


def test_multi_tool_parser_shares_invalid_marker_propagates() -> None:
    """A Wind INVALID/missing marker in shares cell must NOT be coerced
    to 0 — Linda msg=91b45123 no-coerce-to-0 rule. The parser leaves
    shares=None + status=MISSING (extended NL with marker)."""
    from funds_dashboard.parsers.etf_snapshot import (
        EtfSnapshotMultiInput,
        parse_etf_snapshot_from_multi_tool,
    )
    from funds_dashboard.wind import WindResult
    size = WindResult(
        tool_name="analytics_data:get_financial_data",
        request_payload={"question": "510300.SH 总份额 ..."},
        columns=["Wind代码", "基金简称_中文", "最新总份额", "最新基金规模", "最新单位净值"],
        rows=[["510300.SH", "华泰柏瑞沪深300ETF", "INVALID", 1686.5965, 4.9685]],
        raw_stdout='{}',
    )
    snap = parse_etf_snapshot_from_multi_tool(EtfSnapshotMultiInput(
        windcode="510300.SH",
        quote_result=_quote_result_for_parser(),
        size_result=size,
        trade_date="2026-05-12",
        data_source_version="v1",
        quote_audit_id=1,
    ))
    assert snap.shares is None
    assert snap.shares != 0  # NEVER 0
    assert snap.shares_status == "MISSING"
    assert snap.missing_reason == "not_returned"
    # Other fields still extracted (defensive — one bad cell does not
    # corrupt the rest of the row).
    assert snap.fund_size_yuan == pytest.approx(1686.5965 * 1e8)
    assert snap.unit_nav == 4.9685
