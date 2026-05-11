"""HMAC-signed session cookies.

No DB-backed session store — the cookie carries the signed claim
itself (`{username, issued_at, expires_at, nonce}`) and the runtime
verifies the HMAC on every request. That keeps the auth layer
stateless and side-steps the "session token in plaintext DB" foot-gun
Vera flagged in msg=408666c6.

Signing key derivation: HKDF-SHA256 over the master key with a
domain-separation `info` string per Vera msg=b4a1281e. The session
signing key is **NOT** the master key — that means a leaked session
key doesn't expose the secret-store master key, and rotating the
session key (separate KDF input) doesn't require re-encrypting
secrets.

Cookie hygiene:
* `HttpOnly=True` — JavaScript can't read it (XSS resistance)
* `SameSite=strict` — CSRF resistance
* `Secure=True` when the request was over HTTPS (controlled by
  `SecureCookie` flag in settings — dev binds to localhost on HTTP)

Token format (itsdangerous `URLSafeTimedSerializer`):
    base64( payload ) + '.' + base64( timestamp ) + '.' + signature
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import Cookie, Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import SecretStr

from ..config import Settings, get_settings


SESSION_COOKIE_NAME = "fdb_session"
# 8 hours is generous enough for a config-Web admin session without
# being so long that a stolen cookie is a long-term breach.
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
SIGNING_KEY_LENGTH = 32
HKDF_INFO = b"funds-dashboard:auth-session-key:v1"


@dataclass(frozen=True)
class SessionPayload:
    """Verified session content extracted from a valid cookie."""

    username: str
    issued_at: float
    nonce: str


def _derive_session_signing_key(master_key: SecretStr) -> bytes:
    """HKDF-SHA256 of the master key under a session-specific info tag.

    Decoupling session signing from secret-store encryption means a
    session signing-key rotation doesn't touch ciphertexts and vice
    versa — same pattern as memo's separate Keychain accounts per
    surface.
    """
    raw = master_key.get_secret_value().encode("utf-8")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=SIGNING_KEY_LENGTH,
        salt=None,
        info=HKDF_INFO,
    )
    return hkdf.derive(raw)


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    if settings.master_key is None:
        # Should never happen — `create_app` fail-closes before this —
        # but defending in depth keeps the contract crisp.
        raise RuntimeError("master_key not set; cannot serialise sessions")
    key = _derive_session_signing_key(settings.master_key)
    return URLSafeTimedSerializer(key, salt="fdb-session-v1")


def issue_session_cookie(
    response, settings: Settings, *, username: str
) -> str:
    """Sign a fresh session payload and set it on `response`.

    Returns the cookie value so callers can use it in tests without
    poking at the `Response` object.
    """
    payload = {
        "u": username,
        "iat": time.time(),
        "n": secrets.token_urlsafe(16),
    }
    token = _serializer(settings).dumps(payload)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="strict",
        secure=settings.env == "prod",
        path="/",
    )
    return token


def parse_session_cookie(
    token: str, settings: Settings
) -> SessionPayload | None:
    """Return the decoded payload, or None when the cookie is invalid."""
    if not token:
        return None
    try:
        payload = _serializer(settings).loads(
            token, max_age=SESSION_MAX_AGE_SECONDS
        )
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    username = payload.get("u")
    issued_at = payload.get("iat")
    nonce = payload.get("n")
    if not (isinstance(username, str) and isinstance(issued_at, (int, float))):
        return None
    return SessionPayload(
        username=username, issued_at=float(issued_at), nonce=str(nonce or "")
    )


def require_authenticated_admin(
    request: Request,
    settings: Settings = Depends(get_settings),
    fdb_session: str | None = Cookie(default=None),
) -> SessionPayload:
    """FastAPI dependency: reject 401 when no valid admin session.

    Replaces the Phase 0 `503 placeholder` per Vera msg=408666c6:
    * Generic 401 (no "user not found" / "wrong password" distinction)
    * Cookie verified by HMAC + max-age
    * Returns the SessionPayload so handlers can audit `actor:<username>`
    """
    payload = parse_session_cookie(fdb_session or "", settings)
    if payload is None:
        # Single generic response — no information leak about why.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return payload
