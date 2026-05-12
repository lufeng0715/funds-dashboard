"""Tests for the ETF snapshot read endpoints.

Background: feng-lu's 2026-05-11 19:27 + 20:03 messages ("运行我看看"
/ "没有看到") exposed that the Phase 1 data pipeline lands rows into
`etf_daily_snapshot` but there was no HTTP surface for the UI to
display them. PR `feat/etf-snapshots-api-and-page` introduces the
`/api/v1/etf/snapshots` + `/api/v1/etf/provenance` endpoints; this
file pins their contract.

Contracts under test:
  - auth required (same shape as `/config/*`)
  - empty-DB case returns 200 with empty rows (UI renders empty state)
  - explicit `trade_date` filters rows
  - default (no trade_date) picks the latest day with data
  - `shares_status` + `missing_reason` propagate verbatim (Linda's
    no-coerce-to-0 rule at the JSON boundary)
  - `--force` rerun: multiple `data_source_version`s for same date
    come back deduplicated in the response array
  - `/provenance` 404s when no row, returns full row when found
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from funds_dashboard.auth.password import hash_password
from funds_dashboard.config import Settings, get_settings
from funds_dashboard.db.models import (
    Base,
    DailyReportProvenance,
    EtfDailySnapshot,
    WindFetchAudit,
)
from funds_dashboard.main import create_app


ADMIN_PASSWORD = "correct horse battery staple"
MASTER_KEY = "test-master-key-for-etf-api"


@pytest.fixture(autouse=True)
def _crypto_master_key_env(monkeypatch):
    monkeypatch.setenv("FUNDS_DASHBOARD_MASTER_KEY", MASTER_KEY)


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/etf.db",
        FUNDS_DASHBOARD_MASTER_KEY=MASTER_KEY,
        FUNDS_DASHBOARD_ADMIN_PASSWORD_HASH=hash_password(ADMIN_PASSWORD, rounds=4),
        wind_cli_node_path="node",
        wind_cli_script="cli.mjs",
    )


def _client(settings: Settings) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    return TestClient(app)


def _login(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
    )
    assert resp.status_code == 200, resp.text


def _seed_snapshot(
    session: Session,
    *,
    windcode: str,
    trade_date: date,
    name: str,
    fund_size_yuan: float | None,
    shares: float | None,
    shares_status: str,
    missing_reason: str | None,
    data_source_version: str,
) -> None:
    """Helper to insert one EtfDailySnapshot row + its parent audit."""
    audit = WindFetchAudit(
        wind_tool_name="fund_data:get_fund_price_indicators",
        wind_request_payload='{"codes":["x"]}',
        wind_raw_response='{"ok":true}',
        wind_fetch_timestamp=datetime.now(timezone.utc),
        trade_date=trade_date,
        data_source_version=data_source_version,
        derived_record_count=1,
    )
    session.add(audit)
    session.flush()
    session.add(
        EtfDailySnapshot(
            windcode=windcode,
            trade_date=trade_date,
            name=name,
            fund_size_yuan=fund_size_yuan,
            nav=None,
            cumulative_nav=None,
            change_range=None,
            iopv=None,
            forward_discount=None,
            shares=shares,
            shares_status=shares_status,
            missing_reason=missing_reason,
            data_source_version=data_source_version,
            wind_fetch_audit_id=audit.id,
        )
    )


def _session_for(settings: Settings) -> Session:
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        future=True,
    )
    return Session(engine, future=True)


@pytest.mark.parametrize(
    "path",
    ["/api/v1/etf/snapshots", "/api/v1/etf/provenance"],
)
def test_etf_endpoints_require_admin_session(tmp_path, path) -> None:
    """Mirror `/config/*` auth contract: no session → 401."""
    settings = _settings(tmp_path)
    client = _client(settings)
    resp = client.get(path)
    assert resp.status_code == 401, (path, resp.text)


def test_snapshots_empty_db_returns_200_with_no_rows(tmp_path) -> None:
    """UI-friendly: empty DB → 200 + empty rows, not 404. Lets the
    dashboard render an empty state without a separate error path."""
    settings = _settings(tmp_path)
    client = _client(settings)
    _login(client)
    resp = client.get("/api/v1/etf/snapshots")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rows"] == []
    assert body["data_source_versions"] == []


def test_snapshots_default_returns_latest_trade_date(tmp_path) -> None:
    """When `trade_date` is omitted, the endpoint picks the most
    recent day with at least one row — this is the dashboard's
    default first-paint behaviour."""
    settings = _settings(tmp_path)
    client = _client(settings)
    with _session_for(settings) as session:
        _seed_snapshot(
            session,
            windcode="510300.SH",
            trade_date=date(2026, 5, 10),
            name="华泰柏瑞沪深300ETF",
            fund_size_yuan=1.0e11,
            shares=1_000_000.0,
            shares_status="VALID",
            missing_reason=None,
            data_source_version="20260510#xxx#1",
        )
        _seed_snapshot(
            session,
            windcode="510500.SH",
            trade_date=date(2026, 5, 11),
            name="南方中证500ETF",
            fund_size_yuan=4.97e10,
            shares=None,
            shares_status="MISSING",
            missing_reason="not_returned",
            data_source_version="20260511#xxx#1",
        )
        session.commit()
    _login(client)
    resp = client.get("/api/v1/etf/snapshots")
    body = resp.json()
    assert body["trade_date"] == "2026-05-11"  # latest, not 2026-05-10
    assert len(body["rows"]) == 1
    assert body["rows"][0]["windcode"] == "510500.SH"


def test_snapshots_preserves_missing_status_verbatim(tmp_path) -> None:
    """Linda's no-coerce-to-0 rule extends to the JSON boundary:
    `shares_status="MISSING"` + `shares=None` come back exactly as
    stored. UI MUST be able to distinguish "no data" from "0"."""
    settings = _settings(tmp_path)
    client = _client(settings)
    with _session_for(settings) as session:
        _seed_snapshot(
            session,
            windcode="588200.SH",
            trade_date=date(2026, 5, 11),
            name="嘉实上证科创板芯片ETF",
            fund_size_yuan=4.79e10,
            shares=None,
            shares_status="MISSING",
            missing_reason="not_returned",
            data_source_version="20260511#xxx#1",
        )
        session.commit()
    _login(client)
    resp = client.get("/api/v1/etf/snapshots?trade_date=2026-05-11")
    body = resp.json()
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["shares"] is None
    assert row["shares"] != 0
    assert row["shares_status"] == "MISSING"
    assert row["missing_reason"] == "not_returned"


def test_snapshots_force_rerun_returns_latest_row_per_windcode(tmp_path) -> None:
    """`--force` rerun creates a second `data_source_version` for the
    same `(windcode, trade_date)` triple. The dashboard main table must
    show only the latest effective row per ETF while retaining all
    versions in `data_source_versions` for audit/provenance."""
    settings = _settings(tmp_path)
    client = _client(settings)
    with _session_for(settings) as session:
        for seq in (1, 2):
            _seed_snapshot(
                session,
                windcode="510300.SH",
                trade_date=date(2026, 5, 11),
                name="华泰柏瑞沪深300ETF",
                fund_size_yuan=1.0e11,
                shares=1_000_000.0 + seq,  # distinguish by value
                shares_status="VALID",
                missing_reason=None,
                data_source_version=f"20260511#xxx#{seq}",
            )
        session.commit()
    _login(client)
    resp = client.get("/api/v1/etf/snapshots?trade_date=2026-05-11")
    body = resp.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["data_source_version"] == "20260511#xxx#2"
    assert body["rows"][0]["shares"] == 1_000_002.0
    assert set(body["data_source_versions"]) == {
        "20260511#xxx#1",
        "20260511#xxx#2",
    }


def test_snapshots_real_versions_outrank_demo_rows(tmp_path) -> None:
    """Demo rows are historical placeholders. If a real fetch exists
    for the same ETF/date, the main table must choose the real row even
    when the demo row's timestamp sorts later lexicographically."""
    settings = _settings(tmp_path)
    client = _client(settings)
    with _session_for(settings) as session:
        _seed_snapshot(
            session,
            windcode="510300.SH",
            trade_date=date(2026, 5, 11),
            name="demo",
            fund_size_yuan=1.0e11,
            shares=None,
            shares_status="MISSING",
            missing_reason="not_returned",
            data_source_version="20260511#20260512T235959Z#demo",
        )
        _seed_snapshot(
            session,
            windcode="510300.SH",
            trade_date=date(2026, 5, 11),
            name="real",
            fund_size_yuan=1.1e11,
            shares=None,
            shares_status="MISSING",
            missing_reason="not_returned",
            data_source_version="20260511#20260512T010000Z#2",
        )
        session.commit()
    _login(client)
    resp = client.get("/api/v1/etf/snapshots?trade_date=2026-05-11")
    body = resp.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["name"] == "real"
    assert body["rows"][0]["data_source_version"] == "20260511#20260512T010000Z#2"


def test_provenance_returns_404_when_missing(tmp_path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    _login(client)
    resp = client.get("/api/v1/etf/provenance?trade_date=2026-05-11")
    assert resp.status_code == 404


def test_provenance_happy_path(tmp_path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    with _session_for(settings) as session:
        session.add(
            DailyReportProvenance(
                report_date=date(2026, 5, 11),
                markdown_path="daily-reports/2026-05-11.md",
                data_source_versions="20260511#xxx#1,20260511#xxx#2",
            )
        )
        session.commit()
    _login(client)
    resp = client.get("/api/v1/etf/provenance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["report_date"] == "2026-05-11"
    assert body["markdown_path"] == "daily-reports/2026-05-11.md"
    assert "20260511#xxx#1" in body["data_source_versions"]
    assert "20260511#xxx#2" in body["data_source_versions"]
