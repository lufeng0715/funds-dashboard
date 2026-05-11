"""Authentication dependencies for the v1 API.

Phase 0 wiring per Vera msg=ca796844 HIGH-1: every router under
`/api/v1` declares this dependency by default, with `/health` opting
back out via `dependencies=[]`. Real session / bearer-token logic
lands when the config-Web Phase 0.5 PR ships and produces an admin
session backend; until then the dependency raises 503 so the gap is
loud, not silent.

NB: this file is the *contract*. Implementation slots in below; the
shape is what other routers depend on.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status


def require_authenticated_admin(request: Request) -> None:
    """Reject unauthenticated requests with 503 (placeholder).

    The Phase 0.5 config-Web PR replaces the body with a real session
    / bearer-token check. Until then, every protected endpoint will
    503 — fail-loud rather than ship a route that silently lets
    anyone hit the admin surface.
    """
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Admin authentication not yet wired (Phase 0.5 pending). "
            "This endpoint will accept a session cookie once the "
            "config-Web PR lands."
        ),
    )
