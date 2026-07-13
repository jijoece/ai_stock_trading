"""Deterministic operational health rules (docs/milestone-7.md Step 22, ADR
0005 Decision 7). `evaluate_cycle_health` is a PURE function — cycle-level
counters in, one of `HEALTHY` / `DEGRADED` / `PAUSE_RECOMMENDED` /
`PAUSE_REQUIRED` out, versioned via `policy_version`, no DB/network access
inside it, mirroring `research/promotion.py::evaluate_promotion`'s shape
exactly (exhaustive status enum that raises on unrecognized value,
dataclass-of-reasons result).

`apply_health_result` is a SEPARATE function and the only thing in this
module that calls `shadow/pause.py::request_pause(...)` — and only when
`health_result.status == PAUSE_REQUIRED` AND the relevant
`safety.pause_on_*` flag is configured true for the triggering reason. This
enforces the "health function detects, a separate caller acts" boundary
(ADR 0005 Decision 7): `evaluate_cycle_health` itself never touches
`shadow_pause_state`, never imports `shadow.pause`, and has no side effects.

No automatic unpause: nothing in this module ever calls `resume()`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from . import pause as pause_mod
from .config import ShadowOperationsConfiguration

Clock = Callable[[], datetime]

POLICY_VERSION = "health/v1"

STATUS_HEALTHY = "HEALTHY"
STATUS_DEGRADED = "DEGRADED"
STATUS_PAUSE_RECOMMENDED = "PAUSE_RECOMMENDED"
STATUS_PAUSE_REQUIRED = "PAUSE_REQUIRED"

HEALTH_STATUSES = (STATUS_HEALTHY, STATUS_DEGRADED, STATUS_PAUSE_RECOMMENDED, STATUS_PAUSE_REQUIRED)

# Ordering used to pick the worst status across multiple independent checks.
_SEVERITY_ORDER = {
    STATUS_HEALTHY: 0, STATUS_DEGRADED: 1, STATUS_PAUSE_RECOMMENDED: 2, STATUS_PAUSE_REQUIRED: 3,
}

# Degraded thresholds are set at a configurable fraction of the configured
# pause threshold — "approaching the line" rather than a second independent
# config surface (docs/milestone-7.md Step 22 only names the safety.pause_on_*
# thresholds; DEGRADED is this module's own, versioned interpretation of
# "getting close").
DEGRADED_FRACTION_OF_PAUSE_THRESHOLD = 0.6

# Reasons a PAUSE_REQUIRED verdict may carry — each maps to the specific
# safety.pause_on_* flag that governs whether `apply_health_result` may act
# on it.
REASON_PROVIDER_FAILURE_RATE = "provider_failure_rate"
REASON_RETRY_EXHAUSTION_RATE = "retry_exhaustion_rate"
REASON_UNSUPPORTED_CLAIM_RATE = "unsupported_claim_rate"
REASON_RECONCILIATION_MISMATCH = "reconciliation_mismatch"
REASON_BUDGET_BREACH = "budget_breach"


class HealthPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class CycleHealthInputs:
    """Every input `evaluate_cycle_health` may consider. Rates are
    `float | None` — `None` means "no data for this dimension this cycle"
    (e.g. no Claude roles were invoked), which this function treats as
    "does not contribute a failure," never as a fabricated 0.0 or 1.0."""

    provider_success_rate: float | None
    evidence_completeness_rate: float | None
    claude_role_success_rate: float | None
    retry_rate: float | None
    retry_exhaustion_rate: float | None
    unsupported_claim_rate: float | None
    output_truncation_rate: float | None
    latency_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    pricing_configured: bool
    paper_reconciliation_mismatch: bool
    duplicate_prevention_violation: bool
    cycle_duration_seconds: float | None
    budget_breached: bool = False


@dataclass(frozen=True)
class HealthPolicyConfig:
    """Thresholds sourced from `config/shadow_operations.yaml`'s `safety.*`
    section (via `ShadowOperationsConfiguration.safety`) plus this module's
    own `policy_version` — no second config file/surface is introduced."""

    policy_version: str
    pause_on_provider_failure_rate: float
    pause_on_retry_exhaustion_rate: float
    pause_on_unsupported_claim_rate: float
    pause_on_reconciliation_mismatch: bool
    pause_on_budget_breach: bool
    max_cycle_duration_seconds: int | None = None

    @classmethod
    def from_shadow_config(cls, config: ShadowOperationsConfiguration, *, policy_version: str = POLICY_VERSION) -> "HealthPolicyConfig":
        return cls(
            policy_version=policy_version,
            pause_on_provider_failure_rate=config.safety.pause_on_provider_failure_rate,
            pause_on_retry_exhaustion_rate=config.safety.pause_on_retry_exhaustion_rate,
            pause_on_unsupported_claim_rate=config.safety.pause_on_unsupported_claim_rate,
            pause_on_reconciliation_mismatch=config.safety.pause_on_reconciliation_mismatch,
            pause_on_budget_breach=config.safety.pause_on_budget_breach,
            max_cycle_duration_seconds=config.budgets.max_latency_seconds_per_cycle,
        )


@dataclass(frozen=True)
class HealthResult:
    status: str
    policy_version: str
    reasons: tuple[str, ...]
    triggering_flags: tuple[str, ...]  # which REASON_* constants drove a PAUSE_REQUIRED verdict

    def __post_init__(self) -> None:
        if self.status not in HEALTH_STATUSES:
            raise HealthPolicyError(f"health status {self.status!r} is not one of {HEALTH_STATUSES} — fails closed")


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY_ORDER[a] >= _SEVERITY_ORDER[b] else b


def evaluate_cycle_health(inputs: CycleHealthInputs, config: HealthPolicyConfig) -> HealthResult:
    """Pure function: identical inputs always reproduce the identical
    result. No DB/network access; no import of `shadow.pause` at call time
    (module-level import only exists for `apply_health_result` below, this
    function itself never calls anything from it)."""
    status = STATUS_HEALTHY
    reasons: list[str] = []
    triggering_flags: list[str] = []

    def _consider_rate(value: float | None, threshold: float, label: str, reason_flag: str) -> None:
        nonlocal status
        if value is None:
            return
        degraded_threshold = threshold * DEGRADED_FRACTION_OF_PAUSE_THRESHOLD
        if value > threshold:
            reasons.append(f"{label} {value:.3f} > pause threshold {threshold:.3f}")
            triggering_flags.append(reason_flag)
            status = _worse(status, STATUS_PAUSE_REQUIRED)
        elif value > degraded_threshold:
            reasons.append(f"{label} {value:.3f} > degraded threshold {degraded_threshold:.3f} (pause threshold {threshold:.3f})")
            status = _worse(status, STATUS_DEGRADED)

    if inputs.provider_success_rate is not None:
        _consider_rate(1.0 - inputs.provider_success_rate, config.pause_on_provider_failure_rate, "provider_failure_rate", REASON_PROVIDER_FAILURE_RATE)
    _consider_rate(inputs.retry_exhaustion_rate, config.pause_on_retry_exhaustion_rate, "retry_exhaustion_rate", REASON_RETRY_EXHAUSTION_RATE)
    _consider_rate(inputs.unsupported_claim_rate, config.pause_on_unsupported_claim_rate, "unsupported_claim_rate", REASON_UNSUPPORTED_CLAIM_RATE)

    if inputs.paper_reconciliation_mismatch:
        reasons.append("paper_reconciliation_mismatch is True")
        if config.pause_on_reconciliation_mismatch:
            triggering_flags.append(REASON_RECONCILIATION_MISMATCH)
            status = _worse(status, STATUS_PAUSE_REQUIRED)
        else:
            status = _worse(status, STATUS_PAUSE_RECOMMENDED)

    if inputs.duplicate_prevention_violation:
        reasons.append("duplicate_prevention_violation is True — lease/idempotency guarantee was violated")
        status = _worse(status, STATUS_PAUSE_REQUIRED)
        triggering_flags.append(REASON_RECONCILIATION_MISMATCH)

    if inputs.budget_breached:
        reasons.append("budget_breached is True")
        if config.pause_on_budget_breach:
            triggering_flags.append(REASON_BUDGET_BREACH)
            status = _worse(status, STATUS_PAUSE_REQUIRED)
        else:
            status = _worse(status, STATUS_PAUSE_RECOMMENDED)

    if inputs.output_truncation_rate is not None and inputs.output_truncation_rate > 0:
        reasons.append(f"output_truncation_rate {inputs.output_truncation_rate:.3f} > 0")
        status = _worse(status, STATUS_DEGRADED if inputs.output_truncation_rate <= 0.25 else STATUS_PAUSE_RECOMMENDED)

    if inputs.cost_usd is not None and inputs.cost_usd > 0 and not inputs.pricing_configured:
        # Real cost accrued but pricing unavailable to explain/verify it —
        # unknown-cost recurring Claude operation is exactly the condition
        # ADR 0005 Decision 5 structurally blocks pre-cycle; this branch is
        # the defense-in-depth *post*-cycle detection of the same condition.
        reasons.append("cost_usd is positive but pricing_configured is False — cost cannot be verified")
        status = _worse(status, STATUS_PAUSE_RECOMMENDED)

    if (
        config.max_cycle_duration_seconds is not None
        and inputs.cycle_duration_seconds is not None
        and inputs.cycle_duration_seconds > config.max_cycle_duration_seconds
    ):
        reasons.append(
            f"cycle_duration_seconds {inputs.cycle_duration_seconds:.0f} > max {config.max_cycle_duration_seconds}"
        )
        status = _worse(status, STATUS_PAUSE_RECOMMENDED)

    if not reasons:
        reasons.append("all monitored rates within configured thresholds")

    return HealthResult(
        status=status, policy_version=config.policy_version, reasons=tuple(reasons),
        triggering_flags=tuple(dict.fromkeys(triggering_flags)),
    )


_FLAG_TO_PAUSE_TARGET = {
    REASON_PROVIDER_FAILURE_RATE: pause_mod.STATE_PAUSED_PROVIDER_HEALTH,
    REASON_RETRY_EXHAUSTION_RATE: pause_mod.STATE_PAUSED_RESEARCH_QUALITY,
    REASON_UNSUPPORTED_CLAIM_RATE: pause_mod.STATE_PAUSED_RESEARCH_QUALITY,
    REASON_RECONCILIATION_MISMATCH: pause_mod.STATE_PAUSED_RECONCILIATION,
    REASON_BUDGET_BREACH: pause_mod.STATE_PAUSED_BUDGET,
}

_FLAG_TO_CONFIG_ATTR = {
    REASON_PROVIDER_FAILURE_RATE: "pause_on_provider_failure_rate",
    REASON_RETRY_EXHAUSTION_RATE: "pause_on_retry_exhaustion_rate",
    REASON_UNSUPPORTED_CLAIM_RATE: "pause_on_unsupported_claim_rate",
    REASON_RECONCILIATION_MISMATCH: "pause_on_reconciliation_mismatch",
    REASON_BUDGET_BREACH: "pause_on_budget_breach",
}


def apply_health_result(
    conn, health_result: HealthResult, config: HealthPolicyConfig, clock: Clock,
    source: str = pause_mod.SOURCE_AUTOMATIC_HEALTH_RULE,
) -> pause_mod.PauseState | None:
    """The ONLY function in this module that calls
    `shadow/pause.py::request_pause(...)`. Only acts when
    `health_result.status == PAUSE_REQUIRED` AND at least one of the
    triggering reasons has its corresponding `safety.pause_on_*` flag
    configured true. Returns the new `PauseState` if a pause was requested,
    or `None` if no action was taken (status not PAUSE_REQUIRED, no
    triggering flag configured to auto-pause, or the system is already
    KILLED — `request_pause` itself refuses to downgrade KILLED).

    Never calls `resume()` — no automatic unpause exists anywhere in this
    module (docs/milestone-7.md Step 22: "no automatic unpause after a
    critical event")."""
    if health_result.status != STATUS_PAUSE_REQUIRED:
        return None

    # Rate-flag booleans are on `config` as the actual float/bool thresholds
    # rather than a second boolean map — re-derive "is this flag's
    # corresponding pause_on_* configured true" from the same config object
    # `evaluate_cycle_health` already used to produce `triggering_flags`.
    boolish_flags = {
        REASON_PROVIDER_FAILURE_RATE: config.pause_on_provider_failure_rate > 0,
        REASON_RETRY_EXHAUSTION_RATE: config.pause_on_retry_exhaustion_rate > 0,
        REASON_UNSUPPORTED_CLAIM_RATE: config.pause_on_unsupported_claim_rate > 0,
        REASON_RECONCILIATION_MISMATCH: config.pause_on_reconciliation_mismatch,
        REASON_BUDGET_BREACH: config.pause_on_budget_breach,
    }

    active_flags = [flag for flag in health_result.triggering_flags if boolish_flags.get(flag, False)]
    if not active_flags:
        return None

    target_state = _FLAG_TO_PAUSE_TARGET[active_flags[0]]
    reason = f"automatic health rule ({config.policy_version}): " + "; ".join(health_result.reasons)
    try:
        return pause_mod.request_pause(
            conn, reason, source, target_state=target_state, clock=clock,
        )
    except pause_mod.PauseStateError:
        # System is already KILLED (or another structural refusal) —
        # `request_pause` fails closed on its own; this function does not
        # retry or override that, it simply reports "no action taken."
        return None
