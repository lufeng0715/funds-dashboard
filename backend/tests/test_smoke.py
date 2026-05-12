"""Smoke tests for the FastAPI scaffold.

Confirms that `create_app()` boots, `/api/v1/health` answers,
and the SQLAlchemy session factory comes up against an in-memory
SQLite. These are the bare minimum that the scaffold is wired
together — domain tests against Wind / ETF tables land once the
field-dictionary-driven migration is in.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from funds_dashboard.config import Settings
from funds_dashboard.config_store import crypto
from funds_dashboard.main import StartupConfigError, create_app


@pytest.fixture
def app_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        FUNDS_DASHBOARD_MASTER_KEY="test-master-key-for-pytest-only",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


def test_health_endpoint_returns_ok(app_client):
    """`/api/v1/health` confirms the app boots end-to-end and that
    the public route opts out of admin auth."""
    resp = app_client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["timestamp"]


def test_missing_master_key_refuses_startup(tmp_path):
    """Vera consistency_checks §12 + msg=ca796844 CRITICAL-1: backend
    MUST raise (not silently boot) when the master key is unset.
    """
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        FUNDS_DASHBOARD_MASTER_KEY=None,
    )
    with pytest.raises(StartupConfigError, match="MASTER_KEY"):
        create_app(settings)


def test_empty_master_key_refuses_startup(tmp_path):
    """An empty-string master key is functionally the same as missing —
    refuse to boot rather than encrypt with `b""`.
    """
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        FUNDS_DASHBOARD_MASTER_KEY="",
    )
    with pytest.raises(StartupConfigError):
        create_app(settings)


def test_create_app_makes_settings_master_key_available_to_crypto(
    tmp_path, monkeypatch
):
    """The app may load `.env` through Pydantic settings rather than
    process env. Secret encryption still needs the same key because the
    crypto module reads the versioned key from `os.environ`.
    """
    monkeypatch.delenv("FUNDS_DASHBOARD_MASTER_KEY", raising=False)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        FUNDS_DASHBOARD_MASTER_KEY="settings-only-master-key",
    )

    create_app(settings)

    assert crypto.decrypt(crypto.encrypt("secret")) == "secret"


def test_cli_fetch_makes_settings_master_key_available_to_crypto(
    tmp_path, monkeypatch
):
    """The standalone CLI bypasses create_app(), so it must install the
    settings-loaded master key before runner decrypts secret_config.
    """
    monkeypatch.delenv("FUNDS_DASHBOARD_MASTER_KEY", raising=False)
    monkeypatch.setattr(
        "funds_dashboard.cli.get_settings",
        lambda: Settings(
            database_url=f"sqlite:///{tmp_path}/test.db",
            FUNDS_DASHBOARD_MASTER_KEY="cli-settings-only-master-key",
        ),
    )
    monkeypatch.setattr("funds_dashboard.db.init_sessionmaker", lambda _: None)
    captured: dict[str, bool] = {}

    def fake_run_daily_fetch(settings, *, trade_date, force=False):
        captured["encrypt_ok"] = crypto.decrypt(crypto.encrypt("secret")) == "secret"

        class Result:
            data_source_version = "20260511#test#1"
            audit_rows = 1
            derived_rows = 0
            failed_windcodes: list[str] = []
            markdown_path = None

        return Result()

    monkeypatch.setattr(
        "funds_dashboard.scheduler.runner.run_daily_fetch",
        fake_run_daily_fetch,
    )

    from funds_dashboard.cli import fetch

    assert fetch(["--trade-date", "2026-05-11"]) == 0
    assert captured["encrypt_ok"] is True
