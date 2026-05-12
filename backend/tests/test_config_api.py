"""Tests for Phase 0.5 config-Web endpoints.

Security contract from Linda/Vera:

* config endpoints are admin-only;
* secret values never appear in status, audit, or connection-test responses;
* first-run Wind key seeding from env is idempotent and audited;
* Wind connection tests return only operational status, never credentials.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from funds_dashboard.auth.password import hash_password
from funds_dashboard.config import Settings, get_settings
from funds_dashboard.db.models import Base, ConfigAuditLog, SecretConfig
from funds_dashboard.main import create_app
from funds_dashboard.wind import WindResult


ADMIN_PASSWORD = "correct horse battery staple"
MASTER_KEY = "test-master-key-for-config-api"
SEEDED_WIND_KEY = "ak_seeded_from_env_1234567890"


@pytest.fixture(autouse=True)
def _crypto_master_key_env(monkeypatch):
    monkeypatch.setenv("FUNDS_DASHBOARD_MASTER_KEY", MASTER_KEY)


def _settings(tmp_path, *, wind_key: str | None = None) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        FUNDS_DASHBOARD_MASTER_KEY=MASTER_KEY,
        FUNDS_DASHBOARD_ADMIN_PASSWORD_HASH=hash_password(ADMIN_PASSWORD, rounds=4),
        WIND_API_KEY=wind_key,
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
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200


def _db_session(settings: Settings) -> Session:
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        future=True,
    )
    return Session(engine, future=True)


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("get", "/api/v1/config/status", None),
        ("put", "/api/v1/config/sections/scheduler", {"values": {"cron_daily": "* * * * *"}}),
        ("put", "/api/v1/config/secrets/wind_api_key", {"value": "ak_unauth_SECRET"}),
        ("delete", "/api/v1/config/secrets/wind_api_key", None),
        ("post", "/api/v1/config/test/wind", None),
        ("get", "/api/v1/config/audit", None),
    ],
)
def test_config_endpoints_require_admin_session(
    tmp_path, method: str, path: str, json_body: dict[str, object] | None
) -> None:
    client = _client(_settings(tmp_path))
    response = client.request(method.upper(), path, json=json_body)
    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required."


def test_status_seeds_wind_key_from_env_once_and_never_leaks(tmp_path) -> None:
    settings = _settings(tmp_path, wind_key=SEEDED_WIND_KEY)
    client = _client(settings)
    _login(client)

    first = client.get("/api/v1/config/status")
    second = client.get("/api/v1/config/status")

    assert first.status_code == 200
    assert second.status_code == 200
    body = first.json()
    assert body["secrets"]["wind_api_key"]["configured"] is True
    assert body["secrets"]["wind_api_key"]["masked"] == "****7890"
    assert SEEDED_WIND_KEY not in str(body)
    assert "ak_seeded" not in str(body)

    with _db_session(settings) as session:
        secrets = session.scalars(
            select(SecretConfig).where(SecretConfig.name == "wind_api_key")
        ).all()
        seed_audits = session.scalars(
            select(ConfigAuditLog).where(
                ConfigAuditLog.action == "seeded_from_env",
                ConfigAuditLog.config_name == "wind_api_key",
            )
        ).all()

    assert len(secrets) == 1
    assert len(seed_audits) == 1
    assert SEEDED_WIND_KEY.encode("utf-8") not in secrets[0].ciphertext


def test_secret_put_delete_and_audit_never_return_raw_value(tmp_path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    _login(client)

    secret = "ak_written_by_admin_ABCDEFGH"
    put_response = client.put(
        "/api/v1/config/secrets/wind_api_key",
        json={"value": secret},
    )
    audit_response = client.get("/api/v1/config/audit")
    delete_response = client.delete("/api/v1/config/secrets/wind_api_key")
    status_response = client.get("/api/v1/config/status")

    assert put_response.status_code == 200
    assert put_response.json()["masked"] == "****EFGH"
    assert delete_response.status_code == 200
    assert status_response.json()["secrets"]["wind_api_key"]["configured"] is False
    for payload in (
        put_response.json(),
        audit_response.json(),
        delete_response.json(),
        status_response.json(),
    ):
        assert secret not in str(payload)
        assert "ak_written_by_admin" not in str(payload)


def test_runtime_section_put_is_visible_in_status(tmp_path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    _login(client)

    response = client.put(
        "/api/v1/config/sections/scheduler",
        json={"values": {"cron_daily": "0 18 * * MON-FRI", "timezone": "Asia/Shanghai"}},
    )
    status_response = client.get("/api/v1/config/status")

    assert response.status_code == 200
    assert status_response.status_code == 200
    assert status_response.json()["runtime"]["scheduler"]["cron_daily"] == (
        "0 18 * * MON-FRI"
    )


def test_wind_connection_test_returns_operational_status_without_key(tmp_path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    _login(client)
    secret = "ak_connection_test_SECRET1234"
    client.put("/api/v1/config/secrets/wind_api_key", json={"value": secret})

    with patch("funds_dashboard.api.v1.config.WindClient") as wind_client_cls:
        wind_client_cls.return_value.call.return_value = WindResult(
            tool_name="fund_data:get_fund_price_indicators",
            request_payload={"probe": True},
            columns=["ok"],
            rows=[[1]],
            raw_stdout='{"content":[]}',
        )
        response = client.post("/api/v1/config/test/wind")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["latency_ms"], int | float)
    assert secret not in str(body)
    assert "wind_api_key" not in body
    assert "ak_connection_test" not in str(body)


def test_wind_connection_test_uses_reachable_fund_quote_probe(tmp_path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    _login(client)
    client.put(
        "/api/v1/config/secrets/wind_api_key",
        json={"value": "ak_connection_test_SECRET1234"},
    )

    with patch("funds_dashboard.api.v1.config.WindClient") as wind_client_cls:
        wind_client_cls.return_value.call.return_value = WindResult(
            tool_name="fund_data:get_fund_quote",
            request_payload={"windcode": "510300.SH"},
            columns=["time", "price"],
            rows=[["2026-05-11 14:59:00", 4.966]],
            raw_stdout='{"content":[]}',
        )
        response = client.post("/api/v1/config/test/wind")

    assert response.status_code == 200
    wind_client_cls.return_value.call.assert_called_once_with(
        "fund_data:get_fund_quote", {"windcode": "510300.SH"}
    )
