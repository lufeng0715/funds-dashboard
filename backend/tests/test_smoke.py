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
from funds_dashboard.main import create_app


@pytest.fixture
def app_client(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/test.db")
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


def test_health_endpoint_returns_ok(app_client):
    """`/api/v1/health` confirms the app boots end-to-end."""
    resp = app_client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["timestamp"]
