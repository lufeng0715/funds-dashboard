"""Daily fetch orchestration.

The runner is the thin "do one day's work" coordinator that both the
scheduled APScheduler job and the manual `funds-dashboard-fetch CLI
call route through. Keeping the orchestration in one place means the
audit story (one `wind_fetch_audit` row per Wind call, derived rows
linked back to it) is identical regardless of how the run was
triggered.

Phase 1 wires three steps that PR #3 and PR #5 made safe:

  1. resolve the Wind API key — first from encrypted `secret_config`
     (seeded by the config-Web `seed_wind_key_from_env`), then from
     plain `settings.wind_api_key` for dev convenience.
  2. for each `etf_pool.ETF_POOL_V0` entry: `WindClient.call` →
     `parse_etf_snapshot_rows` → `record_wind_fetch` +
     `record_etf_snapshots`. The audit-row write redacts secrets;
     the parser preserves `INVALID/MISSING/NOT_APPLICABLE` semantics
     and writes `missing_reason` alongside `shares_status`.
  3. emit a minimal daily-report markdown into
     `settings.daily_report_output_dir` for llm-wiki ingestion +
     log a `DailyReportProvenance` row pointing at it.

The fund-company aggregate and the FAQ section of the report land in
the next slice — the scope here is "first real ETF row in production"
which is what feng-lu asked for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..config_store import crypto
from ..db import session_scope
from ..db.audit import record_etf_snapshots, record_wind_fetch
from ..db.models import DailyReportProvenance, SecretConfig, WindFetchAudit
from ..etf_pool import ETF_POOL_V0, active_windcodes
from ..parsers.etf_snapshot import (
    EtfSnapshotMultiInput,
    parse_etf_snapshot_from_multi_tool,
)
from ..wind import WindClient, WindError


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


@dataclass
class FetchRunResult:
    """Tally of what one `run_daily_fetch` invocation actually did.

    Useful for CLI exit-code branching and for the e2e tests that
    assert how many rows landed.
    """

    data_source_version: str
    audit_rows: int = 0
    derived_rows: int = 0
    failed_windcodes: list[str] = None  # type: ignore[assignment]
    markdown_path: Path | None = None

    def __post_init__(self) -> None:
        if self.failed_windcodes is None:
            object.__setattr__(self, "failed_windcodes", [])


def _resolve_wind_api_key(session: Session, settings: Settings) -> str | None:
    """Pull the active Wind API key — encrypted store first, env fallback.

    Phase 0.5 seeds the env-supplied key into `secret_config.wind_api_key`
    on first boot (`api.v1.config.seed_wind_key_from_env`). Once that
    seed happens, the encrypted row is the source of truth — the env
    can be unset and the runner keeps working. If neither exists,
    return None and let the caller log + bail.
    """
    row = session.scalar(
        select(SecretConfig).where(SecretConfig.name == "wind_api_key")
    )
    if row is not None:
        return crypto.decrypt(
            crypto.EncryptedSecret(
                ciphertext=row.ciphertext,
                nonce=row.nonce,
                salt=row.salt,
                algorithm_version=row.algorithm_version,
                key_version=row.key_version,
            )
        )
    if settings.wind_api_key is not None:
        return settings.wind_api_key.get_secret_value()
    return None


def _build_wind_client(settings: Settings, api_key: str | None) -> WindClient:
    return WindClient(
        node_path=settings.wind_cli_node_path,
        cli_script=settings.wind_cli_script,
        api_key=api_key,
        timeout_s=60.0,
    )


def _safe_wind_call(
    session: Session,
    wind: WindClient,
    *,
    tool_name: str,
    payload: dict,
    trade_date: date,
    version: str,
):
    """Run one Wind tool call + audit the response (or the failure).

    Returns `(result, audit_row)` on success; `(None, None)` on
    `WindError`. The audit row persists `wind_raw_response` even on
    the success path so a future post-mortem can replay the exact
    bytes the backend returned. Linda msg=64b7d14d preserved-fixture
    rule lives here at the audit-write layer.

    Each tool call gets its own audit row (one Wind call = one
    `wind_fetch_audit` entry) — Phase 0 design ports through unchanged.
    """
    from ..wind import WindResult  # avoid surfacing in module-level deps

    try:
        result = wind.call(tool_name, payload)
    except WindError as exc:
        LOG.warning(
            "wind fetch failed: tool=%s payload=%s err=%s",
            tool_name, payload, exc,
        )
        return None, None
    audit = record_wind_fetch(
        session,
        result=result,
        trade_date=trade_date,
        data_source_version=version,
    )
    return result, audit


def _empty_wind_result(windcode: str):
    """Sentinel `WindResult` for the case where one of the two-call
    flow tools failed but the other succeeded.

    Holding a real `WindResult` (vs `None`) keeps the parser's
    column-index lookup simple — it just returns None for every
    field. Audit-side this synthetic value is never recorded; only
    the live tool responses get audit rows.
    """
    from ..wind import WindResult

    return WindResult(
        tool_name="<synthetic-empty>",
        request_payload={"windcode": windcode},
        columns=[],
        rows=[],
        raw_stdout="",
    )


def _emit_daily_report(
    settings: Settings,
    *,
    trade_date: date,
    data_source_version: str,
    snapshots: list[tuple[str, str, float | None, str, str | None]],
) -> Path:
    """Generate the minimal Phase 1 markdown report.

    Linda v3 SSOT + Nova msg=ea6be16d cross-product RAG contract:
    every numeric field that's missing carries an explicit reason in
    the rendered cell; never "无数据" / 0 collapse.

    `snapshots` rows are tuples `(windcode, name, fund_size_yuan,
    shares_status, missing_reason)`.
    """
    output_dir = settings.daily_report_output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{trade_date.isoformat()}.md"

    lines: list[str] = [
        "---",
        f"title: 基金每日数据汇报 {trade_date.isoformat()}",
        "category: funds",
        "report_type: daily_dashboard",
        f"report_date: {trade_date.isoformat()}",
        f"data_source_version: {data_source_version}",
        "data_source: wind",
        "---",
        "",
        f"# 基金每日数据汇报 {trade_date.isoformat()}",
        "",
        "## 重点 ETF 规模快照",
        "",
        "| windcode | 简称 | 基金规模 (亿元) | 份额状态 | 缺失原因 |",
        "|---|---|---|---|---|",
    ]
    for windcode, name, fund_size_yuan, shares_status, missing_reason in snapshots:
        scale = (
            f"{fund_size_yuan / 1e8:.2f}"
            if fund_size_yuan is not None
            else "—"
        )
        reason = missing_reason if missing_reason else "—"
        lines.append(
            f"| {windcode} | {name or '—'} | {scale} | {shares_status} | {reason} |"
        )
    lines.append("")
    lines.append(
        "> 缺失原因映射：`invalid_value` = Wind 返回无效值；"
        "`not_returned` = 字段未返回；`not_applicable` = 当前标的不适用。"
        "数值列为 `—` 表示无效或未返回 — **不是 0**。"
    )
    lines.append("")
    # Data-source footnote — Linda msg=2527f6d1 review condition:
    # NAV / fund_size 来源必须可解释，不能和正式日终单位净值混淆。
    # Phase 0 runner uses the available Wind tools (the original
    # structured `fund_data:get_fund_price_indicators` is currently
    # down on the Wind backend); when it comes back the runner can
    # be swapped to use it again without changing this report shape.
    lines.append(
        "> **字段来源**："
        "`基金规模` 来自 `analytics_data:get_financial_data` NL 查询（"
        "Wind 结构化 JSON 返回，非自由文本）；"
        "其他字段（`净值` / `份额` / `折溢价` / `累计净值` / `涨跌幅`）原本应由 "
        "`fund_data:get_fund_price_indicators` 提供，但该工具当前 Wind 后端"
        "不可用，故未在表中显示 — 未填字段一律 `MISSING/not_returned`，"
        "**不是 0**。`get_fund_quote` 的 intraday MATCH 价已写入 "
        "`etf_daily_snapshot.nav` 作为日间价格代理（非日终单位净值）。"
    )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run_daily_fetch(
    settings: Settings,
    *,
    trade_date: date,
    force: bool = False,
) -> FetchRunResult:
    """Coordinate one day's fetch — Wind → parser → DB → markdown.

    `force=False` (default) refuses to overwrite an existing fetch for
    the same trade date; pass `force=True` to bump the `seq` portion
    of `data_source_version`. Old rows stay put — every rerun creates
    a new version.

    Returns a `FetchRunResult` summary; callers (CLI / scheduler job)
    typically log it and translate to a CLI exit code.
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
            return FetchRunResult(
                data_source_version=make_data_source_version(
                    trade_date, fetch_utc, seq - 1
                )
            )

        api_key = _resolve_wind_api_key(session, settings)
        if api_key is None:
            LOG.error(
                "no Wind API key available (neither secret_config nor "
                "env). Run the config-Web seed or set WIND_API_KEY."
            )
            return FetchRunResult(
                data_source_version=make_data_source_version(
                    trade_date, fetch_utc, seq
                )
            )

        version = make_data_source_version(trade_date, fetch_utc, seq)
        LOG.info("starting fetch: version=%s pool_size=%d", version, len(ETF_POOL_V0))
        wind = _build_wind_client(settings, api_key)

        report_rows: list[tuple[str, str, float | None, str, str | None]] = []
        failed: list[str] = []
        derived_total = 0
        audit_total = 0

        for windcode in active_windcodes():
            # Two-call flow per ETF (PR (d) runner-fund-data-tool-switch):
            # `get_fund_quote` for intraday MATCH (NAV proxy) +
            # `analytics_data:get_financial_data` for name + fund_size.
            # The original `get_fund_price_indicators` probe is broken
            # on the Wind backend right now (Alex msg=293ecab0); both
            # replacements were verified working against the same key.
            #
            # Audit invariant (Linda + Vera): persist BOTH raw responses
            # as separate `wind_fetch_audit` rows so any future Wind
            # tool surprise can be reproduced from `wind_raw_response`
            # without needing the broken upstream.
            quote_result, quote_audit = _safe_wind_call(
                session,
                wind,
                tool_name="fund_data:get_fund_quote",
                payload={"windcode": windcode},
                trade_date=trade_date,
                version=version,
            )
            if quote_result is None:
                LOG.warning("get_fund_quote failed for %s", windcode)
                failed.append(windcode)
                report_rows.append(
                    (windcode, "—", None, "MISSING", "not_returned")
                )
                continue
            audit_total += 1

            size_result, _size_audit = _safe_wind_call(
                session,
                wind,
                tool_name="analytics_data:get_financial_data",
                payload={
                    "question": f"{windcode} 最新基金规模 中文简称"
                },
                trade_date=trade_date,
                version=version,
            )
            if size_result is None:
                # The price-only path can still produce a row (NAV
                # known), but without a name/size the row's value is
                # limited. Surface as MISSING/not_returned on size
                # rather than skip — feng-lu prefers partial visibility
                # over hidden failures.
                LOG.warning(
                    "analytics_data:get_financial_data failed for %s "
                    "— fund_size_yuan / name will be MISSING",
                    windcode,
                )
            else:
                audit_total += 1

            snap = parse_etf_snapshot_from_multi_tool(
                EtfSnapshotMultiInput(
                    windcode=windcode,
                    quote_result=quote_result,
                    size_result=size_result or _empty_wind_result(windcode),
                    trade_date=trade_date.isoformat(),
                    data_source_version=version,
                    quote_audit_id=quote_audit.id,
                )
            )
            inserted = record_etf_snapshots(session, [snap])
            quote_audit.derived_record_count = inserted
            derived_total += inserted

            report_rows.append(
                (
                    snap.windcode,
                    snap.name or windcode,
                    snap.fund_size_yuan,
                    snap.shares_status,
                    snap.missing_reason,
                )
            )

        markdown_path = _emit_daily_report(
            settings,
            trade_date=trade_date,
            data_source_version=version,
            snapshots=report_rows,
        )

        # UPSERT — Linda msg=9589ed01 ruling: "one trade date → one
        # current daily-report". `--force` reruns update the existing
        # row in place (data_source_versions / markdown_path /
        # generated_at) so the dashboard's "today's report" lookup
        # always finds exactly one match. (Vera msg=3d6bc478 MEDIUM.)
        existing = session.scalar(
            select(DailyReportProvenance).where(
                DailyReportProvenance.report_date == trade_date
            )
        )
        relative_path = str(
            markdown_path.relative_to(settings.daily_report_output_dir.parent)
        )
        if existing is None:
            session.add(
                DailyReportProvenance(
                    report_date=trade_date,
                    markdown_path=relative_path,
                    data_source_versions=version,
                )
            )
        else:
            # Append the new version to the audit trail token; the
            # dashboard / RAG ingestion can read every contributing
            # version without losing history. `generated_at` updates
            # automatically via the row mutation triggering Python-side
            # defaults? No — `default=...` only fires on INSERT. Set
            # `generated_at` explicitly here so the UPDATE actually
            # reflects the rerun timestamp.
            existing_versions = existing.data_source_versions or ""
            joined = (
                f"{existing_versions},{version}"
                if existing_versions
                else version
            )
            existing.markdown_path = relative_path
            existing.data_source_versions = joined
            existing.generated_at = datetime.now(timezone.utc)

        return FetchRunResult(
            data_source_version=version,
            audit_rows=audit_total,
            derived_rows=derived_total,
            failed_windcodes=failed,
            markdown_path=markdown_path,
        )
