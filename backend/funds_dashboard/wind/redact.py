"""Secret redaction for Wind audit writes.

Nova msg=bccd488e flagged that the Wind API key has already been
exposed in chat history and should be treated as such. The audit
table (`wind_fetch_audit.wind_request_payload`,
`wind_fetch_audit.wind_raw_response`) is a high-value persistence
target — anything written there lasts as long as the database, so we
strip any `ak_*` token before insert.

The regex is intentionally narrow: it matches the published vendor
prefix shape (`ak_` followed by alphanumerics / `_` / `-` totalling
≥20 chars) so legitimate fields that happen to start with `ak_`
won't get redacted (we don't expect any in Wind responses but the
narrow shape keeps the function safe to call on arbitrary text).

This module has zero side effects — callers feed it strings and get
strings back. Vera's `qa/consistency_checks.md` will own a regression
test that greps `wind_fetch_audit` for any `^ak_[A-Za-z0-9_-]{20,}`
match and fails CI if present.
"""

from __future__ import annotations

import re


# Vendor key prefix per feng-lu msg=86df4780. The character class covers
# what the published sample uses; the >= 20-char tail rules out
# accidental matches like `ak_id_42`. The actual published key was
# 34 chars total, so the threshold is comfortably conservative.
_SECRET_RE = re.compile(r"ak_[A-Za-z0-9_-]{20,}")
_REDACTED_TOKEN = "[REDACTED_API_KEY]"


def redact_secrets(text: str) -> str:
    """Return *text* with Wind API keys replaced by `[REDACTED_API_KEY]`.

    Safe to call on JSON strings, log lines, request payloads, or any
    other free-form text. Non-string input raises `TypeError` — callers
    should serialize before redacting.
    """
    if not isinstance(text, str):
        raise TypeError(f"redact_secrets expects str, got {type(text).__name__}")
    return _SECRET_RE.sub(_REDACTED_TOKEN, text)
