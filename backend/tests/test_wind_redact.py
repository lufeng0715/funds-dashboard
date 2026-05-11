"""Unit tests for the Wind secret redactor.

Vera msg=bccd488e flagged that any `ak_*` token must be stripped
before it can land in `wind_fetch_audit.wind_request_payload` or
`wind_raw_response`. These tests are the first line of defence — the
DB-level grep that Vera owns in `qa/consistency_checks.md` is the
second.
"""

from __future__ import annotations

import pytest

from funds_dashboard.wind.redact import redact_secrets, _REDACTED_TOKEN


def test_redact_strips_the_published_key_pattern() -> None:
    """The exact published key shape is masked."""
    text = "WIND_API_KEY=ak_BovgxMdWJ4BRORb7FEk5oJQLVtsUy_LP and more"
    out = redact_secrets(text)
    assert "ak_BovgxMdWJ4BRORb7FEk5oJQLVtsUy_LP" not in out
    assert _REDACTED_TOKEN in out


def test_redact_inside_json_payload() -> None:
    """Redaction works on JSON-shaped text — the audit raw_response case."""
    payload = '{"params": {"key": "ak_BovgxMdWJ4BRORb7FEk5oJQLVtsUy_LP"}}'
    out = redact_secrets(payload)
    assert "ak_Bovgx" not in out
    assert _REDACTED_TOKEN in out


def test_redact_preserves_short_ak_prefixes() -> None:
    """`ak_id_42` and similar short tokens are not real keys; leave alone."""
    text = "internal field ak_id_42 should survive"
    assert redact_secrets(text) == text


def test_redact_no_match_passes_through_unchanged() -> None:
    """Idempotent on text with no secrets."""
    text = "nothing to redact here"
    assert redact_secrets(text) == text


def test_redact_handles_multiple_occurrences() -> None:
    """Two keys in one string both get masked independently."""
    text = (
        "first ak_BovgxMdWJ4BRORb7FEk5oJQLVtsUy_LP "
        "second ak_AnotherKeyWithEnoughCharsToBeReal_xyz"
    )
    out = redact_secrets(text)
    assert "ak_Bovgx" not in out
    assert "ak_Another" not in out
    assert out.count(_REDACTED_TOKEN) == 2


def test_redact_rejects_non_string_input() -> None:
    """Caller must serialize before redacting — a hard error makes that explicit."""
    with pytest.raises(TypeError):
        redact_secrets({"not": "a string"})  # type: ignore[arg-type]
