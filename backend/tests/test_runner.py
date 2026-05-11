"""Scheduler runner end-to-end tests.

The runner glues `WindClient.call → parse_etf_snapshot_rows →
record_wind_fetch + record_etf_snapshots → daily-report markdown`.
These tests exercise that chain with a stubbed WindClient so they
run in milliseconds and don't depend on the live Wind backend, but
they cover the same data shapes Linda's real probe captured in
`tests/fixtures/real_wind_samples.py`.

Three flavours are pinned:
* happy path — every ETF in the pool returns data → derived rows
  land, markdown emitted with all entries
* partial Wind failure — one ETF errors → fetch survives, that
  windcode lands as a `not_returned` row in the report, valid ETFs
  still persist
* secret resolution — encrypted `secret_config.wind_api_key` is
  preferred over `settings.wind_api_key`; if neither set, runner
  refuses cleanly (logs error, returns zero-row result, no crash)
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from funds_dashboard.config import Settings
from funds_dashboard.config_store import crypto
from funds_dashboard.db import init_sessionmaker, session_scope
from funds_dashboard.db.audit import record_etf_snapshots, record_wind_fetch
from funds_dashboard.db.models import (
    Base,
    DailyReportProvenance,
    EtfDailySnapshot,
    SecretConfig,
    WindFetchAudit,
)
from funds_dashboard.scheduler.runner import run_daily_fetch
from funds_dashboard.wind import WindError, WindResult


_REPORT_TRADE_DATE = date(2026, 5, 11)


def _make_settings(tmp_path, *, wind_api_key: str | None = "ak_runner_test_KEY_12345") -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/runner.db",
        FUNDS_DASHBOARD_MASTER_KEY="runner-master-key",
        FUNDS_DASHBOARD_ADMIN_PASSWORD_HASH=(
            "$2b$04$abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNO."
        ),
        WIND_API_KEY=wind_api_key,
        wind_cli_node_path="node",
        wind_cli_script="cli.mjs",
        daily_report_output_dir=tmp_path / "reports",
    )


@pytest.fixture(autouse=True)
def _master_key_env(monkeypatch):
    monkeypatch.setenv("FUNDS_DASHBOARD_MASTER_KEY", "runner-master-key")


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = _make_settings(tmp_path)
    init_sessionmaker(s.database_url)
    # Migration shortcut: create all tables directly (alembic is tested
    # elsewhere; here we just want a usable DB for the runner to write to).
    from sqlalchemy import create_engine

    engine = create_engine(s.database_url, future=True)
    Base.metadata.create_all(engine)
    return s


def _wind_result(windcode: str, *, shares: object = 5_000_000.0) -> WindResult:
    return WindResult(
        tool_name="fund_data:get_fund_price_indicators",
        request_payload={"codes": [windcode]},
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
                f"name-{windcode}",
                1.0,
                shares,
                1.0e10,
                1.0,
                1.0,
                0.0,
                1.0,
                0.0,
                windcode,
            ]
        ],
        raw_stdout='{"data":{}}',
    )


def test_run_daily_fetch_happy_path_writes_rows_and_markdown(settings, tmp_path) -> None:
    """Every ETF in the pool returns numeric data → all rows persist
    + markdown gets written."""

    def _fake_call(self, tool_name, payload):
        windcode = payload["codes"][0]
        return _wind_result(windcode)

    with patch("funds_dashboard.scheduler.runner.WindClient.call", _fake_call):
        result = run_daily_fetch(settings, trade_date=_REPORT_TRADE_DATE)

    assert result.audit_rows == 3
    assert result.derived_rows == 3
    assert result.failed_windcodes == []
    assert result.markdown_path is not None
    assert result.markdown_path.exists()

    # DB state
    with session_scope() as session:
        snapshots = session.scalars(select(EtfDailySnapshot)).all()
        assert {s.windcode for s in snapshots} == {
            "510300.SH",
            "510500.SH",
            "588200.SH",
        }
        audits = session.scalars(select(WindFetchAudit)).all()
        assert len(audits) == 3
        # `derived_record_count` was updated post-insert
        for a in audits:
            assert a.derived_record_count == 1

    # Markdown content
    text = result.markdown_path.read_text(encoding="utf-8")
    assert "report_type: daily_dashboard" in text
    assert "510300.SH" in text
    assert "510500.SH" in text
    assert "—" not in text.split("|")[3]  # no missing-data sentinel in valid row col


def test_run_daily_fetch_handles_invalid_shares_real_wind_shape(settings) -> None:
    """A WindResult with `SHARES="INVALID"` lands with `shares=NULL,
    shares_status="INVALID", missing_reason="invalid_value"`. The
    `FUNDSIZE` next to it is still persisted. (Anchors the runner
    contract to Linda's real-probe boundary.)"""

    def _fake_call(self, tool_name, payload):
        return _wind_result(payload["codes"][0], shares="INVALID")

    with patch("funds_dashboard.scheduler.runner.WindClient.call", _fake_call):
        result = run_daily_fetch(settings, trade_date=_REPORT_TRADE_DATE)

    assert result.derived_rows == 3
    with session_scope() as session:
        rows = session.scalars(select(EtfDailySnapshot)).all()
        for row in rows:
            assert row.shares is None
            assert row.shares != 0
            assert row.shares_status == "INVALID"
            assert row.missing_reason == "invalid_value"
            assert row.fund_size_yuan == 1.0e10  # still landed

    text = result.markdown_path.read_text(encoding="utf-8")
    assert "invalid_value" in text


def test_run_daily_fetch_partial_failure_leaves_other_rows_intact(settings) -> None:
    """One ETF fails → the failure shows up in the report as a
    not_returned row but the other ETFs still persist."""

    def _fake_call(self, tool_name, payload):
        windcode = payload["codes"][0]
        if windcode == "510500.SH":
            raise WindError("simulated backend hiccup", stdout="", stderr="net")
        return _wind_result(windcode)

    with patch("funds_dashboard.scheduler.runner.WindClient.call", _fake_call):
        result = run_daily_fetch(settings, trade_date=_REPORT_TRADE_DATE)

    assert result.audit_rows == 2  # only the two that succeeded
    assert result.derived_rows == 2
    assert result.failed_windcodes == ["510500.SH"]

    text = result.markdown_path.read_text(encoding="utf-8")
    assert "510500.SH" in text  # the failure still shows up in the table
    # Failed row marker
    assert "not_returned" in text


def test_run_daily_fetch_refuses_without_api_key(tmp_path) -> None:
    """Neither secret_config nor env carries the key → runner returns
    a zero-row result and does NOT crash. Logs are checked elsewhere."""
    s = _make_settings(tmp_path, wind_api_key=None)
    init_sessionmaker(s.database_url)
    from sqlalchemy import create_engine

    engine = create_engine(s.database_url, future=True)
    Base.metadata.create_all(engine)

    result = run_daily_fetch(s, trade_date=_REPORT_TRADE_DATE)
    assert result.audit_rows == 0
    assert result.derived_rows == 0
    assert result.markdown_path is None


def test_run_daily_fetch_prefers_encrypted_secret_over_env(settings) -> None:
    """When `secret_config.wind_api_key` is set, the runner uses THAT,
    not the env var, even if both exist. This is the Phase 0.5
    seed-then-rotate workflow's correctness condition."""
    # Seed encrypted secret with a different value than env's
    bundle = crypto.encrypt("ak_FROM_SECRET_CONFIG_xxxxxxxxxxxxxx")
    with session_scope() as session:
        session.add(
            SecretConfig(
                name="wind_api_key",
                ciphertext=bundle.ciphertext,
                nonce=bundle.nonce,
                salt=bundle.salt,
                algorithm_version=bundle.algorithm_version,
                key_version=bundle.key_version,
                updated_by="test_seed",
            )
        )

    captured_keys: list[str | None] = []

    def _fake_init(self, *, node_path, cli_script, api_key, timeout_s=60.0):
        captured_keys.append(api_key)
        self._node_path = node_path
        self._cli_script = cli_script
        self._timeout_s = timeout_s
        self._api_key = api_key

    def _fake_call(self, tool_name, payload):
        return _wind_result(payload["codes"][0])

    with patch(
        "funds_dashboard.scheduler.runner.WindClient.__init__", _fake_init
    ), patch("funds_dashboard.scheduler.runner.WindClient.call", _fake_call):
        run_daily_fetch(settings, trade_date=_REPORT_TRADE_DATE)

    assert "ak_FROM_SECRET_CONFIG_xxxxxxxxxxxxxx" in captured_keys
    # The env-supplied `ak_runner_test_KEY_12345` (from _make_settings)
    # was NOT used — encrypted store wins.
    assert "ak_runner_test_KEY_12345" not in captured_keys
