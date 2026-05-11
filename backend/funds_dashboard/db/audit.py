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
from typing import TYPE_CHECKING

from .models import WindFetchAudit
from ..wind.redact import redact_secrets


if TYPE_CHECKING:  # avoid import cycle at runtime
    from sqlalchemy.orm import Session

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
