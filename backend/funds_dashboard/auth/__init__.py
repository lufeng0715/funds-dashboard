"""Admin authentication for the config-Web surface.

Two pieces: bcrypt password verify (against the
`FUNDS_DASHBOARD_ADMIN_PASSWORD_HASH` env var, never against a
plaintext stored anywhere) and HMAC-signed session cookies (no DB
round-trip on every request; the cookie carries the signed claim).

Vera msg=408666c6 verification gates:

* session token NOT plaintext in DB — we sign with itsdangerous, no
  DB row at all
* bcrypt hash from env, plaintext password never touches storage
* login failure returns generic 401, no "user/password wrong" leak
* cookie is HttpOnly + SameSite=Strict (+ Secure when HTTPS bound)
* POST writes guard via `require_authenticated_admin`; GET /health
  stays public
"""

from .password import verify_admin_password
from .session import (
    SESSION_COOKIE_NAME,
    SessionPayload,
    issue_session_cookie,
    parse_session_cookie,
    require_authenticated_admin,
)

__all__ = [
    "SESSION_COOKIE_NAME",
    "SessionPayload",
    "issue_session_cookie",
    "parse_session_cookie",
    "require_authenticated_admin",
    "verify_admin_password",
]
