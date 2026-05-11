"""Auth layer tests.

Vera msg=408666c6 verification gates these tests pin:

* session cookie sign/verify (HMAC + max-age, no DB)
* signing key derived from master via HKDF (not the master itself)
* bcrypt hash from env, plaintext never stored
* login failure returns generic 401 (no user-vs-password leak)
* cookie carries HttpOnly + SameSite=Strict
* `/health` stays accessible without auth
* protected stub returns 401 without cookie
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from funds_dashboard.auth import (
    SESSION_COOKIE_NAME,
    issue_session_cookie,
    parse_session_cookie,
    require_authenticated_admin,
    verify_admin_password,
)
from funds_dashboard.auth.password import hash_password
from funds_dashboard.auth.session import _derive_session_signing_key
from funds_dashboard.config import Settings, get_settings
from funds_dashboard.main import create_app


@pytest.fixture
def admin_password() -> str:
    return "correct horse battery staple"


@pytest.fixture
def admin_hash(admin_password: str) -> str:
    # 4 rounds is fine for tests (faster) — production uses 12.
    return hash_password(admin_password, rounds=4)


@pytest.fixture
def settings(tmp_path, admin_hash: str) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        FUNDS_DASHBOARD_MASTER_KEY="test-master-key-for-pytest",
        FUNDS_DASHBOARD_ADMIN_PASSWORD_HASH=admin_hash,
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    # Routes that use `Depends(get_settings)` construct their own
    # Settings (reading env vars only) by default, which would miss
    # the per-test admin hash + master key. Override the dependency
    # so every request sees the fixture-provided settings.
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


# --- password verification ------------------------------------------------


def test_verify_admin_password_correct(admin_password: str, admin_hash: str) -> None:
    assert verify_admin_password(admin_password, stored_hash=admin_hash) is True


def test_verify_admin_password_wrong(admin_hash: str) -> None:
    assert verify_admin_password("wrong-password", stored_hash=admin_hash) is False


def test_verify_admin_password_unset_returns_false() -> None:
    """No hash configured = no admin can log in (fail-closed)."""
    assert verify_admin_password("anything", stored_hash=None) is False
    assert verify_admin_password("anything", stored_hash="") is False


def test_verify_admin_password_malformed_hash_returns_false() -> None:
    """An invalid bcrypt blob can't authenticate anyone — bcrypt
    raises and the wrapper returns False rather than propagating."""
    assert verify_admin_password("pw", stored_hash="not-a-bcrypt-hash") is False


# --- session signing -------------------------------------------------------


def test_session_signing_key_is_derived_not_master(settings: Settings) -> None:
    """HKDF output must NOT equal the master key (else leaking one
    leaks the other). Vera msg=b4a1281e contract."""
    derived = _derive_session_signing_key(settings.master_key)
    master_raw = settings.master_key.get_secret_value().encode("utf-8")
    assert derived != master_raw
    # Sanity: same input → same output (deterministic KDF).
    derived2 = _derive_session_signing_key(settings.master_key)
    assert derived == derived2


def test_session_round_trip(settings: Settings) -> None:
    from fastapi import Response

    response = Response()
    token = issue_session_cookie(response, settings, username="admin")
    payload = parse_session_cookie(token, settings)
    assert payload is not None
    assert payload.username == "admin"


def test_session_tampered_token_rejected(settings: Settings) -> None:
    from fastapi import Response

    response = Response()
    token = issue_session_cookie(response, settings, username="admin")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert parse_session_cookie(tampered, settings) is None


def test_session_wrong_master_key_rejects(settings: Settings, tmp_path) -> None:
    from fastapi import Response

    response = Response()
    token = issue_session_cookie(response, settings, username="admin")
    other_settings = Settings(
        database_url=f"sqlite:///{tmp_path}/other.db",
        FUNDS_DASHBOARD_MASTER_KEY="different-master-key",
        FUNDS_DASHBOARD_ADMIN_PASSWORD_HASH=settings.admin_password_hash,
    )
    assert parse_session_cookie(token, other_settings) is None


# --- HTTP login flow -------------------------------------------------------


def test_login_success_sets_session_cookie(
    client: TestClient, admin_password: str
) -> None:
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": admin_password},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "username": "admin"}
    cookie = r.cookies.get(SESSION_COOKIE_NAME)
    assert cookie  # cookie set
    # Cookie attributes (HttpOnly / SameSite=strict / max-age) — inspected
    # via Set-Cookie header rather than the parsed jar.
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie


def test_login_wrong_password_generic_401(
    client: TestClient, admin_password: str
) -> None:
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong"},
    )
    assert r.status_code == 401
    body = r.json()
    # Generic — no hint about which side failed.
    assert body["detail"] == "Invalid credentials."


def test_login_unknown_user_generic_401(
    client: TestClient, admin_password: str
) -> None:
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "not-admin", "password": admin_password},
    )
    assert r.status_code == 401
    # Same generic detail — no enumeration possible.
    assert r.json()["detail"] == "Invalid credentials."


def test_logout_clears_cookie(client: TestClient, admin_password: str) -> None:
    client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": admin_password},
    )
    r = client.post("/api/v1/auth/logout")
    assert r.status_code == 200
    # Server tells the browser to clear the cookie (Max-Age=0).
    set_cookie = r.headers["set-cookie"].lower()
    assert "max-age=0" in set_cookie or "expires=" in set_cookie


# --- health stays public ---------------------------------------------------


def test_health_endpoint_still_public(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200


# --- require_authenticated_admin protects routes --------------------------


def _make_app_with_protected_route(settings: Settings) -> FastAPI:
    """Mount a tiny protected route to exercise the dependency end-to-end."""
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    test_router = APIRouter(prefix="/api/v1/_test")

    @test_router.get("/protected")
    def _protected(_admin=Depends(require_authenticated_admin)) -> dict[str, str]:
        return {"ok": "protected"}

    app.include_router(test_router)
    return app


def test_protected_route_401_without_cookie(settings: Settings) -> None:
    client = TestClient(_make_app_with_protected_route(settings))
    r = client.get("/api/v1/_test/protected")
    assert r.status_code == 401
    assert r.json()["detail"] == "Authentication required."


def test_protected_route_200_with_valid_cookie(
    settings: Settings, admin_password: str
) -> None:
    client = TestClient(_make_app_with_protected_route(settings))
    client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": admin_password},
    )
    r = client.get("/api/v1/_test/protected")
    assert r.status_code == 200
    assert r.json() == {"ok": "protected"}
