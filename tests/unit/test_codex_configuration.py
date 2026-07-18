"""Tests for the Codex research-provider configuration (Milestone 12)."""
from __future__ import annotations

import pytest

from trading_research.research.configuration import KNOWN_PROVIDERS, ResearchConfigError, load_research_config

VALID_YAML = """
version: 1
research:
  enabled: false
  provider: codex
  model: gpt-5.1-codex
  max_attempts_per_role: 2
  request_timeout_seconds: 60
  max_input_characters: 100000
  max_evidence_items: 100
  max_items_per_source_category: 25
  max_claims_per_role: 20
  max_output_tokens: 4000
  require_point_in_time_safe: true
  require_evidence_for_material_claims: true
  fail_on_stale_required_evidence: true
  allow_parallel_roles: false
codex:
  binary_path: /opt/homebrew/bin/codex
  minimum_version: "0.144.0"
  terminate_grace_seconds: 5
  maximum_stdout_bytes: 1048576
  maximum_stderr_bytes: 65536
  maximum_jsonl_line_bytes: 262144
  maximum_jsonl_events: 10000
  maximum_schema_bytes: 262144
  maximum_prompt_bytes: 524288
  working_directory: /private/tmp/agentic-trading-desk-codex-test
  require_chatgpt_authentication: true
  require_usage_metadata: true
roles:
  - fundamental
  - technical
  - bull
  - bear
  - manager
overlay:
  policy_version: research-overlay.v1
  allow_score_increase: false
  allow_position_size_increase: false
  incomplete_action: ANALYSIS_INCOMPLETE
  critical_risk_action: FORCE_NO_ACTION
"""


def _write(tmp_path, text):
    path = tmp_path / "research.yaml"
    path.write_text(text)
    return path


def test_codex_provider_name_is_known():
    assert "codex" in KNOWN_PROVIDERS


def test_codex_provider_accepted_and_loads(tmp_path):
    cfg = load_research_config(_write(tmp_path, VALID_YAML))
    assert cfg.provider == "codex"
    assert cfg.codex is not None
    assert cfg.codex.minimum_version == "0.144.0"
    cfg.require_ready()  # explicit model + codex section present — must not raise


def test_codex_provider_without_model_fails_closed_on_require_ready(tmp_path):
    bad = VALID_YAML.replace("model: gpt-5.1-codex", "model: null")
    cfg = load_research_config(_write(tmp_path, bad))  # loads fine — disabled config shouldn't fail to load
    with pytest.raises(ResearchConfigError, match="model"):
        cfg.require_ready()


def test_codex_provider_without_codex_section_fails_closed_on_require_ready(tmp_path):
    lines = VALID_YAML.splitlines()
    # Strip the entire top-level `codex:` block.
    filtered = []
    skipping = False
    for line in lines:
        if line == "codex:":
            skipping = True
            continue
        if skipping and line and not line.startswith(" ") and not line.startswith("\t"):
            skipping = False
        if not skipping:
            filtered.append(line)
    stripped = "\n".join(filtered)
    cfg = load_research_config(_write(tmp_path, stripped))
    assert cfg.codex is None
    with pytest.raises(ResearchConfigError, match="codex"):
        cfg.require_ready()


def test_codex_section_missing_keys_fails_closed(tmp_path):
    bad = VALID_YAML.replace("  require_usage_metadata: true\n", "")
    with pytest.raises(ResearchConfigError, match="missing keys"):
        load_research_config(_write(tmp_path, bad))


def test_codex_section_unknown_keys_fails_closed(tmp_path):
    bad = VALID_YAML.replace(
        "  require_usage_metadata: true\n", "  require_usage_metadata: true\n  extra_unknown_key: true\n"
    )
    with pytest.raises(ResearchConfigError, match="unknown keys"):
        load_research_config(_write(tmp_path, bad))


def test_codex_section_invalid_booleans_fail_closed(tmp_path):
    bad = VALID_YAML.replace("require_chatgpt_authentication: true", 'require_chatgpt_authentication: "yes"')
    with pytest.raises(ResearchConfigError, match="boolean"):
        load_research_config(_write(tmp_path, bad))


def test_codex_section_invalid_limits_fail_closed(tmp_path):
    bad = VALID_YAML.replace("maximum_stdout_bytes: 1048576", "maximum_stdout_bytes: not-an-int")
    with pytest.raises(ResearchConfigError):
        load_research_config(_write(tmp_path, bad))


def test_build_codex_provider_config_wires_through(tmp_path):
    fake_binary = tmp_path / "fake-codex"
    fake_binary.write_text("#!/usr/bin/env python3\n")
    fake_binary.chmod(0o755)
    yaml_text = VALID_YAML.replace("binary_path: /opt/homebrew/bin/codex", f"binary_path: {fake_binary}")
    cfg = load_research_config(_write(tmp_path, yaml_text))
    from trading_research.research.codex_provider import CodexProviderConfig

    provider_config = cfg.build_codex_provider_config()
    assert isinstance(provider_config, CodexProviderConfig)
    assert provider_config.model == "gpt-5.1-codex"
    assert provider_config.minimum_version == "0.144.0"
    assert provider_config.request_timeout_seconds == 60


def test_disabled_default_config_does_not_select_codex():
    cfg = load_research_config()
    assert cfg.enabled is False
    assert cfg.provider != "codex"


def test_unrecognized_top_level_key_still_fails_closed(tmp_path):
    bad = VALID_YAML + "\nnonsense_top_level_key: true\n"
    with pytest.raises(ResearchConfigError, match="unknown top-level"):
        load_research_config(_write(tmp_path, bad))
