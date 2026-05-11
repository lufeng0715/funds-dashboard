"""CLI entry points for serve / fetch.

`pyproject.toml` wires these as `funds-dashboard-serve` and
`funds-dashboard-fetch` console scripts.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

import uvicorn

from .config import get_settings


LOG = logging.getLogger(__name__)


def serve() -> int:
    """Run the FastAPI app via uvicorn.

    Reads `host` / `port` / `log_level` from settings; sticks with
    uvicorn's defaults for everything else so deployment specifics
    (workers, reload) stay in the process supervisor's config.
    """
    settings = get_settings()
    uvicorn.run(
        "funds_dashboard.main:make_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
    return 0


def fetch(argv: list[str] | None = None) -> int:
    """One-shot Wind fetch for a specific trade date.

    Used by ops to backfill or replay a day. Scheduler-driven
    invocations go through `funds_dashboard.scheduler` directly.
    """
    parser = argparse.ArgumentParser(
        prog="funds-dashboard-fetch",
        description="One-shot Wind fetch + DB write for a trade date.",
    )
    parser.add_argument(
        "--trade-date",
        type=date.fromisoformat,
        required=True,
        help="ISO trade date, e.g. 2026-05-10",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bump data_source_version seq even when a successful "
        "fetch already exists for this date.",
    )
    args = parser.parse_args(argv)

    # Lazy import so `serve` doesn't pay the cost when not needed.
    from .scheduler.runner import run_daily_fetch

    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    LOG.info("manual fetch: trade_date=%s force=%s", args.trade_date, args.force)
    result = run_daily_fetch(settings, trade_date=args.trade_date, force=args.force)
    LOG.info(
        "fetch done: version=%s audit_rows=%d derived_rows=%d failed=%s markdown=%s",
        result.data_source_version,
        result.audit_rows,
        result.derived_rows,
        result.failed_windcodes,
        result.markdown_path,
    )
    # Exit 1 when nothing landed AND we expected something to (e.g.
    # every Wind call failed). 0 when at least one ETF made it in.
    if result.audit_rows == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(fetch())
