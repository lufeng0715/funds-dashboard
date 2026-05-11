# funds-dashboard MVP Field Dictionary

Owner: @Linda-基金专家
Date: 2026-05-11
Data source: Wind skill only

## MVP Scope

The approved MVP ships these two modules first:

1. Key index ETF scale change dashboard.
2. Daily fund scale summary.

New fund applications and funds in issuance are data probes in the MVP. They should have reserved schemas and later UI tabs, but they must not block the first Web release.

## Wind Access Contract

Use the Wind skill CLI:

```bash
node /Users/lufeng/.agents/skills/wind-mcp-skill/scripts/cli.mjs call <server_type> <tool_name> '<params_json>'
```

Observed stdout shape:

```json
{
  "ok": true,
  "server_type": "fund_data",
  "tool": "get_fund_price_indicators",
  "content": [
    {
      "type": "text",
      "text": "{\"data\":{\"columns\":[],\"rows\":[]},\"error\":null}"
    }
  ],
  "isError": false
}
```

Implementation requirement:

- Store the full outer JSON in `wind_fetch_audit.wind_raw_response`.
- Store the exact CLI request payload in `wind_fetch_audit.wind_request_payload`.
- Scrub secrets before writing `wind_fetch_audit.wind_request_payload` or `wind_fetch_audit.wind_raw_response`. Any token matching `ak_[A-Za-z0-9_-]+` must be replaced with `[REDACTED_API_KEY]`.
- Parse `content[0].text` as JSON before deriving rows.
- Treat `ok=false`, `isError=true`, missing `content[0].text`, invalid inner JSON, or non-null inner `error` as fetch failures.
- Do not convert missing, invalid, or unavailable Wind values to `0`.
- The frontend must never read or receive `WIND_API_KEY`.

## Audit Table

Table: `wind_fetch_audit`

| Field | Type | Required | Meaning |
|---|---:|---:|---|
| `id` | BIGINT | yes | Database primary key. |
| `trade_date` | DATE | yes | Trading date represented by the fetch. |
| `wind_tool_name` | TEXT | yes | Example: `fund_data:get_fund_price_indicators`. |
| `wind_request_payload` | JSONB/TEXT | yes | Exact request payload. |
| `wind_raw_response` | JSONB/TEXT | yes | Full CLI stdout JSON. |
| `wind_fetch_timestamp` | TIMESTAMP UTC | yes | Actual fetch time. |
| `data_source_version` | TEXT | yes | Stable readable token: `<trade_date>#<fetch_utc>#<seq>`, e.g. `20260510#20260511T090000Z#2`. |
| `derived_record_count` | INT | yes | Number of derived rows from this fetch. |

Rules:

- Successful `--force` rerun creates a new version, never overwrites prior derived rows.
- Derived tables must include both `wind_fetch_audit_id` and `data_source_version`.
- `wind_fetch_audit_id` is the database FK; `data_source_version` is for display, logs, markdown provenance, and cross-table tracing.

## Key ETF Pool V0

The first pool should be explicit and small enough to verify manually.

| Bucket | Index/Theme | Suggested ETF examples |
|---|---|---|
| Broad-based | 沪深300 | `510300.SH`, `510310.SH`, `159919.SZ` |
| Broad-based | 中证500 | `510500.SH`, `159922.SZ`, `510580.SH` |
| Broad-based | 中证1000 | `512100.SH`, `159629.SZ`, `159633.SZ` |
| Broad-based | 创业板 | `159915.SZ`, `159949.SZ` |
| Broad-based | 科创50 | `588000.SH`, `588080.SH`, `588200.SH` |
| Broad-based | A500 | Add after Wind code verification. |
| Style | 红利 | `510880.SH`, `515180.SH`, `159545.SZ` |
| Sector | 证券 | `512880.SH`, `512000.SH` |
| Sector | 半导体/芯片 | `512480.SH`, `588200.SH`, `159995.SZ` |
| Sector | 医药 | `512010.SH`, `159929.SZ` |
| Sector | 新能源 | `516160.SH`, `515030.SH` |

Alex should make the pool a config table or YAML file, not hard-coded Python constants. Each item needs `windcode`, `display_name`, `bucket`, `index_or_theme`, `is_active`, `start_date`, and `notes`.

## Table: `etf_daily_snapshot`

One row per ETF per `trade_date` per `data_source_version`.

| Field | Type | Required | Wind source / derivation | Display label |
|---|---:|---:|---|---|
| `id` | BIGINT | yes | DB PK | - |
| `wind_fetch_audit_id` | BIGINT | yes | FK | - |
| `data_source_version` | TEXT | yes | From audit table | 数据版本 |
| `trade_date` | DATE | yes | Job input / Wind date | 交易日 |
| `windcode` | TEXT | yes | Wind `windcode` | 代码 |
| `fund_name` | TEXT | yes | `NAME` / `证券简称` | 基金简称 |
| `latest_price` | DECIMAL | no | `MATCH` / 最新成交价 | 最新成交价 |
| `nav` | DECIMAL | no | `NETVALUE` / 最新净值 | 最新净值 |
| `accumulated_nav` | DECIMAL | no | `ACCUMULATEDNETVALUE` | 累计净值 |
| `daily_return_pct` | DECIMAL | no | `CHANGERANGE` | 涨跌幅 |
| `iopv` | DECIMAL | no | `IOPV` | IOPV |
| `premium_discount_pct` | DECIMAL | no | `FORWARDDISCOUNT` / 贴水率 | 折溢价率 |
| `fund_size_raw` | DECIMAL | no | `FUNDSIZE` | 基金规模 |
| `fund_size_unit` | TEXT | yes | Set from Wind field metadata or parser | 规模单位 |
| `fund_share_raw` | DECIMAL | no | `SHARES` / 基金最新份额 | 基金份额 |
| `fund_share_unit` | TEXT | no | Set from parser | 份额单位 |
| `share_value_status` | TEXT | yes | Parser | 份额字段状态 |
| `missing_reason` | TEXT | no | Parser | 缺失原因 |
| `source_tool_name` | TEXT | yes | `fund_data:get_fund_price_indicators` | Wind 工具 |
| `source_fields` | JSONB/TEXT | yes | Field list requested | Wind 字段 |
| `created_at` | TIMESTAMP | yes | DB default | 入库时间 |

Allowed `share_value_status`:

- `available`: Wind returned a numeric share value.
- `not_returned`: field is absent from Wind response.
- `invalid_value`: Wind returned `INVALID` or another non-numeric marker.
- `not_applicable`: not applicable for this product.

Allowed `missing_reason`:

- `non_trading_day`
- `suspended_or_abnormal`
- `wind_field_uncovered`
- `wind_invalid_value`
- `parse_error`

## Table: `etf_scale_decomposition`

One row per ETF per `trade_date` per lookback window.

| Field | Type | Required | Meaning |
|---|---:|---:|---|
| `id` | BIGINT | yes | DB PK |
| `wind_fetch_audit_id` | BIGINT | yes | FK |
| `data_source_version` | TEXT | yes | Version token |
| `trade_date` | DATE | yes | Current date |
| `windcode` | TEXT | yes | ETF code |
| `lookback_days` | INT | yes | `1`, `5`, or `20` |
| `current_fund_size` | DECIMAL | no | Current Wind fund size |
| `previous_fund_size` | DECIMAL | no | Previous comparable size |
| `scale_change_amount` | DECIMAL | no | Current minus previous |
| `scale_change_pct` | DECIMAL | no | Change divided by previous |
| `current_nav` | DECIMAL | no | Current NAV |
| `previous_nav` | DECIMAL | no | Previous NAV |
| `nav_effect_amount` | DECIMAL | no | Estimated scale change caused by NAV movement |
| `share_effect_amount` | DECIMAL | no | Estimated scale change caused by share movement |
| `decomposition_status` | TEXT | yes | Whether decomposition is reliable |
| `attention_label` | TEXT | no | UI label |
| `attention_reason` | TEXT | no | Numeric reason for label |

Rules:

- If current or previous share is missing/invalid, set `decomposition_status = "insufficient_share_data"` and do not fabricate `share_effect_amount`.
- If only fund size and NAV are available, show total scale change and mark driver as `unknown_driver`.
- ETF scale growth must not be described as "net inflow" unless share data supports it.

Allowed `attention_label`:

- `scale_surge`
- `scale_drop`
- `consecutive_inflow`
- `consecutive_outflow`

UI labels must include text and numeric basis, e.g. `规模突增 · 1日 +12.4亿 / +8.1%`.

Initial attention thresholds:

- `scale_surge`: 1-day scale change is greater than `+10%` and greater than `+5亿元`.
- `scale_drop`: 1-day scale change is lower than `-10%` and lower than `-5亿元`.
- `consecutive_inflow`: 5 consecutive trading days with positive share-effect amount. If share data is unavailable, do not emit this label.
- `consecutive_outflow`: 5 consecutive trading days with negative share-effect amount. If share data is unavailable, do not emit this label.

These thresholds are v0 defaults and must be stored in backend configuration, not frontend code.

## Table: `fund_company_aggregate`

One row per fund company per `trade_date` per `data_source_version`.

| Field | Type | Required | Wind source / derivation |
|---|---:|---:|---|
| `id` | BIGINT | yes | DB PK |
| `wind_fetch_audit_id` | BIGINT | yes | FK |
| `data_source_version` | TEXT | yes | Version token |
| `trade_date` | DATE | yes | Job input / Wind date |
| `fund_company` | TEXT | yes | `基金管理人` |
| `total_aum` | DECIMAL | no | Wind company summary, unit normalized |
| `total_aum_unit` | TEXT | yes | Use `亿元` for display by default |
| `fund_count` | INT | no | Wind company summary |
| `etf_aum` | DECIMAL | no | Wind company ETF scale |
| `etf_count` | INT | no | Derived from ETF rows if available |
| `money_market_aum` | DECIMAL | no | Later extension |
| `bond_aum` | DECIMAL | no | Later extension |
| `equity_aum` | DECIMAL | no | Later extension |
| `hybrid_aum` | DECIMAL | no | Later extension |
| `qdii_aum` | DECIMAL | no | Later extension |
| `fof_aum` | DECIMAL | no | Later extension |
| `source_tool_name` | TEXT | yes | `fund_data:get_fund_company_info` |
| `source_fields` | JSONB/TEXT | yes | Requested question/fields |

MVP can populate `total_aum`, `fund_count`, and `etf_aum` first. Category breakdowns are nullable until Wind field probes are stable.

## Table: `daily_report_provenance`

One row per conclusion card, markdown FAQ answer, or anomaly label.

| Field | Type | Required | Meaning |
|---|---:|---:|---|
| `id` | BIGINT | yes | DB PK |
| `trade_date` | DATE | yes | Report date |
| `report_type` | TEXT | yes | `daily_dashboard` |
| `section_key` | TEXT | yes | `overview`, `etf_scale`, `fund_company_summary`, `new_fund_applications_probe`, `funds_in_issuance_probe`, `faq` |
| `entity_type` | TEXT | no | `etf`, `fund_company`, `market`, `fund_product` |
| `entity_id` | TEXT | no | Wind code or company name |
| `conclusion_text` | TEXT | yes | Human-readable conclusion |
| `wind_fetch_audit_id` | BIGINT | yes | FK |
| `data_source_version` | TEXT | yes | Version token |
| `wind_tool_name` | TEXT | yes | Tool name |
| `wind_fields` | JSONB/TEXT | yes | Wind fields used |
| `calculation_method` | TEXT | yes | Short formula/method |
| `fetch_utc` | TIMESTAMP | yes | Fetch timestamp |

## Markdown Daily Report Contract

Path:

```text
categories/funds/daily-reports/YYYY-MM-DD.md
```

Frontmatter:

```yaml
---
title: 基金每日数据汇报 YYYY-MM-DD
category: funds
report_date: YYYY-MM-DD
report_type: daily_dashboard
data_source: wind
data_source_version: YYYYMMDD#YYYYMMDDTHHMMSSZ#N
keywords: [基金, ETF, 规模变化]
---
```

Chunk guidance for Nova:

- `# 一句话结论`: independent chunk.
- `# ETF 规模变化`: independent section chunk.
- `# 每日基金规模汇总`: independent section chunk.
- `# 新申报基金`: independent section chunk when probe data is enabled.
- `# 发行中基金`: independent section chunk when probe data is enabled.
- `# FAQ`: each Q&A is an independent chunk.

Each conclusion or FAQ answer must include provenance in machine-readable form near the answer:

```yaml
data_provenance:
  - wind_tool_name: fund_data:get_fund_price_indicators
    wind_fields: [FUNDSIZE, SHARES, NETVALUE]
    fetch_utc: YYYY-MM-DDTHH:MM:SSZ
    data_source_version: YYYYMMDD#YYYYMMDDTHHMMSSZ#N
    status: VALID
    calculation_method: scale_change = current_fund_size - previous_fund_size
```

Allowed `data_provenance.status` values:

- `VALID`: Wind returned a usable value and the report used it.
- `INVALID`: Wind returned an explicit invalid marker such as `INVALID`.
- `MISSING`: Wind did not return the requested field.
- `NOT_APPLICABLE`: Field is not applicable to the instrument or section.

Invalid and missing values must be explicit in the markdown text. Do not silently omit them.

Correct:

```markdown
ETF 588200.SH 今日规模为 X 亿元，份额变化无法计算（Wind 字段 SHARES 返回 INVALID）。
```

Incorrect:

```markdown
ETF 588200.SH 今日规模为 X 亿元。
```

When a key field is `INVALID` or `MISSING`, the `# FAQ` section should include one Q&A explaining why the metric is unavailable:

```markdown
问题：为什么 ETF 588200.SH 今日的份额变化数据不可用？
答案：Wind 字段 `SHARES` 在 trade_date=YYYY-MM-DD 返回 INVALID。可能原因包括数据延迟、标的状态异常或字段口径调整；本报告不据此推断资金流入/流出，建议次日重跑或对照 Wind 终端确认。
data_provenance:
  - wind_tool_name: fund_data:get_fund_price_indicators
    wind_fields: [SHARES]
    fetch_utc: YYYY-MM-DDTHH:MM:SSZ
    data_source_version: YYYYMMDD#YYYYMMDDTHHMMSSZ#N
    status: INVALID
    calculation_method: no calculation; source field unavailable
```

## API Endpoints

Minimum endpoints for Alex:

- `GET /api/v1/funds/etf-daily/?trade_date=YYYY-MM-DD`
- `GET /api/v1/funds/aggregate/?trade_date=YYYY-MM-DD`
- `GET /api/v1/funds/daily-report/{date}.md`
- `POST /api/v1/funds/fetch/run` with `{ "trade_date": "YYYY-MM-DD", "force": true|false }`

## UI Acceptance Constraints

The first Web version has three pages:

1. Overview.
2. ETF Scale.
3. Summary.

Overview answers only three questions:

- Who changed most today?
- Was the change driven by shares or NAV?
- Does it require manual attention?

Every conclusion card must show a lightweight "口径/来源" entry with Wind tool, fields, fetch time, and calculation method.

Do not use color as the only signal for anomalies. Always show label text plus numeric basis.

Top-bar data status:

- `正常`: all core fields used by the page are `VALID`.
- `部分缺失`: at least one core field is `INVALID` or `MISSING`, but the page's primary conclusion can still be generated.
- `等待披露`: missing or invalid key fields prevent the page's primary conclusion from being generated.

Core fields for MVP:

- Overview: `fund_size_raw`, `nav`, `daily_return_pct`, and at least one scale-change window.
- ETF Scale: `fund_size_raw`, `nav`, `fund_share_raw` when driver decomposition is displayed.
- Summary: `total_aum`, `fund_count`, `etf_aum` where available.

## Secrets Handling

- Use `WIND_API_KEY=<redacted>` in docs and sample commands.
- Store the real key only in local environment, deployment secrets, or a gitignored `.env`.
- Do not commit `.env` files.
- Do not write API keys into `wind_fetch_audit`, app logs, markdown reports, frontend payloads, screenshots, or test fixtures.
- Add a regression test that fails if persisted audit payloads or responses contain a substring matching `ak_`.
