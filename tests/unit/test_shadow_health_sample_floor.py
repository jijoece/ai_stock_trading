"""Milestone 11.3 Part 23: provider-health sample-size protection.

A 1-request cycle's 100% failure rate must not read the same as a
100-request cycle's — below `minimum_requests_for_failure_rate`, the
provider_failure_rate check is INSUFFICIENT_DATA, not an automatic pause or
a fabricated pass. Covers 1/1, 2/2, threshold crossing, and recovery."""
from __future__ import annotations

from decimal import Decimal

from trading_research.shadow.health import (
    CHECK_NAME_PROVIDER_FAILURE_RATE,
    CHECK_STATUS_FAIL,
    CHECK_STATUS_INSUFFICIENT_DATA,
    CHECK_STATUS_PASS,
    STATUS_HEALTHY,
    STATUS_PAUSE_REQUIRED,
    CycleHealthInputs,
    HealthPolicyConfig,
    evaluate_cycle_health,
)


def _config(**overrides) -> HealthPolicyConfig:
    kwargs = dict(
        policy_version="health/v1-test", pause_on_provider_failure_rate=0.50, pause_on_retry_exhaustion_rate=0.50,
        pause_on_unsupported_claim_rate=0.25, pause_on_reconciliation_mismatch=True, pause_on_budget_breach=True,
        max_cycle_duration_seconds=900, minimum_requests_for_failure_rate=5,
    )
    kwargs.update(overrides)
    return HealthPolicyConfig(**kwargs)


def _inputs(**overrides) -> CycleHealthInputs:
    kwargs = dict(
        provider_success_rate=0.0, evidence_completeness_rate=None, claude_role_success_rate=None, retry_rate=None,
        retry_exhaustion_rate=None, unsupported_claim_rate=None, output_truncation_rate=None, latency_seconds=None,
        input_tokens=None, output_tokens=None, cost_usd=None, pricing_configured=True,
        paper_reconciliation_mismatch=False, duplicate_prevention_violation=False, cycle_duration_seconds=120.0,
        budget_breached=False,
    )
    kwargs.update(overrides)
    return CycleHealthInputs(**kwargs)


def _find(result, check_name):
    return next(c for c in result.checks if c.check_name == check_name)


def test_1_of_1_failure_below_floor_is_insufficient_data_not_pause():
    result = evaluate_cycle_health(_inputs(provider_success_rate=0.0, provider_request_count=1), _config())
    check = _find(result, CHECK_NAME_PROVIDER_FAILURE_RATE)
    assert check.status == CHECK_STATUS_INSUFFICIENT_DATA
    assert result.status != STATUS_PAUSE_REQUIRED


def test_2_of_2_failure_below_floor_is_insufficient_data_not_pause():
    result = evaluate_cycle_health(_inputs(provider_success_rate=0.0, provider_request_count=2), _config())
    check = _find(result, CHECK_NAME_PROVIDER_FAILURE_RATE)
    assert check.status == CHECK_STATUS_INSUFFICIENT_DATA
    assert result.status != STATUS_PAUSE_REQUIRED


def test_below_floor_input_value_still_reported_for_observability():
    result = evaluate_cycle_health(_inputs(provider_success_rate=0.0, provider_request_count=1), _config())
    check = _find(result, CHECK_NAME_PROVIDER_FAILURE_RATE)
    assert check.input_value == "1.000000"  # the raw computed rate, not hidden


def test_at_floor_sample_size_evaluates_normally():
    # 5 requests, 100% failure, floor=5 -> sample size no longer below floor.
    result = evaluate_cycle_health(_inputs(provider_success_rate=0.0, provider_request_count=5), _config())
    check = _find(result, CHECK_NAME_PROVIDER_FAILURE_RATE)
    assert check.status == CHECK_STATUS_FAIL
    assert result.status == STATUS_PAUSE_REQUIRED


def test_large_sample_high_success_rate_passes_normally():
    result = evaluate_cycle_health(_inputs(provider_success_rate=0.99, provider_request_count=100), _config())
    check = _find(result, CHECK_NAME_PROVIDER_FAILURE_RATE)
    assert check.status == CHECK_STATUS_PASS
    assert result.status == STATUS_HEALTHY


def test_recovery_after_threshold_crossing():
    """A cycle that paused for a large-sample failure recovers to healthy
    once a subsequent (independently evaluated, since evaluate_cycle_health
    is pure/per-cycle) sample shows success — no residual pause state
    leaking between cycles at this pure-function layer."""
    paused = evaluate_cycle_health(_inputs(provider_success_rate=0.0, provider_request_count=20), _config())
    assert paused.status == STATUS_PAUSE_REQUIRED

    recovered = evaluate_cycle_health(_inputs(provider_success_rate=1.0, provider_request_count=20), _config())
    assert recovered.status == STATUS_HEALTHY


def test_severe_explicit_provider_error_bypasses_sample_floor():
    """An explicit severe provider error must still pause immediately even
    from a single request — the sample floor only protects against noisy
    small-sample *rates*, not an unambiguous outage signal."""
    result = evaluate_cycle_health(
        _inputs(provider_success_rate=0.0, provider_request_count=1, provider_severe_error=True), _config(),
    )
    check = _find(result, CHECK_NAME_PROVIDER_FAILURE_RATE)
    assert check.status == CHECK_STATUS_FAIL
    assert result.status == STATUS_PAUSE_REQUIRED


def test_no_sample_count_supplied_preserves_prior_behavior():
    """A caller that doesn't supply provider_request_count at all (None) —
    e.g. any not-yet-updated call site — keeps the pre-Part-23 behavior of
    evaluating whatever rate is present, since there's no sample-size
    context to gate on."""
    result = evaluate_cycle_health(_inputs(provider_success_rate=0.0, provider_request_count=None), _config())
    check = _find(result, CHECK_NAME_PROVIDER_FAILURE_RATE)
    assert check.status == CHECK_STATUS_FAIL


def test_sample_floor_default_is_backward_compatible():
    config = HealthPolicyConfig(
        policy_version="v", pause_on_provider_failure_rate=0.5, pause_on_retry_exhaustion_rate=0.5,
        pause_on_unsupported_claim_rate=0.25, pause_on_reconciliation_mismatch=True, pause_on_budget_breach=True,
    )
    assert config.minimum_requests_for_failure_rate == 1
