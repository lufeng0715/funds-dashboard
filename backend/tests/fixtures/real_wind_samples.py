"""Real Wind CLI responses captured by Linda's probe — production
regression fixtures.

Linda msg=be2a5b22 (2026-05-11): the first live Wind probe against
`fund_data.get_fund_price_indicators` for `510300.SH` returned
`SHARES="INVALID"` — confirming that the "INVALID" literal isn't a
test-time hypothetical but a real production return value from the
Wind backend.

Every parser change after PR #3 must continue to handle this sample
correctly. The fixtures here are imported by `test_etf_parser.py`
and `test_etf_persistence.py` so any regression of the
`INVALID → shares_status="INVALID" + missing_reason="invalid_value"`
invariant breaks CI before it can land.

When a new live Wind probe surfaces another boundary, copy the raw
columns + rows in here as a new sample with the timestamp + source
message id so the audit trail is durable.
"""

from __future__ import annotations

from typing import Any

from funds_dashboard.wind import WindResult


# Linda's first live probe (2026-05-11 ~07:36, msg=be2a5b22).
# Result: 510300.SH (沪深300ETF华泰柏瑞), `SHARES` field came back as
# the literal string "INVALID" — exact validation of the parser
# contract.
LINDA_510300_PROBE_2026_05_11 = WindResult(
    tool_name="fund_data:get_fund_price_indicators",
    request_payload={
        "codes": ["510300.SH"],
        "indexes": (
            "中文简称,最新成交价,涨跌幅,基金最新份额,基金规模,"
            "最新净值,累计净值,成交额"
        ),
    },
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
    rows=[
        [
            "沪深300ETF",
            4.966,
            "INVALID",           # SHARES — the contract case
            1.99914e11,          # FUNDSIZE — 1999.14 亿元
            4.9685,              # NETVALUE
            4.9685,              # ACCUMULATEDNETVALUE (placeholder)
            1.64,                # CHANGERANGE
            4.966,               # IOPV (placeholder ≈ MATCH)
            0.0,                 # FORWARDDISCOUNT (placeholder)
            "510300.SH",
        ]
    ],
    raw_stdout=(
        '{"ok":true,"server_type":"fund_data",'
        '"tool":"get_fund_price_indicators",'
        '"content":[{"type":"text","text":"<truncated for fixture>"}]}'
    ),
)


def linda_probe_payload() -> dict[str, Any]:
    """Helper for tests that want the input shape without the WindResult wrapping."""
    return {
        "wind_result": LINDA_510300_PROBE_2026_05_11,
        "trade_date": "2026-05-11",
        "data_source_version": "20260511#20260511T080000Z#1",
        "wind_fetch_audit_id": 1,
    }
