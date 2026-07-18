"""Tests for shadow/config.py (docs/milestone-7.md Step 12, Step 27
section covering shadow-operations config)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from trading_research.shadow.config import (
    DEFAULT_SHADOW_OPERATIONS_CONFIG_PATH,
    ShadowOperationsConfigError,
    load_shadow_operations_config,
)

VALID_RAW = {
    "version": 1,
    "shadow_operations": {
        "enabled": False, "mode": "SHADOW_ENHANCED", "allow_baseline_paper_submission": False,
        "allow_enhanced_submission": False, "require_market_open_day": True,
        "run_window_timezone": "America/Los_Angeles", "run_window_start": "06:30", "run_window_end": "08:30",
        "max_catch_up_cycles": 1, "lease_ttl_seconds": 3600, "stale_run_timeout_seconds": 7200,
        "continue_on_symbol_failure": True,
    },
    "schedule": {"enabled": False, "cadence": "DAILY_MARKET_DAY", "intended_local_time": "06:45"},
    "budgets": {
        "require_pricing_for_real_claude": True, "max_symbols_per_cycle": 10, "max_roles_per_symbol": 5,
        "max_attempts_per_role": 2, "max_input_tokens_per_cycle": 100000, "max_output_tokens_per_cycle": 50000,
        "max_latency_seconds_per_cycle": 900, "max_estimated_cost_per_cycle_usd": 5.0,
        "max_actual_cost_per_day_usd": 10.0, "max_actual_cost_per_month_usd": 100.0,
        "emergency_margin_fraction": 0.1,
    },
    "safety": {
        "pause_on_provider_failure_rate": 0.5, "pause_on_retry_exhaustion_rate": 0.5,
        "pause_on_unsupported_claim_rate": 0.25, "pause_on_reconciliation_mismatch": True,
        "pause_on_budget_breach": True,
    },
}


def _write_config(raw: dict) -> Path:
    tmp_dir = tempfile.mkdtemp()
    path = Path(tmp_dir) / "shadow_operations.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _mutate(section: str, key: str, value) -> dict:
    import copy

    raw = copy.deepcopy(VALID_RAW)
    raw[section][key] = value
    return raw


# --- default repository file loads cleanly and is disabled by default -------


def test_default_shadow_operations_yaml_loads_and_is_disabled_by_default():
    config = load_shadow_operations_config(DEFAULT_SHADOW_OPERATIONS_CONFIG_PATH)
    assert config.shadow_operations.enabled is False
    assert config.schedule.enabled is False
    assert config.shadow_operations.allow_enhanced_submission is False
    assert config.shadow_operations.allow_baseline_paper_submission is False


def test_config_hash_computed_and_stable():
    path = _write_config(VALID_RAW)
    c1 = load_shadow_operations_config(path)
    c2 = load_shadow_operations_config(path)
    assert c1.config_hash == c2.config_hash
    assert len(c1.config_hash) == 64  # sha256 hex digest


# --- fail-closed on missing file / bad yaml ----------------------------------


def test_missing_file_raises_config_error():
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config("/nonexistent/path/shadow_operations.yaml")


def test_invalid_yaml_raises_config_error():
    tmp_dir = tempfile.mkdtemp()
    path = Path(tmp_dir) / "bad.yaml"
    path.write_text("{ not: valid: yaml: [")
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


def test_missing_top_level_section_raises():
    import copy

    raw = copy.deepcopy(VALID_RAW)
    del raw["budgets"]
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


def test_missing_required_key_raises():
    raw = _mutate("shadow_operations", "enabled", False)
    del raw["shadow_operations"]["mode"]
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


# --- unknown mode fails closed ------------------------------------------------


def test_unknown_mode_fails_closed():
    raw = _mutate("shadow_operations", "mode", "LIVE_ENHANCED")
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


def test_unknown_cadence_fails_closed():
    raw = _mutate("schedule", "cadence", "HOURLY")
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


# --- allow_enhanced_submission is structurally impossible to set true -------


def test_allow_enhanced_submission_true_is_structurally_rejected():
    raw = _mutate("shadow_operations", "allow_enhanced_submission", True)
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


def test_allow_baseline_paper_submission_true_is_accepted_but_still_disabled_by_default():
    # allow_baseline_paper_submission is a normal (non-structurally-forbidden)
    # flag -- true must be accepted at the config layer (paper-only path
    # already exists elsewhere), unlike allow_enhanced_submission.
    raw = _mutate("shadow_operations", "allow_baseline_paper_submission", True)
    path = _write_config(raw)
    config = load_shadow_operations_config(path)
    assert config.shadow_operations.allow_baseline_paper_submission is True
    assert config.shadow_operations.allow_enhanced_submission is False


# --- negative/zero budget values rejected ------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "max_symbols_per_cycle", "max_roles_per_symbol", "max_attempts_per_role", "max_input_tokens_per_cycle",
        "max_output_tokens_per_cycle", "max_latency_seconds_per_cycle", "max_estimated_cost_per_cycle_usd",
        "max_actual_cost_per_day_usd", "max_actual_cost_per_month_usd",
    ],
)
def test_zero_budget_value_rejected(key):
    raw = _mutate("budgets", key, 0)
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


@pytest.mark.parametrize(
    "key",
    [
        "max_symbols_per_cycle", "max_roles_per_symbol", "max_attempts_per_role", "max_input_tokens_per_cycle",
        "max_output_tokens_per_cycle", "max_latency_seconds_per_cycle", "max_estimated_cost_per_cycle_usd",
        "max_actual_cost_per_day_usd", "max_actual_cost_per_month_usd",
    ],
)
def test_negative_budget_value_rejected(key):
    raw = _mutate("budgets", key, -1)
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


def test_zero_lease_ttl_rejected():
    raw = _mutate("shadow_operations", "lease_ttl_seconds", 0)
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


def test_zero_max_catch_up_cycles_rejected():
    raw = _mutate("shadow_operations", "max_catch_up_cycles", 0)
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


def test_zero_stale_run_timeout_rejected():
    raw = _mutate("shadow_operations", "stale_run_timeout_seconds", 0)
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_out_of_range_safety_rate_rejected(value):
    raw = _mutate("safety", "pause_on_provider_failure_rate", value)
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_out_of_range_emergency_margin_rejected(value):
    raw = _mutate("budgets", "emergency_margin_fraction", value)
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


def test_valid_boundary_values_accepted():
    import copy

    raw = copy.deepcopy(VALID_RAW)
    raw["safety"]["pause_on_provider_failure_rate"] = 0.0
    raw["safety"]["pause_on_retry_exhaustion_rate"] = 1.0
    raw["budgets"]["emergency_margin_fraction"] = 0.0
    path = _write_config(raw)
    config = load_shadow_operations_config(path)
    assert config.safety.pause_on_provider_failure_rate == 0.0
    assert config.budgets.emergency_margin_fraction == 0.0


# --- Milestone 11.3.1 Item 7: strict boolean parsing for every boolean field -

STRICT_BOOL_FIELDS = [
    ("shadow_operations", "enabled"),
    ("shadow_operations", "allow_baseline_paper_submission"),
    ("shadow_operations", "allow_enhanced_submission"),
    ("shadow_operations", "require_market_open_day"),
    ("shadow_operations", "continue_on_symbol_failure"),
    ("schedule", "enabled"),
    ("budgets", "require_pricing_for_real_claude"),
    ("safety", "pause_on_reconciliation_mismatch"),
    ("safety", "pause_on_budget_breach"),
]


@pytest.mark.parametrize("section,key", STRICT_BOOL_FIELDS)
@pytest.mark.parametrize("value", ["true", "false", "yes", "no", 1, 0, None])
def test_strict_bool_rejects_permissive_values(section, key, value):
    raw = _mutate(section, key, value)
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


@pytest.mark.parametrize("section,key", STRICT_BOOL_FIELDS)
def test_strict_bool_rejects_list_and_mapping(section, key):
    for value in ([True], {"a": True}):
        raw = _mutate(section, key, value)
        path = _write_config(raw)
        with pytest.raises(ShadowOperationsConfigError):
            load_shadow_operations_config(path)


@pytest.mark.parametrize("section,key", STRICT_BOOL_FIELDS)
@pytest.mark.parametrize("value", [True, False])
def test_strict_bool_accepts_real_booleans(section, key, value):
    if (section, key) == ("shadow_operations", "allow_enhanced_submission") and value is True:
        pytest.skip("allow_enhanced_submission=true is structurally rejected regardless of boolean strictness")
    raw = _mutate(section, key, value)
    path = _write_config(raw)
    config = load_shadow_operations_config(path)
    section_obj = getattr(config, section)
    assert getattr(section_obj, key) is value


def test_malformed_boolean_fails_before_any_downstream_use():
    # A malformed required boolean must raise during load_shadow_operations_config
    # itself -- before any scheduler, lease, budget, provider, Claude, or broker
    # code ever sees a ShadowOperationsConfiguration built from it.
    raw = _mutate("shadow_operations", "enabled", "false")
    path = _write_config(raw)
    with pytest.raises(ShadowOperationsConfigError):
        load_shadow_operations_config(path)


def test_repository_default_config_still_loads_with_strict_bool_parsing():
    config = load_shadow_operations_config(DEFAULT_SHADOW_OPERATIONS_CONFIG_PATH)
    assert config.shadow_operations.enabled is False
    assert config.schedule.enabled is False


def test_config_hash_remains_deterministic_with_strict_bool_parsing():
    path = _write_config(VALID_RAW)
    c1 = load_shadow_operations_config(path)
    c2 = load_shadow_operations_config(path)
    assert c1.config_hash == c2.config_hash
