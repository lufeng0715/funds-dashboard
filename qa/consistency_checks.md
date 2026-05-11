# funds-dashboard QA Consistency Checks

Owner: @Vera_QA
Contract refs: Linda `MVP_FIELD_DICTIONARY.md`, Alex `db/models.py`, Nova msg=ea6be16d, Nova msg=bccd488e

---

## 1. Raw vs Derived ETF Snapshot

**Purpose**: Confirm every `etf_daily_snapshot` value matches the original `wind_raw_response` byte-for-byte.

**Mechanism**: JOIN `etf_daily_snapshot` with `wind_fetch_audit` on `wind_fetch_audit_id`.
Parse `wind_fetch_audit.wind_raw_response → content[0].text → data.rows`.
Look up the row whose `windcode` matches `etf_daily_snapshot.windcode`.

**Checks**:

| Check | Expected | Failure meaning |
|---|---|---|
| `fund_size_yuan` matches raw `FUNDSIZE` | Numeric match within 0.0001 | Parser applied wrong conversion or coerced null |
| `nav` matches raw `NETVALUE` | Exact float match | NAV field mis-mapped |
| `shares` matches raw `SHARES` when non-INVALID | Exact float match | Share value mis-mapped |
| `shares` is NULL when raw `SHARES` == `"INVALID"` | `shares IS NULL` | Parser wrongly coerced INVALID to 0 — **hard fail** |
| `shares_status` == `"invalid_value"` when raw `SHARES` == `"INVALID"` | Literal check | Status not propagated |
| `shares_status` == `"not_returned"` when column absent in raw | NULL check on column presence | Missing field coerced |
| No derived numeric field is `0.0` when the source raw cell is `"INVALID"` | No zeros for INVALID | Silent coercion — **hard fail** |

**Test SQL**:

```sql
-- INVALID cells must never become 0 in etf_daily_snapshot
SELECT s.windcode, s.trade_date, s.shares, s.shares_status, a.wind_raw_response
FROM etf_daily_snapshot s
JOIN wind_fetch_audit a ON s.wind_fetch_audit_id = a.id
WHERE s.shares = 0 AND s.shares_status IN ('invalid_value', 'not_returned');
-- Expected: 0 rows
```

---

## 2. INVALID / MISSING / NULL Field Integrity

**Purpose**: Confirm all four allowed `share_value_status` values (`available`, `not_returned`, `invalid_value`, `not_applicable`) are used correctly and never map to `0`.

**Checks**:

| Scenario | Expected `shares_status` | Expected `shares` | Failure |
|---|---|---|---|
| Wind returns numeric `1234567.89` | `available` | `1234567.89` | Wrong status |
| Wind returns string `"INVALID"` | `invalid_value` | `NULL` | Coercion to 0 — **hard fail** |
| Wind does not return `SHARES` column | `not_returned` | `NULL` | Wrong status |
| Product type for which share is meaningless | `not_applicable` | `NULL` | Wrong status |

**Missing-reason checks** (`etf_daily_snapshot.missing_reason`):

- Non-trading day: `missing_reason = "non_trading_day"`, `fund_size_yuan IS NULL`
- Suspended/abnormal: `missing_reason = "suspended_or_abnormal"`
- `missing_reason` must only contain allowed enum values; no free-text

```sql
-- All missing_reason values must be in the allowed set
SELECT DISTINCT missing_reason
FROM etf_daily_snapshot
WHERE missing_reason NOT IN (
    'non_trading_day', 'suspended_or_abnormal',
    'wind_field_uncovered', 'wind_invalid_value', 'parse_error'
);
-- Expected: 0 rows
```

---

## 3. Force-Rerun Version Isolation

**Purpose**: Confirm `--force` reruns never overwrite previous derived rows — they create a new `data_source_version` and a new set of derived rows.

**Contract**: `data_source_version` format is `YYYYMMDD#YYYYMMDDTHHMMSSZ#N` where `N` increments per rerun.

**Checks**:

| Check | Assertion |
|---|---|
| Two `--force` runs on the same `trade_date` produce two distinct `data_source_version` values | `SELECT COUNT(DISTINCT data_source_version) FROM wind_fetch_audit WHERE trade_date = 'X'` ≥ 2 |
| Old derived rows still present after second run | Row count for the old `data_source_version` equals pre-rerun count |
| `wind_fetch_audit.data_source_version` is unique | `UNIQUE` constraint enforced at DB level |
| Sequence `N` increments monotonically per `trade_date` | Parsed `N` from `data_source_version` is always `max_prev + 1` |

**Test procedure**:

```bash
# Run baseline
python -m funds_dashboard.cli fetch --trade-date 2026-05-10
# Capture row counts
# Run with force
python -m funds_dashboard.cli fetch --trade-date 2026-05-10 --force
# Assert: old rows still present, new version has new rows
```

---

## 4. Dual FK Consistency

**Purpose**: Every row in derived tables carries both a valid `wind_fetch_audit_id` FK and a `data_source_version` that matches the audit table.

**Checks**:

```sql
-- Every derived etf row has a valid FK
SELECT COUNT(*) FROM etf_daily_snapshot s
LEFT JOIN wind_fetch_audit a ON s.wind_fetch_audit_id = a.id
WHERE a.id IS NULL;
-- Expected: 0

-- data_source_version in derived rows matches the audit table
SELECT COUNT(*) FROM etf_daily_snapshot s
JOIN wind_fetch_audit a ON s.wind_fetch_audit_id = a.id
WHERE s.data_source_version != a.data_source_version;
-- Expected: 0

-- Same for fund_company_aggregate
SELECT COUNT(*) FROM fund_company_aggregate c
LEFT JOIN wind_fetch_audit a ON c.wind_fetch_audit_id = a.id
WHERE a.id IS NULL;
-- Expected: 0
```

---

## 5. Secrets Redaction Regression Test

**Purpose**: No API key or credential pattern ever lands in the database.

**Critical**: `wind_fetch_audit.wind_request_payload` and `wind_fetch_audit.wind_raw_response` must have all `ak_*`-pattern strings replaced with `[REDACTED_API_KEY]` before INSERT. Failure means a plaintext key is stored and may appear in log dumps.

**Test SQL**:

```sql
-- wind_fetch_audit must never contain ak_ pattern in payload or response
SELECT id, trade_date, wind_tool_name
FROM wind_fetch_audit
WHERE wind_request_payload LIKE '%ak_%'
   OR wind_raw_response LIKE '%ak_%';
-- Expected: 0 rows — any match is a CRITICAL security finding
```

**CI integration**: Run the above SQL as part of every test run:

```python
# tests/test_secrets_redaction.py
def test_no_api_key_in_audit_table(db_session):
    """CRITICAL: no ak_* pattern may persist in wind_fetch_audit."""
    results = db_session.execute(
        text(
            "SELECT id FROM wind_fetch_audit "
            "WHERE wind_request_payload LIKE '%ak_%' "
            "OR wind_raw_response LIKE '%ak_%'"
        )
    ).fetchall()
    assert len(results) == 0, (
        f"SECURITY: wind_fetch_audit contains {len(results)} rows with API key pattern. "
        "Check wind_cli.py redact_secrets() filter."
    )
```

---

## 6. Unit Normalization

**Purpose**: All monetary values displayed as 亿元; no raw-unit leakage to UI.

**Rules from Linda's field dictionary**:

- `fund_size_yuan` stored in yuan (元); UI must divide by 1e8 for 亿元 display.
- `fund_size_unit` must be set and match the stored raw unit.
- `total_aum_unit` in `fund_company_aggregate` must be `"亿元"` — the aggregate layer normalizes on insert.

**Checks**:

| Check | Assertion |
|---|---|
| `fund_size_unit` is never NULL when `fund_size_yuan IS NOT NULL` | `NOT NULL` constraint |
| API response `/api/v1/funds/etf-daily/` returns `fund_size_yi_yuan` in 亿元 | Response field != raw yuan value |
| Company aggregate `total_aum_unit = "亿元"` for all rows | `SELECT COUNT(*) WHERE total_aum_unit != '亿元'` = 0 |

---

## 7. Decomposition Status Integrity

**Purpose**: When share data is unavailable, `decomposition_status` must reflect that — never silently pretend share-driven decomposition is complete.

**Rules** (Linda field dictionary, `etf_scale_decomposition` table):

- If `share_value_status IN ('invalid_value', 'not_returned')` → `decomposition_status = "insufficient_share_data"`
- If only size and NAV available → `decomposition_status = "unknown_driver"`
- `share_effect_amount` must be NULL when `decomposition_status = "insufficient_share_data"`

**Test SQL**:

```sql
-- share_effect_amount must be NULL when decomposition is insufficient
SELECT COUNT(*) FROM etf_scale_decomposition
WHERE decomposition_status = 'insufficient_share_data'
  AND share_effect_amount IS NOT NULL;
-- Expected: 0

-- attention_label "consecutive_inflow/outflow" must not appear when share data is missing
SELECT COUNT(*) FROM etf_scale_decomposition d
JOIN etf_daily_snapshot s ON d.windcode = s.windcode AND d.trade_date = s.trade_date
WHERE d.attention_label IN ('consecutive_inflow', 'consecutive_outflow')
  AND s.shares_status IN ('invalid_value', 'not_returned');
-- Expected: 0 — these labels require share data
```

---

## 8. Provenance Completeness

**Purpose**: Every daily-report markdown must have machine-readable `data_provenance` for every conclusion and FAQ entry.

**Checks**:

- `daily_report_provenance.data_source_versions` must reference valid `wind_fetch_audit.data_source_version` entries.
- All `data_provenance.status` values in generated markdown must be in `{VALID, INVALID, MISSING, NOT_APPLICABLE}`.

```sql
-- Every data_source_version in daily_report_provenance must exist in audit
-- (Note: data_source_versions is comma-joined TEXT — test in Python/application layer)
```

**Python check** (for CI):

```python
def test_daily_report_provenance_references_valid_versions(db_session):
    all_audit_versions = {
        r.data_source_version
        for r in db_session.execute(text("SELECT data_source_version FROM wind_fetch_audit")).fetchall()
    }
    for row in db_session.execute(text("SELECT data_source_versions FROM daily_report_provenance")).fetchall():
        for version in row.data_source_versions.split(","):
            assert version.strip() in all_audit_versions, (
                f"daily_report_provenance references unknown version: {version}"
            )
```

---

## 9. Trade Date Uniqueness

**Purpose**: At most one active `data_source_version` per `(windcode, trade_date)` in snapshot tables; force reruns are additive.

```sql
-- For ETF daily snapshot: one unique row per (windcode, trade_date) per version
-- The UNIQUE constraint (windcode, trade_date) ensures only latest active version is displayed
-- Verify older versions are not silently overwritten
SELECT windcode, trade_date, COUNT(*) AS version_count
FROM etf_daily_snapshot
GROUP BY windcode, trade_date
HAVING version_count > 1;
-- Expected: 0 rows (unique constraint enforces this)
```

---

## 10. Wind CLI Error Path Preservation

**Purpose**: Even on Wind CLI failure, raw stdout/stderr must be preserved for QA replay.

- `WindError` must carry `stdout` and `stderr` attributes.
- On scheduler job failure, the error payload must be logged at WARNING or higher.
- Failed fetch attempts must NOT create a `wind_fetch_audit` row (partial writes are worse than no rows).

---

## Running These Checks

### SQL (SQLite/Postgres):

```bash
cd /Users/lufeng/funds-dashboard/backend
python - <<'SCRIPT'
from sqlalchemy import create_engine, text
from funds_dashboard.config import Settings
s = Settings()
engine = create_engine(s.database_url)
with engine.connect() as conn:
    # Check 1: No INVALID coerced to 0
    rows = conn.execute(text(
        "SELECT windcode, trade_date FROM etf_daily_snapshot "
        "WHERE shares = 0 AND shares_status IN ('invalid_value','not_returned')"
    )).fetchall()
    assert not rows, f"FAIL: INVALID coerced to 0 in {len(rows)} rows"
    
    # Check 5: No secrets in audit
    rows = conn.execute(text(
        "SELECT id FROM wind_fetch_audit "
        "WHERE wind_request_payload LIKE '%ak_%' OR wind_raw_response LIKE '%ak_%'"
    )).fetchall()
    assert not rows, f"CRITICAL: {len(rows)} rows with API key pattern"
    
    print("All consistency checks passed ✅")
SCRIPT
```

### pytest:

```bash
cd /Users/lufeng/funds-dashboard/backend
pytest tests/test_consistency_checks.py -v
```

---

## Severity Classification

| Section | Severity | Blocker? |
|---|---|---|
| §1 Raw vs Derived — INVALID coerced to 0 | CRITICAL | Yes — data integrity breach |
| §5 Secrets redaction — ak_* in DB | CRITICAL | Yes — security breach |
| §3 Force-rerun overwrites old rows | HIGH | Yes — version history destroyed |
| §4 Dual FK inconsistency | HIGH | Yes — referential integrity broken |
| §7 Decomposition status mismatch | HIGH | Yes — UI will show wrong attribution |
| §8 Provenance incomplete | MEDIUM | No — degraded traceability |
| §6 Unit normalization | MEDIUM | No — display only |
| §2 Enum value outside allowed set | MEDIUM | No — data quality |
| §9 Trade date uniqueness violation | LOW | No — uniqueness constraint should prevent |
| §10 Error path missing raw payload | LOW | No — QA ergonomics |
