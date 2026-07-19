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

from trading_research.research.codex_provider import (
    MINIMAL_PATH,
    CodexProviderConfig,
    CodexResearchProvider,
)
from trading_research.research.errors import (
    MalformedOutputError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderTransientError,
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
        json_schema=SCHEMA, model_name="gpt-5.6-sol", max_output_tokens=100, temperature=0,
        prompt_name="test", prompt_version="v1", prompt_hash="prompt-hash",
        system_prompt_hash="system-hash", schema_version="test.v1", attempt_number=1,
        validation_feedback=feedback,
    )


def _fake_binary(tmp_path: Path, body: str) -> Path:
    binary = tmp_path / "fake-codex"
    binary.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


_PREFLIGHT_STANZA = """
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
    sys.exit(0)
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
    sys.exit(0)
"""


def _normal_fake(tmp_path: Path, *, structured=None, usage=True, extra_lines: str = "") -> tuple[Path, Path]:
    capture = tmp_path / "capture.json"
    structured = {"value": "ok"} if structured is None else structured
    text = json.dumps(structured)
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "thread-abc"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}}),
    ]
    if usage:
        lines.append(json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 11, "output_tokens": 7, "cached_input_tokens": 3, "reasoning_output_tokens": 0},
        }))
    else:
        lines.append(json.dumps({"type": "turn.completed", "usage": {}}))
    body = f"""
import json, os, pathlib, sys
args = sys.argv[1:]
{_PREFLIGHT_STANZA}
if args[:1] == ["exec"]:
    prompt = sys.stdin.read()
    pathlib.Path({str(capture)!r}).write_text(json.dumps({{
        "argv": args,
        "prompt": prompt,
        "environment_keys": sorted(os.environ),
        "path": os.environ.get("PATH"),
    }}))
{chr(10).join('    print(' + repr(line) + ')' for line in lines)}
{extra_lines}
    sys.exit(0)
sys.exit(1)
"""
    return _fake_binary(tmp_path, body), capture


def _config(tmp_path: Path, binary: Path, **overrides) -> CodexProviderConfig:
    workdir = tmp_path / "runtime"
    workdir.mkdir(mode=0o700, exist_ok=True)
    values = {
        "binary_path": binary,
        "minimum_version": "0.144.5",
        "model": "gpt-5.6-sol",
        "request_timeout_seconds": 2,
        "terminate_grace_seconds": 1,
        "maximum_stdout_bytes": 16_384,
        "maximum_stderr_bytes": 4_096,
        "maximum_jsonl_line_bytes": 8_192,
        "maximum_jsonl_events": 20,
        "maximum_schema_bytes": 8_192,
        "maximum_prompt_bytes": 8_192,
        "working_directory": workdir,
        "pricing_entries": (
            PricingEntry(
                provider="codex", model="gpt-5.6-sol", effective_date="2020-01-01", currency="USD",
                input_price_per_million=Decimal("3"), output_price_per_million=Decimal("15"),
                pricing_version="test-api-equivalent",
            ),
        ),
    }
    values.update(overrides)
    return CodexProviderConfig(**values)


# --- Configuration -----------------------------------------------------


def test_config_rejects_insecure_working_directory_and_non_absolute_binary(tmp_path):
    binary, _ = _normal_fake(tmp_path)
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="working_directory"):
        _config(tmp_path, binary, working_directory=insecure)
    with pytest.raises(ValueError, match="binary_path"):
        _config(tmp_path, Path("relative-codex"))


def test_config_rejects_working_directory_inside_repository(tmp_path):
    from trading_research.config import REPO_ROOT

    binary, _ = _normal_fake(tmp_path)
    with pytest.raises(ValueError, match="working_directory"):
        _config(tmp_path, binary, working_directory=REPO_ROOT / "some-subdir")


def test_config_rejects_symlinked_working_directory(tmp_path):
    binary, _ = _normal_fake(tmp_path)
    real_dir = tmp_path / "real-workdir"
    real_dir.mkdir(mode=0o700)
    link = tmp_path / "linked-workdir"
    link.symlink_to(real_dir)
    with pytest.raises(ValueError, match="symbolic link"):
        _config(tmp_path, binary, working_directory=link)


def test_config_rejects_missing_binary(tmp_path):
    with pytest.raises(ProviderUnavailableError) as exc_info:
        _config(tmp_path, tmp_path / "missing-codex")
    assert exc_info.value.code == "CODEX_BINARY_MISSING"


def test_config_rejects_non_executable_binary(tmp_path):
    binary = tmp_path / "not-executable"
    binary.write_text("not a script")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        _config(tmp_path, binary)
    assert exc_info.value.code == "CODEX_BINARY_NOT_EXECUTABLE"


def test_config_requires_explicit_model(tmp_path):
    binary, _ = _normal_fake(tmp_path)
    with pytest.raises(ValueError, match="model"):
        _config(tmp_path, binary, model="")


def test_config_requires_true_authentication_and_usage_flags(tmp_path):
    binary, _ = _normal_fake(tmp_path)
    with pytest.raises(ValueError, match="require_chatgpt_authentication"):
        _config(tmp_path, binary, require_chatgpt_authentication=False)
    with pytest.raises(ValueError, match="require_usage_metadata"):
        _config(tmp_path, binary, require_usage_metadata=False)


def test_config_rejects_non_positive_limits(tmp_path):
    binary, _ = _normal_fake(tmp_path)
    with pytest.raises(ValueError, match="maximum_stdout_bytes"):
        _config(tmp_path, binary, maximum_stdout_bytes=0)


# --- Preflight -----------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "code"),
    [
        ("0.144.4", "CODEX_VERSION_UNSUPPORTED"),  # below the tested floor
        ("0.144.5", None),  # exact tested floor — accepted
        ("0.144.9", None),  # within the declared range — accepted
        ("0.145.0", "CODEX_VERSION_UNSUPPORTED"),  # ceiling is exclusive
        ("1.0.0", "CODEX_VERSION_UNSUPPORTED"),  # untested future major version
        ("not-a-version", "CODEX_VERSION_UNPARSABLE"),  # malformed
        ("0.144.5-beta", "CODEX_VERSION_UNSUPPORTED"),  # prerelease, never accepted
    ],
)
def test_version_preflight_fails_closed(tmp_path, version, code):
    binary = _fake_binary(tmp_path, f"""
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print({version!r})
else:
    print("Logged in using ChatGPT")
""")
    provider = CodexResearchProvider(_config(tmp_path, binary))
    if code is None:
        result = provider.preflight()
        assert result.ready is True
        assert result.adapter_version == "codex-jsonl/v1"
    else:
        with pytest.raises(ProviderUnavailableError) as exc_info:
            provider.preflight()
        assert exc_info.value.code == code


def test_unsupported_version_fails_before_any_inference_subprocess(tmp_path):
    """Milestone 12.1 Item 2: an unsupported CLI version must fail preflight
    before `generate_structured` ever invokes `codex exec` — proven here by
    a fake binary that raises if invoked with `exec` (only `--version`/
    `login status` are legitimate preflight calls)."""
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("0.145.0")
elif args[:1] == ["exec"]:
    sys.exit(99)  # must never be reached
else:
    print("Logged in using ChatGPT")
""")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_VERSION_UNSUPPORTED"


def test_login_status_nonzero_exit_fails_closed(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
else:
    print("error", file=sys.stderr)
    sys.exit(1)
""")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).preflight()
    assert exc_info.value.code == "CODEX_LOGIN_STATUS_FAILED"


@pytest.mark.parametrize(
    ("login_output", "code"),
    [
        ("Not logged in", "CODEX_NOT_AUTHENTICATED"),
        ("Logged in using API key", "CODEX_UNEXPECTED_AUTH_METHOD"),
        ("Logged in using access token", "CODEX_UNEXPECTED_AUTH_METHOD"),
    ],
)
def test_login_status_rejects_non_chatgpt_authentication(tmp_path, login_output, code):
    marker = tmp_path / "inference-called"
    binary = _fake_binary(tmp_path, f"""
import pathlib, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print({login_output!r})
else:
    pathlib.Path({str(marker)!r}).write_text("called")
""")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).preflight()
    assert exc_info.value.code == code
    assert not marker.exists()


def test_chatgpt_authentication_accepted(tmp_path):
    binary, _ = _normal_fake(tmp_path)
    result = CodexResearchProvider(_config(tmp_path, binary)).preflight()
    assert result.ready is True
    assert result.authenticated is True
    assert result.authentication_method == "chatgpt"
    assert result.binary_version == "0.144.5"


def test_preflight_caching_and_forced_refresh(tmp_path):
    calls = tmp_path / "calls.txt"
    binary = _fake_binary(tmp_path, f"""
import pathlib, sys
args = sys.argv[1:]
p = pathlib.Path({str(calls)!r})
p.write_text((p.read_text() if p.exists() else "") + "x")
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
else:
    print("Logged in using ChatGPT")
""")
    provider = CodexResearchProvider(_config(tmp_path, binary))
    provider.preflight()
    provider.preflight()
    assert calls.read_text() == "xx"  # cached: version+login only ran once
    provider.preflight(force=True)
    assert calls.read_text() == "xxxx"


# --- Command hardening -----------------------------------------------------


def test_hardened_command_stdin_environment_output_and_usage(tmp_path, monkeypatch):
    binary, capture_path = _normal_fake(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("ALPACA_API_SECRET", "must-not-leak")
    parent_before = dict(os.environ)

    response = CodexResearchProvider(_config(tmp_path, binary)).generate_structured(
        _request(feedback=("VALIDATION_DYNAMIC_MARKER",))
    )

    assert response.provider == "codex"
    assert response.model_name == "gpt-5.6-sol"
    assert response.parsed_json == {"value": "ok"}
    assert response.raw_text == '{"value":"ok"}'
    assert response.usage.input_tokens == 11
    assert response.usage.output_tokens == 7
    assert response.usage.cache_read_tokens == 3
    assert response.usage.cache_write_tokens is None
    assert response.usage.cost_estimate_basis == "SUBSCRIPTION_API_EQUIVALENT_ESTIMATE"
    assert response.usage.configured_model_alias == "gpt-5.6-sol"
    assert response.usage.resolved_model_name is None  # Codex never reports one — never invented
    assert response.usage.provider_cli_version == "0.144.5"
    assert response.usage.provider_adapter_version == "codex-jsonl/v1"
    assert response.provider_request_id == "thread-abc"

    capture = json.loads(capture_path.read_text())
    argv = capture["argv"]
    assert argv[0] == "exec"
    for flag in (
        "--ephemeral", "--ignore-user-config", "--ignore-rules", "--sandbox",
        "--skip-git-repo-check", "--color", "--output-schema", "--json", "--model",
    ):
        assert flag in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--color") + 1] == "never"
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    assert argv[-1] == "-"
    assert 'approval_policy="never"' in argv
    assert 'web_search="disabled"' in argv
    assert "features.shell_tool=false" in argv
    assert "features.apps=false" in argv
    assert "features.remote_plugin=false" in argv
    assert "features.network_proxy.enabled=false" in argv

    forbidden = {
        "--dangerously-bypass-approvals-and-sandbox", "--add-dir", "-C", "--cd",
        "workspace-write", "danger-full-access",
    }
    assert forbidden.isdisjoint(argv)
    assert "SYSTEM_DYNAMIC_MARKER" not in argv
    assert "USER_DYNAMIC_MARKER" not in argv
    assert "VALIDATION_DYNAMIC_MARKER" not in argv
    assert "SYSTEM_DYNAMIC_MARKER" in capture["prompt"]
    assert "USER_DYNAMIC_MARKER" in capture["prompt"]
    assert "VALIDATION_DYNAMIC_MARKER" in capture["prompt"]
    assert capture["path"] == MINIMAL_PATH
    assert "OPENAI_API_KEY" not in capture["environment_keys"]
    assert "CODEX_API_KEY" not in capture["environment_keys"]
    assert "ANTHROPIC_API_KEY" not in capture["environment_keys"]
    assert "ALPACA_API_SECRET" not in capture["environment_keys"]
    assert dict(os.environ) == parent_before


def test_repository_never_added_to_working_directory(tmp_path):
    binary, capture_path = _normal_fake(tmp_path)
    CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    capture = json.loads(capture_path.read_text())
    assert "--add-dir" not in capture["argv"]


# --- Environment -----------------------------------------------------------


def test_no_parent_environment_leakage(tmp_path, monkeypatch):
    binary, capture_path = _normal_fake(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgres://leak")
    monkeypatch.setenv("PYTHONPATH", "/leak")
    monkeypatch.setenv("MCP_TOKEN", "leak")
    CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    capture = json.loads(capture_path.read_text())
    # `__CF_USER_TEXT_ENCODING` is injected by macOS itself into every child
    # process regardless of the env dict passed to subprocess.Popen — not a
    # leak from this provider's allowlist.
    allowed = {"HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "PATH", "__CF_USER_TEXT_ENCODING"}
    assert set(capture["environment_keys"]).issubset(allowed)
    assert "DATABASE_URL" not in capture["environment_keys"]
    assert "PYTHONPATH" not in capture["environment_keys"]
    assert "MCP_TOKEN" not in capture["environment_keys"]


# --- Temporary schema handling -----------------------------------------------


def test_temp_schema_written_private_and_cleaned_after_success(tmp_path):
    binary, capture_path = _normal_fake(tmp_path)
    provider = CodexResearchProvider(_config(tmp_path, binary))
    provider.generate_structured(_request())
    schema_dir = provider._config.working_directory / ".codex-schemas"
    assert list(schema_dir.iterdir()) == []


def test_temp_schema_cleaned_after_provider_error(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    sys.exit(1)
""")
    provider = CodexResearchProvider(_config(tmp_path, binary))
    with pytest.raises(ProviderUnavailableError):
        provider.generate_structured(_request())
    schema_dir = provider._config.working_directory / ".codex-schemas"
    assert list(schema_dir.iterdir()) == []


def test_temp_schema_cleaned_after_timeout(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys, time
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    time.sleep(30)
""")
    provider = CodexResearchProvider(_config(tmp_path, binary, request_timeout_seconds=1))
    with pytest.raises(ProviderTimeoutError):
        provider.generate_structured(_request())
    schema_dir = provider._config.working_directory / ".codex-schemas"
    assert list(schema_dir.iterdir()) == []


def test_oversize_schema_rejected(tmp_path):
    # The byte limit is now enforced against the normalized *transport*
    # schema, before it is ever written to disk — a more precisely
    # attributed failure than the old write-time CODEX_SCHEMA_REJECTED path
    # (`_write_temp_schema`'s own byte check remains as defense in depth but
    # is unreachable in this flow since the transport-level check runs first
    # against the identical byte count).
    binary, _ = _normal_fake(tmp_path)
    huge_schema_request = _request()
    provider = CodexResearchProvider(_config(tmp_path, binary, maximum_schema_bytes=5))
    with pytest.raises(ProviderUnavailableError) as exc_info:
        provider.generate_structured(huge_schema_request)
    assert exc_info.value.code == "CODEX_TRANSPORT_SCHEMA_UNSUPPORTED"


def test_transport_schema_written_to_disk_not_canonical_schema(tmp_path):
    """The file passed via `--output-schema` must be the Codex-compatible
    transport schema (bound keywords stripped, all properties required) —
    never the canonical schema directly."""
    captured_schema = tmp_path / "captured-schema.json"
    binary = _fake_binary(tmp_path, f"""
import json, pathlib, sys
args = sys.argv[1:]
{_PREFLIGHT_STANZA}
if args[:1] == ["exec"]:
    schema_path = args[args.index("--output-schema") + 1]
    pathlib.Path({str(captured_schema)!r}).write_text(pathlib.Path(schema_path).read_text())
    sys.stdin.read()
    print(json.dumps({{"type": "thread.started", "thread_id": "t"}}))
    print(json.dumps({{"type": "turn.started"}}))
    print(json.dumps({{"type": "item.completed", "item": {{"id": "item_0", "type": "agent_message", "text": json.dumps({{"value": "ok"}})}}}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 0, "reasoning_output_tokens": 0}}}}))
    sys.exit(0)
sys.exit(1)
""")
    CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    written = json.loads(captured_schema.read_text())
    assert "maxLength" not in json.dumps(written)
    assert written["required"] == ["value"]
    assert written["additionalProperties"] is False


def test_response_passing_transport_schema_but_failing_canonical_is_rejected(tmp_path):
    """A response that satisfies the broader transport schema (e.g. a
    string longer than the canonical maxLength, which transport no longer
    bounds) must still be rejected by canonical post-response validation and
    never persisted as a successful role report."""
    oversized_value = "y" * 100  # canonical SCHEMA bounds `value` to maxLength 20
    binary, _ = _normal_fake(tmp_path, structured={"value": oversized_value})
    with pytest.raises(SchemaValidationError):
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())


# --- JSONL parsing -----------------------------------------------------------


def test_realistic_captured_fixture_response(tmp_path):
    from tests.fixtures.codex_jsonl_fixtures import successful_turn_jsonl

    fixture_text = successful_turn_jsonl(json.dumps({"value": "ok"}))
    binary = tmp_path / "fake-codex"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if args[:1] == ['--version']:\n"
        "    print('codex-cli 0.144.5')\n"
        "elif args[:2] == ['login', 'status']:\n"
        "    print('Logged in using ChatGPT')\n"
        "else:\n"
        "    sys.stdin.read()\n"
        f"    sys.stdout.write({fixture_text!r})\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    response = CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert response.parsed_json == {"value": "ok"}
    assert response.usage.input_tokens == 13226
    assert response.usage.output_tokens == 15
    assert response.usage.cache_read_tokens == 0


def test_invalid_utf8_rejected(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    sys.stdout.buffer.write(b"\\xff\\xfe\\n")
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_INVALID_JSONL"


def test_malformed_line_rejected(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    print("{not valid json")
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_INVALID_JSONL"


def test_non_object_event_rejected(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    print("[1, 2, 3]")
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_INVALID_JSONL"


def test_unknown_event_type_rejected(tmp_path):
    binary = _fake_binary(tmp_path, """
import json, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    print(json.dumps({"type": "some.unrecognized.event"}))
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_INVALID_JSONL"


def test_missing_terminal_event_rejected(tmp_path):
    binary = _fake_binary(tmp_path, """
import json, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_TERMINAL_EVENT_MISSING"


def test_duplicate_terminal_event_rejected(tmp_path):
    binary = _fake_binary(tmp_path, """
import json, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_MULTIPLE_TERMINAL_EVENTS"


def test_event_after_terminal_event_rejected(tmp_path):
    binary = _fake_binary(tmp_path, """
import json, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
    print(json.dumps({"type": "turn.started"}))
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_INVALID_JSONL"


def test_final_output_missing_rejected(tmp_path):
    binary = _fake_binary(tmp_path, """
import json, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_FINAL_OUTPUT_MISSING"


def test_final_output_not_object_rejected(tmp_path):
    binary, _ = _normal_fake(tmp_path, structured="just-a-string")
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_FINAL_OUTPUT_MALFORMED"


def test_markdown_and_trailing_prose_rejected(tmp_path):
    binary = _fake_binary(tmp_path, """
import json, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    item = {"id": "item_0", "type": "agent_message", "text": "```json\\n{\\"value\\": \\"ok\\"}\\n```"}
    print(json.dumps({"type": "item.completed", "item": item}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_FINAL_OUTPUT_MALFORMED"


def test_usage_missing_rejected(tmp_path):
    binary, _ = _normal_fake(tmp_path, usage=False)
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_USAGE_METADATA_MISSING"


def test_usage_negative_or_wrong_type_rejected(tmp_path):
    binary = _fake_binary(tmp_path, """
import json, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    item = {"id": "item_0", "type": "agent_message", "text": json.dumps({"value": "ok"})}
    print(json.dumps({"type": "item.completed", "item": item}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": -1, "output_tokens": "seven"}}))
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_USAGE_METADATA_MISSING"


def _reasoning_usage_binary(tmp_path, *, input_tokens=11, output_tokens=7, reasoning_output_tokens):
    reasoning_field = (
        "" if reasoning_output_tokens is None else f', "reasoning_output_tokens": {reasoning_output_tokens}'
    )
    return _fake_binary(tmp_path, f"""
import json, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    item = {{"id": "item_0", "type": "agent_message", "text": json.dumps({{"value": "ok"}})}}
    print(json.dumps({{"type": "item.completed", "item": item}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": {input_tokens}, "output_tokens": {output_tokens}{reasoning_field}}}}}))
""")


def test_reasoning_tokens_exceeding_output_tokens_rejected(tmp_path):
    """Milestone 12.1 Item 4, required test #5: reasoning greater than total
    output fails under the REASONING_INCLUDED_IN_OUTPUT policy."""
    binary = _reasoning_usage_binary(tmp_path, output_tokens=7, reasoning_output_tokens=8)
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_REASONING_TOKENS_INVALID"


def test_reasoning_tokens_negative_rejected(tmp_path):
    binary = _reasoning_usage_binary(tmp_path, reasoning_output_tokens=-1)
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_USAGE_METADATA_MISSING"


def test_reasoning_tokens_persisted_and_within_output_tokens(tmp_path):
    """Required test #1: reasoning-token value is persisted. Required test
    #3: no double-counting — `output_tokens` alone remains the effective
    total under the inclusion policy."""
    binary = _reasoning_usage_binary(tmp_path, input_tokens=11, output_tokens=7, reasoning_output_tokens=4)
    response = CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert response.usage.output_tokens == 7  # unchanged — reasoning is a subset, never added on top
    assert response.usage.reasoning_output_tokens == 4
    assert response.usage.token_accounting_policy == "REASONING_INCLUDED_IN_OUTPUT"


def test_missing_reasoning_tokens_represented_as_none(tmp_path):
    """Required test #2: missing reasoning tokens are represented as
    unavailable (None), never fabricated as zero."""
    binary = _reasoning_usage_binary(tmp_path, reasoning_output_tokens=None)
    response = CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert response.usage.reasoning_output_tokens is None
    assert response.usage.token_accounting_policy == "REASONING_INCLUDED_IN_OUTPUT"


def test_event_count_overflow_rejected(tmp_path):
    binary, _ = _normal_fake(tmp_path)
    provider = CodexResearchProvider(_config(tmp_path, binary, maximum_jsonl_events=2))
    with pytest.raises(MalformedOutputError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "CODEX_INVALID_JSONL"


def test_line_size_overflow_rejected(tmp_path):
    binary = _fake_binary(tmp_path, """
import json, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    item = {"id": "item_0", "type": "agent_message", "text": "x" * 5000}
    print(json.dumps({"type": "item.completed", "item": item}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
""")
    provider = CodexResearchProvider(_config(tmp_path, binary, maximum_jsonl_line_bytes=100))
    with pytest.raises(MalformedOutputError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "CODEX_INVALID_JSONL"


def test_total_stdout_overflow_rejected(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    print("x" * 20000)
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary, maximum_stdout_bytes=8192)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_OUTPUT_OVERFLOW"


def test_stderr_overflow_bounded_and_not_exposed(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    sys.stderr.write("sensitive-diagnostic" * 1000)
    print("{}")
""")
    with pytest.raises(MalformedOutputError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary, maximum_stderr_bytes=100)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_STDERR_OVERFLOW"
    assert "sensitive-diagnostic" not in str(exc_info.value)


# --- Error mapping -----------------------------------------------------------


def test_timeout_maps_to_provider_timeout_and_reaps_threads(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys, time
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    time.sleep(30)
""")
    provider = CodexResearchProvider(_config(tmp_path, binary, request_timeout_seconds=1))
    before = {thread.ident for thread in threading.enumerate()}
    with pytest.raises(ProviderTimeoutError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "CODEX_PROCESS_TIMEOUT"
    assert {thread.ident for thread in threading.enumerate()} == before


def test_rate_limit_maps_to_provider_rate_limit_error(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    print("rate limit exceeded", file=sys.stderr)
    sys.exit(1)
""")
    with pytest.raises(ProviderRateLimitError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_RATE_LIMITED"


def test_quota_exhaustion_maps_to_provider_unavailable(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    print("usage limit reached for this plan", file=sys.stderr)
    sys.exit(1)
""")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_QUOTA_EXHAUSTED"


def test_schema_rejection_maps_to_provider_unavailable(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    print("invalid schema supplied", file=sys.stderr)
    sys.exit(1)
""")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_SCHEMA_REJECTED"


def test_transient_network_error_is_retryable(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    print("network connection reset", file=sys.stderr)
    sys.exit(1)
""")
    from trading_research.research.errors import ProviderTransientError

    with pytest.raises(ProviderTransientError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    # Milestone 12.1 Item 3: "connection reset" is its own NETWORK category,
    # distinct from a generic transient service failure.
    assert exc_info.value.code == "CODEX_NETWORK_FAILURE"
    assert exc_info.value.retryable is True


def test_transient_service_error_maps_to_transient_failure(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    print("service temporarily unavailable", file=sys.stderr)
    sys.exit(1)
""")
    from trading_research.research.errors import ProviderTransientError

    with pytest.raises(ProviderTransientError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_TRANSIENT_FAILURE"
    assert exc_info.value.retryable is True


def test_unknown_process_failure_maps_to_provider_unavailable(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    sys.exit(3)
""")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_PROCESS_EXITED"


@pytest.mark.parametrize(
    ("turn_failed_message", "expected_code", "expected_type", "expected_retryable"),
    [
        ("Codex authentication failed", "CODEX_NOT_AUTHENTICATED", ProviderUnavailableError, False),
        ("Your quota is exhausted", "CODEX_QUOTA_EXHAUSTED", ProviderUnavailableError, False),
        ("Rate limit exceeded", "CODEX_RATE_LIMITED", ProviderRateLimitError, True),
        ("Service temporarily unavailable", "CODEX_TRANSIENT_FAILURE", ProviderTransientError, True),
        ("dns lookup failed", "CODEX_NETWORK_FAILURE", ProviderTransientError, True),
        ("a totally unrecognized safe message", "CODEX_PROCESS_EXITED", ProviderUnavailableError, False),
    ],
)
def test_turn_failed_uses_the_same_typed_taxonomy_as_nonzero_exit(
    tmp_path, turn_failed_message, expected_code, expected_type, expected_retryable,
):
    """Milestone 12.1 Item 3: `turn.failed` (exit code 0) must map to the
    identical typed code a nonzero exit with the same diagnostic would."""
    body = f"""
import json, sys
args = sys.argv[1:]
{_PREFLIGHT_STANZA}
if args[:1] == ["exec"]:
    print(json.dumps({{"type": "thread.started", "thread_id": "thread-abc"}}))
    print(json.dumps({{"type": "turn.started"}}))
    print(json.dumps({{"type": "turn.failed", "error": {{"message": {turn_failed_message!r}}}}}))
    sys.exit(0)
sys.exit(1)
"""
    binary = _fake_binary(tmp_path, body)
    with pytest.raises(expected_type) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert exc_info.value.code == expected_code
    assert exc_info.value.retryable is expected_retryable


def test_turn_failed_message_never_appears_in_exception_text(tmp_path):
    secret_looking_message = "authentication failed: session=sk-ant-should-never-leak"
    body = f"""
import json, sys
args = sys.argv[1:]
{_PREFLIGHT_STANZA}
if args[:1] == ["exec"]:
    print(json.dumps({{"type": "thread.started", "thread_id": "thread-abc"}}))
    print(json.dumps({{"type": "turn.failed", "error": {{"message": {secret_looking_message!r}}}}}))
    sys.exit(0)
sys.exit(1)
"""
    binary = _fake_binary(tmp_path, body)
    with pytest.raises(ProviderUnavailableError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert "sk-ant-should-never-leak" not in str(exc_info.value)


def test_raw_stderr_never_appears_in_exception(tmp_path):
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    sys.stderr.write("TOP-SECRET-DIAGNOSTIC-STRING")
    sys.exit(1)
""")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())
    assert "TOP-SECRET-DIAGNOSTIC-STRING" not in str(exc_info.value)
    assert "TOP-SECRET-DIAGNOSTIC-STRING" not in str(exc_info.value.metadata)


# --- Prompt size -----------------------------------------------------------


def test_prompt_size_limit_is_enforced_before_any_subprocess(tmp_path):
    marker = tmp_path / "called"
    binary = _fake_binary(tmp_path, f"""
import pathlib
pathlib.Path({str(marker)!r}).write_text("called")
""")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        CodexResearchProvider(_config(tmp_path, binary, maximum_prompt_bytes=10)).generate_structured(_request())
    assert exc_info.value.code == "CODEX_PROMPT_TOO_LARGE"
    assert "SYSTEM_DYNAMIC_MARKER" not in str(exc_info.value)
    assert not marker.exists()


def test_local_schema_validation_is_mandatory(tmp_path):
    binary, _ = _normal_fake(tmp_path, structured={"value": "x" * 50})
    with pytest.raises(SchemaValidationError):
        CodexResearchProvider(_config(tmp_path, binary)).generate_structured(_request())


# --- Milestone 12.1.1 Item 6: configured codex.minimum_version is enforced ---


def test_installed_version_below_configured_minimum_fails(tmp_path):
    """Required test #1: installed 0.144.5 is within the adapter's supported
    range, but the operator configured a higher minimum (0.144.9) — must fail."""
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
else:
    print("Logged in using ChatGPT")
""")
    provider = CodexResearchProvider(_config(tmp_path, binary, minimum_version="0.144.9"))
    with pytest.raises(ProviderUnavailableError) as exc_info:
        provider.preflight()
    assert exc_info.value.code == "CODEX_VERSION_UNSUPPORTED"
    assert exc_info.value.metadata["configured_minimum_version"] == "0.144.9"
    assert exc_info.value.metadata["cli_version"] == "0.144.5"


def test_installed_version_equal_to_configured_minimum_passes(tmp_path):
    """Required test #2."""
    binary, _ = _normal_fake(tmp_path)  # reports 0.144.5
    provider = CodexResearchProvider(_config(tmp_path, binary, minimum_version="0.144.5"))
    result = provider.preflight()
    assert result.ready is True
    assert result.configured_minimum_version == "0.144.5"


def test_installed_version_outside_adapter_policy_fails_even_if_above_configured_minimum(tmp_path):
    """Required test #3: an installed version above the configured minimum
    but outside the closed adapter-contract range must still fail — a
    higher configured minimum never widens the adapter's own tested range."""
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.145.0")
else:
    print("Logged in using ChatGPT")
""")
    provider = CodexResearchProvider(_config(tmp_path, binary, minimum_version="0.144.5"))
    with pytest.raises(ProviderUnavailableError) as exc_info:
        provider.preflight()
    assert exc_info.value.code == "CODEX_VERSION_UNSUPPORTED"


@pytest.mark.parametrize("version", ["not-a-version", "0.144.5-beta"])
def test_malformed_and_prerelease_versions_fail_regardless_of_configured_minimum(tmp_path, version):
    """Required test #4."""
    binary = _fake_binary(tmp_path, f"""
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print({version!r})
else:
    print("Logged in using ChatGPT")
""")
    provider = CodexResearchProvider(_config(tmp_path, binary, minimum_version="0.144.5"))
    with pytest.raises(ProviderUnavailableError) as exc_info:
        provider.preflight()
    assert exc_info.value.code in ("CODEX_VERSION_UNPARSABLE", "CODEX_VERSION_UNSUPPORTED")


def test_no_inference_occurs_when_below_configured_minimum(tmp_path):
    """Required test #5: mirrors `test_unsupported_version_fails_before_any_inference_subprocess`
    but for the configured-minimum check specifically."""
    binary = _fake_binary(tmp_path, """
import sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.144.5")
elif args[:1] == ["exec"]:
    sys.exit(99)  # must never be reached
else:
    print("Logged in using ChatGPT")
""")
    provider = CodexResearchProvider(_config(tmp_path, binary, minimum_version="0.144.9"))
    with pytest.raises(ProviderUnavailableError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "CODEX_VERSION_UNSUPPORTED"
