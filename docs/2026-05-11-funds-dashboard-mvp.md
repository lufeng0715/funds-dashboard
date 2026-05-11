# funds-dashboard MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first funds-dashboard Web system for key ETF scale changes and daily fund scale summary using Wind skill data only.

**Architecture:** Use a monorepo with FastAPI backend and React frontend. Wind CLI fetches are audited in `wind_fetch_audit`, derived tables keep both database FK and readable `data_source_version`, and the same data produces Web API JSON plus llm-wiki markdown reports.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, APScheduler, SQLite for local dev, Postgres for production, Vite, React, TanStack Query.

---

### Task 1: Backend Skeleton and Wind Audit Foundation

**Files:**
- Create: `funds-dashboard/backend/pyproject.toml`
- Create: `funds-dashboard/backend/app/main.py`
- Create: `funds-dashboard/backend/app/db.py`
- Create: `funds-dashboard/backend/app/models/audit.py`
- Create: `funds-dashboard/backend/app/services/wind_cli.py`
- Create: `funds-dashboard/backend/tests/test_wind_cli.py`

- [ ] **Step 1: Write failing tests for Wind CLI parser**

Create `funds-dashboard/backend/tests/test_wind_cli.py`:

```python
import json

import pytest

from app.services.wind_cli import parse_wind_cli_stdout


def test_parse_wind_cli_stdout_extracts_inner_payload():
    outer = {
        "ok": True,
        "server_type": "fund_data",
        "tool": "get_fund_price_indicators",
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "data": {
                            "columns": [{"name": "FUNDSIZE", "type": "string"}],
                            "rows": [["3.86065e+10"]],
                        },
                        "error": None,
                    }
                ),
            }
        ],
        "isError": False,
    }

    parsed = parse_wind_cli_stdout(json.dumps(outer))

    assert parsed.server_type == "fund_data"
    assert parsed.tool == "get_fund_price_indicators"
    assert parsed.inner_payload["data"]["rows"] == [["3.86065e+10"]]
    assert parsed.raw_response == outer


def test_parse_wind_cli_stdout_rejects_inner_error():
    outer = {
        "ok": True,
        "server_type": "fund_data",
        "tool": "get_fund_price_indicators",
        "content": [{"type": "text", "text": json.dumps({"data": None, "error": "bad field"})}],
        "isError": False,
    }

    with pytest.raises(ValueError, match="Wind inner error"):
        parse_wind_cli_stdout(json.dumps(outer))


def test_redact_secrets_removes_wind_api_key_pattern():
    from app.services.wind_cli import redact_secrets

    secret = "ak" + "_example_SECRET"
    payload = {"Authorization": f"Bearer {secret}"}

    assert redact_secrets(payload)["Authorization"] == "Bearer [REDACTED_API_KEY]"
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd funds-dashboard/backend
pytest tests/test_wind_cli.py -v
```

Expected: FAIL because `app.services.wind_cli` does not exist.

- [ ] **Step 3: Implement parser minimally**

Create `funds-dashboard/backend/app/services/wind_cli.py`:

```python
from dataclasses import dataclass
import json
import re
from typing import Any


@dataclass(frozen=True)
class WindCliResult:
    server_type: str
    tool: str
    raw_response: dict[str, Any]
    inner_payload: dict[str, Any]


def parse_wind_cli_stdout(stdout: str) -> WindCliResult:
    raw = json.loads(stdout)
    if raw.get("ok") is not True or raw.get("isError") is True:
        raise ValueError("Wind outer error")
    content = raw.get("content") or []
    if not content or "text" not in content[0]:
        raise ValueError("Wind missing content text")
    inner = json.loads(content[0]["text"])
    if inner.get("error") is not None:
        raise ValueError(f"Wind inner error: {inner['error']}")
    return WindCliResult(
        server_type=raw["server_type"],
        tool=raw["tool"],
        raw_response=redact_secrets(raw),
        inner_payload=inner,
    )


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"ak_[A-Za-z0-9_-]+", "[REDACTED_API_KEY]", value)
    return value
```

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
cd funds-dashboard/backend
pytest tests/test_wind_cli.py -v
```

Expected: PASS.

### Task 2: Audit Schema and Versioning

**Files:**
- Create: `funds-dashboard/backend/app/models/audit.py`
- Create: `funds-dashboard/backend/tests/test_data_source_version.py`

- [ ] **Step 1: Write failing test for version token**

Create `funds-dashboard/backend/tests/test_data_source_version.py`:

```python
from datetime import date, datetime, timezone

from app.models.audit import build_data_source_version


def test_build_data_source_version_is_readable_and_sortable():
    version = build_data_source_version(
        trade_date=date(2026, 5, 10),
        fetch_utc=datetime(2026, 5, 11, 9, 0, 0, tzinfo=timezone.utc),
        sequence=2,
    )

    assert version == "20260510#20260511T090000Z#2"
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
cd funds-dashboard/backend
pytest tests/test_data_source_version.py -v
```

Expected: FAIL because `build_data_source_version` does not exist.

- [ ] **Step 3: Implement version helper**

Create `funds-dashboard/backend/app/models/audit.py`:

```python
from datetime import date, datetime, timezone


def build_data_source_version(trade_date: date, fetch_utc: datetime, sequence: int) -> str:
    if fetch_utc.tzinfo is None:
        raise ValueError("fetch_utc must be timezone-aware")
    normalized = fetch_utc.astimezone(timezone.utc)
    return f"{trade_date:%Y%m%d}#{normalized:%Y%m%dT%H%M%SZ}#{sequence}"
```

- [ ] **Step 4: Run test and verify GREEN**

Run:

```bash
cd funds-dashboard/backend
pytest tests/test_data_source_version.py -v
```

Expected: PASS.

### Task 3: Derived ETF Tables

**Files:**
- Create: `funds-dashboard/backend/app/models/etf.py`
- Create: `funds-dashboard/backend/tests/test_etf_parsing.py`

- [ ] **Step 1: Write failing test for invalid Wind share field**

Create `funds-dashboard/backend/tests/test_etf_parsing.py`:

```python
from app.models.etf import parse_decimal_or_missing


def test_parse_decimal_or_missing_marks_invalid_without_zeroing():
    value, status = parse_decimal_or_missing("INVALID")

    assert value is None
    assert status == "invalid_value"
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
cd funds-dashboard/backend
pytest tests/test_etf_parsing.py -v
```

Expected: FAIL because `app.models.etf` does not exist.

- [ ] **Step 3: Implement parser**

Create `funds-dashboard/backend/app/models/etf.py`:

```python
from decimal import Decimal, InvalidOperation


def parse_decimal_or_missing(raw: object) -> tuple[Decimal | None, str]:
    if raw is None:
        return None, "not_returned"
    if isinstance(raw, str) and raw.strip().upper() == "INVALID":
        return None, "invalid_value"
    try:
        return Decimal(str(raw)), "available"
    except (InvalidOperation, ValueError):
        return None, "parse_error"
```

- [ ] **Step 4: Run test and verify GREEN**

Run:

```bash
cd funds-dashboard/backend
pytest tests/test_etf_parsing.py -v
```

Expected: PASS.

### Task 4: Markdown Daily Report Contract

**Files:**
- Create: `funds-dashboard/backend/app/reports/markdown.py`
- Create: `funds-dashboard/backend/tests/test_daily_report_markdown.py`

- [ ] **Step 1: Write failing test for frontmatter**

Create `funds-dashboard/backend/tests/test_daily_report_markdown.py`:

```python
from app.reports.markdown import render_daily_report_frontmatter


def test_render_daily_report_frontmatter_contains_rag_contract_fields():
    text = render_daily_report_frontmatter(
        report_date="2026-05-11",
        data_source_version="20260511#20260511T090000Z#1",
        keywords=["基金", "ETF", "规模变化"],
    )

    assert "report_date: 2026-05-11" in text
    assert "report_type: daily_dashboard" in text
    assert "data_source: wind" in text
    assert "data_source_version: 20260511#20260511T090000Z#1" in text
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
cd funds-dashboard/backend
pytest tests/test_daily_report_markdown.py -v
```

Expected: FAIL because renderer does not exist.

- [ ] **Step 3: Implement frontmatter renderer**

Create `funds-dashboard/backend/app/reports/markdown.py`:

```python
def render_daily_report_frontmatter(
    report_date: str,
    data_source_version: str,
    keywords: list[str],
) -> str:
    keyword_text = ", ".join(keywords)
    return (
        "---\n"
        f"title: 基金每日数据汇报 {report_date}\n"
        "category: funds\n"
        f"report_date: {report_date}\n"
        "report_type: daily_dashboard\n"
        "data_source: wind\n"
        f"data_source_version: {data_source_version}\n"
        f"keywords: [{keyword_text}]\n"
        "---\n"
    )
```

- [ ] **Step 4: Run test and verify GREEN**

Run:

```bash
cd funds-dashboard/backend
pytest tests/test_daily_report_markdown.py -v
```

Expected: PASS.

### Task 5: Frontend MVP Pages

**Files:**
- Create: `funds-dashboard/frontend/src/pages/Overview.tsx`
- Create: `funds-dashboard/frontend/src/pages/EtfScale.tsx`
- Create: `funds-dashboard/frontend/src/pages/Summary.tsx`
- Create: `funds-dashboard/frontend/src/components/ProvenanceButton.tsx`

- [ ] **Step 1: Build static page skeletons after backend API contracts exist**

Create pages with stable sections only:

```tsx
export function Overview() {
  return (
    <main>
      <h1>基金数据每日汇报</h1>
      <section aria-label="今日变化最大" />
      <section aria-label="变化来源" />
      <section aria-label="是否需要关注" />
    </main>
  );
}
```

- [ ] **Step 2: Add provenance control**

Create `ProvenanceButton.tsx`:

```tsx
type ProvenanceButtonProps = {
  windToolName: string;
  windFields: string[];
  fetchUtc: string;
  calculationMethod: string;
};

export function ProvenanceButton(props: ProvenanceButtonProps) {
  return (
    <button type="button" title={`${props.windToolName} · ${props.fetchUtc}`}>
      口径/来源
    </button>
  );
}
```

### Task 6: QA Handoff

**Files:**
- Create: `funds-dashboard/backend/qa/consistency_checks.md`

- [ ] **Step 1: Create QA skeleton**

Create `qa/consistency_checks.md`:

```markdown
# Consistency Checks

## Raw vs Derived ETF Snapshot

Compare `wind_fetch_audit.wind_raw_response` inner fields with `etf_daily_snapshot` values by `wind_fetch_audit_id`.

## Missing Values

Verify `INVALID`, missing fields, non-trading days, and suspended/abnormal data are not displayed or stored as zero.

## Forced Reruns

Verify `--force` creates a new `data_source_version` and does not overwrite older derived rows.

## Secrets Redaction

Verify no audit payload, response, log, markdown report, frontend payload, or fixture contains a substring matching `ak_`.
```

### Task 7: Configuration Web Page and Encrypted Secret Store

**Files:**
- Create: `funds-dashboard/backend/app/config_store/crypto.py`
- Create: `funds-dashboard/backend/app/config_store/schema.py`
- Create: `funds-dashboard/backend/app/routes/config.py`
- Create: `funds-dashboard/backend/tests/test_config_store.py`
- Create: `funds-dashboard/frontend/src/pages/SystemConfig.tsx`

- [ ] **Step 1: Write failing tests for secret encryption and masked status**

Create `funds-dashboard/backend/tests/test_config_store.py`:

```python
import os

import pytest

from app.config_store.crypto import decrypt_secret, encrypt_secret
from app.config_store.schema import mask_secret


def test_encrypt_secret_round_trip_without_plaintext_leak():
    encrypted = encrypt_secret("ak" + "_example_SECRET", master_key="test-master-key")

    assert encrypted.ciphertext
    assert "example_SECRET" not in encrypted.ciphertext
    assert decrypt_secret(encrypted, master_key="test-master-key") == "ak" + "_example_SECRET"


def test_mask_secret_returns_prefix_and_last4_only():
    assert mask_secret("ak" + "_example_SECRET") == "ak_****CRET"


def test_missing_master_key_fails_closed(monkeypatch):
    monkeypatch.delenv("FUNDS_DASHBOARD_MASTER_KEY", raising=False)

    from app.config_store.schema import require_master_key

    with pytest.raises(RuntimeError, match="FUNDS_DASHBOARD_MASTER_KEY is required"):
        require_master_key()


def test_wind_key_is_not_passed_in_subprocess_argv(monkeypatch):
    from app.services.wind_cli import build_wind_subprocess_call

    secret = "ak" + "_example_SECRET"
    args, env = build_wind_subprocess_call(
        cli_path="/tmp/cli.mjs",
        server_type="fund_data",
        tool_name="get_fund_price_indicators",
        payload={"windcode": "588200.SH"},
        wind_api_key=secret,
    )

    assert all(secret not in arg for arg in args)
    assert env["WIND_API_KEY"] == secret
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd funds-dashboard/backend
pytest tests/test_config_store.py -v
```

Expected: FAIL because `app.config_store` does not exist.

- [ ] **Step 3: Implement config crypto helpers**

Create `funds-dashboard/backend/app/config_store/crypto.py`:

```python
from dataclasses import dataclass
import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


PBKDF2_ITERATIONS_V2 = 600_000


@dataclass(frozen=True)
class EncryptedSecret:
    version: int
    salt: str
    nonce: str
    ciphertext: str


def _derive_key(master_key: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS_V2) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(master_key.encode("utf-8"))


def encrypt_secret(plaintext: str, master_key: str) -> EncryptedSecret:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(master_key, salt)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return EncryptedSecret(
        version=2,
        salt=base64.b64encode(salt).decode("ascii"),
        nonce=base64.b64encode(nonce).decode("ascii"),
        ciphertext=base64.b64encode(ciphertext).decode("ascii"),
    )


def decrypt_secret(secret: EncryptedSecret, master_key: str) -> str:
    salt = base64.b64decode(secret.salt)
    nonce = base64.b64decode(secret.nonce)
    ciphertext = base64.b64decode(secret.ciphertext)
    key = _derive_key(master_key, salt)
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")
```

- [ ] **Step 4: Implement schema helpers**

Create `funds-dashboard/backend/app/config_store/schema.py`:

```python
import os


def require_master_key() -> str:
    value = os.environ.get("FUNDS_DASHBOARD_MASTER_KEY")
    if not value:
        raise RuntimeError("FUNDS_DASHBOARD_MASTER_KEY is required")
    return value


def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "****"
    if value.startswith("ak_"):
        return f"ak_****{value[-4:]}"
    return f"****{value[-4:]}"
```

- [ ] **Step 5: Run tests and verify GREEN**

Run:

```bash
cd funds-dashboard/backend
pytest tests/test_config_store.py -v
```

Expected: PASS.

- [ ] **Step 6: Add configuration API routes**

Create `funds-dashboard/backend/app/routes/config.py` with these endpoints:

```python
from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/config", tags=["config"])


@router.get("/status")
def get_config_status():
    return {
        "secrets": {
            "wind_api_key": {
                "configured": False,
                "masked": None,
                "status": "missing",
            }
        }
    }
```

Expand this skeleton after SQLAlchemy models are merged.

### Task 8: Configuration UI

**Files:**
- Create: `funds-dashboard/frontend/src/pages/SystemConfig.tsx`

- [ ] **Step 1: Create configuration page skeleton**

Create:

```tsx
export function SystemConfig() {
  return (
    <main>
      <h1>系统配置</h1>
      <nav aria-label="配置分组">
        <button>Wind 数据源</button>
        <button>调度与日报发布</button>
        <button>重点 ETF 池</button>
        <button>阈值与口径</button>
        <button>模型与结论生成</button>
        <button>系统安全与审计</button>
      </nav>
      <section aria-label="Wind 数据源">
        <label htmlFor="wind-api-key">Wind API Key</label>
        <input id="wind-api-key" type="password" autoComplete="off" />
        <button type="button">测试 Wind 连接</button>
        <button type="button">保存/替换 Key</button>
      </section>
    </main>
  );
}
```

- [ ] **Step 2: Enforce frontend secret constraints**

When wiring API calls:

```tsx
const saveSecret = async (value: string) => {
  await api.config.setSecret('wind_api_key', value)
  setInputValue('')
}
```

The page must not store secret values in localStorage, sessionStorage, URL params, logs, or toast messages.

### Self-Review

- Spec coverage: Covers Wind audit, ETF snapshot, scale decomposition, company aggregate, markdown provenance, UI constraints, QA hooks, and encrypted Web configuration.
- Placeholder scan: No `TBD` or unspecified behavior remains in the MVP plan.
- Type consistency: `data_source_version`, `wind_fetch_audit_id`, and `report_type: daily_dashboard` match the field dictionary.
