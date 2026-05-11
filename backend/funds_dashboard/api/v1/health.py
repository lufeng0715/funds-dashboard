"""Health endpoint.

Phase-0 placeholder so the FastAPI app can be smoke-tested before the
ETF + fund-company endpoints land. Returns the package version and a
millisecond UTC timestamp — no DB hit, no Wind hit, fast enough to
sit behind any load balancer's health probe.
"""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from funds_dashboard import __version__


router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    timestamp: str


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=__version__,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    )
