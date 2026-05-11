"""Application config.

Settings are read from environment variables (and a `.env` file if
present). Anything that can be set per-deployment lives here; literal
constants used by domain logic live next to that logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # funds-dashboard/


class Settings(BaseSettings):
    """Runtime config.

    The Wind CLI is the only external dependency and may need a
    non-default location depending on where the team `Wind` skill is
    installed. `WIND_CLI_PATH` env var overrides the default lookup
    that walks `$PATH` for `node` + a known cli.mjs.
    """

    model_config = SettingsConfigDict(
        env_prefix="FUNDS_DASHBOARD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- runtime ---
    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    # --- HTTP ---
    host: str = "127.0.0.1"
    port: int = 8000

    # --- database ---
    database_url: str = Field(
        default="sqlite:///./funds_dashboard.db",
        description=(
            "SQLAlchemy URL. SQLite for dev, Postgres for prod "
            "(`postgresql+psycopg://user:pw@host/db`)."
        ),
    )

    # --- Wind CLI ---
    wind_cli_node_path: str = Field(
        default="node",
        description="Path or alias to the `node` runtime.",
    )
    wind_cli_script: str = Field(
        default="scripts/cli.mjs",
        description=(
            "Path (relative or absolute) to the Wind skill CLI entry "
            "script. Resolved against the agent's installed skill "
            "directory; can be overridden for testing with a stub."
        ),
    )
    # Wind aimarket key. Read from the un-prefixed `WIND_API_KEY` env
    # var per the team convention Linda confirmed in msg=91b45123 and
    # feng-lu posted in msg=86df4780. Not committed; .env is git-ignored.
    # Logged-out / displayed-out forms must always mask this value.
    wind_api_key: str | None = Field(
        default=None,
        alias="WIND_API_KEY",
        description=(
            "Wind aimarket API key. Sourced from `WIND_API_KEY` env "
            "var (no prefix). Never log this value; mask before any "
            "stdout / DB write."
        ),
    )

    # --- scheduler ---
    cron_daily: str = Field(
        default="0 17 * * MON-FRI",
        description="APScheduler cron expression for the daily fetch.",
    )
    scheduler_timezone: str = "Asia/Shanghai"

    # --- llm-wiki integration ---
    daily_report_output_dir: Path = Field(
        default=REPO_ROOT.parent / "llm-wiki" / "data" / "real-wiki"
        / "categories" / "funds" / "daily-reports",
        description=(
            "Where to emit the daily-report markdown pages so the "
            "llm-wiki ingestion pipeline can consume them. "
            "See docs/contracts/funds-daily-report-ingestion.md."
        ),
    )


def get_settings() -> Settings:
    """Lazy singleton — pytest tests can monkey-patch fields.

    The function-call shape (rather than module-level constant) makes
    the dependency explicit in routers / services and trivially
    overridable in tests via `app.dependency_overrides[get_settings]`.
    """
    return Settings()
