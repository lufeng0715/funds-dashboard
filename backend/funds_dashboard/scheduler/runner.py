"""Daily fetch orchestration.

The runner is the thin "do one day's work" coordinator that both the
scheduled APScheduler job and the manual `funds-dashboard-fetch CLI
call route through. Keeping the orchestration in one place means the
audit story (one `wind_fetch_audit` row per Wind call, derived rows
linked back to it) is identical regardless of how the run was
triggered.

This module is **placeholder-grade** for Phase 0. Once Linda's field
dictionary is final (msg=91b45123 follow-up), we wire:

  1. ETF pool definition pull (currently TODO — needs Linda's pool
     list).
  2. `wind.WindClient.call("fund_data:get_fund_price_indicators", ...)`
     per ETF, persist to `EtfDailySnapshot`.
  3. `wind.WindClient.call("fund_data:get_fund_company_info", ...)`
     for major companies, persist to `FundCompanyAggregate`.
  4. Generate daily-report markdown into `settings.daily_report_output_dir`
     so the llm-wiki ingestion pipeline can pick it up.
  5. Log a `DailyReportProvenance` row with all contributing
     `data_source_version` tokens.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import select

from ..config import Settings
from ..db import session_scope
from ..db.models import WindFetchAudit


LOG = logging.getLogger(__name__)


def make_data_source_version(trade_date: date, fetch_utc: datetime, seq: int) -> str:
    """Construct the `<trade_date>#<fetch_utc>#<seq>` token.

    Linda msg=ba58015d's format. Token is ASCII-sortable so DB indexes
    on `data_source_version` perform predictably across a year of
    daily rows.
    """
    return (
        f"{trade_date.isoformat().replace('-', '')}"
        f"#{fetch_utc.strftime('%Y%m%dT%H%M%SZ')}"
        f"#{seq}"
    )


def next_seq_for_date(session, trade_date: date) -> int:
    """Pick the next `seq` integer for this trade date.

    Lowest unused integer ≥ 1, derived from existing
    `wind_fetch_audit` rows. Reused for every fetch within the same
    `run_daily_fetch` invocation (so one fetch session = one seq,
    regardless of how many Wind tools are called).
    """
    stmt = select(WindFetchAudit.data_source_version).where(
        WindFetchAudit.trade_date == trade_date
    )
    used: set[int] = set()
    for (version,) in session.execute(stmt):
        try:
            used.add(int(version.rsplit("#", 1)[1]))
        except (IndexError, ValueError):
            continue
    candidate = 1
    while candidate in used:
        candidate += 1
    return candidate


def run_daily_fetch(
    settings: Settings,
    *,
    trade_date: date,
    force: bool = False,
) -> int:
    """Coordinate one day's fetch.

    Phase-0 stub: confirms session bootstrap works + emits the next
    `data_source_version` token but doesn't yet invoke Wind. Wired
    fully when Linda's ETF pool + field-dict land.

    Returns a CLI-style exit code: 0 success, 1 if `force=False` and
    a successful fetch already exists for this date.
    """
    fetch_utc = datetime.now(timezone.utc)
    with session_scope() as session:
        seq = next_seq_for_date(session, trade_date)
        if seq > 1 and not force:
            LOG.warning(
                "trade_date=%s already has %d fetch(es); pass --force "
                "to record a new version",
                trade_date,
                seq - 1,
            )
            return 1
        version = make_data_source_version(trade_date, fetch_utc, seq)
        LOG.info("planning fetch: version=%s", version)
        # TODO(@Linda): once the ETF pool and field-dict are final,
        # call WindClient.call(...) here, persist to audit + derived
        # tables, and emit the daily-report markdown.
    return 0
