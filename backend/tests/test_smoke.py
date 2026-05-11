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
