"""v1 API routers.

`/api/v1/` is the only public namespace right now. A future major
contract bump would land under `/api/v2/`; in-flight schema
extensions stay inside v1 because they're additive (new optional
fields / new endpoints) per the project's API contract conventions.
"""

from fastapi import APIRouter

from .health import router as health_router

router = APIRouter(prefix="/api/v1")
router.include_router(health_router)
