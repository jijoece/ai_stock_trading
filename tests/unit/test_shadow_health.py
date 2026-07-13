"""Tests for shadow/health.py (docs/milestone-7.md Step 22, Step 27 section L)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.shadow import pause as pause_mod
from trading_research.shadow.health import (
    STATUS_DEGRADED,
    STATUS_HEALTHY,
    STATUS_PAUSE_REQUIRED,
    CycleHealthInputs,
    HealthPolicyConfig,
    HealthPolicyError,
    apply_health_result,
    evaluate_cycle_health,
)
from trading_research.storage.database import connect

BASE_TIME = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)


def _clock_at(t: datetime):
    return lambda: t


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "shadow_health_test.db")
        yield c
        c.close()


def _config(**overrides) -> HealthPolicyConfig:
    kwargs = dict(
        policy_version="health/v1-test", pause_on_provider_failure_rate=0.50, pause_on_retry_exhaustion_rate=0.50,
        pause_on_unsupported_claim_rate=0.25, pause_on_reconciliation_mismatch=True, pause_on_budget_breach=True,
        max_cycle_duration_seconds=900,
    )
    kwargs.update(overrides)
    return HealthPolicyConfig(**kwargs)


def _healthy_inputs(**overrides) -> CycleHealthInputs:
    kwargs = dict(
        provider_success_rate=0.99, evidence_completeness_rate=0.99, claude_role_success_rate=0.99, retry_rate=0.05,
        retry_exhaustion_rate=0.0, unsupported_claim_rate=0.0, output_truncation_rate=0.0, latency_seconds=5.0,
        input_tokens=1000, output_tokens=500, cost_usd=Decimal("0.10"), pricing_configured=True,
        paper_reconciliation_mismatch=False, duplicate_prevention_violation=False, cycle_duration_seconds=120.0,
        budget_breached=False,
    )
    kwargs.update(overrides)
    return CycleHealthInputs(**kwargs)


# --- healthy ------------------------------------------------------------------------


def test_healthy_result():
    result = evaluate_cycle_health(_healthy_inputs(), _config())
    assert result.status == STATUS_HEALTHY
    assert result.policy_version == "health/v1-test"
    assert len(result.reasons) >= 1


def test_identical_inputs_reproduce_identical_result():
    inputs = _healthy_inputs()
    config = _config()
    r1 = evaluate_cycle_health(inputs, config)
    r2 = evaluate_cycle_health(inputs, config)
    assert r1 == r2


# --- degraded -----------------------------------------------------------------------


def test_degraded_result_from_elevated_but_subthreshold_provider_failure_rate():
    # pause threshold 0.50, degraded threshold = 0.60 * 0.50 = 0.30
    inputs = _healthy_inputs(provider_success_rate=1.0 - 0.35)  # failure_rate = 0.35
    result = evaluate_cycle_health(inputs, _config())
    assert result.status == STATUS_DEGRADED


def test_degraded_from_output_truncation():
    inputs = _healthy_inputs(output_truncation_rate=0.10)
    result = evaluate_cycle_health(inputs, _config())
    assert result.status == STATUS_DEGRADED


# --- pause recommended ---------------------------------------------------------------


def test_pause_recommended_from_reconciliation_mismatch_when_not_configured_to_autopause():
    config = _config(pause_on_reconciliation_mismatch=False)
    inputs = _healthy_inputs(paper_reconciliation_mismatch=True)
    result = evaluate_cycle_health(inputs, config)
    assert result.status == "PAUSE_RECOMMENDED"


def test_pause_recommended_from_unknown_pricing_with_positive_cost():
    inputs = _healthy_inputs(cost_usd=Decimal("1.50"), pricing_configured=False)
    result = evaluate_cycle_health(inputs, _config())
    assert result.status == "PAUSE_RECOMMENDED"
    assert any("pricing_configured" in r for r in result.reasons)


# --- pause required -------------------------------------------------------------------


def test_pause_required_from_provider_failure_rate():
    inputs = _healthy_inputs(provider_success_rate=1.0 - 0.60)  # failure_rate 0.60 > 0.50 threshold
    result = evaluate_cycle_health(inputs, _config())
    assert result.status == STATUS_PAUSE_REQUIRED
    assert "provider_failure_rate" in result.triggering_flags


def test_pause_required_from_retry_exhaustion_rate():
    inputs = _healthy_inputs(retry_exhaustion_rate=0.75)
    result = evaluate_cycle_health(inputs, _config())
    assert result.status == STATUS_PAUSE_REQUIRED
    assert "retry_exhaustion_rate" in result.triggering_flags


def test_pause_required_from_unsupported_claim_rate():
    inputs = _healthy_inputs(unsupported_claim_rate=0.40)
    result = evaluate_cycle_health(inputs, _config())
    assert result.status == STATUS_PAUSE_REQUIRED
    assert "unsupported_claim_rate" in result.triggering_flags


def test_pause_required_from_reconciliation_mismatch_when_configured():
    inputs = _healthy_inputs(paper_reconciliation_mismatch=True)
    result = evaluate_cycle_health(inputs, _config(pause_on_reconciliation_mismatch=True))
    assert result.status == STATUS_PAUSE_REQUIRED
    assert "reconciliation_mismatch" in result.triggering_flags


def test_pause_required_from_duplicate_prevention_violation():
    inputs = _healthy_inputs(duplicate_prevention_violation=True)
    result = evaluate_cycle_health(inputs, _config())
    assert result.status == STATUS_PAUSE_REQUIRED


def test_pause_required_from_budget_breach_when_configured():
    inputs = _healthy_inputs(budget_breached=True)
    result = evaluate_cycle_health(inputs, _config(pause_on_budget_breach=True))
    assert result.status == STATUS_PAUSE_REQUIRED
    assert "budget_breach" in result.triggering_flags


# --- exhaustive status enum fails closed -----------------------------------------------


def test_unrecognized_status_raises():
    from trading_research.shadow.health import HealthResult

    with pytest.raises(HealthPolicyError):
        HealthResult(status="NOT_A_STATUS", policy_version="v1", reasons=(), triggering_flags=())


# --- None values do not fabricate a failure --------------------------------------------


def test_none_rates_do_not_trigger_pause():
    inputs = _healthy_inputs(
        provider_success_rate=None, retry_exhaustion_rate=None, unsupported_claim_rate=None,
        output_truncation_rate=None,
    )
    result = evaluate_cycle_health(inputs, _config())
    assert result.status == STATUS_HEALTHY


# --- apply_health_result: detect vs act separation --------------------------------------


def test_apply_health_result_requests_pause_when_required_and_flag_configured(conn):
    inputs = _healthy_inputs(provider_success_rate=1.0 - 0.60)
    result = evaluate_cycle_health(inputs, _config())
    assert result.status == STATUS_PAUSE_REQUIRED
    new_state = apply_health_result(conn, result, _config(), clock=_clock_at(BASE_TIME))
    assert new_state is not None
    assert new_state.state == pause_mod.STATE_PAUSED_PROVIDER_HEALTH
    assert new_state.source == pause_mod.SOURCE_AUTOMATIC_HEALTH_RULE
    assert pause_mod.current_state(conn).state == pause_mod.STATE_PAUSED_PROVIDER_HEALTH


def test_apply_health_result_does_nothing_when_healthy(conn):
    result = evaluate_cycle_health(_healthy_inputs(), _config())
    outcome = apply_health_result(conn, result, _config(), clock=_clock_at(BASE_TIME))
    assert outcome is None
    assert pause_mod.current_state(conn).state == pause_mod.STATE_ACTIVE


def test_apply_health_result_does_nothing_when_pause_required_but_flag_not_configured(conn):
    # retry_exhaustion_rate pause flag disabled (0.0 means "never auto-pause for this")
    config = _config(pause_on_retry_exhaustion_rate=0.0)
    inputs = _healthy_inputs(retry_exhaustion_rate=0.75)
    result = evaluate_cycle_health(inputs, config)
    assert result.status == STATUS_PAUSE_REQUIRED  # still detects it
    outcome = apply_health_result(conn, result, config, clock=_clock_at(BASE_TIME))
    assert outcome is None  # but does not act, since the flag says not to auto-pause
    assert pause_mod.current_state(conn).state == pause_mod.STATE_ACTIVE


def test_apply_health_result_never_calls_resume():
    """Structural check: this module must never call resume() — no
    automatic unpause exists."""
    import ast
    import trading_research.shadow.health as health_module

    source = Path(health_module.__file__).read_text()
    tree = ast.parse(source)
    resume_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "resume"
    ]
    assert resume_calls == []


def test_apply_health_result_does_not_act_on_already_killed_system(conn):
    pause_mod.kill(conn, "critical safety issue", "jijo", clock=_clock_at(BASE_TIME))
    inputs = _healthy_inputs(provider_success_rate=1.0 - 0.60)
    result = evaluate_cycle_health(inputs, _config())
    outcome = apply_health_result(conn, result, _config(), clock=_clock_at(BASE_TIME))
    assert outcome is None
    assert pause_mod.current_state(conn).state == pause_mod.STATE_KILLED
