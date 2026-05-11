"""SQLAlchemy models.

The schema is anchored on `wind_fetch_audit` — every row of Wind data
we persist links back to one fetch event (with raw stdout preserved)
so QA can audit raw vs derived values byte-for-byte. Linda + Vera
+ Nova converged on this contract in #基金数据每日汇报 around
msg=2948fd3d / msg=58003866 / msg=ba58015d / msg=bce51905.

Derived tables (ETF snapshots, fund-company aggregates, daily-report
provenance, …) all carry **both**:

* `wind_fetch_audit_id` — INT FK, the hot-path constraint, NOT NULL.
* `data_source_version` — TEXT, the human-readable token
  (`<trade_date>#<fetch_utc>#<seq>`). Indexed for dashboard lookups
  and cross-table reasoning.

The reasoning for both: FK enforces referential integrity, the token
is what shows up in error messages, dashboards, and the wiki
`data_provenance` field.

The derived tables below are deliberately minimal — they cover the
P0 scope (ETF daily snapshot + fund company aggregate) Linda confirmed
in msg=91b45123 and msg=ea6be16d. Additional tables (new申报 /
发行中 / index components) land in subsequent migrations once
@Linda's field dictionary is final.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base — every table inherits."""


# --- audit -----------------------------------------------------------------


class WindFetchAudit(Base):
    """One row per Wind CLI invocation.

    `wind_raw_response` is preserved verbatim (full subprocess stdout)
    so Vera's `qa/consistency_checks.md` can JOIN raw vs derived for
    any cell.

    `data_source_version` is `<trade_date>#<fetch_utc>#<seq>` per
    Linda msg=ba58015d. `seq` increments when a `--force` rerun
    happens for the same `trade_date`; old rows are never overwritten.

    SQLite stores JSON columns as TEXT under the hood; Postgres
    promotes to JSONB. We use `String` + JSON-string in raw form to
    stay portable; downstream queries can parse on demand.
    """

    __tablename__ = "wind_fetch_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    wind_tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    wind_request_payload: Mapped[str] = mapped_column(String, nullable=False)
    wind_raw_response: Mapped[str] = mapped_column(String, nullable=False)
    wind_fetch_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    data_source_version: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    derived_record_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    __table_args__ = (
        Index("ix_wind_fetch_audit_tool_date", "wind_tool_name", "trade_date"),
    )


# --- ETF daily snapshot ----------------------------------------------------


class EtfDailySnapshot(Base):
    """ETF daily snapshot — one row per (windcode, trade_date).

    Field list from Linda msg=91b45123 (`fund_data:get_fund_price_indicators`)::

        NAME / MATCH / SHARES / FUNDSIZE / NETVALUE /
        ACCUMULATEDNETVALUE / CHANGERANGE / IOPV / FORWARDDISCOUNT /
        windcode

    `INVALID` / missing field handling per Nova msg=ea6be16d's RAG
    contract: store the literal value or NULL — never coerce. A
    `shares_status` enum field distinguishes `VALID` / `INVALID` /
    `MISSING` so the daily-report markdown can faithfully transcribe
    "份额数据待确认" instead of guessing 0.
    """

    __tablename__ = "etf_daily_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    wind_fetch_audit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("wind_fetch_audit.id"), nullable=False, index=True
    )
    data_source_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    windcode: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    fund_size_yuan: Mapped[float | None] = mapped_column(nullable=True)
    nav: Mapped[float | None] = mapped_column(nullable=True)
    cumulative_nav: Mapped[float | None] = mapped_column(nullable=True)
    change_range: Mapped[float | None] = mapped_column(nullable=True)
    iopv: Mapped[float | None] = mapped_column(nullable=True)
    forward_discount: Mapped[float | None] = mapped_column(nullable=True)

    shares: Mapped[float | None] = mapped_column(nullable=True)
    # status semantics (per Nova msg=ea6be16d). The DB-level Enum
    # constraint prevents an upstream string typo from sneaking in;
    # Vera msg=ca796844 MEDIUM-2 specifically asked for this.
    shares_status: Mapped[str] = mapped_column(
        SAEnum(
            "VALID",
            "INVALID",
            "MISSING",
            "NOT_APPLICABLE",
            name="shares_status_enum",
        ),
        nullable=False,
    )

    __table_args__ = (
        # `data_source_version` is part of the unique key so a `--force`
        # rerun for the same (windcode, trade_date) creates a new row
        # rather than colliding (Vera msg=ca796844 MEDIUM-1).
        UniqueConstraint(
            "windcode",
            "trade_date",
            "data_source_version",
            name="uq_etf_daily_code_date_version",
        ),
    )

    audit: Mapped["WindFetchAudit"] = relationship(WindFetchAudit)


# --- fund company aggregate ------------------------------------------------


class FundCompanyAggregate(Base):
    """Aggregate by 基金公司 — one row per (company_name, trade_date).

    Sourced from `fund_data:get_fund_company_info` per Linda
    msg=91b45123. Fields validated against returned columns; we keep
    the schema narrow to start, room to add columns when the field
    dictionary finalizes.
    """

    __tablename__ = "fund_company_aggregate"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    wind_fetch_audit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("wind_fetch_audit.id"), nullable=False, index=True
    )
    data_source_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    company_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    aum_yuan: Mapped[float | None] = mapped_column(nullable=True)
    fund_count: Mapped[int | None] = mapped_column(nullable=True)
    etf_aum_yuan: Mapped[float | None] = mapped_column(nullable=True)

    __table_args__ = (
        # Include `data_source_version` so `--force` re-fetches for the
        # same day don't trigger a UNIQUE conflict. Same reasoning as
        # `etf_daily_snapshot` above. (Vera msg=ca796844 MEDIUM-1.)
        UniqueConstraint(
            "company_name",
            "trade_date",
            "data_source_version",
            name="uq_fund_company_date_version",
        ),
    )

    audit: Mapped["WindFetchAudit"] = relationship(WindFetchAudit)


# --- daily report provenance ----------------------------------------------


class DailyReportProvenance(Base):
    """Audit trail for each daily-report markdown emission.

    Cross-product contract (Nova msg=3288dc8b): every daily-report
    markdown page emitted to llm-wiki is logged here so we can trace
    "this report was generated from this Wind fetch at this time".

    The `markdown_path` is relative to
    `settings.daily_report_output_dir`.
    """

    __tablename__ = "daily_report_provenance"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    markdown_path: Mapped[str] = mapped_column(String(255), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    data_source_versions: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc=(
            "Comma-joined list of `data_source_version` tokens that "
            "contributed to this report — typically multiple Wind "
            "fetches (ETF + company aggregate)."
        ),
    )
