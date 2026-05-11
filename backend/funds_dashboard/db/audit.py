"""Audit-row persistence helpers.

Centralises every write into `wind_fetch_audit` so the secret-redaction
step runs in exactly one place (Vera msg=ca796844 CRITICAL-2). Callers
pass a `WindResult` and the helper builds the row — they never reach
for `wind_raw_response` directly, which removes the foot-gun of
forgetting to redact at one call site.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Iterable

from .models import EtfDailySnapshot, WindFetchAudit
from ..wind.redact import redact_secrets


if TYPE_CHECKING:  # avoid import cycle at runtime
    from sqlalchemy.orm import Session

    from ..parsers.etf_snapshot import ParsedEtfSnapshot
    from ..wind import WindResult


def record_wind_fetch(
    session: "Session",
    *,
    result: "WindResult",
    trade_date: date,
    data_source_version: str,
    derived_record_count: int = 0,
) -> WindFetchAudit:
    """Persist a `WindFetchAudit` row from a `WindResult`.

    Both `wind_request_payload` and `wind_raw_response` are funneled
    through `redact_secrets()` so any `ak_*` token that slipped into the
    Wind response can never reach the audit table.

    Returns the persisted row (caller may use `.id` as the FK target
    for derived-table inserts).
    """
    audit = WindFetchAudit(
        trade_date=trade_date,
        wind_tool_name=result.tool_name,
        wind_request_payload=redact_secrets(
            json.dumps(result.request_payload, ensure_ascii=False)
        ),
        wind_raw_response=redact_secrets(result.raw_stdout),
        wind_fetch_timestamp=datetime.now(timezone.utc),
        data_source_version=data_source_version,
        derived_record_count=derived_record_count,
    )
    session.add(audit)
    session.flush()  # populate audit.id without committing
    return audit


def record_etf_snapshots(
    session: "Session",
    parsed_rows: "Iterable[ParsedEtfSnapshot]",
) -> int:
    """Persist parser output to `etf_daily_snapshot`.

    `ParsedEtfSnapshot.trade_date` arrives as an ISO string (the
    parser is pure / doesn't import datetime); convert to `date`
    here so the model's typed column accepts it.

    Returns the number of rows inserted. Caller (scheduler runner)
    typically updates the originating `WindFetchAudit.derived_record_count`
    with this value.
    """
    count = 0
    for parsed in parsed_rows:
        session.add(
            EtfDailySnapshot(
                wind_fetch_audit_id=parsed.wind_fetch_audit_id,
                data_source_version=parsed.data_source_version,
                windcode=parsed.windcode,
                trade_date=date.fromisoformat(parsed.trade_date),
                name=parsed.name,
                fund_size_yuan=parsed.fund_size_yuan,
                nav=parsed.nav,
                cumulative_nav=parsed.cumulative_nav,
                change_range=parsed.change_range,
                iopv=parsed.iopv,
                forward_discount=parsed.forward_discount,
                shares=parsed.shares,
                shares_status=parsed.shares_status,
            )
        )
        count += 1
    session.flush()
    return count
