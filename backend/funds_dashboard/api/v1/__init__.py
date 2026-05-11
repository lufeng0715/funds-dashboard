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
from .login import router as login_router

router = APIRouter(prefix="/api/v1")
# Public routes (uptime probe + login flow). Login itself can't require
# auth — that would be a chicken-and-egg loop.
router.include_router(health_router)
router.include_router(login_router)

# Protected sub-routers (config-Web, scheduler controls, data
# endpoints) attach `Depends(require_authenticated_admin)` at
# include time. Sample wiring for the config router will land when
# alex-wu-coding's PR adds `funds_dashboard.api.v1.config`:
#
#     from .config import router as config_router
#     router.include_router(
#         config_router,
#         dependencies=[Depends(require_authenticated_admin)],
#     )

__all__ = ["router", "require_authenticated_admin"]
