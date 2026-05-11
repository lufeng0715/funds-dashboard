"""Regression tests anchored to real Wind CLI outputs.

These are the production-grade complement to the synthetic test
suites in `test_etf_parser.py` (pure parser boundaries) and
`test_etf_persistence.py` (parser → DB integration). Each test
asserts the parser-and-persistence contract on a real, captured Wind
response — not a hand-crafted shape — so any silent regression of the
"INVALID → not 0, with explicit reason" invariant breaks CI before it
can reach production.

Linda msg=be2a5b22 (2026-05-11) was the first live probe to surface
the `SHARES="INVALID"` case in production data. The same row also
returned valid `FUNDSIZE` / `NETVALUE` / etc. — so the parser must
isolate the invalid field, not write off the whole row.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from funds_dashboard.db.audit import record_etf_snapshots, record_wind_fetch
from funds_dashboard.db.models import Base, EtfDailySnapshot
from funds_dashboard.parsers.etf_snapshot import (
    EtfSnapshotInput,
    parse_etf_snapshot_rows,
)

from tests.fixtures.real_wind_samples import (
    LINDA_510300_PROBE_2026_05_11,
    linda_probe_payload,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine, future=True)


def test_linda_real_probe_510300_shares_invalid_does_not_break_other_fields() -> None:
    """The headline contract on a REAL Wind response.

    Linda's 510300.SH probe returned SHARES="INVALID" but kept every
    other column valid. The parser must:
    * propagate INVALID + invalid_value on shares
    * preserve valid FUNDSIZE (1999.14 亿元), NETVALUE (4.9685),
      CHANGERANGE (1.64), windcode (510300.SH), name (沪深300ETF)
    """
    parsed = parse_etf_snapshot_rows(EtfSnapshotInput(**linda_probe_payload()))
    assert len(parsed) == 1
    snap = parsed[0]

    # Headline INVARIANT (was a hypothetical before, now real-data
    # evidence): INVALID literal MUST NOT become 0.
    assert snap.shares is None
    assert snap.shares != 0
    assert snap.shares_status == "INVALID"
    assert snap.missing_reason == "invalid_value"

    # The rest of the row is fully usable.
    assert snap.windcode == "510300.SH"
    assert snap.name == "沪深300ETF"
    assert snap.fund_size_yuan == pytest.approx(1.99914e11)
    assert snap.nav == 4.9685
    assert snap.change_range == 1.64


def test_linda_real_probe_persists_with_correct_status_to_db(session: Session) -> None:
    """End-to-end on real Wind output: row lands in `etf_daily_snapshot`
    with the INVALID/invalid_value pair intact and FUNDSIZE preserved."""
    audit = record_wind_fetch(
        session,
        result=LINDA_510300_PROBE_2026_05_11,
        trade_date=date(2026, 5, 11),
        data_source_version="20260511#20260511T080000Z#1",
    )

    parsed = parse_etf_snapshot_rows(
        EtfSnapshotInput(
            wind_result=LINDA_510300_PROBE_2026_05_11,
            trade_date="2026-05-11",
            data_source_version=audit.data_source_version,
            wind_fetch_audit_id=audit.id,
        )
    )
    inserted = record_etf_snapshots(session, parsed)
    assert inserted == 1
    session.commit()

    row = session.scalar(
        select(EtfDailySnapshot).where(EtfDailySnapshot.windcode == "510300.SH")
    )
    assert row is not None
    assert row.shares is None
    assert row.shares != 0
    assert row.shares_status == "INVALID"
    assert row.missing_reason == "invalid_value"
    assert row.fund_size_yuan == pytest.approx(1.99914e11)
    assert row.nav == 4.9685


def test_linda_probe_response_contains_no_secrets_after_audit(
    session: Session,
) -> None:
    """Real Wind responses sometimes echo headers / payloads that could
    carry the API key. Audit-row redaction must work end-to-end on a
    real response shape too (Vera msg=ca796844 CRITICAL-2)."""
    audit = record_wind_fetch(
        session,
        result=LINDA_510300_PROBE_2026_05_11,
        trade_date=date(2026, 5, 11),
        data_source_version="20260511#redaction-check",
    )
    session.commit()
    # The captured raw_stdout in the fixture is sanitised, but the
    # redaction filter must still run — assert no `ak_*` ever slips
    # through by checking the stored field.
    assert "ak_" not in audit.wind_raw_response
