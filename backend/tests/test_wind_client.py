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


# --- argv shape (Wind CLI signature compliance) --------------------------


def test_tool_name_with_colon_splits_into_server_type_and_tool() -> None:
    """Real-run bug 2026-05-11 (Alex msg=293ecab0): the Wind CLI signature
    is `cli.mjs call <server_type> <tool_name> '<json>'` — server_type
    and tool_name are SEPARATE positional argv entries.

    The runner uses the project-internal `<server_type>:<tool_name>`
    convention (audit + fixtures, e.g. `fund_data:get_fund_price_indicators`).
    The wrapper MUST split that colon into two argv positions before
    spawning, otherwise the CLI sees one bogus arg and returns
    `exit 1` for every call.
    """
    client = WindClient(node_path="node", cli_script="cli.mjs")
    with patch("funds_dashboard.wind.subprocess.run") as run_mock:
        run_mock.return_value = _fake_completed(_well_formed_envelope())
        client.call("fund_data:get_fund_quote", {"windcode": "510300.SH"})

    args, kwargs = run_mock.call_args
    argv = args[0] if args else kwargs["args"]
    # Expected shape:
    #   [<node>, <cli.mjs>, "call", "fund_data", "get_fund_quote", <json>]
    # The colon-form `fund_data:get_fund_quote` MUST NOT appear as a
    # single arg.
    assert "fund_data:get_fund_quote" not in argv, (
        f"colon-form tool_name leaked as single argv element: {argv}"
    )
    assert "call" in argv
    call_idx = argv.index("call")
    assert argv[call_idx + 1] == "fund_data", (
        f"server_type position wrong; argv after `call` = {argv[call_idx + 1:]}"
    )
    assert argv[call_idx + 2] == "get_fund_quote", (
        f"tool_name position wrong; argv after server_type = {argv[call_idx + 2:]}"
    )


# --- columns shape normalization (Vera msg=c0ac5ba4 HIGH) ----------------


def test_columns_list_of_dicts_normalized_to_list_of_strings() -> None:
    """Real Wind regression (Vera caught on PR #16 first live fetch):
    `fund_data:get_fund_quote` and `analytics_data:get_financial_data`
    return `columns` as `list[dict]` (e.g. `[{"name":"MATCH",
    "type":"float"}, ...]`), not `list[str]` like
    `get_fund_price_indicators` did.

    Downstream parsers build a `{col: i for i, col in enumerate(cols)}`
    map which raises `TypeError: unhashable type: 'dict'` if cols is
    list[dict]. The WindClient normalises both shapes to `list[str]`
    so every parser sees the same surface regardless of which Wind
    tool produced the response.
    """
    inner_dict_cols = json.dumps({
        "data": {
            "columns": [
                {"name": "MATCH", "type": "float"},
                {"name": "AVGPRICE", "type": "float"},
                {"name": "TIME", "type": "string"},
            ],
            "rows": [[1.234, 1.0, "14:59:00"]],
        },
        "error": None,
    })
    envelope = json.dumps({"content": [{"type": "text", "text": inner_dict_cols}]})
    client = WindClient(node_path="node", cli_script="cli.mjs")
    with patch("funds_dashboard.wind.subprocess.run") as run_mock:
        run_mock.return_value = _fake_completed(envelope)
        result = client.call("fund_data:get_fund_quote", {"windcode": "510300.SH"})

    # Every column entry must be a plain string after normalization.
    for col in result.columns:
        assert isinstance(col, str), f"column not normalized to str: {col!r}"
    assert result.columns == ["MATCH", "AVGPRICE", "TIME"]


def test_columns_list_of_strings_passes_through_unchanged() -> None:
    """The list-of-strings shape (used by `get_fund_price_indicators`)
    continues to work unchanged after the normalization layer was added."""
    inner_str_cols = json.dumps({
        "data": {
            "columns": ["NAME", "MATCH", "SHARES"],
            "rows": [["华泰柏瑞", 1.0, 1000.0]],
        },
        "error": None,
    })
    envelope = json.dumps({"content": [{"type": "text", "text": inner_str_cols}]})
    client = WindClient(node_path="node", cli_script="cli.mjs")
    with patch("funds_dashboard.wind.subprocess.run") as run_mock:
        run_mock.return_value = _fake_completed(envelope)
        result = client.call(
            "fund_data:get_fund_price_indicators", {"codes": ["510300.SH"]}
        )

    assert result.columns == ["NAME", "MATCH", "SHARES"]


def test_tool_name_without_colon_routes_to_empty_server_type() -> None:
    """Backwards-compat: a bare tool name (no `<server_type>:` prefix)
    is forwarded with an empty server_type so the CLI returns a clear
    error rather than silently routing to the wrong backend."""
    client = WindClient(node_path="node", cli_script="cli.mjs")
    with patch("funds_dashboard.wind.subprocess.run") as run_mock:
        run_mock.return_value = _fake_completed(_well_formed_envelope())
        client.call("get_fund_quote", {"windcode": "510300.SH"})

    args, kwargs = run_mock.call_args
    argv = args[0] if args else kwargs["args"]
    call_idx = argv.index("call")
    assert argv[call_idx + 1] == "", (
        f"missing-colon must yield empty server_type; got {argv[call_idx + 1]!r}"
    )
    assert argv[call_idx + 2] == "get_fund_quote"
