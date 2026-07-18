"""Deterministic operational health rules (docs/milestone-7.md Step 22, ADR
0005 Decision 7; field-level diagnostics added docs/milestone-7.2.md Part 2).
`evaluate_cycle_health` is a PURE function — cycle-level counters in, one of
`HEALTHY` / `DEGRADED` / `PAUSE_RECOMMENDED` / `PAUSE_REQUIRED` out, versioned
via `policy_version`, no DB/network access inside it, mirroring
`research/promotion.py::evaluate_promotion`'s shape exactly (exhaustive
status enum that raises on unrecognized value, dataclass-of-reasons result).
It additionally returns one `HealthCheckResult` per evaluated dimension
(`HealthResult.checks`), in deterministic order, so every input/threshold/
comparison that fed the summary verdict is independently explainable and
persistable — this is what `docs/milestone-7.2.md` calls "field-level health
diagnostics."

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

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable

from . import pause as pause_mod
from .config import ShadowOperationsConfiguration

Clock = Callable[[], datetime]

POLICY_VERSION = "health/v2"

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
REASON_PROVIDER_STRUCTURAL_ERROR = "provider_structural_error"
# docs/milestone-7.2.md Part 6/9: a duplicate-prevention (lease/idempotency)
# violation is a structural safety-guarantee break, not a configurable rate —
# it previously reused REASON_RECONCILIATION_MISMATCH's flag, which meant an
# operator setting safety.pause_on_reconciliation_mismatch=false would also
# (almost certainly unintentionally) suppress auto-pause on a duplicate-
# prevention violation. Given its own, dedicated, ALWAYS-enabled flag instead
# (see `apply_health_result`'s `boolish_flags`) — this is a
# CONFIGURATION-MAPPING correction, not a new pause dimension: the summary
# `evaluate_cycle_health` status/verdict for this input is unchanged
# (still unconditionally PAUSE_REQUIRED when True), only which config flag
# gates the *pause action* changes.
REASON_DUPLICATE_PREVENTION_VIOLATION = "duplicate_prevention_violation"
# Milestone 12.1.1 Item 7: model-provider (Codex/Claude Code/Anthropic)
# health — independent from REASON_PROVIDER_FAILURE_RATE (evidence
# providers).
REASON_MODEL_PROVIDER_FAILURE_RATE = "model_provider_failure_rate"

# --- Field-level check status vocabulary (docs/milestone-7.2.md Part 2) -------
CHECK_STATUS_PASS = "PASS"
CHECK_STATUS_WARNING = "WARNING"
CHECK_STATUS_FAIL = "FAIL"
CHECK_STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
CHECK_STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

CHECK_STATUSES = (
    CHECK_STATUS_PASS, CHECK_STATUS_WARNING, CHECK_STATUS_FAIL, CHECK_STATUS_NOT_APPLICABLE,
    CHECK_STATUS_INSUFFICIENT_DATA,
)

# Deterministic, stable check-name ordering — the exact sequence
# `evaluate_cycle_health` builds `checks` in, and the sequence persisted /
# returned by the CLI (docs/milestone-7.2.md Part 2: "deterministic ordering").
CHECK_NAME_PROVIDER_FAILURE_RATE = "provider_failure_rate"
CHECK_NAME_EVIDENCE_COMPLETENESS_RATE = "evidence_completeness_rate"
CHECK_NAME_CLAUDE_ROLE_SUCCESS_RATE = "claude_role_success_rate"
CHECK_NAME_RETRY_RATE = "retry_rate"
CHECK_NAME_RETRY_EXHAUSTION_RATE = "retry_exhaustion_rate"
CHECK_NAME_UNSUPPORTED_CLAIM_RATE = "unsupported_claim_rate"
CHECK_NAME_OUTPUT_TRUNCATION_RATE = "output_truncation_rate"
CHECK_NAME_INPUT_TOKENS = "input_tokens"
CHECK_NAME_OUTPUT_TOKENS = "output_tokens"
CHECK_NAME_LATENCY_SECONDS = "latency_seconds"
CHECK_NAME_COST_PRICING = "cost_usd_pricing"
CHECK_NAME_PRICING_CONFIGURED = "pricing_configured"
CHECK_NAME_PAPER_RECONCILIATION_MISMATCH = "paper_reconciliation_mismatch"
CHECK_NAME_DUPLICATE_PREVENTION_VIOLATION = "duplicate_prevention_violation"
CHECK_NAME_CYCLE_DURATION_SECONDS = "cycle_duration_seconds"
CHECK_NAME_BUDGET_BREACHED = "budget_breached"
# Milestone 12.1.1 Item 7: independent model-provider (Codex/Claude
# Code/Anthropic) health — distinct from CHECK_NAME_PROVIDER_FAILURE_RATE,
# which covers evidence providers (SEC/Alpaca/Reddit), never the model
# provider itself.
CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE = "model_provider_failure_rate"

CHECK_NAMES_IN_ORDER = (
    CHECK_NAME_PROVIDER_FAILURE_RATE, CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE, CHECK_NAME_EVIDENCE_COMPLETENESS_RATE,
    CHECK_NAME_CLAUDE_ROLE_SUCCESS_RATE,
    CHECK_NAME_RETRY_RATE, CHECK_NAME_RETRY_EXHAUSTION_RATE, CHECK_NAME_UNSUPPORTED_CLAIM_RATE,
    CHECK_NAME_OUTPUT_TRUNCATION_RATE, CHECK_NAME_INPUT_TOKENS, CHECK_NAME_OUTPUT_TOKENS,
    CHECK_NAME_LATENCY_SECONDS, CHECK_NAME_COST_PRICING, CHECK_NAME_PRICING_CONFIGURED,
    CHECK_NAME_PAPER_RECONCILIATION_MISMATCH, CHECK_NAME_DUPLICATE_PREVENTION_VIOLATION,
    CHECK_NAME_CYCLE_DURATION_SECONDS, CHECK_NAME_BUDGET_BREACHED,
)


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
    # Milestone 11.3 Part 23: sample-size context for provider_failure_rate.
    # `None` means "caller did not supply a count" (treated as unlimited —
    # preserves pre-Part-23 behavior for any caller not yet updated).
    # `provider_severe_error=True` bypasses the sample floor entirely — an
    # explicit, unambiguous provider outage signal (e.g. a connection
    # refused / auth-rejected on every attempt) must still pause
    # immediately even from a single request, distinct from "a noisy 1-of-1
    # rate looks bad but might just be one flaky call."
    provider_request_count: int | None = None
    provider_severe_error: bool = False
    provider_health_mode: str = "PRODUCTION"
    provider_required_categories: tuple[str, ...] = ()
    provider_required_providers: tuple[str, ...] = ()
    provider_observed_providers: tuple[str, ...] = ()
    provider_missing_required_providers: tuple[str, ...] = ()
    provider_missing_required_categories: tuple[str, ...] = ()
    # Milestone 12.1 Item 6: categories whose OWN required-provider requests
    # failed that category's own success-rate floor — computed independently
    # per category (never diluted by another provider's success) in
    # `evidence_providers/health.py::evaluate_required_category_health`.
    provider_unhealthy_required_categories: tuple[str, ...] = ()
    # Milestone 12.1.1 Item 5: required categories that are individually
    # INSUFFICIENT_DATA (own request count below own sample floor) — never
    # merged into `provider_unhealthy_required_categories` (that tuple
    # drives FAIL; this one must drive INSUFFICIENT_DATA instead, never PASS).
    provider_insufficient_required_categories: tuple[str, ...] = ()
    # Milestone 12.1.1 Item 7: independent model-provider health, sourced
    # from persisted `research_attempts` for the current scheduler run
    # (`shadow/model_provider_health.py`), never from evidence-provider
    # request rows. `model_provider_structural_failure=True` bypasses the
    # sample floor exactly like `provider_severe_error` does for the
    # evidence-provider dimension — an unambiguous structural failure (auth,
    # quota, unsupported version/model, invalid config, contract rejection,
    # missing usage metadata) must pause immediately, never wait for a
    # hysteresis streak.
    model_provider_success_rate: float | None = None
    model_provider_request_count: int | None = None
    model_provider_structural_failure: bool = False
    provider_per_provider_metrics: dict = field(default_factory=dict)
    provider_severe_error_categories: tuple[str, ...] = ()
    provider_policy_version: str = ""
    provider_policy_hash: str = ""


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
    # Milestone 11.3 Part 23: below this many provider requests this cycle,
    # provider_failure_rate is INSUFFICIENT_DATA rather than a computed
    # pass/fail/degraded rate — a 1-of-1 failure is not distinguishable from
    # noise. Default of 1 preserves prior behavior (every non-empty sample
    # was evaluated) for any config that hasn't set this explicitly.
    minimum_requests_for_failure_rate: int = 1
    # Milestone 12.1.1 Item 7: independent model-provider threshold/sample
    # floor — deliberately separate fields from the evidence-provider ones
    # above so a change to one never silently retunes the other.
    pause_on_model_provider_failure_rate: float = 0.5
    minimum_requests_for_model_provider_failure_rate: int = 1

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
            minimum_requests_for_failure_rate=config.safety.minimum_requests_for_failure_rate,
            pause_on_model_provider_failure_rate=config.safety.pause_on_model_provider_failure_rate,
            minimum_requests_for_model_provider_failure_rate=(
                config.safety.minimum_requests_for_model_provider_failure_rate
            ),
        )


def _fmt(value: object) -> str | None:
    """Stable, secret-free stringification for a check's `input_value`/
    `threshold_value` — never raw model content, never a container that could
    hide a secret (docs/milestone-7.2.md Part 2: "no secrets; no raw model
    content")."""
    if value is None:
        return None
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


@dataclass(frozen=True)
class HealthCheckResult:
    """One fully-explainable evaluated dimension (docs/milestone-7.2.md Part
    2). `applicable=False` means the current health policy has no pause
    threshold for this dimension at all (it is captured/persisted for
    observability — e.g. feeding `shadow/readiness.py` — but does not itself
    drive `HealthResult.status`); such a check's `status` is always
    `NOT_APPLICABLE`, never a fabricated PASS. `applicable=True` with a
    `None` `input_value` means the data is genuinely missing this cycle
    (`status=INSUFFICIENT_DATA`), never silently treated as zero."""

    check_name: str
    status: str
    input_value: str | None
    input_unit: str | None
    threshold_value: str | None
    threshold_unit: str | None
    comparison: str
    applicable: bool
    pause_flag_enabled: bool
    reason: str

    def __post_init__(self) -> None:
        if self.status not in CHECK_STATUSES:
            raise HealthPolicyError(f"check status {self.status!r} is not one of {CHECK_STATUSES} — fails closed")
        if self.check_name not in CHECK_NAMES_IN_ORDER:
            raise HealthPolicyError(f"check_name {self.check_name!r} is not one of {CHECK_NAMES_IN_ORDER} — fails closed")


@dataclass(frozen=True)
class HealthResult:
    status: str
    policy_version: str
    reasons: tuple[str, ...]
    triggering_flags: tuple[str, ...]  # which REASON_* constants drove a PAUSE_REQUIRED verdict
    checks: tuple[HealthCheckResult, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in HEALTH_STATUSES:
            raise HealthPolicyError(f"health status {self.status!r} is not one of {HEALTH_STATUSES} — fails closed")


# Backwards/forwards-compatible alias — docs/milestone-7.2.md's own spec
# names this type `CycleHealthResult`; this module keeps the pre-existing
# `HealthResult` name (used throughout `shadow/scheduler.py` and the existing
# test suite) as the single implementation and exposes this alias so either
# name resolves to the identical class ("use existing conventions where
# possible").
CycleHealthResult = HealthResult


@dataclass(frozen=True)
class EffectiveHealthDecision:
    single_cycle_status: str
    hysteresis_status: str
    effective_status: str
    immediate_pause: bool
    reasons: tuple[str, ...]
    triggering_flags: tuple[str, ...]
    policy_version: str

    @property
    def status(self) -> str:
        return self.effective_status


def provider_health_check(result: HealthResult) -> HealthCheckResult:
    return next(check for check in result.checks if check.check_name == CHECK_NAME_PROVIDER_FAILURE_RATE)


def provider_health_is_qualified(result: HealthResult) -> bool:
    return provider_health_check(result).status in (
        CHECK_STATUS_PASS, CHECK_STATUS_WARNING, CHECK_STATUS_FAIL,
    )


# --- Milestone 12.1 Item 5: dimension-specific hysteresis inputs ------------
#
# `evaluate_cycle_health` already produces one independent `HealthCheckResult`
# per dimension (docs/milestone-7.2.md Part 2). The bug this fixes is
# entirely downstream, in how the scheduler previously fed persistent
# hysteresis (`shadow/health_hysteresis.py`): ONE global `qualified` boolean
# derived only from the evidence-provider check, applied to a SINGLE
# hysteresis scope covering every dimension's status. An insufficient
# evidence-provider sample therefore silently suppressed a genuinely FAILing
# retry-exhaustion or unsupported-claim rate for that entire cycle.
#
# These helpers let the scheduler evaluate persistent hysteresis
# independently per rate-based dimension — reusing the existing per-`scope`
# hysteresis engine in `health_hysteresis.py` (no schema change needed, that
# engine already keys state/evaluation history by `scope`) rather than
# collapsing every dimension into one call.
DIMENSION_EVIDENCE_PROVIDER_FAILURE = "EVIDENCE_PROVIDER_FAILURE"
DIMENSION_RETRY_EXHAUSTION = "RETRY_EXHAUSTION"
DIMENSION_UNSUPPORTED_CLAIMS = "UNSUPPORTED_CLAIMS"
# Milestone 12.1.1 Item 7: independent of DIMENSION_EVIDENCE_PROVIDER_FAILURE
# — its own persistent hysteresis scope/streak, sourced from
# `research_attempts`, never evidence-provider success clearing this streak
# or vice versa.
DIMENSION_MODEL_PROVIDER_FAILURE = "MODEL_PROVIDER_FAILURE"

# Rate-based dimensions with their own persistent hysteresis scope. Structural
# dimensions (reconciliation, duplicate-prevention, budget breach) remain
# immediate — see `combine_effective_health_decision`'s `structural_flags` —
# and are deliberately NOT included here.
HYSTERESIS_DIMENSIONS = (
    (DIMENSION_EVIDENCE_PROVIDER_FAILURE, CHECK_NAME_PROVIDER_FAILURE_RATE),
    (DIMENSION_MODEL_PROVIDER_FAILURE, CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE),
    (DIMENSION_RETRY_EXHAUSTION, CHECK_NAME_RETRY_EXHAUSTION_RATE),
    (DIMENSION_UNSUPPORTED_CLAIMS, CHECK_NAME_UNSUPPORTED_CLAIM_RATE),
)


def check_by_name(result: HealthResult, check_name: str) -> HealthCheckResult:
    return next(check for check in result.checks if check.check_name == check_name)


def dimension_is_qualified(check: HealthCheckResult) -> bool:
    """A dimension is qualified for hysteresis counting only when its own
    check this cycle actually drew a real conclusion — `PASS`, `WARNING`, or
    `FAIL` — evaluated per-dimension, never inherited from a different
    dimension's sample size (Milestone 12.1.1 Item 3: an allowlist, not a
    single-value denylist, so `NOT_APPLICABLE` — a fixture-only/deterministic
    cycle where the check does not even apply — and `INSUFFICIENT_DATA` both
    move neither the failure nor the recovery streak, and any future
    non-conclusive status added to `CHECK_STATUSES` fails closed to
    unqualified by default instead of silently counting as healthy)."""
    return check.status in (CHECK_STATUS_PASS, CHECK_STATUS_WARNING, CHECK_STATUS_FAIL)


def dimension_cycle_status(check: HealthCheckResult) -> str:
    """This dimension's own single-cycle status, independent of every other
    dimension's worst-of-all `HealthResult.status`."""
    if check.status == CHECK_STATUS_FAIL:
        return STATUS_PAUSE_REQUIRED
    if check.status == CHECK_STATUS_WARNING:
        return STATUS_DEGRADED
    # PASS or INSUFFICIENT_DATA — the latter is never counted (see
    # `dimension_is_qualified`), so HEALTHY is a safe, unused-when-unqualified
    # placeholder rather than a fabricated verdict.
    return STATUS_HEALTHY


def worst_health_status(statuses: "tuple[str, ...]") -> str:
    """Public, variadic form of the module's own severity ordering — the
    overall hysteresis status the scheduler feeds into
    `combine_effective_health_decision` is the worst of every dimension's
    independently-computed hysteresis decision."""
    if not statuses:
        return STATUS_HEALTHY
    worst = statuses[0]
    for status in statuses[1:]:
        worst = _worse(worst, status)
    return worst


def combine_effective_health_decision(
    single_cycle: HealthResult, hysteresis_status: str, *, provider_severe_categories: tuple[str, ...] = (),
) -> EffectiveHealthDecision:
    structural_flags = {
        REASON_RECONCILIATION_MISMATCH, REASON_DUPLICATE_PREVENTION_VIOLATION, REASON_BUDGET_BREACH,
    }
    immediate_pause = bool(structural_flags.intersection(single_cycle.triggering_flags)) or bool(
        provider_severe_categories
    )
    if immediate_pause:
        effective_status = STATUS_PAUSE_REQUIRED
    elif single_cycle.status == STATUS_PAUSE_REQUIRED:
        # Non-structural rate failures are governed by persistent hysteresis.
        effective_status = hysteresis_status
    else:
        # Preserve alert-only single-cycle warnings/recommendations from
        # non-provider dimensions without allowing them to bypass pausing.
        effective_status = _worse(single_cycle.status, hysteresis_status)
    flags = list(single_cycle.triggering_flags)
    reasons = list(single_cycle.reasons)
    if provider_severe_categories:
        flags.append(REASON_PROVIDER_STRUCTURAL_ERROR)
        reasons.append(
            "structural provider categories require immediate pause: " + ", ".join(provider_severe_categories)
        )
    reasons.append(
        f"single_cycle={single_cycle.status}; hysteresis={hysteresis_status}; effective={effective_status}"
    )
    return EffectiveHealthDecision(
        single_cycle_status=single_cycle.status, hysteresis_status=hysteresis_status,
        effective_status=effective_status, immediate_pause=immediate_pause,
        reasons=tuple(reasons), triggering_flags=tuple(dict.fromkeys(flags)),
        policy_version=single_cycle.policy_version,
    )


def immediate_pause_required(
    single_cycle: HealthResult, *, provider_severe_categories: tuple[str, ...] = (),
) -> bool:
    structural_flags = {
        REASON_RECONCILIATION_MISMATCH, REASON_DUPLICATE_PREVENTION_VIOLATION, REASON_BUDGET_BREACH,
    }
    return bool(structural_flags.intersection(single_cycle.triggering_flags)) or bool(provider_severe_categories)


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY_ORDER[a] >= _SEVERITY_ORDER[b] else b


def _rate_check(
    *, check_name: str, value: float | None, threshold: float, label: str, applicable: bool = True,
    sample_size: int | None = None, minimum_sample_size: int | None = None, sample_floor_bypassed: bool = False,
) -> tuple[HealthCheckResult, str | None, str | None]:
    """Builds one rate-vs-pause-threshold `HealthCheckResult`, PLUS returns
    `(reason_text_or_None, degraded_reason_text_or_None)` so
    `evaluate_cycle_health` can fold the exact same evaluation into its
    existing `status`/`reasons`/`triggering_flags` computation without
    duplicating the comparison logic. `pause_flag_enabled` reflects whether
    the caller's `threshold > 0` (a `0.0` threshold means "never auto-pause
    for this dimension," matching `apply_health_result`'s own
    `boolish_flags` interpretation).

    Milestone 11.3 Part 23 sample floor: when `sample_size` and
    `minimum_sample_size` are both given and `sample_size <
    minimum_sample_size` (and `sample_floor_bypassed` is not set — e.g. an
    explicit severe provider error), the check is INSUFFICIENT_DATA rather
    than a computed PASS/WARNING/FAIL — a small sample's rate is not
    treated as either "healthy" or an automatic outage. `input_value` still
    reports the raw computed rate (not hidden) so it remains observable."""
    pause_flag_enabled = threshold > 0
    degraded_threshold = threshold * DEGRADED_FRACTION_OF_PAUSE_THRESHOLD
    if value is None:
        check = HealthCheckResult(
            check_name=check_name, status=CHECK_STATUS_INSUFFICIENT_DATA, input_value=None, input_unit="fraction",
            threshold_value=_fmt(threshold), threshold_unit="fraction", comparison=">",
            applicable=applicable, pause_flag_enabled=pause_flag_enabled,
            reason=f"no data this cycle for {label} — not fabricated as 0.0",
        )
        return check, None, None
    below_sample_floor = (
        not sample_floor_bypassed and sample_size is not None and minimum_sample_size is not None
        and sample_size < minimum_sample_size
    )
    if below_sample_floor:
        check = HealthCheckResult(
            check_name=check_name, status=CHECK_STATUS_INSUFFICIENT_DATA, input_value=_fmt(value), input_unit="fraction",
            threshold_value=_fmt(threshold), threshold_unit="fraction", comparison=">",
            applicable=applicable, pause_flag_enabled=pause_flag_enabled,
            reason=(
                f"{label} sample size {sample_size} < minimum {minimum_sample_size} required for a failure-rate "
                "verdict — not treated as pass or automatic pause"
            ),
        )
        return check, None, None
    if value > threshold:
        reason_text = f"{label} {value:.3f} > pause threshold {threshold:.3f}"
        check = HealthCheckResult(
            check_name=check_name, status=CHECK_STATUS_FAIL, input_value=_fmt(value), input_unit="fraction",
            threshold_value=_fmt(threshold), threshold_unit="fraction", comparison=">",
            applicable=applicable, pause_flag_enabled=pause_flag_enabled, reason=reason_text,
        )
        return check, reason_text, None
    if value > degraded_threshold:
        degraded_text = (
            f"{label} {value:.3f} > degraded threshold {degraded_threshold:.3f} (pause threshold {threshold:.3f})"
        )
        check = HealthCheckResult(
            check_name=check_name, status=CHECK_STATUS_WARNING, input_value=_fmt(value), input_unit="fraction",
            threshold_value=_fmt(threshold), threshold_unit="fraction", comparison=">",
            applicable=applicable, pause_flag_enabled=pause_flag_enabled, reason=degraded_text,
        )
        return check, None, degraded_text
    check = HealthCheckResult(
        check_name=check_name, status=CHECK_STATUS_PASS, input_value=_fmt(value), input_unit="fraction",
        threshold_value=_fmt(threshold), threshold_unit="fraction", comparison=">",
        applicable=applicable, pause_flag_enabled=pause_flag_enabled,
        reason=f"{label} {value:.3f} within configured thresholds",
    )
    return check, None, None


def _observational_check(
    *, check_name: str, value: object, unit: str | None, reason: str,
) -> HealthCheckResult:
    """A dimension this policy version captures/persists but does not
    threshold (docs/milestone-7.2.md Part 1: NOT_APPLICABLE classification —
    e.g. `evidence_completeness_rate`, raw token/latency sums). Never
    `PASS`/`FAIL` — there is no threshold to pass or fail."""
    return HealthCheckResult(
        check_name=check_name, status=CHECK_STATUS_NOT_APPLICABLE, input_value=_fmt(value), input_unit=unit,
        threshold_value=None, threshold_unit=None, comparison="n/a", applicable=False, pause_flag_enabled=False,
        reason=reason,
    )


def evaluate_cycle_health(inputs: CycleHealthInputs, config: HealthPolicyConfig) -> HealthResult:
    """Pure function: identical inputs always reproduce the identical
    result. No DB/network access; no import of `shadow.pause` at call time
    (module-level import only exists for `apply_health_result` below, this
    function itself never calls anything from it).

    Returns `HealthResult.checks`: exactly one `HealthCheckResult` per
    dimension named in docs/milestone-7.2.md Part 1, in `CHECK_NAMES_IN_ORDER`
    (deterministic ordering, docs/milestone-7.2.md Part 2)."""
    status = STATUS_HEALTHY
    reasons: list[str] = []
    triggering_flags: list[str] = []
    checks: dict[str, HealthCheckResult] = {}

    # --- provider_failure_rate (thresholded: safety.pause_on_provider_failure_rate) ---
    provider_failure_rate = 1.0 - inputs.provider_success_rate if inputs.provider_success_rate is not None else None
    check, fail_reason, degraded_reason = _rate_check(
        check_name=CHECK_NAME_PROVIDER_FAILURE_RATE, value=provider_failure_rate,
        threshold=config.pause_on_provider_failure_rate, label="provider_failure_rate",
        sample_size=inputs.provider_request_count, minimum_sample_size=config.minimum_requests_for_failure_rate,
        sample_floor_bypassed=inputs.provider_severe_error,
    )
    if inputs.provider_health_mode == CHECK_STATUS_NOT_APPLICABLE:
        check = HealthCheckResult(
            check_name=CHECK_NAME_PROVIDER_FAILURE_RATE, status=CHECK_STATUS_NOT_APPLICABLE,
            input_value=None, input_unit="fraction", threshold_value=_fmt(config.pause_on_provider_failure_rate),
            threshold_unit="fraction", comparison="n/a", applicable=False, pause_flag_enabled=False,
            reason="provider telemetry is explicitly not applicable for this fixture-only cycle",
        )
        fail_reason = degraded_reason = None
    elif (
        inputs.provider_missing_required_providers or inputs.provider_missing_required_categories
        or inputs.provider_unhealthy_required_categories
    ):
        # Milestone 12.1 Item 6: a required category present but failing its
        # OWN success-rate floor (`provider_unhealthy_required_categories`)
        # must FAIL exactly like an outright-missing required category —
        # never diluted by an unrelated required/optional provider's
        # success in the aggregate `provider_failure_rate` above.
        missing = (
            tuple(inputs.provider_missing_required_categories) + tuple(inputs.provider_missing_required_providers)
            + tuple(inputs.provider_unhealthy_required_categories)
        )
        fail_reason = "required provider coverage missing or unhealthy: " + ", ".join(missing)
        degraded_reason = None
        check = HealthCheckResult(
            check_name=CHECK_NAME_PROVIDER_FAILURE_RATE, status=CHECK_STATUS_FAIL,
            input_value=_fmt(provider_failure_rate), input_unit="fraction",
            threshold_value=_fmt(config.pause_on_provider_failure_rate), threshold_unit="fraction",
            comparison="required coverage", applicable=True,
            pause_flag_enabled=config.pause_on_provider_failure_rate > 0, reason=fail_reason,
        )
    elif inputs.provider_insufficient_required_categories:
        # Milestone 12.1.1 Item 5: a required category that made fewer
        # requests than ITS OWN sample floor (e.g. market_data floor=3,
        # observed=1) must never be masked by a passing aggregate
        # `provider_failure_rate` computed across every provider (SEC's own
        # healthy volume cannot hide Alpaca's own insufficient sample) —
        # only reached once MISSING/FAIL required categories are ruled out
        # above, matching the policy "MISSING/FAIL -> FAIL,
        # INSUFFICIENT_DATA -> INSUFFICIENT_DATA, else evaluate the rate".
        insufficient_reason = (
            "required categories below their own sample floor: "
            + ", ".join(inputs.provider_insufficient_required_categories)
        )
        fail_reason = degraded_reason = None
        check = HealthCheckResult(
            check_name=CHECK_NAME_PROVIDER_FAILURE_RATE, status=CHECK_STATUS_INSUFFICIENT_DATA,
            input_value=_fmt(provider_failure_rate), input_unit="fraction",
            threshold_value=_fmt(config.pause_on_provider_failure_rate), threshold_unit="fraction",
            comparison="required coverage", applicable=True, pause_flag_enabled=False, reason=insufficient_reason,
        )
    checks[CHECK_NAME_PROVIDER_FAILURE_RATE] = check
    if fail_reason:
        reasons.append(fail_reason)
        triggering_flags.append(REASON_PROVIDER_FAILURE_RATE)
        status = _worse(status, STATUS_PAUSE_REQUIRED)
    elif degraded_reason:
        reasons.append(degraded_reason)
        status = _worse(status, STATUS_DEGRADED)

    # --- model_provider_failure_rate (Milestone 12.1.1 Item 7): independent of
    # the evidence-provider dimension above — a healthy SEC/Alpaca cycle says
    # nothing about whether Codex/Claude Code/Anthropic itself is healthy. ---
    model_provider_failure_rate = (
        1.0 - inputs.model_provider_success_rate if inputs.model_provider_success_rate is not None else None
    )
    model_check, model_fail_reason, model_degraded_reason = _rate_check(
        check_name=CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE, value=model_provider_failure_rate,
        threshold=config.pause_on_model_provider_failure_rate, label="model_provider_failure_rate",
        sample_size=inputs.model_provider_request_count,
        minimum_sample_size=config.minimum_requests_for_model_provider_failure_rate,
        sample_floor_bypassed=inputs.model_provider_structural_failure,
    )
    if inputs.model_provider_structural_failure:
        model_fail_reason = "model-provider structural failure requires an immediate pause"
        model_degraded_reason = None
        model_check = HealthCheckResult(
            check_name=CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE, status=CHECK_STATUS_FAIL,
            input_value=_fmt(model_provider_failure_rate), input_unit="fraction",
            threshold_value=_fmt(config.pause_on_model_provider_failure_rate), threshold_unit="fraction",
            comparison="structural failure", applicable=True, pause_flag_enabled=True, reason=model_fail_reason,
        )
    checks[CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE] = model_check
    if model_fail_reason:
        reasons.append(model_fail_reason)
        triggering_flags.append(REASON_MODEL_PROVIDER_FAILURE_RATE)
        status = _worse(status, STATUS_PAUSE_REQUIRED)
    elif model_degraded_reason:
        reasons.append(model_degraded_reason)
        status = _worse(status, STATUS_DEGRADED)

    # --- evidence_completeness_rate: captured, not thresholded by this policy ---
    checks[CHECK_NAME_EVIDENCE_COMPLETENESS_RATE] = _observational_check(
        check_name=CHECK_NAME_EVIDENCE_COMPLETENESS_RATE, value=inputs.evidence_completeness_rate, unit="fraction",
        reason=(
            "no configured safety.pause_on_* threshold for this dimension in this policy version — "
            "captured for shadow-readiness only"
            if inputs.evidence_completeness_rate is not None else
            "no data this cycle for evidence_completeness_rate — not fabricated as 0.0"
        ),
    )

    # --- claude_role_success_rate: captured, not thresholded by this policy ---
    checks[CHECK_NAME_CLAUDE_ROLE_SUCCESS_RATE] = _observational_check(
        check_name=CHECK_NAME_CLAUDE_ROLE_SUCCESS_RATE, value=inputs.claude_role_success_rate, unit="fraction",
        reason=(
            "no configured safety.pause_on_* threshold for this dimension in this policy version"
            if inputs.claude_role_success_rate is not None else
            "no data this cycle for claude_role_success_rate — not fabricated as 0.0"
        ),
    )

    # --- retry_rate: captured, not thresholded (only retry_exhaustion_rate is) ---
    checks[CHECK_NAME_RETRY_RATE] = _observational_check(
        check_name=CHECK_NAME_RETRY_RATE, value=inputs.retry_rate, unit="fraction",
        reason=(
            "no configured safety.pause_on_* threshold for this dimension in this policy version"
            if inputs.retry_rate is not None else
            "no data this cycle for retry_rate — not fabricated as 0.0"
        ),
    )

    # --- retry_exhaustion_rate (thresholded: safety.pause_on_retry_exhaustion_rate) ---
    check, fail_reason, degraded_reason = _rate_check(
        check_name=CHECK_NAME_RETRY_EXHAUSTION_RATE, value=inputs.retry_exhaustion_rate,
        threshold=config.pause_on_retry_exhaustion_rate, label="retry_exhaustion_rate",
    )
    checks[CHECK_NAME_RETRY_EXHAUSTION_RATE] = check
    if fail_reason:
        reasons.append(fail_reason)
        triggering_flags.append(REASON_RETRY_EXHAUSTION_RATE)
        status = _worse(status, STATUS_PAUSE_REQUIRED)
    elif degraded_reason:
        reasons.append(degraded_reason)
        status = _worse(status, STATUS_DEGRADED)

    # --- unsupported_claim_rate (thresholded: safety.pause_on_unsupported_claim_rate) ---
    check, fail_reason, degraded_reason = _rate_check(
        check_name=CHECK_NAME_UNSUPPORTED_CLAIM_RATE, value=inputs.unsupported_claim_rate,
        threshold=config.pause_on_unsupported_claim_rate, label="unsupported_claim_rate",
    )
    checks[CHECK_NAME_UNSUPPORTED_CLAIM_RATE] = check
    if fail_reason:
        reasons.append(fail_reason)
        triggering_flags.append(REASON_UNSUPPORTED_CLAIM_RATE)
        status = _worse(status, STATUS_PAUSE_REQUIRED)
    elif degraded_reason:
        reasons.append(degraded_reason)
        status = _worse(status, STATUS_DEGRADED)

    # --- paper_reconciliation_mismatch (thresholded: safety.pause_on_reconciliation_mismatch) ---
    reconciliation_pause_flag = bool(config.pause_on_reconciliation_mismatch)
    if inputs.paper_reconciliation_mismatch:
        reasons.append("paper_reconciliation_mismatch is True")
        if config.pause_on_reconciliation_mismatch:
            triggering_flags.append(REASON_RECONCILIATION_MISMATCH)
            status = _worse(status, STATUS_PAUSE_REQUIRED)
            recon_status, recon_reason = CHECK_STATUS_FAIL, "paper_reconciliation_mismatch is True — pause_on_reconciliation_mismatch enabled"
        else:
            status = _worse(status, STATUS_PAUSE_RECOMMENDED)
            recon_status, recon_reason = CHECK_STATUS_FAIL, "paper_reconciliation_mismatch is True — pause_on_reconciliation_mismatch disabled, recommending only"
    else:
        recon_status, recon_reason = CHECK_STATUS_PASS, "paper_reconciliation_mismatch is False"
    checks[CHECK_NAME_PAPER_RECONCILIATION_MISMATCH] = HealthCheckResult(
        check_name=CHECK_NAME_PAPER_RECONCILIATION_MISMATCH, status=recon_status,
        input_value=_fmt(inputs.paper_reconciliation_mismatch), input_unit="boolean", threshold_value="True",
        threshold_unit="boolean", comparison="==", applicable=True, pause_flag_enabled=reconciliation_pause_flag,
        reason=recon_reason,
    )

    # --- duplicate_prevention_violation (docs/milestone-7.2.md Part 9: dedicated,
    # ALWAYS-enabled flag — see REASON_DUPLICATE_PREVENTION_VIOLATION docstring
    # above for why this no longer reuses REASON_RECONCILIATION_MISMATCH's
    # configurable flag). The summary status this input drives is unchanged:
    # unconditionally PAUSE_REQUIRED when True.
    if inputs.duplicate_prevention_violation:
        reasons.append("duplicate_prevention_violation is True — lease/idempotency guarantee was violated")
        status = _worse(status, STATUS_PAUSE_REQUIRED)
        triggering_flags.append(REASON_DUPLICATE_PREVENTION_VIOLATION)
        dup_status, dup_reason = CHECK_STATUS_FAIL, "duplicate_prevention_violation is True — always pauses, not gated by any configurable flag"
    else:
        dup_status, dup_reason = CHECK_STATUS_PASS, "duplicate_prevention_violation is False"
    checks[CHECK_NAME_DUPLICATE_PREVENTION_VIOLATION] = HealthCheckResult(
        check_name=CHECK_NAME_DUPLICATE_PREVENTION_VIOLATION, status=dup_status,
        input_value=_fmt(inputs.duplicate_prevention_violation), input_unit="boolean", threshold_value="True",
        threshold_unit="boolean", comparison="==", applicable=True, pause_flag_enabled=True, reason=dup_reason,
    )

    # --- budget_breached (thresholded: safety.pause_on_budget_breach) ---
    budget_pause_flag = bool(config.pause_on_budget_breach)
    if inputs.budget_breached:
        reasons.append("budget_breached is True")
        if config.pause_on_budget_breach:
            triggering_flags.append(REASON_BUDGET_BREACH)
            status = _worse(status, STATUS_PAUSE_REQUIRED)
            budget_status, budget_reason = CHECK_STATUS_FAIL, "budget_breached is True — pause_on_budget_breach enabled"
        else:
            status = _worse(status, STATUS_PAUSE_RECOMMENDED)
            budget_status, budget_reason = CHECK_STATUS_FAIL, "budget_breached is True — pause_on_budget_breach disabled, recommending only"
    else:
        budget_status, budget_reason = CHECK_STATUS_PASS, "budget_breached is False"
    checks[CHECK_NAME_BUDGET_BREACHED] = HealthCheckResult(
        check_name=CHECK_NAME_BUDGET_BREACHED, status=budget_status, input_value=_fmt(inputs.budget_breached),
        input_unit="boolean", threshold_value="True", threshold_unit="boolean", comparison="==", applicable=True,
        pause_flag_enabled=budget_pause_flag, reason=budget_reason,
    )

    # --- output_truncation_rate: evaluated (DEGRADED/PAUSE_RECOMMENDED ceiling
    # only — no safety.pause_on_* flag exists for this dimension, so it can
    # never drive PAUSE_REQUIRED) ---
    if inputs.output_truncation_rate is None:
        checks[CHECK_NAME_OUTPUT_TRUNCATION_RATE] = HealthCheckResult(
            check_name=CHECK_NAME_OUTPUT_TRUNCATION_RATE, status=CHECK_STATUS_INSUFFICIENT_DATA, input_value=None,
            input_unit="fraction", threshold_value="0.000", threshold_unit="fraction", comparison=">",
            applicable=True, pause_flag_enabled=False,
            reason="no data this cycle for output_truncation_rate — not fabricated as 0.0",
        )
    elif inputs.output_truncation_rate > 0:
        reasons.append(f"output_truncation_rate {inputs.output_truncation_rate:.3f} > 0")
        if inputs.output_truncation_rate <= 0.25:
            status = _worse(status, STATUS_DEGRADED)
            trunc_status = CHECK_STATUS_WARNING
        else:
            status = _worse(status, STATUS_PAUSE_RECOMMENDED)
            trunc_status = CHECK_STATUS_FAIL
        checks[CHECK_NAME_OUTPUT_TRUNCATION_RATE] = HealthCheckResult(
            check_name=CHECK_NAME_OUTPUT_TRUNCATION_RATE, status=trunc_status,
            input_value=_fmt(inputs.output_truncation_rate), input_unit="fraction", threshold_value="0.000",
            threshold_unit="fraction", comparison=">", applicable=True, pause_flag_enabled=False,
            reason=f"output_truncation_rate {inputs.output_truncation_rate:.3f} > 0 (no pause_on_* flag exists — ceiling is PAUSE_RECOMMENDED)",
        )
    else:
        checks[CHECK_NAME_OUTPUT_TRUNCATION_RATE] = HealthCheckResult(
            check_name=CHECK_NAME_OUTPUT_TRUNCATION_RATE, status=CHECK_STATUS_PASS,
            input_value=_fmt(inputs.output_truncation_rate), input_unit="fraction", threshold_value="0.000",
            threshold_unit="fraction", comparison=">", applicable=True, pause_flag_enabled=False,
            reason="output_truncation_rate is 0.0",
        )

    # --- input_tokens / output_tokens: informational sums, never thresholded ---
    checks[CHECK_NAME_INPUT_TOKENS] = _observational_check(
        check_name=CHECK_NAME_INPUT_TOKENS, value=inputs.input_tokens, unit="tokens",
        reason=(
            "informational only — real, non-fabricated sum of attempt-level input tokens"
            if inputs.input_tokens is not None else "no attempt reported input_tokens this cycle"
        ),
    )
    checks[CHECK_NAME_OUTPUT_TOKENS] = _observational_check(
        check_name=CHECK_NAME_OUTPUT_TOKENS, value=inputs.output_tokens, unit="tokens",
        reason=(
            "informational only — real, non-fabricated sum of attempt-level output tokens"
            if inputs.output_tokens is not None else "no attempt reported output_tokens this cycle"
        ),
    )

    # --- latency_seconds: informational sum of Claude ATTEMPT latency — never
    # compared against any threshold, and never conflated with
    # cycle_duration_seconds (whole scheduler wall-clock time) below.
    checks[CHECK_NAME_LATENCY_SECONDS] = _observational_check(
        check_name=CHECK_NAME_LATENCY_SECONDS, value=inputs.latency_seconds, unit="seconds",
        reason=(
            "informational only — sum of individual Claude ATTEMPT latencies (seconds); "
            "deliberately distinct from cycle_duration_seconds (whole scheduler wall-clock time)"
            if inputs.latency_seconds is not None else "no attempt reported latency this cycle"
        ),
    )

    # --- cost_usd / pricing_configured cross-check (thresholded: real cost
    # accrued but pricing unavailable to explain/verify it) ---
    if inputs.cost_usd is not None and inputs.cost_usd > 0 and not inputs.pricing_configured:
        # Real cost accrued but pricing unavailable to explain/verify it —
        # unknown-cost recurring Claude operation is exactly the condition
        # ADR 0005 Decision 5 structurally blocks pre-cycle; this branch is
        # the defense-in-depth *post*-cycle detection of the same condition.
        reasons.append("cost_usd is positive but pricing_configured is False — cost cannot be verified")
        status = _worse(status, STATUS_PAUSE_RECOMMENDED)
        cost_status = CHECK_STATUS_FAIL
        cost_reason = "cost_usd is positive but pricing_configured is False — cost cannot be verified"
    elif inputs.cost_usd is None:
        cost_status = CHECK_STATUS_INSUFFICIENT_DATA
        cost_reason = "no priced attempt-level usage this cycle — not fabricated as $0"
    else:
        cost_status = CHECK_STATUS_PASS
        cost_reason = "cost_usd is either $0 or pricing_configured is True"
    checks[CHECK_NAME_COST_PRICING] = HealthCheckResult(
        check_name=CHECK_NAME_COST_PRICING, status=cost_status, input_value=_fmt(inputs.cost_usd),
        input_unit="usd", threshold_value="0", threshold_unit="usd", comparison=">", applicable=True,
        pause_flag_enabled=False, reason=cost_reason,
    )
    checks[CHECK_NAME_PRICING_CONFIGURED] = _observational_check(
        check_name=CHECK_NAME_PRICING_CONFIGURED, value=inputs.pricing_configured, unit="boolean",
        reason="informational — folded into the cost_usd_pricing cross-check above, not independently thresholded",
    )

    # --- cycle_duration_seconds (DEGRADED/PAUSE_RECOMMENDED ceiling only — no
    # safety.pause_on_* flag exists for this dimension either) ---
    if config.max_cycle_duration_seconds is None:
        checks[CHECK_NAME_CYCLE_DURATION_SECONDS] = HealthCheckResult(
            check_name=CHECK_NAME_CYCLE_DURATION_SECONDS, status=CHECK_STATUS_NOT_APPLICABLE,
            input_value=_fmt(inputs.cycle_duration_seconds), input_unit="seconds", threshold_value=None,
            threshold_unit="seconds", comparison="n/a", applicable=False, pause_flag_enabled=False,
            reason="no max_cycle_duration_seconds configured in this policy",
        )
    elif inputs.cycle_duration_seconds is None:
        checks[CHECK_NAME_CYCLE_DURATION_SECONDS] = HealthCheckResult(
            check_name=CHECK_NAME_CYCLE_DURATION_SECONDS, status=CHECK_STATUS_INSUFFICIENT_DATA, input_value=None,
            input_unit="seconds", threshold_value=str(config.max_cycle_duration_seconds), threshold_unit="seconds",
            comparison=">", applicable=True, pause_flag_enabled=False,
            reason="no cycle_duration_seconds recorded this cycle",
        )
    elif inputs.cycle_duration_seconds > config.max_cycle_duration_seconds:
        reasons.append(
            f"cycle_duration_seconds {inputs.cycle_duration_seconds:.0f} > max {config.max_cycle_duration_seconds}"
        )
        status = _worse(status, STATUS_PAUSE_RECOMMENDED)
        checks[CHECK_NAME_CYCLE_DURATION_SECONDS] = HealthCheckResult(
            check_name=CHECK_NAME_CYCLE_DURATION_SECONDS, status=CHECK_STATUS_FAIL,
            input_value=_fmt(inputs.cycle_duration_seconds), input_unit="seconds",
            threshold_value=str(config.max_cycle_duration_seconds), threshold_unit="seconds", comparison=">",
            applicable=True, pause_flag_enabled=False,
            reason=(
                f"cycle_duration_seconds {inputs.cycle_duration_seconds:.0f} > max "
                f"{config.max_cycle_duration_seconds} (whole scheduler wall-clock time, not Claude attempt latency; "
                "no pause_on_* flag exists — ceiling is PAUSE_RECOMMENDED)"
            ),
        )
    else:
        checks[CHECK_NAME_CYCLE_DURATION_SECONDS] = HealthCheckResult(
            check_name=CHECK_NAME_CYCLE_DURATION_SECONDS, status=CHECK_STATUS_PASS,
            input_value=_fmt(inputs.cycle_duration_seconds), input_unit="seconds",
            threshold_value=str(config.max_cycle_duration_seconds), threshold_unit="seconds", comparison=">",
            applicable=True, pause_flag_enabled=False,
            reason=f"cycle_duration_seconds {inputs.cycle_duration_seconds:.0f} within max {config.max_cycle_duration_seconds}",
        )

    if not reasons:
        reasons.append("all monitored rates within configured thresholds")

    ordered_checks = tuple(checks[name] for name in CHECK_NAMES_IN_ORDER)

    return HealthResult(
        status=status, policy_version=config.policy_version, reasons=tuple(reasons),
        triggering_flags=tuple(dict.fromkeys(triggering_flags)), checks=ordered_checks,
    )


_FLAG_TO_PAUSE_TARGET = {
    REASON_PROVIDER_FAILURE_RATE: pause_mod.STATE_PAUSED_PROVIDER_HEALTH,
    REASON_MODEL_PROVIDER_FAILURE_RATE: pause_mod.STATE_PAUSED_PROVIDER_HEALTH,
    REASON_RETRY_EXHAUSTION_RATE: pause_mod.STATE_PAUSED_RESEARCH_QUALITY,
    REASON_UNSUPPORTED_CLAIM_RATE: pause_mod.STATE_PAUSED_RESEARCH_QUALITY,
    REASON_RECONCILIATION_MISMATCH: pause_mod.STATE_PAUSED_RECONCILIATION,
    REASON_DUPLICATE_PREVENTION_VIOLATION: pause_mod.STATE_PAUSED_RECONCILIATION,
    REASON_BUDGET_BREACH: pause_mod.STATE_PAUSED_BUDGET,
    REASON_PROVIDER_STRUCTURAL_ERROR: pause_mod.STATE_PAUSED_PROVIDER_HEALTH,
}

_FLAG_TO_CONFIG_ATTR = {
    REASON_PROVIDER_FAILURE_RATE: "pause_on_provider_failure_rate",
    REASON_MODEL_PROVIDER_FAILURE_RATE: "pause_on_model_provider_failure_rate",
    REASON_RETRY_EXHAUSTION_RATE: "pause_on_retry_exhaustion_rate",
    REASON_UNSUPPORTED_CLAIM_RATE: "pause_on_unsupported_claim_rate",
    REASON_RECONCILIATION_MISMATCH: "pause_on_reconciliation_mismatch",
    REASON_BUDGET_BREACH: "pause_on_budget_breach",
    # REASON_DUPLICATE_PREVENTION_VIOLATION intentionally has no config attr —
    # see its dedicated, always-true entry in `apply_health_result`'s
    # `boolish_flags` below.
}


def apply_health_result(
    conn, health_result: HealthResult | EffectiveHealthDecision, config: HealthPolicyConfig, clock: Clock,
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
        REASON_MODEL_PROVIDER_FAILURE_RATE: config.pause_on_model_provider_failure_rate > 0,
        REASON_RETRY_EXHAUSTION_RATE: config.pause_on_retry_exhaustion_rate > 0,
        REASON_UNSUPPORTED_CLAIM_RATE: config.pause_on_unsupported_claim_rate > 0,
        REASON_RECONCILIATION_MISMATCH: config.pause_on_reconciliation_mismatch,
        # docs/milestone-7.2.md Part 9 fix: a duplicate-prevention violation
        # (lease/idempotency guarantee broken) is always eligible to
        # auto-pause — it must never be silently suppressed merely because
        # an operator disabled the (semantically unrelated)
        # pause_on_reconciliation_mismatch rate flag.
        REASON_DUPLICATE_PREVENTION_VIOLATION: True,
        REASON_BUDGET_BREACH: config.pause_on_budget_breach,
        REASON_PROVIDER_STRUCTURAL_ERROR: True,
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
