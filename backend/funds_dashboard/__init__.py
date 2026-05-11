"""funds-dashboard backend.

A FastAPI service that fetches daily fund / ETF data from the Wind
Financial Terminal (via the team `Wind` skill CLI) and surfaces
structured reports for the dashboard frontend plus markdown wiki
pages for the llm-wiki RAG pipeline.

See the repo root README.md for the MVP scope and the cross-product
contracts. Module entry points:

* `funds_dashboard.wind` — Python wrapper around `node scripts/cli.mjs
  call <tool> <payload>`. Persists raw stdout to `wind_fetch_audit`.
* `funds_dashboard.db.models` — SQLAlchemy models. Every derived table
  has both a `wind_fetch_audit_id` FK (constraint) and a string
  `data_source_version` (human-readable token).
* `funds_dashboard.api.v1` — HTTP routers under `/api/v1/...`.
* `funds_dashboard.scheduler` — APScheduler jobs (default daily 17:00
  local). Cron is env-overrideable via `FUNDS_DASHBOARD_CRON_DAILY`.
"""

__version__ = "0.0.1"
