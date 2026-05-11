"""Wind Financial Terminal client (subprocess wrapper).

The team Wind skill exposes a `node scripts/cli.mjs call <tool>
<payload>` interface. Calls return a triple-nested JSON envelope::

    stdout (outer JSON):
      {"content": [{"type": "text", "text": "<inner JSON string>"}], ...}

    inner JSON (parsed from content[0].text):
      {"data": {"columns": [...], "rows": [[...], ...]}, "error": null}

This module is the only place that knows about the envelope. Callers
get a clean `WindResult(columns, rows, raw)` dataclass back, plus the
raw stdout for audit persistence (`wind_fetch_audit.wind_raw_response`).

Linda msg=91b45123 confirmed:

* Field values can be the literal string `"INVALID"`. Downstream MUST
  preserve that — never coerce to 0 — and Nova msg=ea6be16d's RAG
  contract requires explicit `status: INVALID` propagation, not silent
  stripping.
* Sample valid tool calls:
    fund_data:get_fund_price_indicators (ETF snapshot)
    fund_data:get_fund_company_info     (fund company aggregates)

The wrapper does NOT do retry-on-timeout itself; that policy lives in
the scheduler layer (so it can be configured and audited at the
job level rather than buried in the call site).
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any


LOG = logging.getLogger(__name__)


class WindError(Exception):
    """Wind CLI surface failure.

    Carries the raw stdout/stderr so QA can reproduce — see Vera's
    consistency_checks.md requirement that raw responses be preserved
    even on the failure path.
    """

    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        return_code: int = 0,
    ):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code


@dataclass(frozen=True)
class WindResult:
    """Parsed Wind tool response.

    `raw_stdout` is preserved verbatim so it can be persisted to
    `wind_fetch_audit.wind_raw_response`. `columns` / `rows` are the
    inner `data.columns` / `data.rows` arrays — `INVALID` cells are
    kept as-is (string literal), see module docstring.
    """

    tool_name: str
    request_payload: dict[str, Any]
    columns: list[str]
    rows: list[list[Any]]
    raw_stdout: str


class WindClient:
    """Subprocess wrapper around the Wind skill CLI.

    Construct with a `Settings` instance so the CLI path / node binary
    is configurable per-environment. Each `call()` invocation is a
    fresh subprocess — no daemon, no connection pool. That keeps the
    audit story simple (one row in `wind_fetch_audit` per `call()`)
    and matches how the skill is meant to be consumed.
    """

    def __init__(
        self,
        *,
        node_path: str,
        cli_script: str,
        timeout_s: float = 60.0,
    ):
        self._node_path = node_path
        self._cli_script = cli_script
        self._timeout_s = timeout_s

    def call(self, tool_name: str, payload: dict[str, Any]) -> WindResult:
        """Invoke `<node> <cli_script> call <tool> <payload-json>`.

        Returns parsed `WindResult` on success. Raises `WindError` on
        non-zero exit, malformed JSON, or an `error` field inside the
        inner JSON body.
        """
        payload_json = json.dumps(payload, ensure_ascii=False)
        argv = [self._node_path, self._cli_script, "call", tool_name, payload_json]
        LOG.info("wind call: %s payload=%s", tool_name, payload_json)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WindError(
                f"wind call timed out after {self._timeout_s}s",
                stderr=str(exc),
            ) from exc
        except FileNotFoundError as exc:
            raise WindError(
                f"wind CLI not found: {self._node_path} {self._cli_script}. "
                "Check FUNDS_DASHBOARD_WIND_CLI_NODE_PATH / "
                "FUNDS_DASHBOARD_WIND_CLI_SCRIPT settings."
            ) from exc

        raw_stdout = proc.stdout
        if proc.returncode != 0:
            raise WindError(
                f"wind CLI exit {proc.returncode}",
                stdout=raw_stdout,
                stderr=proc.stderr,
                return_code=proc.returncode,
            )

        # Outer envelope: {"content": [{"type": "text", "text": "<inner>"}], ...}
        try:
            outer = json.loads(raw_stdout)
        except json.JSONDecodeError as exc:
            raise WindError(
                f"wind CLI returned non-JSON stdout: {exc}",
                stdout=raw_stdout,
                stderr=proc.stderr,
            ) from exc

        inner_text = _extract_inner_text(outer)
        try:
            inner = json.loads(inner_text)
        except json.JSONDecodeError as exc:
            raise WindError(
                f"wind CLI inner-JSON malformed: {exc}",
                stdout=raw_stdout,
                stderr=proc.stderr,
            ) from exc

        if inner.get("error"):
            raise WindError(
                f"wind CLI returned error: {inner['error']!r}",
                stdout=raw_stdout,
                stderr=proc.stderr,
            )

        data = inner.get("data") or {}
        columns = list(data.get("columns") or [])
        rows = list(data.get("rows") or [])

        return WindResult(
            tool_name=tool_name,
            request_payload=payload,
            columns=columns,
            rows=rows,
            raw_stdout=raw_stdout,
        )


def _extract_inner_text(outer: dict[str, Any]) -> str:
    """Pull `content[0].text` from the outer envelope.

    Defensive: missing / empty / wrong-type content surfaces a clear
    `WindError` rather than an opaque IndexError.
    """
    content = outer.get("content")
    if not isinstance(content, list) or not content:
        raise WindError(
            f"wind CLI outer envelope missing `content` array: {outer!r}"
        )
    first = content[0]
    if not isinstance(first, dict):
        raise WindError(
            f"wind CLI envelope content[0] not an object: {first!r}"
        )
    text = first.get("text")
    if not isinstance(text, str):
        raise WindError(
            f"wind CLI envelope content[0].text not a string: {first!r}"
        )
    return text
