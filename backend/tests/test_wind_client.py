"""Tests for the Wind subprocess wrapper.

Critical-class assertion (Nova msg=138a79cc, Vera msg=58003866):
the API key must travel via `env=` to the subprocess, never via argv.
A `ps aux` leak would let any same-host process exfiltrate the key.
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch

import pytest

from funds_dashboard.wind import WindClient, WindError


_AK_PATTERN = re.compile(r"^ak_[A-Za-z0-9_-]{10,}$")


def _fake_completed(stdout: str, returncode: int = 0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = ""
    proc.returncode = returncode
    return proc


def _well_formed_envelope() -> str:
    inner = json.dumps({"data": {"columns": ["a"], "rows": [[1]]}, "error": None})
    return json.dumps({"content": [{"type": "text", "text": inner}]})


def test_api_key_travels_via_env_not_argv() -> None:
    """CRITICAL — the secret must never appear in `args`."""
    client = WindClient(
        node_path="node",
        cli_script="cli.mjs",
        api_key="ak_BovgxMdWJ4BRORb7FEk5oJQLVtsUy_LP",
    )
    with patch("funds_dashboard.wind.subprocess.run") as run_mock:
        run_mock.return_value = _fake_completed(_well_formed_envelope())
        client.call("fund_data:get_fund_price_indicators", {"code": "588200.SH"})

    args, kwargs = run_mock.call_args
    argv = args[0] if args else kwargs["args"]
    # `argv` must contain no element that looks like an API key.
    for token in argv:
        assert not _AK_PATTERN.match(str(token)), (
            f"API key leaked into argv: {token!r}. "
            "Use env= to pass secrets to subprocesses."
        )
    # And the env passed in *must* carry it.
    env = kwargs["env"]
    assert env["WIND_API_KEY"] == "ak_BovgxMdWJ4BRORb7FEk5oJQLVtsUy_LP"


def test_envelope_parsing_happy_path() -> None:
    client = WindClient(node_path="node", cli_script="cli.mjs")
    with patch("funds_dashboard.wind.subprocess.run") as run_mock:
        run_mock.return_value = _fake_completed(_well_formed_envelope())
        result = client.call("fund_data:get_fund_price_indicators", {"code": "588200.SH"})
    assert result.columns == ["a"]
    assert result.rows == [[1]]
    assert "data" in result.raw_stdout  # raw preserved for audit


def test_non_zero_exit_raises_with_audit_context() -> None:
    client = WindClient(node_path="node", cli_script="cli.mjs")
    with patch("funds_dashboard.wind.subprocess.run") as run_mock:
        run_mock.return_value = _fake_completed("partial", returncode=2)
        run_mock.return_value.stderr = "wind: connection refused"
        with pytest.raises(WindError) as ei:
            client.call("fund_data:foo", {})
    assert ei.value.return_code == 2
    assert "connection refused" in ei.value.stderr


def test_inner_error_field_surfaces_as_wind_error() -> None:
    inner = json.dumps({"data": {}, "error": "tool unavailable"})
    envelope = json.dumps({"content": [{"type": "text", "text": inner}]})
    client = WindClient(node_path="node", cli_script="cli.mjs")
    with patch("funds_dashboard.wind.subprocess.run") as run_mock:
        run_mock.return_value = _fake_completed(envelope)
        with pytest.raises(WindError) as ei:
            client.call("fund_data:foo", {})
    assert "tool unavailable" in str(ei.value)


def test_missing_content_array_raises_clear_error() -> None:
    bad = json.dumps({"some": "other"})  # no content[] at all
    client = WindClient(node_path="node", cli_script="cli.mjs")
    with patch("funds_dashboard.wind.subprocess.run") as run_mock:
        run_mock.return_value = _fake_completed(bad)
        with pytest.raises(WindError, match="missing `content`"):
            client.call("fund_data:foo", {})


def test_no_api_key_passes_env_without_wind_api_key() -> None:
    """When no key configured, env should NOT inject a placeholder."""
    client = WindClient(node_path="node", cli_script="cli.mjs", api_key=None)
    with patch("funds_dashboard.wind.subprocess.run") as run_mock:
        run_mock.return_value = _fake_completed(_well_formed_envelope())
        client.call("fund_data:foo", {})
    env = run_mock.call_args.kwargs["env"]
    assert "WIND_API_KEY" not in env
