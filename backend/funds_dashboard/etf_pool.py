"""ETF pool V0 — initial set of windcodes the daily fetch covers.

Linda v3 SSOT field dictionary §"Key ETF Pool V0" — the first iteration
small enough to verify manually. Production should move this list into
the `runtime_config` table (admins edit via the config-Web page), but
for the scheduler runner test-first phase the list lives in code so the
e2e tests have a deterministic input.

Each entry mirrors the schema Linda specified:
`windcode / display_name / bucket / index_or_theme / is_active / start_date / notes`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class EtfPoolEntry:
    windcode: str
    display_name: str
    bucket: str
    index_or_theme: str
    is_active: bool = True
    start_date: date | None = None
    notes: str = ""


# Subset of Linda's full V0 list — the scheduler runner uses these
# until the runtime_config admin UI takes over. Conservative size (one
# anchor per bucket) so a single live fetch round trip stays under a
# few seconds; the operator-supplied list can grow to 30+ symbols once
# the Wind backend's rate-limit behaviour is observed.
ETF_POOL_V0: tuple[EtfPoolEntry, ...] = (
    EtfPoolEntry(
        windcode="510300.SH",
        display_name="沪深300ETF华泰柏瑞",
        bucket="broad_based",
        index_or_theme="沪深300",
        notes="Linda 探针验证 SHARES=INVALID 的标的",
    ),
    EtfPoolEntry(
        windcode="510500.SH",
        display_name="中证500ETF",
        bucket="broad_based",
        index_or_theme="中证500",
    ),
    EtfPoolEntry(
        windcode="588200.SH",
        display_name="科创50ETF",
        bucket="broad_based",
        index_or_theme="科创50",
    ),
)


def active_windcodes() -> list[str]:
    """Return the windcodes that the next scheduled fetch should touch."""
    return [entry.windcode for entry in ETF_POOL_V0 if entry.is_active]
