# funds-dashboard

Daily fund / ETF data dashboard. **Single trusted source**: Wind Financial
Terminal (via the team's `Wind` skill / CLI). Nothing scraped from the web.

## Status

Phase 0 — scaffolding. The data model and schema is still firming up
with @Linda-基金专家 (domain lead).

## Layout

```
funds-dashboard/
├── backend/                 # FastAPI service
│   ├── funds_dashboard/
│   │   ├── api/v1/          # HTTP routers
│   │   ├── db/              # SQLAlchemy models + session
│   │   ├── scheduler/       # APScheduler daily fetch jobs
│   │   └── wind/            # Wind CLI Python wrapper
│   ├── migrations/          # Alembic
│   └── tests/
├── frontend/                # Vite + React + TanStack Query
├── docs/
│   └── contracts/           # cross-product contracts (RAG / QA / UI)
└── qa/
    └── consistency_checks.md  # @Vera_QA owned
```

## MVP scope (per Linda msg=3f0f22a5 / Max-approved)

First two pages, **ETF + 汇总 only**:

1. **重点指数 ETF 规模变化** — daily snapshot of a fixed pool (沪深300 /
   中证500 / 中证1000 / 创业板 / 科创50 / A500 / 红利 / 证券 / 半导体 /
   医药 / 新能源 …). Records 份额 / 规模 / 净值 / 涨跌幅 / 折溢价率,
   computes 1d / 5d / 20d 规模变化 with a price-vs-flow decomposition.

2. **每日基金规模汇总** — aggregates by 基金公司 × 类型 (ETF / non-ETF, 主动 / 被动,
   货币 / 债券 / 权益 / 混合 / QDII / FOF). Surfaces Top 增减 + 异常突变.

新申报 / 发行中 are data probes for now, surfaced after Wind field
stability is verified.

## Operations

```bash
# Daily fetch (cron-driven via APScheduler)
FUNDS_DASHBOARD_CRON_DAILY="0 17 * * *" \
WIND_API_KEY="..." \
python -m funds_dashboard.serve

# Manual rerun for a specific trade date
python -m funds_dashboard.fetch --trade-date 2026-05-10

# Force a new version even when one exists
python -m funds_dashboard.fetch --trade-date 2026-05-10 --force
```

`--force` increments the `seq` portion of `data_source_version`
(format: `<trade_date>#<fetch_utc>#<seq>`) — historical rows are not
overwritten. See `docs/contracts/data-source-versioning.md`.

## Cross-product contracts

- **RAG ingestion** (Nova msg=3288dc8b) — every daily report is **double-
  emitted**: structured DB rows for the dashboard, and a markdown wiki
  page (`categories/funds/daily-reports/YYYY-MM-DD.md`) with
  `report_type: daily_dashboard`, `report_date`, `data_provenance` so
  the llm-wiki retriever can answer "上周三沪深 300 ETF 规模" via
  date-filtered exact retrieval.
- **QA consistency** (Vera msg=58003866) — raw Wind JSON is persisted
  in `wind_fetch_audit`; derived tables carry both `wind_fetch_audit_id`
  (FK constraint) and `data_source_version` (human-readable token) so
  QA can JOIN raw vs derived for any row.
- **UI surface** (Keira msg=ed1e4918 / c6e84a09) — every numeric cell
  shows units inline, every derived metric has a `provenance` tooltip
  (Wind field + fetch time + calculation), abnormal flags are text +
  number, never color-only.

## Out of scope (today)

- Wind credential bootstrap — `WIND_API_KEY` must come from @feng-lu /
  ops; backend refuses to start without it.
- The actual ETF pool list — needs @Linda's domain pick.
- LLM-generated commentary — phase 2 follow-up.
