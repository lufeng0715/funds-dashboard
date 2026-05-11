"""FastAPI application factory.

Build the app via `create_app()` so tests can inject overrides
(settings, session factory, Wind client). The module-level `app`
attribute exists for ASGI servers that need it directly
(`uvicorn funds_dashboard.main:app`).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from .api.v1 import router as v1_router
from .config import Settings, get_settings
from .db import init_sessionmaker


LOG = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    init_sessionmaker(settings.database_url)

    app = FastAPI(
        title="funds-dashboard",
        version="0.0.1",
        description=(
            "Daily fund / ETF dashboard backed by Wind. See repo "
            "README for MVP scope and cross-product contracts."
        ),
    )
    app.include_router(v1_router)
    return app


app = create_app()
