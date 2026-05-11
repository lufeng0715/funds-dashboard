"""Bcrypt password verification.

The admin password is **never** stored as plaintext. Operators stash
the bcrypt hash in `FUNDS_DASHBOARD_ADMIN_PASSWORD_HASH` (env var)
via `python -m bcrypt` or any equivalent tool, and the runtime only
ever compares a submitted password to that hash.

`verify_admin_password` runs in constant time relative to the hash
(bcrypt's design property) — the wrapper catches any malformed-hash
exception and returns `False` so an unset / corrupted hash fails
closed rather than crashing.
"""

from __future__ import annotations

import logging

import bcrypt


LOG = logging.getLogger(__name__)


def hash_password(plaintext: str, *, rounds: int = 12) -> str:
    """Bcrypt-hash a plaintext password.

    Used by the operator-side `python -m funds_dashboard.scripts.hash_password`
    helper (added in a follow-up) to generate the value that goes
    into `FUNDS_DASHBOARD_ADMIN_PASSWORD_HASH`. Not called at request
    time — the runtime only verifies, never hashes.
    """
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(rounds)).decode(
        "utf-8"
    )


def verify_admin_password(plaintext: str, *, stored_hash: str | None) -> bool:
    """Constant-time check of `plaintext` against `stored_hash`.

    Returns False (not raises) when:
    * `stored_hash` is None / empty (admin not configured)
    * `stored_hash` isn't a valid bcrypt blob

    fail-closed: any error → False so the login endpoint returns the
    generic 401 the design contract specifies.
    """
    if not stored_hash:
        LOG.warning(
            "admin password verification attempted but "
            "FUNDS_DASHBOARD_ADMIN_PASSWORD_HASH is not set"
        )
        return False
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), stored_hash.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        LOG.warning("bcrypt verify error (treating as auth fail): %s", exc)
        return False
