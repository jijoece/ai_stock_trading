"""Persistent, multi-cycle provider-health hysteresis (Milestone 11.3.1
Item 8 Part C).

`shadow/health.py::evaluate_cycle_health` is a pure, single-cycle function by
design (ADR 0005 Decision 7) — it has no memory of prior cycles. That is
correct for "what does this cycle's data say," but a single noisy/failing
cycle should not, by itself, recommend or require a pause, and a single
lucky healthy cycle after a real outage should not, by itself, look
"recovered." This module adds the missing memory: a durable, versioned,
consecutive-failure/consecutive-recovery counter persisted in
`shadow_health_hysteresis_state`, read and rewritten fresh from the database
on every call — never an in-memory global — so it survives a process
restart exactly like every other piece of durable state in this repository.

This module never calls `shadow/pause.py::request_pause` or `resume()`. It
only computes and persists a `decision` (HEALTHY/DEGRADED/PAUSE_RECOMMENDED/
PAUSE_REQUIRED) for observability and for a caller (e.g. `shadow/scheduler.py`)
to fold into its own existing single-cycle `apply_health_result` action.
Nothing in this module ever automatically clears an operator-initiated
PAUSED_MANUAL or KILLED system state — see `_effective_decision`.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from ..hashing import hash_config
from ..storage import shadow_alerts_repositories as repo
from ..storage.transactions import transaction
from . import pause as pause_mod
from .health import STATUS_DEGRADED, STATUS_HEALTHY, STATUS_PAUSE_RECOMMENDED, STATUS_PAUSE_REQUIRED

Clock = Callable[[], datetime]

POLICY_VERSION = "persistent_health/v1"

DEFAULT_SCOPE = "default"

STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class HysteresisPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class PersistentHealthPolicyConfig:
    """Every threshold that governs the hysteresis state machine, plus
    `policy_version` — hashed via `config_hash` and stored alongside the
    persisted state so a configuration change is a detectable policy
    boundary (Required test 14), not a silent behavior change applied
    retroactively to an existing failure/recovery streak."""

    policy_version: str = POLICY_VERSION
    warning_after_n_failures: int = 1
    pause_recommended_after_n_failures: int = 2
    pause_required_after_m_failures: int = 3
    recovery_streak: int = 2

    def __post_init__(self) -> None:
        for name in (
            "warning_after_n_failures", "pause_recommended_after_n_failures",
            "pause_required_after_m_failures", "recovery_streak",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise HysteresisPolicyError(f"{name} must be a positive integer")
        if not (
            self.warning_after_n_failures
            <= self.pause_recommended_after_n_failures
            <= self.pause_required_after_m_failures
        ):
            raise HysteresisPolicyError(
                "warning_after_n_failures <= pause_recommended_after_n_failures <= "
                "pause_required_after_m_failures must hold"
            )

    def config_hash(self) -> str:
        return hash_config({
            "policy_version": self.policy_version,
            "warning_after_n_failures": self.warning_after_n_failures,
            "pause_recommended_after_n_failures": self.pause_recommended_after_n_failures,
            "pause_required_after_m_failures": self.pause_required_after_m_failures,
            "recovery_streak": self.recovery_streak,
        })


@dataclass(frozen=True)
class PersistentHealthDecision:
    scope: str
    decision: str
    consecutive_failures: int
    consecutive_recoveries: int
    qualified_cycle_count: int
    failing_cycle_count: int
    reasons: tuple[str, ...]
    idempotent_replay: bool
    policy_reset: bool
    previous_decision: str = STATUS_HEALTHY
    policy_hash: str = ""
    effective_status: str = STATUS_HEALTHY


@dataclass(frozen=True)
class HysteresisEvaluationEvidence:
    sample_size: int = 0
    minimum_sample_size: int = 0
    aggregate_success_rate: float | None = None
    required_categories: tuple[str, ...] = ()
    required_providers: tuple[str, ...] = ()
    observed_providers: tuple[str, ...] = ()
    missing_required_providers: tuple[str, ...] = ()
    missing_required_categories: tuple[str, ...] = ()
    per_provider_metrics: dict | None = None
    severe_error_categories: tuple[str, ...] = ()
    provider_policy_hash: str = ""


def _initial_state(scope: str, config: PersistentHealthPolicyConfig, now: datetime) -> dict:
    return {
        "scope": scope, "policy_version": config.policy_version, "policy_hash": config.config_hash(),
        "decision": STATUS_HEALTHY, "consecutive_failures": 0, "consecutive_recoveries": 0,
        "qualified_cycle_count": 0, "failing_cycle_count": 0, "window_start": now.isoformat(),
        "window_end": now.isoformat(), "last_cycle_id": None, "last_evaluated_at": now.isoformat(),
        "reasons_json": "[]", "per_provider_metrics_json": "{}",
    }


def _effective_decision(conn, computed_decision: str) -> tuple[str, bool]:
    """Milestone 11.3.1 Item 8: never let a computed recovery (or any
    computed decision) contradict an operator-initiated PAUSED_MANUAL or a
    KILLED system state -- those are never automatically cleared by this
    module (it doesn't call resume() itself, but a caller trusting this
    `decision` field alone must also see it stay at PAUSE_REQUIRED while the
    system is actually still manually paused or killed, rather than reading
    a misleading recovered HEALTHY)."""
    system_state = pause_mod.current_state(conn).state
    if system_state in (pause_mod.STATE_PAUSED_MANUAL, pause_mod.STATE_KILLED):
        return STATUS_PAUSE_REQUIRED, True
    return computed_decision, False


def _operational_effective(single_cycle_status: str, hysteresis_status: str, immediate_pause: bool) -> str:
    if immediate_pause:
        return STATUS_PAUSE_REQUIRED
    if single_cycle_status == STATUS_PAUSE_REQUIRED:
        return hysteresis_status
    order = {STATUS_HEALTHY: 0, STATUS_DEGRADED: 1, STATUS_PAUSE_RECOMMENDED: 2, STATUS_PAUSE_REQUIRED: 3}
    return single_cycle_status if order[single_cycle_status] >= order[hysteresis_status] else hysteresis_status


def evaluate_and_persist_hysteresis(
    conn, *, scope: str = DEFAULT_SCOPE, cycle_id: str, cycle_status: str, qualified: bool,
    severe_error: bool = False, config: PersistentHealthPolicyConfig, clock: Clock,
    evidence: HysteresisEvaluationEvidence | None = None, immediate_pause: bool = False,
) -> PersistentHealthDecision:
    """Advance (or replay) the persistent hysteresis state machine by
    exactly one cycle.

    `cycle_status` is the per-cycle `shadow/health.py::HealthResult.status`
    for this cycle. `qualified` is `False` when the cycle had insufficient
    real provider-request data to draw any conclusion at all (never treated
    as a failure OR a recovery). `severe_error` bypasses ordinary consecutive
    counting and immediately drives PAUSE_REQUIRED (an unambiguous outage
    signal), mirroring `shadow/health.py`'s own sample-floor-bypass
    convention for `provider_severe_error`.

    Idempotent: calling this again with the same `cycle_id` returns the
    already-persisted decision unchanged rather than double-counting.
    """
    if cycle_status not in (STATUS_HEALTHY, STATUS_DEGRADED, STATUS_PAUSE_RECOMMENDED, STATUS_PAUSE_REQUIRED):
        raise HysteresisPolicyError(f"cycle_status {cycle_status!r} is not a known health status")

    now = clock()
    evidence = evidence or HysteresisEvaluationEvidence()
    policy_hash = hash_config({
        "hysteresis_policy_hash": config.config_hash(),
        "provider_policy_hash": evidence.provider_policy_hash,
    })
    prior_evaluation = repo.load_health_hysteresis_evaluation(
        conn, scope=scope, cycle_id=cycle_id, policy_hash=policy_hash,
    )
    if prior_evaluation is not None:
        return PersistentHealthDecision(
            # Replay the persisted operational decision as well as the
            # rolling hysteresis state.  In particular, a manual pause or
            # kill may have forced `effective_status=PAUSE_REQUIRED` even
            # when the computed hysteresis status was healthy; returning the
            # raw status here would let a scheduler replay clear that fence.
            scope=scope, decision=prior_evaluation["effective_status"],
            consecutive_failures=prior_evaluation["consecutive_failures_after"],
            consecutive_recoveries=prior_evaluation["consecutive_recoveries_after"],
            qualified_cycle_count=(repo.load_health_hysteresis_state(conn, scope) or {}).get("qualified_cycle_count", 0),
            failing_cycle_count=(repo.load_health_hysteresis_state(conn, scope) or {}).get("failing_cycle_count", 0),
            reasons=tuple(json.loads(prior_evaluation["reasons_json"])), idempotent_replay=True,
            policy_reset=False, previous_decision=prior_evaluation["previous_hysteresis_status"],
            policy_hash=policy_hash, effective_status=prior_evaluation["effective_status"],
        )

    existing = repo.load_health_hysteresis_state(conn, scope)
    policy_reset = False

    if existing is None:
        state = _initial_state(scope, config, now)
    elif existing["policy_hash"] != policy_hash:
        # Milestone 11.3.1 Item 8 Required test 14: a configuration change
        # is a detectable policy boundary -- reset the streak rather than
        # silently applying new thresholds to a streak accumulated under
        # the old policy.
        state = _initial_state(scope, config, now)
        policy_reset = True
    else:
        state = dict(existing)

    previous_decision = state["decision"]
    failures_before = state["consecutive_failures"]
    recoveries_before = state["consecutive_recoveries"]
    consecutive_failures = state["consecutive_failures"]
    consecutive_recoveries = state["consecutive_recoveries"]
    qualified_cycle_count = state["qualified_cycle_count"]
    failing_cycle_count = state["failing_cycle_count"]
    decision = state["decision"]
    reasons: list[str] = []

    if severe_error:
        consecutive_failures += 1
        consecutive_recoveries = 0
        qualified_cycle_count += 1
        failing_cycle_count += 1
        decision = STATUS_PAUSE_REQUIRED
        reasons.append("severe provider error bypasses the sample floor and hysteresis counting")
    elif not qualified:
        reasons.append(
            f"cycle {cycle_id!r} has insufficient data — does not count toward failure or recovery streaks"
        )
    elif cycle_status in (STATUS_PAUSE_RECOMMENDED, STATUS_PAUSE_REQUIRED, STATUS_DEGRADED):
        consecutive_failures += 1
        consecutive_recoveries = 0
        qualified_cycle_count += 1
        failing_cycle_count += 1
        if consecutive_failures >= config.pause_required_after_m_failures:
            decision = STATUS_PAUSE_REQUIRED
        elif consecutive_failures >= config.pause_recommended_after_n_failures:
            decision = STATUS_PAUSE_RECOMMENDED
        elif consecutive_failures >= config.warning_after_n_failures:
            decision = STATUS_DEGRADED
        reasons.append(
            f"consecutive failing qualified cycles = {consecutive_failures} "
            f"(this cycle status: {cycle_status})"
        )
    else:  # STATUS_HEALTHY
        consecutive_recoveries += 1
        consecutive_failures = 0
        qualified_cycle_count += 1
        if decision != STATUS_HEALTHY and consecutive_recoveries >= config.recovery_streak:
            decision = STATUS_HEALTHY
            reasons.append(f"recovered after {consecutive_recoveries} consecutive healthy qualified cycles")
        else:
            reasons.append(f"consecutive healthy qualified cycles = {consecutive_recoveries}")

    if immediate_pause:
        reasons.append("structural health failure requires an immediate pause")
    operational_effective = _operational_effective(cycle_status, decision, immediate_pause)
    effective_decision, forced_by_system_state = _effective_decision(conn, operational_effective)
    if forced_by_system_state:
        reasons.append(
            "system pause state is PAUSED_MANUAL or KILLED — hysteresis decision is never "
            "automatically cleared past that"
        )

    new_state = {
        "scope": scope, "policy_version": config.policy_version, "policy_hash": policy_hash,
        "decision": decision, "consecutive_failures": consecutive_failures,
        "consecutive_recoveries": consecutive_recoveries, "qualified_cycle_count": qualified_cycle_count,
        "failing_cycle_count": failing_cycle_count, "window_start": state["window_start"],
        "window_end": now.isoformat(), "last_cycle_id": cycle_id, "last_evaluated_at": now.isoformat(),
        "reasons_json": json.dumps(reasons),
        "per_provider_metrics_json": json.dumps(evidence.per_provider_metrics or {}, sort_keys=True),
    }
    evaluation = {
        "evaluation_id": f"health-eval-{uuid.uuid4().hex}", "scope": scope, "cycle_id": cycle_id,
        "policy_version": config.policy_version, "policy_hash": policy_hash,
        "single_cycle_status": cycle_status, "previous_hysteresis_status": previous_decision,
        "new_hysteresis_status": decision, "effective_status": effective_decision,
        "qualified": int(qualified), "sample_size": evidence.sample_size,
        "minimum_sample_size": evidence.minimum_sample_size,
        "aggregate_success_rate": evidence.aggregate_success_rate,
        "required_categories_json": json.dumps(list(evidence.required_categories)),
        "required_providers_json": json.dumps(list(evidence.required_providers)),
        "observed_providers_json": json.dumps(list(evidence.observed_providers)),
        "missing_required_providers_json": json.dumps(list(evidence.missing_required_providers)),
        "missing_required_categories_json": json.dumps(list(evidence.missing_required_categories)),
        "per_provider_metrics_json": json.dumps(evidence.per_provider_metrics or {}, sort_keys=True),
        "severe_error_categories_json": json.dumps(list(evidence.severe_error_categories)),
        "consecutive_failures_before": failures_before,
        "consecutive_failures_after": consecutive_failures,
        "consecutive_recoveries_before": recoveries_before,
        "consecutive_recoveries_after": consecutive_recoveries,
        "reasons_json": json.dumps(reasons), "evaluated_at": now.isoformat(),
    }
    with transaction(conn):
        repo.save_health_hysteresis_state(conn, new_state, commit=False)
        inserted = repo.insert_health_hysteresis_evaluation(conn, evaluation)
        if not inserted:
            raise HysteresisPolicyError("hysteresis evaluation identity raced; retry the idempotent evaluation")

    return PersistentHealthDecision(
        scope=scope, decision=(effective_decision if forced_by_system_state else decision),
        consecutive_failures=consecutive_failures,
        consecutive_recoveries=consecutive_recoveries, qualified_cycle_count=qualified_cycle_count,
        failing_cycle_count=failing_cycle_count, reasons=tuple(reasons), idempotent_replay=False,
        policy_reset=policy_reset, previous_decision=previous_decision, policy_hash=policy_hash,
        effective_status=effective_decision,
    )
