"""CLI entry-point tests.

These tests pin behaviours of the `funds-dashboard-fetch` CLI that
were silently broken until the 2026-05-11 real-run debug session
(Alex msg=293ecab0 in #基金数据每日汇报:da92d052) — most notably
the fact that the CLI was bypassing FastAPI's lifespan hook and
therefore never initialised the global session factory, leading to
a `RuntimeError: session factory not initialized` at first fetch.

Vera's QA review (msg=4342f203 / msg=6f3c678e) called out the
test-coverage gap directly: every existing test wired session
factory by hand and then constructed the runner — none of them
actually exercised the `cli.fetch()` entry. This file closes that gap.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

from funds_dashboard.cli import fetch as cli_fetch
from funds_dashboard.config import Settings
from funds_dashboard.db.models import Base
from funds_dashboard.wind import WindResult


@pytest.fixture
def _patched_settings(tmp_path, monkeypatch):
    """Wire the CLI to a temp SQLite DB + masked admin password so
    `get_settings()` returns a valid Settings inside the CLI process.
    """
    db_path = tmp_path / "cli_test.db"
    monkeypatch.setenv("FUNDS_DASHBOARD_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("FUNDS_DASHBOARD_MASTER_KEY", "cli-test-master-key")
    monkeypatch.setenv(
        "FUNDS_DASHBOARD_ADMIN_PASSWORD_HASH",
        "$2b$04$abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNO.",
    )
    monkeypatch.setenv("WIND_API_KEY", "ak_cli_test_KEY_xxxxxxxxxxxxxxxx")
    monkeypatch.setenv("FUNDS_DASHBOARD_WIND_CLI_SCRIPT", "cli.mjs")
    monkeypatch.setenv(
        "FUNDS_DASHBOARD_DAILY_REPORT_OUTPUT_DIR",
        str(tmp_path / "reports"),
    )
    # Create the schema in the temp DB using create_all (the alembic
    # path is exercised separately).
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    yield db_path


def _ok_wind_result(windcode: str) -> WindResult:
    return WindResult(
        tool_name="fund_data:get_fund_price_indicators",
        request_payload={"codes": [windcode]},
        columns=[
            "NAME", "MATCH", "SHARES", "FUNDSIZE", "NETVALUE",
            "ACCUMULATEDNETVALUE", "CHANGERANGE", "IOPV",
            "FORWARDDISCOUNT", "windcode",
        ],
        rows=[
            [f"name-{windcode}", 1.0, 5_000_000.0, 1.0e10, 1.0,
             1.0, 0.0, 1.0, 0.0, windcode],
        ],
        raw_stdout='{"data":{}}',
    )


def test_cli_fetch_initialises_session_factory(_patched_settings) -> None:
    """Real-run regression (Alex msg=293ecab0): the CLI bypassed
    FastAPI's lifespan hook and never initialised the global session
    factory, so `run_daily_fetch`'s `session_scope()` raised
    `RuntimeError: session factory not initialized` at first fetch.

    The fix is for `cli.fetch()` to call `init_sessionmaker(...)`
    after reading settings and before invoking the runner. This test
    pins that contract by driving the entry point directly with a
    stubbed Wind backend: if init were missing, the test fails with
    the same RuntimeError seen in production.
    """

    def _fake_call(self, tool_name, payload):
        return _ok_wind_result(payload["codes"][0])

    with patch(
        "funds_dashboard.scheduler.runner.WindClient.call", _fake_call
    ):
        exit_code = cli_fetch(
            argv=["--trade-date", "2026-05-11", "--force"]
        )

    # Exit code 0 = at least one ETF landed. The bug surfaced as
    # RuntimeError BEFORE any return path, so reaching this assertion
    # is itself the contract pin.
    assert exit_code == 0


def test_cli_fetch_writes_markdown(_patched_settings, tmp_path) -> None:
    """End-to-end via the CLI entry point: stub WindClient, drive
    fetch, verify markdown lands under
    `daily_report_output_dir/<trade_date>.md`.

    This is the "real CLI path" coverage Vera asked for — distinct
    from `test_runner.py` which calls `run_daily_fetch` directly and
    sets up the session factory by hand.
    """

    def _fake_call(self, tool_name, payload):
        return _ok_wind_result(payload["codes"][0])

    with patch(
        "funds_dashboard.scheduler.runner.WindClient.call", _fake_call
    ):
        cli_fetch(argv=["--trade-date", "2026-05-11", "--force"])

    md = tmp_path / "reports" / "2026-05-11.md"
    assert md.exists(), "CLI fetch should write the daily-report markdown"
    text = md.read_text(encoding="utf-8")
    assert "report_type: daily_dashboard" in text
    # Production rows landed
    for code in ("510300.SH", "510500.SH", "588200.SH"):
        assert code in text


def test_cli_fetch_exit_code_1_when_all_wind_calls_fail(
    _patched_settings,
) -> None:
    """Exit-code contract from the original CLI doc: returns 1 when
    `audit_rows == 0` (every Wind call failed). The CLI is the surface
    ops tooling reads to decide if a backfill needs retrying.
    """
    from funds_dashboard.wind import WindError

    def _always_fail(self, tool_name, payload):
        raise WindError("simulated total outage", stdout="", stderr="all-down")

    with patch(
        "funds_dashboard.scheduler.runner.WindClient.call", _always_fail
    ):
        exit_code = cli_fetch(argv=["--trade-date", "2026-05-11", "--force"])

    assert exit_code == 1
