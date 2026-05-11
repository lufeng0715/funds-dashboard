"""End-to-end persistence test for the ETF snapshot path.

Verifies the chain `WindResult → parser → record_wind_fetch +
record_etf_snapshots` writes the audit row + every derived row into
an in-memory SQLite database, with `shares_status` carrying the
`VALID / INVALID / MISSING / NOT_APPLICABLE` enum unchanged. This is
the integration-grade complement to `tests/test_etf_parser.py` (pure
function) — together they guarantee `"INVALID" never becomes 0` from
parser through DB.

No Wind subprocess, no scheduler — just the three persistence
primitives (`record_wind_fetch` + `record_etf_snapshots`) called
inline so the test can run in 0.05s.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from funds_dashboard.db.audit import record_etf_snapshots, record_wind_fetch
from funds_dashboard.db.models import Base, EtfDailySnapshot, WindFetchAudit
from funds_dashboard.parsers.etf_snapshot import (
    EtfSnapshotInput,
    parse_etf_snapshot_rows,
)
from funds_dashboard.wind import WindResult


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine, future=True)


def _result(rows: list[list[object]]) -> WindResult:
    return WindResult(
        tool_name="fund_data:get_fund_price_indicators",
        request_payload={"codes": ["588200.SH"]},
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
        raw_stdout='{"data":{"columns":[],"rows":[]}}',
    )


def test_wind_to_audit_to_derived_chain_preserves_invalid_status(
    session: Session,
) -> None:
    """End-to-end: a Wind row with `"INVALID"` shares survives the
    full parser → audit → derived insert chain with `shares=NULL` and
    `shares_status="INVALID"`. The headline contract."""
    result = _result(
        rows=[
            [
                "INVALID-Shares-ETF",
                1.5,
                "INVALID",
                3_000_000.0,
                1.5,
                1.5,
                0.01,
                1.5,
                0.0,
                "588200.SH",
            ]
        ]
    )

    audit = record_wind_fetch(
        session,
        result=result,
        trade_date=date(2026, 5, 11),
        data_source_version="20260511#20260511T100000Z#1",
    )
    assert audit.id is not None  # FK target ready

    parsed = parse_etf_snapshot_rows(
        EtfSnapshotInput(
            wind_result=result,
            trade_date="2026-05-11",
            data_source_version=audit.data_source_version,
            wind_fetch_audit_id=audit.id,
        )
    )
    inserted = record_etf_snapshots(session, parsed)
    assert inserted == 1
    session.commit()

    snapshots = session.scalars(select(EtfDailySnapshot)).all()
    assert len(snapshots) == 1
    row = snapshots[0]
    # The headline invariant — explicit assertions for both shape and
    # value, in case some future writer "helpfully" coerces None to 0.
    assert row.shares is None
    assert row.shares != 0
    assert row.shares_status == "INVALID"
    # Other valid fields survive the conversion.
    assert row.fund_size_yuan == 3_000_000.0
    assert row.wind_fetch_audit_id == audit.id


def test_wind_secrets_redacted_in_audit_table(session: Session) -> None:
    """`record_wind_fetch` runs redact_secrets before insert (Vera
    msg=ca796844 CRITICAL-2). Even if a Wind tool stuffs the key
    into its echoed response, the audit row stays clean."""
    leaky_stdout = (
        '{"echoed_payload":{"api_key":"ak_NeverShouldHaveBeenInThis_responsePayload"}}'
    )
    result = WindResult(
        tool_name="fund_data:probe",
        request_payload={"api_key": "ak_NeverShouldHaveBeenInThis_responsePayload"},
        columns=[],
        rows=[],
        raw_stdout=leaky_stdout,
    )
    audit = record_wind_fetch(
        session,
        result=result,
        trade_date=date(2026, 5, 11),
        data_source_version="20260511#20260511T100000Z#redacted",
    )
    session.commit()

    persisted = session.scalar(select(WindFetchAudit).where(WindFetchAudit.id == audit.id))
    assert persisted is not None
    assert "ak_NeverShouldHaveBeenInThis_responsePayload" not in persisted.wind_raw_response
    assert "ak_NeverShouldHaveBeenInThis_responsePayload" not in persisted.wind_request_payload
    assert "REDACTED" in persisted.wind_raw_response


def test_multi_row_batch_preserves_per_row_status(session: Session) -> None:
    result = _result(
        rows=[
            ["A", 1.0, 100.0, 100.0, 1.0, 1.0, 0.0, 1.0, 0.0, "111.SH"],
            ["B", 2.0, "INVALID", 200.0, 2.0, 2.0, 0.0, 2.0, 0.0, "222.SH"],
            ["C", 3.0, "NOT_APPLICABLE", 300.0, 3.0, 3.0, 0.0, 3.0, 0.0, "333.SH"],
        ]
    )
    audit = record_wind_fetch(
        session,
        result=result,
        trade_date=date(2026, 5, 11),
        data_source_version="20260511#20260511T100000Z#batch",
    )
    parsed = parse_etf_snapshot_rows(
        EtfSnapshotInput(
            wind_result=result,
            trade_date="2026-05-11",
            data_source_version=audit.data_source_version,
            wind_fetch_audit_id=audit.id,
        )
    )
    inserted = record_etf_snapshots(session, parsed)
    assert inserted == 3
    session.commit()

    by_code = {
        row.windcode: row for row in session.scalars(select(EtfDailySnapshot)).all()
    }
    assert by_code["111.SH"].shares == 100.0
    assert by_code["111.SH"].shares_status == "VALID"
    assert by_code["222.SH"].shares is None
    assert by_code["222.SH"].shares_status == "INVALID"
    assert by_code["333.SH"].shares is None
    assert by_code["333.SH"].shares_status == "NOT_APPLICABLE"
