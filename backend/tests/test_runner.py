"""Scheduler runner end-to-end tests.

The runner glues `WindClient.call → parse_etf_snapshot_rows →
record_wind_fetch + record_etf_snapshots → daily-report markdown`.
These tests exercise that chain with a stubbed WindClient so they
run in milliseconds and don't depend on the live Wind backend, but
they cover the same data shapes Linda's real probe captured in
`tests/fixtures/real_wind_samples.py`.

Three flavours are pinned:
* happy path — every ETF in the pool returns data → derived rows
  land, markdown emitted with all entries
* partial Wind failure — one ETF errors → fetch survives, that
  windcode lands as a `not_returned` row in the report, valid ETFs
  still persist
* secret resolution — encrypted `secret_config.wind_api_key` is
  preferred over `settings.wind_api_key`; if neither set, runner
  refuses cleanly (logs error, returns zero-row result, no crash)
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from funds_dashboard.config import Settings
from funds_dashboard.config_store import crypto
from funds_dashboard.db import init_sessionmaker, session_scope
from funds_dashboard.db.audit import record_etf_snapshots, record_wind_fetch
from funds_dashboard.db.models import (
    Base,
    DailyReportProvenance,
    EtfDailySnapshot,
    SecretConfig,
    WindFetchAudit,
)
from funds_dashboard.scheduler.runner import run_daily_fetch
from funds_dashboard.wind import WindError, WindResult


_REPORT_TRADE_DATE = date(2026, 5, 11)


def _make_settings(tmp_path, *, wind_api_key: str | None = "ak_runner_test_KEY_12345") -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/runner.db",
        FUNDS_DASHBOARD_MASTER_KEY="runner-master-key",
        FUNDS_DASHBOARD_ADMIN_PASSWORD_HASH=(
            "$2b$04$abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNO."
        ),
        WIND_API_KEY=wind_api_key,
        wind_cli_node_path="node",
        wind_cli_script="cli.mjs",
        daily_report_output_dir=tmp_path / "reports",
    )


@pytest.fixture(autouse=True)
def _master_key_env(monkeypatch):
    monkeypatch.setenv("FUNDS_DASHBOARD_MASTER_KEY", "runner-master-key")


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = _make_settings(tmp_path)
    init_sessionmaker(s.database_url)
    # Migration shortcut: create all tables directly (alembic is tested
    # elsewhere; here we just want a usable DB for the runner to write to).
    from sqlalchemy import create_engine

    engine = create_engine(s.database_url, future=True)
    Base.metadata.create_all(engine)
    return s


def _quote_result(windcode: str, *, last_match: object = 1.234) -> WindResult:
    """Fake `fund_data:get_fund_quote` response — intraday minute bars.

    Matches the real CLI shape captured by Alex msg=293ecab0 probe:
    last row's `MATCH` is the latest quoted price. Other columns
    elided for brevity (parser only reads MATCH on the last row).
    """
    return WindResult(
        tool_name="fund_data:get_fund_quote",
        request_payload={"windcode": windcode},
        columns=["MATCH", "AVGPRICE", "VOLUME", "TURNOVER", "TIME", "_DATE"],
        rows=[
            ["1.000", "0.999", "100", "100", "2026/05/11 09:30:00.000(+32)", "20260511"],
            [last_match, "1.0", "200", "200", "2026/05/11 14:59:00.000(+32)", "20260511"],
        ],
        raw_stdout='{"data":{}}',
    )


def _size_result(
    windcode: str,
    *,
    name: str | None = None,
    size_yi: float = 100.0,
    name_column: str = "基金简称_中文",
) -> WindResult:
    """Fake `analytics_data:get_financial_data` size response.

    `name_column` defaults to `基金简称_中文` — the real-Wind column
    header captured by Vera msg=e1c92857 on PR #16's first live fetch.
    Pass `name_column="证券简称"` to verify the parser's fallback
    accepts the alternate header name (defence-in-depth against Wind
    NL router picking different phrasings).

    `name` defaults to `f"name-{windcode}"` so tests can keep the
    "name landed" assertion. Pass explicit `None` to simulate a Wind
    response without name (rare; analytics_data usually returns it).
    """
    rendered_name = f"name-{windcode}" if name is None else name
    return WindResult(
        tool_name="analytics_data:get_financial_data",
        request_payload={"question": f"{windcode} 最新基金规模 中文简称"},
        columns=["Wind代码", name_column, "最新基金规模"],
        rows=[[windcode, rendered_name, size_yi]],
        raw_stdout='{"data":{}}',
    )


def _make_dispatch(*, override_quote=None, override_size=None):
    """Build a `WindClient.call`-shaped fake that routes by tool_name.

    `override_quote` / `override_size`, when set, replace the default
    response for that tool (used by failure-path tests).
    """
    def _fake_call(self, tool_name, payload):
        windcode = payload.get("windcode") or (
            payload.get("question", "").split()[0] if "question" in payload else ""
        )
        if tool_name == "fund_data:get_fund_quote":
            if override_quote is not None:
                return override_quote(windcode)
            return _quote_result(windcode)
        if tool_name == "analytics_data:get_financial_data":
            if override_size is not None:
                return override_size(windcode)
            return _size_result(windcode)
        raise AssertionError(
            f"unexpected tool_name in test fake: {tool_name!r}"
        )
    return _fake_call


def test_run_daily_fetch_happy_path_writes_rows_and_markdown(settings, tmp_path) -> None:
    """Every ETF in the pool returns numeric data → all rows persist
    + markdown gets written.

    Runner uses two Wind tools per ETF (PR (d) `runner-fund-data-tool-switch`):
      - `fund_data:get_fund_quote` → intraday MATCH last → nav proxy
      - `analytics_data:get_financial_data` → name + fund_size_yuan
    `shares` / `iopv` / etc stay `MISSING/not_returned` because no
    currently-working Wind tool returns them. Markdown shows `MISSING`
    status without a `VALID` claim for those fields.
    """
    with patch(
        "funds_dashboard.scheduler.runner.WindClient.call",
        _make_dispatch(),
    ):
        result = run_daily_fetch(settings, trade_date=_REPORT_TRADE_DATE)

    assert result.audit_rows == 6  # 2 tool calls × 3 ETFs
    assert result.derived_rows == 3
    assert result.failed_windcodes == []
    assert result.markdown_path is not None
    assert result.markdown_path.exists()

    # DB state
    with session_scope() as session:
        snapshots = session.scalars(select(EtfDailySnapshot)).all()
        assert {s.windcode for s in snapshots} == {
            "510300.SH",
            "510500.SH",
            "588200.SH",
        }
        for snap in snapshots:
            # name + fund_size from analytics_data, market_price from quote
            assert snap.name and snap.name.startswith("name-")
            assert snap.fund_size_yuan == 100.0 * 1e8  # 100 亿元 → 元
            # MATCH last-row → `market_price` (PR e rename — Linda
            # hardline #1, this is the intraday quoted price not basis NAV)
            assert snap.market_price == 1.234
            # `shares` family — current stub size_result doesn't carry
            # shares/IOPV/unit_nav columns, so they stay MISSING. The
            # real Wind probe (test_real_wind_regression) covers the
            # populated path.
            assert snap.shares is None
            assert snap.shares_status == "MISSING"
            assert snap.missing_reason == "not_returned"
            assert snap.iopv is None
            assert snap.unit_nav is None
            assert snap.forward_discount is None
        audits = session.scalars(select(WindFetchAudit)).all()
        # 2 tool calls × 3 windcodes = 6 audit rows
        assert len(audits) == 6

    text = result.markdown_path.read_text(encoding="utf-8")
    assert "report_type: daily_dashboard" in text
    data_lines = [
        line for line in text.splitlines()
        if line.startswith("| 510") or line.startswith("| 588")
    ]
    assert len(data_lines) == 3
    for data_line in data_lines:
        cells = [cell.strip() for cell in data_line.split("|")[1:-1]]
        # cells = [windcode, name, fund_size, shares_status, missing_reason]
        # fund_size came back valid; shares is MISSING/not_returned
        assert cells[2] != "—", (
            f"happy path row lost fund_size: {data_line!r}"
        )
        assert cells[3] == "MISSING"
        assert cells[4] == "not_returned"


def test_run_daily_fetch_partial_failure_size_only(settings) -> None:
    """analytics_data fails for one ETF → its row still lands with
    name=None + fund_size=None, but NAV from get_fund_quote is kept.
    Other ETFs unaffected. Pins the "partial visibility > hidden
    failure" preference (Linda + Keira hotfix口径)."""

    def _size_fail(windcode):
        if windcode == "510500.SH":
            raise WindError("simulated analytics_data outage", stdout="", stderr="")
        return _size_result(windcode)

    with patch(
        "funds_dashboard.scheduler.runner.WindClient.call",
        _make_dispatch(override_size=_size_fail),
    ):
        result = run_daily_fetch(settings, trade_date=_REPORT_TRADE_DATE)

    # All 3 ETFs land — none are "failed" since NAV came through
    assert result.failed_windcodes == []
    assert result.derived_rows == 3
    # 3 quote calls (all succeeded) + 2 successful size calls = 5 audit
    # rows (510500's size call failed → no audit row produced for it).
    assert result.audit_rows == 5

    with session_scope() as session:
        rows = session.scalars(
            select(EtfDailySnapshot).where(EtfDailySnapshot.windcode == "510500.SH")
        ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.market_price == 1.234  # quote landed (PR e rename)
        assert row.fund_size_yuan is None  # size failed → MISSING
        assert row.name is None


def test_run_daily_fetch_partial_failure_quote_skip(settings) -> None:
    """get_fund_quote fails for one ETF → that ETF gets skipped + counted
    as failed. Other ETFs still land. Preserves PR #7's "failed Wind
    fetch shows up as not_returned in the report" surface."""

    def _quote_fail(windcode):
        if windcode == "510500.SH":
            raise WindError("simulated quote outage", stdout="", stderr="net")
        return _quote_result(windcode)

    with patch(
        "funds_dashboard.scheduler.runner.WindClient.call",
        _make_dispatch(override_quote=_quote_fail),
    ):
        result = run_daily_fetch(settings, trade_date=_REPORT_TRADE_DATE)

    # 510500 fully failed (no quote → no row); 2 others got both tools
    assert result.audit_rows == 4  # 2×2 successful pairs
    assert result.derived_rows == 2
    assert result.failed_windcodes == ["510500.SH"]

    text = result.markdown_path.read_text(encoding="utf-8")
    assert "510500.SH" in text
    assert "not_returned" in text


def test_run_daily_fetch_refuses_without_api_key(tmp_path) -> None:
    """Neither secret_config nor env carries the key → runner returns
    a zero-row result and does NOT crash. Logs are checked elsewhere."""
    s = _make_settings(tmp_path, wind_api_key=None)
    init_sessionmaker(s.database_url)
    from sqlalchemy import create_engine

    engine = create_engine(s.database_url, future=True)
    Base.metadata.create_all(engine)

    result = run_daily_fetch(s, trade_date=_REPORT_TRADE_DATE)
    assert result.audit_rows == 0
    assert result.derived_rows == 0
    assert result.markdown_path is None


def test_run_daily_fetch_prefers_encrypted_secret_over_env(settings) -> None:
    """When `secret_config.wind_api_key` is set, the runner uses THAT,
    not the env var, even if both exist. This is the Phase 0.5
    seed-then-rotate workflow's correctness condition."""
    # Seed encrypted secret with a different value than env's
    bundle = crypto.encrypt("ak_FROM_SECRET_CONFIG_xxxxxxxxxxxxxx")
    with session_scope() as session:
        session.add(
            SecretConfig(
                name="wind_api_key",
                ciphertext=bundle.ciphertext,
                nonce=bundle.nonce,
                salt=bundle.salt,
                algorithm_version=bundle.algorithm_version,
                key_version=bundle.key_version,
                updated_by="test_seed",
            )
        )

    captured_keys: list[str | None] = []

    def _fake_init(self, *, node_path, cli_script, api_key, timeout_s=60.0):
        captured_keys.append(api_key)
        self._node_path = node_path
        self._cli_script = cli_script
        self._timeout_s = timeout_s
        self._api_key = api_key

    with patch(
        "funds_dashboard.scheduler.runner.WindClient.__init__", _fake_init
    ), patch(
        "funds_dashboard.scheduler.runner.WindClient.call",
        _make_dispatch(),
    ):
        run_daily_fetch(settings, trade_date=_REPORT_TRADE_DATE)

    assert "ak_FROM_SECRET_CONFIG_xxxxxxxxxxxxxx" in captured_keys
    # The env-supplied `ak_runner_test_KEY_12345` (from _make_settings)
    # was NOT used — encrypted store wins.
    assert "ak_runner_test_KEY_12345" not in captured_keys


def test_force_rerun_upserts_daily_report_provenance(settings) -> None:
    """Linda msg=9589ed01 ruling: `--force` reruns UPDATE the
    existing `DailyReportProvenance` row, never duplicate.
    One trade date → one current daily-report.

    Vera msg=3d6bc478 MEDIUM caught this — the first version of the
    runner blew up with `UNIQUE constraint failed` on the second
    fetch. This test pins the UPSERT contract so a future refactor
    that reverts to `session.add(DailyReportProvenance(...))` fails
    immediately.
    """

    with patch(
        "funds_dashboard.scheduler.runner.WindClient.call",
        _make_dispatch(),
    ):
        first = run_daily_fetch(settings, trade_date=_REPORT_TRADE_DATE)
        second = run_daily_fetch(
            settings, trade_date=_REPORT_TRADE_DATE, force=True
        )

    # Both runs succeed — 2 tool calls × 3 ETFs = 6 audit rows per run.
    assert first.audit_rows == 6
    assert second.audit_rows == 6
    # And produce distinct version tokens (different seq).
    assert first.data_source_version != second.data_source_version

    from funds_dashboard.db.models import DailyReportProvenance

    with session_scope() as session:
        rows = session.scalars(select(DailyReportProvenance)).all()
        assert len(rows) == 1, (
            f"force rerun must UPSERT — found {len(rows)} provenance "
            f"rows for trade_date={_REPORT_TRADE_DATE}"
        )
        only = rows[0]
        # `data_source_versions` is the audit-trail accumulator —
        # both run tokens must be present, joined by `,`.
        assert first.data_source_version in only.data_source_versions
        assert second.data_source_version in only.data_source_versions
        # And the row points at the latest markdown.
        assert str(second.markdown_path).endswith(only.markdown_path) or (
            only.markdown_path in str(second.markdown_path)
        )
