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


class StartupConfigError(RuntimeError):
    """Refuse-to-start sentinel for missing security-critical config.

    Raised when a precondition for safely handling secrets fails — e.g.
    `FUNDS_DASHBOARD_MASTER_KEY` is unset. fail-loud per Vera consistency
    check §12 + Nova msg=bccd488e CryptoVault port (memo PR #7).
    """


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    # fail-closed: refuse to boot without the master key. Treat tests
    # that pass an explicit Settings instance with a key set the same
    # way (they can construct a `SecretStr("test")` value).
    # See Vera msg=ca796844 CRITICAL-1.
    if settings.master_key is None or not settings.master_key.get_secret_value():
        raise StartupConfigError(
            "FUNDS_DASHBOARD_MASTER_KEY is not set — refusing to start. "
            "Set it in the deployment environment (NOT in git) and "
            "restart. See docs/MVP_FIELD_DICTIONARY.md §"
            "Bootstrap secrets."
        )

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


# Uvicorn target: `funds_dashboard.main:make_app` (factory form) so we
# don't construct the app at import time. Module-level
# `app = create_app()` would explode any test or tool that imports
# `funds_dashboard.main` without `FUNDS_DASHBOARD_MASTER_KEY` set —
# fail-loud is correct for the actual `uvicorn` boot path, but it
# breaks `pytest` collection (which only wants to import the module).
def make_app() -> FastAPI:
    """Uvicorn entry point — `uvicorn funds_dashboard.main:make_app --factory`."""
    return create_app()
