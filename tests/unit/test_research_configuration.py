"""Tests for research configuration loading (docs/milestone-5.md Step 18)."""
from __future__ import annotations

import pytest

from trading_research.research.configuration import ResearchConfigError, load_research_config
from trading_research.research.errors import UnknownOverlayActionError, UnknownProviderError, UnknownRoleError

VALID_YAML = """
version: 1
research:
  enabled: false
  provider: deterministic
  model: null
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


def test_default_config_file_loads_and_defaults_to_disabled():
    cfg = load_research_config()
    assert cfg.enabled is False


def test_valid_config_loads(tmp_path):
    cfg = load_research_config(_write(tmp_path, VALID_YAML))
    assert cfg.provider == "deterministic"
    assert cfg.roles == ("fundamental", "technical", "bull", "bear", "manager")


def test_unknown_provider_fails_closed(tmp_path):
    bad = VALID_YAML.replace("provider: deterministic", "provider: openai")
    with pytest.raises(UnknownProviderError):
        load_research_config(_write(tmp_path, bad))


def test_unknown_role_fails_closed(tmp_path):
    bad = VALID_YAML.replace("- technical", "- crystal_ball")
    with pytest.raises(UnknownRoleError):
        load_research_config(_write(tmp_path, bad))


def test_unknown_overlay_action_fails_closed(tmp_path):
    bad = VALID_YAML.replace("critical_risk_action: FORCE_NO_ACTION", "critical_risk_action: DO_WHATEVER")
    with pytest.raises(UnknownOverlayActionError):
        load_research_config(_write(tmp_path, bad))


def test_missing_research_section_fails(tmp_path):
    bad = "version: 1\nroles: [fundamental]\noverlay: {policy_version: v1, incomplete_action: ANALYSIS_INCOMPLETE, critical_risk_action: FORCE_NO_ACTION}\n"
    with pytest.raises(ResearchConfigError):
        load_research_config(_write(tmp_path, bad))


def test_anthropic_provider_without_model_fails_closed_on_require_ready(tmp_path):
    bad = VALID_YAML.replace("provider: deterministic", "provider: anthropic")
    cfg = load_research_config(_write(tmp_path, bad))  # loads fine — disabled config shouldn't fail to load
    with pytest.raises(ResearchConfigError):
        cfg.require_ready()


def test_environment_variable_alone_cannot_enable_research(monkeypatch, tmp_path):
    """Setting ANTHROPIC_API_KEY must not flip research.enabled — only the
    YAML config controls that (docs/milestone-5.md Step 18)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    cfg = load_research_config(_write(tmp_path, VALID_YAML))
    assert cfg.enabled is False
