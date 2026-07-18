from __future__ import annotations

import json
import os
import stat
import textwrap
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.research.claude_code_provider import (
    MINIMAL_PATH,
    ClaudeCodeProviderConfig,
    ClaudeCodeResearchProvider,
)
from trading_research.research.errors import (
    MalformedOutputError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    SchemaValidationError,
)
from trading_research.research.models import EvidenceSnapshot, ResearchModelRequest
from trading_research.research.usage import PricingEntry


SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "additionalProperties": False,
    "required": ["value"],
    "properties": {"value": {"type": "string", "maxLength": 20}},
}


def _request(*, feedback: tuple[str, ...] = ()) -> ResearchModelRequest:
    now = datetime.now(timezone.utc)
    snapshot = EvidenceSnapshot(
        snapshot_id="snapshot-1", symbol="AAPL", as_of=now, created_at=now,
        source_records=(), evidence_items=(), deterministic_factors={}, sentiment_metrics={},
        portfolio_context=None, missing_data_reasons=(), conflict_reasons=(), point_in_time_safe=True,
        config_hash="config", git_sha="git",
    )
    return ResearchModelRequest(
        role="fundamental", research_run_id="run-1", snapshot=snapshot,
        system_prompt="SYSTEM_DYNAMIC_MARKER", user_prompt="USER_DYNAMIC_MARKER",
        json_schema=SCHEMA, model_name="sonnet", max_output_tokens=100, temperature=0,
        prompt_name="test", prompt_version="v1", prompt_hash="prompt-hash",
        system_prompt_hash="system-hash", schema_version="test.v1", attempt_number=1,
        validation_feedback=feedback,
    )


def _fake_binary(tmp_path: Path, body: str) -> Path:
    binary = tmp_path / "fake-claude"
    binary.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def _normal_fake(tmp_path: Path, *, structured=None, usage=True, model_usage=None, extra="") -> tuple[Path, Path]:
    capture = tmp_path / "capture.json"
    structured = {"value": "ok"} if structured is None else structured
    model_usage = {"claude-sonnet-resolved": {"inputTokens": 11, "outputTokens": 7}} if model_usage is None else model_usage
    outer = {
        "type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
        "session_id": "safe-session-id", "structured_output": structured,
        "modelUsage": model_usage,
    }
    if usage:
        outer["usage"] = {
            "input_tokens": 11, "output_tokens": 7,
            "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2,
        }
    body = f"""
import json, os, pathlib, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.205")
elif args == ["auth", "status"]:
    print(json.dumps({{"loggedIn": True, "authMethod": "oauth"}}))
else:
    prompt = sys.stdin.read()
    pathlib.Path({str(capture)!r}).write_text(json.dumps({{
        "argv": args,
        "prompt": prompt,
        "environment_keys": sorted(os.environ),
        "oauth_present": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
        "path": os.environ.get("PATH"),
    }}))
    print(json.dumps({outer!r}))
{extra}
"""
    return _fake_binary(tmp_path, body), capture


def _config(tmp_path: Path, binary: Path, **overrides) -> ClaudeCodeProviderConfig:
    workdir = tmp_path / "runtime"
    workdir.mkdir(mode=0o700, exist_ok=True)
    values = {
        "binary_path": binary,
        "minimum_version": "2.1.205",
        "request_timeout_seconds": 2,
        "terminate_grace_seconds": 1,
        "maximum_stdout_bytes": 16_384,
        "maximum_stderr_bytes": 4_096,
        "maximum_schema_bytes": 8_192,
        "maximum_prompt_bytes": 8_192,
        "maximum_budget_usd_per_call": Decimal("0.50"),
        "maximum_turns": 1,
        "working_directory": workdir,
        "pricing_entries": (
            PricingEntry(
                provider="claude_code", model="sonnet", effective_date="2020-01-01", currency="USD",
                input_price_per_million=Decimal("3"), output_price_per_million=Decimal("15"),
                pricing_version="test-api-equivalent",
            ),
        ),
    }
    values.update(overrides)
    return ClaudeCodeProviderConfig(**values)


@pytest.fixture(autouse=True)
def oauth_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "unit-test-oauth-value")


def test_hardened_command_stdin_environment_output_and_usage(tmp_path, monkeypatch):
    binary, capture_path = _normal_fake(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "must-not-leak")
    monkeypatch.setenv("ALPACA_API_SECRET", "must-not-leak")
    parent_before = dict(os.environ)

    response = ClaudeCodeResearchProvider(_config(tmp_path, binary)).generate_structured(
        _request(feedback=("VALIDATION_DYNAMIC_MARKER",))
    )

    assert response.provider == "claude_code"
    assert response.model_name == "claude-sonnet-resolved"
    assert response.parsed_json == {"value": "ok"}
    assert response.raw_text == '{"value":"ok"}'
    assert response.usage.input_tokens == 11
    assert response.usage.output_tokens == 7
    assert response.usage.cache_read_tokens == 3
    assert response.usage.cache_write_tokens == 2
    assert response.usage.cost_estimate_basis == "SUBSCRIPTION_API_EQUIVALENT_ESTIMATE"
    assert response.usage.configured_model_alias == "sonnet"
    assert response.usage.resolved_model_name == "claude-sonnet-resolved"
    assert response.usage.claude_code_version == "2.1.205"

    capture = json.loads(capture_path.read_text())
    argv = capture["argv"]
    assert argv[0] == "-p"
    for flag in (
        "--safe-mode", "--tools", "--disallowedTools", "--strict-mcp-config",
        "--disable-slash-commands", "--permission-mode", "--no-session-persistence",
        "--no-chrome", "--max-turns", "--model", "--output-format", "--json-schema",
        "--max-budget-usd",
    ):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--disallowedTools") + 1] == "mcp__*"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--max-budget-usd") + 1] == "0.50"
    forbidden = {"--bare", "--continue", "--resume", "--session-id", "--allowedTools", "--add-dir", "--mcp-config", "--plugin-dir", "--chrome", "--dangerously-skip-permissions"}
    assert forbidden.isdisjoint(argv)
    assert "SYSTEM_DYNAMIC_MARKER" not in argv
    assert "USER_DYNAMIC_MARKER" not in argv
    assert "VALIDATION_DYNAMIC_MARKER" not in argv
    assert all("unit-test-oauth-value" not in arg for arg in argv)
    assert "SYSTEM_DYNAMIC_MARKER" in capture["prompt"]
    assert "USER_DYNAMIC_MARKER" in capture["prompt"]
    assert "VALIDATION_DYNAMIC_MARKER" in capture["prompt"]
    assert capture["oauth_present"] is True
    assert capture["path"] == MINIMAL_PATH
    assert "ANTHROPIC_API_KEY" not in capture["environment_keys"]
    assert "ANTHROPIC_AUTH_TOKEN" not in capture["environment_keys"]
    assert "ALPACA_API_SECRET" not in capture["environment_keys"]
    assert dict(os.environ) == parent_before


@pytest.mark.parametrize(
    ("version", "code"),
    [("2.1.204", "CLAUDE_CODE_VERSION_UNSUPPORTED"), ("not-a-version", "CLAUDE_CODE_VERSION_UNPARSABLE")],
)
def test_version_preflight_fails_closed(tmp_path, version, code):
    binary = _fake_binary(tmp_path, f"""
import json, sys
if sys.argv[1:] == ["--version"]:
    print({version!r})
else:
    print(json.dumps({{"loggedIn": True, "authMethod": "oauth"}}))
""")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        ClaudeCodeResearchProvider(_config(tmp_path, binary)).preflight()
    assert exc_info.value.code == code


@pytest.mark.parametrize(
    ("auth", "code"),
    [
        ({"loggedIn": False, "authMethod": "oauth"}, "CLAUDE_CODE_NOT_AUTHENTICATED"),
        ({"loggedIn": True, "authMethod": "api_key"}, "CLAUDE_CODE_UNEXPECTED_AUTH_METHOD"),
    ],
)
def test_auth_preflight_fails_closed_without_inference(tmp_path, auth, code):
    marker = tmp_path / "inference-called"
    binary = _fake_binary(tmp_path, f"""
import json, pathlib, sys
if sys.argv[1:] == ["--version"]:
    print("2.1.205")
elif sys.argv[1:] == ["auth", "status"]:
    print(json.dumps({auth!r}))
else:
    pathlib.Path({str(marker)!r}).write_text("called")
""")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        ClaudeCodeResearchProvider(_config(tmp_path, binary)).preflight()
    assert exc_info.value.code == code
    assert not marker.exists()


def test_missing_oauth_fails_without_leaking_prompt(tmp_path, monkeypatch):
    binary, _ = _normal_fake(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        ClaudeCodeResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CLAUDE_CODE_OAUTH_TOKEN_MISSING"
    assert "SYSTEM_DYNAMIC_MARKER" not in str(exc_info.value)


def test_prompt_size_limit_is_enforced_before_any_subprocess(tmp_path):
    marker = tmp_path / "called"
    binary = _fake_binary(tmp_path, f"""
import pathlib
pathlib.Path({str(marker)!r}).write_text("called")
""")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        ClaudeCodeResearchProvider(_config(tmp_path, binary, maximum_prompt_bytes=10)).generate_structured(_request())
    assert exc_info.value.code == "CLAUDE_CODE_PROMPT_TOO_LARGE"
    assert "SYSTEM_DYNAMIC_MARKER" not in str(exc_info.value)
    assert not marker.exists()


def test_local_schema_validation_is_mandatory(tmp_path):
    binary, _ = _normal_fake(tmp_path, structured={"value": "x" * 50})
    with pytest.raises(SchemaValidationError):
        ClaudeCodeResearchProvider(_config(tmp_path, binary)).generate_structured(_request())


@pytest.mark.parametrize(
    ("usage", "model_usage", "code"),
    [
        (False, None, "CLAUDE_CODE_USAGE_METADATA_MISSING"),
        (True, {"one": {}, "two": {}}, "CLAUDE_CODE_USAGE_METADATA_MISSING"),
    ],
)
def test_missing_or_ambiguous_usage_fails_closed(tmp_path, usage, model_usage, code):
    binary, _ = _normal_fake(tmp_path, usage=usage, model_usage=model_usage)
    with pytest.raises(MalformedOutputError) as exc_info:
        ClaudeCodeResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == code


def test_stdout_overflow_is_bounded(tmp_path):
    binary = _fake_binary(tmp_path, """
import json, sys
if sys.argv[1:] == ["--version"]:
    print("2.1.205")
elif sys.argv[1:] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "oauth"}))
else:
    print("x" * 20000)
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        ClaudeCodeResearchProvider(_config(tmp_path, binary, maximum_stdout_bytes=8192)).generate_structured(_request())
    assert exc_info.value.code == "CLAUDE_CODE_OUTPUT_OVERFLOW"


def test_stderr_overflow_is_bounded_and_not_exposed(tmp_path):
    binary = _fake_binary(tmp_path, """
import json, sys
if sys.argv[1:] == ["--version"]:
    print("2.1.205")
elif sys.argv[1:] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "oauth"}))
else:
    sys.stderr.write("sensitive-diagnostic" * 1000)
    print(json.dumps({"structured_output": {"value": "ok"}}))
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        ClaudeCodeResearchProvider(_config(tmp_path, binary, maximum_stderr_bytes=100)).generate_structured(_request())
    assert exc_info.value.code == "CLAUDE_CODE_STDERR_OVERFLOW"
    assert "sensitive-diagnostic" not in str(exc_info.value)


def test_trailing_outer_json_is_rejected(tmp_path):
    binary = _fake_binary(tmp_path, """
import json, sys
if sys.argv[1:] == ["--version"]:
    print("2.1.205")
elif sys.argv[1:] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "oauth"}))
else:
    print('{} {}')
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        ClaudeCodeResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CLAUDE_CODE_INVALID_ENVELOPE"


def test_timeout_reaps_process_and_threads(tmp_path):
    binary = _fake_binary(tmp_path, """
import json, sys, time
if sys.argv[1:] == ["--version"]:
    print("2.1.205")
elif sys.argv[1:] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "oauth"}))
else:
    time.sleep(30)
""")
    provider = ClaudeCodeResearchProvider(_config(tmp_path, binary, request_timeout_seconds=1))
    before = {thread.ident for thread in threading.enumerate()}
    with pytest.raises(ProviderTimeoutError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "CLAUDE_CODE_PROCESS_TIMEOUT"
    assert {thread.ident for thread in threading.enumerate()} == before


def test_config_rejects_insecure_working_directory_and_non_absolute_binary(tmp_path):
    binary, _ = _normal_fake(tmp_path)
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="working_directory"):
        _config(tmp_path, binary, working_directory=insecure)
    with pytest.raises(ValueError, match="binary_path"):
        _config(tmp_path, Path("relative-claude"))


def test_config_rejects_missing_binary(tmp_path):
    with pytest.raises(ProviderUnavailableError) as exc_info:
        _config(tmp_path, tmp_path / "missing-claude")
    assert exc_info.value.code == "CLAUDE_CODE_BINARY_MISSING"
