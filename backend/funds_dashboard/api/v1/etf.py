"""ETF snapshot read endpoints.

Phase 1 visibility layer — the runner already lands rows into
`etf_daily_snapshot`, but until this router shipped there was no
HTTP surface for the UI to display them. feng-lu's 2026-05-11 19:27
DM ("运行我看看") + 20:03 follow-up ("没有看到") + Keira UI
msg=299c1782 + Vera msg=fc606e30 all converged on the same gap.

This router is INTENTIONALLY thin:
  - GET `/etf/snapshots` returns the latest day's rows by default,
    or a specific `trade_date` (ISO `YYYY-MM-DD`) on request
  - GET `/etf/provenance` returns the matching daily-report metadata
    so the UI can link to the rendered markdown

Both endpoints require an authenticated admin per the v1 contract
("protected sub-routers attach `Depends(require_authenticated_admin)`
on each route"). The data is internal-ops surface, not public.

`shares_status` + `missing_reason` are forwarded verbatim — Linda's
no-coerce-to-0 rule (msg=91b45123) lives at the JSON boundary too.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ...auth import SessionPayload, require_authenticated_admin
from ...db import get_db_session
from ...db.models import DailyReportProvenance, EtfDailySnapshot


router = APIRouter(prefix="/etf", tags=["etf"])


# `shares_status` ⊆ {VALID, INVALID, MISSING, NOT_APPLICABLE} per
# Linda/Nova/Vera SSOT. Declared as Literal here so OpenAPI clients
# get the enum in their generated bindings.
SharesStatus = Literal["VALID", "INVALID", "MISSING", "NOT_APPLICABLE"]


class EtfSnapshot(BaseModel):
    """Wire shape for one ETF daily snapshot row.

    Mirrors `funds_dashboard.db.models.EtfDailySnapshot` columns minus
    the audit FK + autoincrement id. `data_source_version` is
    citation-only metadata — UIs render it as a tooltip / footnote so
    the analyst can trace the row back to the underlying Wind fetch.
    """

    windcode: str
    name: str | None
    trade_date: date
    fund_size_yuan: float | None
    nav: float | None = Field(default=None, description="Net asset value (元)")
    cumulative_nav: float | None = None
    change_range: float | None = Field(
        default=None, description="Daily change in basis points (%)"
    )
    iopv: float | None = None
    forward_discount: float | None = None
    shares: float | None
    shares_status: SharesStatus
    missing_reason: str | None
    data_source_version: str


class SnapshotsResponse(BaseModel):
    trade_date: date
    rows: list[EtfSnapshot]
    data_source_versions: list[str]


class ProvenanceResponse(BaseModel):
    report_date: date
    markdown_path: str
    data_source_versions: str
    generated_at: str


def _version_sort_key(version: str) -> tuple[int, str, int, str]:
    """Sort data_source_version tokens by effective dashboard freshness.

    Real runner versions always outrank demo placeholders. Within the
    same kind, use the embedded timestamp and sequence when present,
    with the raw token as a stable final tiebreaker.
    """
    parts = version.split("#")
    timestamp = parts[1] if len(parts) > 1 else ""
    suffix = parts[2] if len(parts) > 2 else ""
    is_demo = suffix == "demo" or version.endswith("#demo")
    try:
        sequence = int(suffix)
    except ValueError:
        sequence = -1
    return (0 if is_demo else 1, timestamp, sequence, version)


def _latest_snapshot_rows(rows: list[EtfDailySnapshot]) -> list[EtfDailySnapshot]:
    """Pick one main-table row per ETF while preserving audit versions elsewhere."""
    latest_by_windcode: dict[str, EtfDailySnapshot] = {}
    for row in rows:
        existing = latest_by_windcode.get(row.windcode)
        if existing is None or _version_sort_key(row.data_source_version) > _version_sort_key(
            existing.data_source_version
        ):
            latest_by_windcode[row.windcode] = row
    return [latest_by_windcode[windcode] for windcode in sorted(latest_by_windcode)]


def _latest_trade_date(session: Session) -> date | None:
    """Most recent date that has at least one ETF snapshot.

    Used as the default `trade_date` for the snapshots endpoint when
    the caller doesn't pass one — saves the UI from having to make
    two round-trips on first paint.
    """
    return session.scalar(
        select(EtfDailySnapshot.trade_date)
        .order_by(desc(EtfDailySnapshot.trade_date))
        .limit(1)
    )


@router.get(
    "/snapshots",
    response_model=SnapshotsResponse,
    summary="List ETF daily snapshots",
)
def list_snapshots(
    trade_date: date | None = Query(
        default=None,
        description=(
            "ISO date. Omit to get the latest available trade_date — "
            "useful for the dashboard's default landing view."
        ),
    ),
    _admin: SessionPayload = Depends(require_authenticated_admin),
    session: Session = Depends(get_db_session),
) -> SnapshotsResponse:
    """Return ETF snapshots for `trade_date` (default: latest).

    If multiple fetches landed for the same `trade_date` (a `--force`
    rerun produces a second `data_source_version`), this endpoint
    returns only the latest effective row per ETF for the main table.
    The response's `data_source_versions` array still carries EVERY
    version for provenance/history.
    """
    target = trade_date or _latest_trade_date(session)
    if target is None:
        # No data at all yet — return an empty response rather than
        # 404 so the UI can render an empty state instead of an error.
        return SnapshotsResponse(
            trade_date=date.today(), rows=[], data_source_versions=[]
        )

    db_rows = (
        session.execute(
            select(EtfDailySnapshot)
            .where(EtfDailySnapshot.trade_date == target)
            .order_by(EtfDailySnapshot.windcode, EtfDailySnapshot.data_source_version)
        )
        .scalars()
        .all()
    )

    rows = [
        EtfSnapshot(
            windcode=r.windcode,
            name=r.name,
            trade_date=r.trade_date,
            fund_size_yuan=r.fund_size_yuan,
            nav=r.nav,
            cumulative_nav=r.cumulative_nav,
            change_range=r.change_range,
            iopv=r.iopv,
            forward_discount=r.forward_discount,
            shares=r.shares,
            shares_status=r.shares_status,
            missing_reason=r.missing_reason,
            data_source_version=r.data_source_version,
        )
        for r in _latest_snapshot_rows(db_rows)
    ]
    versions = sorted({r.data_source_version for r in db_rows}, key=_version_sort_key)

    return SnapshotsResponse(
        trade_date=target, rows=rows, data_source_versions=versions
    )


@router.get(
    "/provenance",
    response_model=ProvenanceResponse,
    summary="Daily-report provenance for a trade_date",
)
def get_provenance(
    trade_date: date | None = Query(
        default=None,
        description=(
            "ISO date. Omit to get the latest day with a "
            "DailyReportProvenance row."
        ),
    ),
    _admin: SessionPayload = Depends(require_authenticated_admin),
    session: Session = Depends(get_db_session),
) -> ProvenanceResponse:
    """Return the `DailyReportProvenance` row for `trade_date`.

    Used by the UI to link out to the rendered markdown report and to
    surface the audit-trail token (`data_source_versions`, possibly
    comma-separated when a `--force` rerun happened).
    """
    if trade_date is None:
        row = session.scalar(
            select(DailyReportProvenance)
            .order_by(desc(DailyReportProvenance.report_date))
            .limit(1)
        )
    else:
        row = session.scalar(
            select(DailyReportProvenance).where(
                DailyReportProvenance.report_date == trade_date
            )
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "no DailyReportProvenance row found"
                + (f" for {trade_date.isoformat()}" if trade_date else "")
            ),
        )
    return ProvenanceResponse(
        report_date=row.report_date,
        markdown_path=row.markdown_path,
        data_source_versions=row.data_source_versions,
        generated_at=row.generated_at.isoformat(timespec="milliseconds"),
    )
