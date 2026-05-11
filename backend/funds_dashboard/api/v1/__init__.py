"""v1 API routers.

`/api/v1/` is the only public namespace right now. A future major
contract bump would land under `/api/v2/`; in-flight schema
extensions stay inside v1 because they're additive (new optional
fields / new endpoints) per the project's API contract conventions.

Auth model: protected sub-routers attach
`Depends(require_authenticated_admin)` explicitly via `include_router(...,
dependencies=...)`. `/health` is the only public route and is mounted
plain. This is the inverse of "auth on parent + opt-out on /health",
which is brittle in FastAPI because nested dependency lists merge
rather than override. (Vera msg=ca796844 HIGH-1.)
"""

from fastapi import APIRouter, Depends

from .auth import require_authenticated_admin
from .health import router as health_router

router = APIRouter(prefix="/api/v1")
# Health = public uptime probe; no auth.
router.include_router(health_router)

# Protected sub-routers (data endpoints, config-Web, scheduler controls)
# attach the auth dependency at include time. Sample wiring for the
# Phase 0.5 config-Web router lands when that PR ships; the pattern
# below is the contract every new protected router follows.
#
#     from .config import router as config_router
#     router.include_router(
#         config_router,
#         dependencies=[Depends(require_authenticated_admin)],
#     )

# Re-exported so tests and other modules can probe the auth dep itself.
__all__ = ["router", "require_authenticated_admin"]
