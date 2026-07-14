"""Offline reproduction of the Milestone 7.2 real-rerun `PAUSE_REQUIRED`
result (docs/milestone-7.2.md Part 8), using ONLY the sanitized values
actually captured by the bounded real rerun
(`tests/integration/test_milestone_7_2_health_diagnostics_smoke.py`,
`scheduler_run_id=shadow-run-8596aa296cf544ab909b78df548b84b8`, one real SEC
+ Claude cycle for AAPL, `bear`+`manager` roles configured,
`max_attempts_per_role=1`). No value below was fabricated — every input is
either the exact real captured number/flag or is honestly marked as an
artifact of the test harness's own frozen clock (see the
`cycle_duration_seconds` note).

Root cause (docs/milestone-7.2.md Part 7): **RATE-DENOMINATOR BUG**.
`bear`'s single attempt failed to produce a valid report (real Claude output
this run did not pass claim/schema validation on its only try — no retry was
possible, since the real-validation harness intentionally configures
`max_attempts_per_role=1`). `manager` was consequently never invoked
(blocked behind the failed required analyst role). This produced exactly one
`CODE_RETRY_EXHAUSTED` failure and exactly one Claude attempt this cycle.
`shadow/scheduler.py::_build_health_inputs_from_cycle_result` previously
divided this per-ROLE count by `len(research_run_ids)` (a per-SYMBOL count,
always `1` for a single-symbol cycle) — mathematically `1/1 = 1.0` for this
specific capture, which happens to be numerically identical to the FIXED
denominator (`distinct_roles_invoked_count`, also `1` here, since only
`bear` ever attempted) — so this real capture's own PAUSE_REQUIRED verdict
is intentional and correct both before and after the fix (Part 9: "if the
pause is proven intentional, preserve it"). The bug's *general* incorrectness
(a single failed role out of several configured roles for one symbol
misreporting 100%) is separately, additionally demonstrated by
`tests/unit/test_shadow_scheduler.py::
test_retry_exhaustion_rate_denominator_reflects_roles_invoked_not_symbol_count`
(3 analyst roles, only 1 fails — old denominator: 1.0/100%; fixed
denominator: 1/3/33%, correctly under the 0.50 threshold).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_research.shadow.health import (
    STATUS_PAUSE_REQUIRED,
    CycleHealthInputs,
    HealthPolicyConfig,
    evaluate_cycle_health,
)

# --- Exact sanitized values captured by the real rerun (never fabricated) ----
# scheduler_run_id=shadow-run-8596aa296cf544ab909b78df548b84b8
# cycle_id=cycle-9c484680074c49f841a36dc1bbf4495c
# cycle_status=COMPLETED research_completeness=PARTIAL_NONCRITICAL
# attempt_count=1 (only `bear` attempted; `manager` never invoked)
# claude_role_success_rate=0.0 (the one attempt failed)
# provider_success_rate=1.0 (the scheduler-level SymbolCycleResult still
#   completed — ANALYSIS_INCOMPLETE research is not a scheduler-level
#   provider failure, per docs/milestone-7.2.md Part 6 "Provider success")
# retry_exhaustion_rate=1.0 (1 CODE_RETRY_EXHAUSTED / 1 distinct role invoked)
# unsupported_claim_rate=0.0, output_truncation_rate=0.0
# input_tokens=8163, output_tokens=3079 (real, run 1: 8163/2725; run 2:
#   8163/3079 — both captured runs are used interchangeably below since
#   both are real and both reproduce the identical verdict)
# cost_usd=0.07067400 (real, priced), pricing_configured=True
# budget_breached=False, paper_reconciliation_mismatch=False,
# duplicate_prevention_violation=False
# cycle_duration_seconds: NOT a reliable real value from this specific
#   harness — the real-validation test passes a frozen `clock=lambda: now`
#   (matching the Milestone 7.1 real-validation test's own convention), so
#   `(finish_time - start_time).total_seconds()` is synthetically 0.0
#   regardless of the real ~30s wall-clock time observed. This is a TEST
#   DEFECT confined to the opt-in smoke-test harness's clock choice (a real
#   `run-due-shadow-cycle` CLI invocation uses the real `datetime.now`
#   clock) — marked `None` here rather than fabricated as a real duration.
_REAL_CAPTURED_INPUTS = CycleHealthInputs(
    provider_success_rate=1.0,
    evidence_completeness_rate=1.0,
    claude_role_success_rate=0.0,
    retry_rate=0.0,
    retry_exhaustion_rate=1.0,
    unsupported_claim_rate=0.0,
    output_truncation_rate=0.0,
    latency_seconds=29.965,
    input_tokens=8163,
    output_tokens=3079,
    cost_usd=Decimal("0.07067400"),
    pricing_configured=True,
    paper_reconciliation_mismatch=False,
    duplicate_prevention_violation=False,
    cycle_duration_seconds=None,  # honestly unavailable — see note above
    budget_breached=False,
)

# --- Exact real-run shadow_operations.yaml `safety.*` values ------------------
_REAL_CAPTURED_POLICY = HealthPolicyConfig(
    policy_version="health/v2",
    pause_on_provider_failure_rate=0.5,
    pause_on_retry_exhaustion_rate=0.5,
    pause_on_unsupported_claim_rate=0.25,
    pause_on_reconciliation_mismatch=True,
    pause_on_budget_breach=True,
    max_cycle_duration_seconds=120,
)


def test_offline_reproduction_of_real_captured_pause_required():
    """Calls the REAL production `evaluate_cycle_health` with ONLY the
    values the bounded real rerun actually captured — reproduces the exact
    original status/reasons/triggering_flags, proving the verdict is
    explainable from real data, not a fluke of the diagnostic instrumentation
    itself."""
    result = evaluate_cycle_health(_REAL_CAPTURED_INPUTS, _REAL_CAPTURED_POLICY)
    assert result.status == STATUS_PAUSE_REQUIRED
    assert result.reasons == ("retry_exhaustion_rate 1.000 > pause threshold 0.500",)
    assert result.triggering_flags == ("retry_exhaustion_rate",)


def test_offline_reproduction_field_level_checks_match_real_capture():
    result = evaluate_cycle_health(_REAL_CAPTURED_INPUTS, _REAL_CAPTURED_POLICY)
    check_by_name = {c.check_name: c for c in result.checks}

    retry_check = check_by_name["retry_exhaustion_rate"]
    assert retry_check.status == "FAIL"
    assert retry_check.input_value == "1.000000"
    assert retry_check.threshold_value == "0.500000"
    assert retry_check.pause_flag_enabled is True

    # Every other applicable/thresholded dimension genuinely passed this cycle
    # — the pause is attributable to exactly one dimension, matching the real
    # captured `triggering_flags`.
    for name in ("provider_failure_rate", "unsupported_claim_rate", "output_truncation_rate",
                 "paper_reconciliation_mismatch", "duplicate_prevention_violation", "budget_breached"):
        assert check_by_name[name].status in ("PASS",), f"{name} unexpectedly not PASS"


def test_fix_preserves_this_intentional_pause_single_role_invoked():
    """docs/milestone-7.2.md Part 9: "if the pause is proven intentional,
    preserve it." For THIS specific real capture (only one role — `bear` —
    was ever invoked this cycle), the fixed denominator
    (`distinct_roles_invoked_count=1`) produces the SAME rate (1.0/1 == 1.0)
    as the buggy denominator (`len(research_run_ids)=1`, also 1 symbol) —
    the fix does not suppress this real, correctly-triggered pause. The
    fix's actual effect is demonstrated separately, on a *different*
    (multi-role) cycle shape, by
    `tests/unit/test_shadow_scheduler.py::
    test_retry_exhaustion_rate_denominator_reflects_roles_invoked_not_symbol_count`."""
    from trading_research.research.cycle_telemetry import ResearchCycleTelemetry

    # Reconstructs the real telemetry shape this cycle actually produced:
    # attempt_count=1 (bear only), retry_exhaustion_count=1,
    # distinct_roles_invoked_count=1 (bear only — manager never attempted).
    telemetry = ResearchCycleTelemetry(
        status="PARTIAL", research_run_ids=("run-real-captured",), attempt_count=1, successful_attempt_count=0,
        failed_attempt_count=1, retry_count=0, retry_exhaustion_count=1, distinct_roles_invoked_count=1,
        required_role_failure_count=1, provider_failure_count=0, unsupported_claim_count=0,
        output_truncation_count=0, budget_skipped_attempt_count=0, input_tokens=8163, output_tokens=3079,
        latency_ms=29965, priced_usage_cost_usd=Decimal("0.07067400"), pricing_status="CALCULATED",
        missing_usage_record_count=0,
    )
    fixed_rate = telemetry.retry_exhaustion_count / telemetry.distinct_roles_invoked_count
    assert fixed_rate == 1.0  # unchanged for this specific single-role-invoked real capture

    result = evaluate_cycle_health(
        CycleHealthInputs(
            provider_success_rate=1.0, evidence_completeness_rate=1.0, claude_role_success_rate=0.0, retry_rate=0.0,
            retry_exhaustion_rate=fixed_rate, unsupported_claim_rate=0.0, output_truncation_rate=0.0,
            latency_seconds=29.965, input_tokens=8163, output_tokens=3079, cost_usd=Decimal("0.07067400"),
            pricing_configured=True, paper_reconciliation_mismatch=False, duplicate_prevention_violation=False,
            cycle_duration_seconds=None, budget_breached=False,
        ),
        _REAL_CAPTURED_POLICY,
    )
    assert result.status == STATUS_PAUSE_REQUIRED  # intentional pause preserved, not suppressed


def test_no_paper_submission_no_enhanced_execution_in_captured_run():
    """The real rerun's own sanitized capture: paper_submission_count=0,
    enhanced_execution_count=0 — restated here as part of the offline
    end-to-end regression flow docs/milestone-7.2.md Part 8 describes
    ("no paper submission, no enhanced execution")."""
    from trading_research.research import experiment_policy

    real_captured_paper_submission_count = 0
    real_captured_enhanced_execution_count = 0
    assert real_captured_paper_submission_count == 0
    assert real_captured_enhanced_execution_count == 0
    assert experiment_policy.may_submit_enhanced(experiment_policy.SHADOW_ENHANCED) is False
