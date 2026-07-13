"""Deterministic promotion gates for research models/prompts
(docs/milestone-6.md Step 18). A newer model or prompt version never becomes
"preferred" merely by being newer — every threshold here is explicit,
versioned configuration, and the function is a pure mapping from inputs to a
status: identical inputs always reproduce the identical decision.

This module has **no code path that returns or implies a live-trading
status** — the status enum below is exhaustive and none of its members
authorize live execution; `PromotionGateConfig.allow_live_promotion` exists
only so config loading can assert it is `False` (docs/milestone-6.md:
"live promotion remains false").
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
STATUS_REJECTED = "REJECTED"
STATUS_SHADOW_ONLY = "SHADOW_ONLY"
STATUS_ELIGIBLE_FOR_PAPER = "ELIGIBLE_FOR_PAPER"
STATUS_PREFERRED_FOR_PAPER = "PREFERRED_FOR_PAPER"
STATUS_ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"

PROMOTION_STATUSES = (
    STATUS_INSUFFICIENT_DATA, STATUS_REJECTED, STATUS_SHADOW_ONLY,
    STATUS_ELIGIBLE_FOR_PAPER, STATUS_PREFERRED_FOR_PAPER, STATUS_ROLLBACK_REQUIRED,
)


class PromotionConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class PromotionGateConfig:
    policy_version: str
    minimum_completed_evaluations: int
    minimum_market_regimes: int
    max_incomplete_analysis_rate: float
    max_unsupported_claim_rate: float
    max_provider_failure_rate: float
    max_retry_rate: float
    min_reproducibility_rate: float
    preferred_excess_return_margin: float
    allow_live_promotion: bool = False
    # --- Milestone 7 additive gate inputs (docs/milestone-7.md Step 24) ---
    # Every field below is optional (`None` = "this gate is not enforced",
    # matching this module's pre-existing pattern of only gating on
    # explicitly configured thresholds) and can only ever ADD a restriction
    # on top of the Milestone 6 decision logic above — none of these can
    # loosen an existing REJECTED/ROLLBACK_REQUIRED/INSUFFICIENT_DATA
    # verdict into something more permissive.
    min_shadow_cycle_completion_rate: float | None = None
    require_cost_known: bool = False
    min_evidence_complete_sample_size: int | None = None

    def __post_init__(self) -> None:
        if self.allow_live_promotion:
            raise PromotionConfigError("allow_live_promotion=true is not permitted — fails closed")
        if self.minimum_completed_evaluations < 0 or self.minimum_market_regimes < 0:
            raise PromotionConfigError("minimum thresholds must be >= 0")
        if self.min_shadow_cycle_completion_rate is not None and not (0.0 <= self.min_shadow_cycle_completion_rate <= 1.0):
            raise PromotionConfigError("min_shadow_cycle_completion_rate must be in [0,1]")
        if self.min_evidence_complete_sample_size is not None and self.min_evidence_complete_sample_size < 0:
            raise PromotionConfigError("min_evidence_complete_sample_size must be >= 0")


@dataclass(frozen=True)
class PromotionMetricInput:
    status: str  # "OK" | "INSUFFICIENT_DATA" | "UNDEFINED" — mirrors evaluation.metrics.MetricsResult.status
    value: float | Decimal | None


@dataclass(frozen=True)
class PromotionGateInputs:
    completed_evaluations: int
    market_regimes_observed: int
    excess_return_enhanced: PromotionMetricInput
    excess_return_baseline: PromotionMetricInput
    max_drawdown_enhanced: PromotionMetricInput
    max_drawdown_baseline: PromotionMetricInput
    incomplete_analysis_rate: float
    unsupported_claim_rate: float
    provider_failure_rate: float
    retry_rate: float
    reproducibility_rate: float | None
    currently_preferred: bool = False
    # --- Milestone 7 additive gate inputs (docs/milestone-7.md Step 24) ---
    shadow_cycle_completion_rate: float | None = None
    cost_known: bool | None = None
    evidence_complete_sample_size: int | None = None


@dataclass(frozen=True)
class PromotionDecision:
    status: str
    policy_version: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in PROMOTION_STATUSES:
            raise PromotionConfigError(f"promotion status {self.status!r} is not one of {PROMOTION_STATUSES} — fails closed")


def evaluate_promotion(inputs: PromotionGateInputs, config: PromotionGateConfig) -> PromotionDecision:
    reasons: list[str] = []

    if inputs.completed_evaluations < config.minimum_completed_evaluations:
        reasons.append(f"completed_evaluations {inputs.completed_evaluations} < minimum {config.minimum_completed_evaluations}")
    if inputs.market_regimes_observed < config.minimum_market_regimes:
        reasons.append(f"market_regimes_observed {inputs.market_regimes_observed} < minimum {config.minimum_market_regimes}")

    # --- Milestone 7 additive gates (docs/milestone-7.md Step 24): each is
    # only enforced when the operator has explicitly configured it
    # (`config.*` non-None/non-default) — an unconfigured gate here behaves
    # exactly as it did before this slice, preserving the existing decision
    # authority. Every one of these can only ADD to `reasons` (and therefore
    # only ever push the result toward INSUFFICIENT_DATA), never remove an
    # existing reason or bypass a Milestone 6 check below.
    if config.min_shadow_cycle_completion_rate is not None:
        if inputs.shadow_cycle_completion_rate is None:
            reasons.append("shadow_cycle_completion_rate is unknown but min_shadow_cycle_completion_rate is configured — fails closed")
        elif inputs.shadow_cycle_completion_rate < config.min_shadow_cycle_completion_rate:
            reasons.append(
                f"shadow_cycle_completion_rate {inputs.shadow_cycle_completion_rate:.3f} < minimum "
                f"{config.min_shadow_cycle_completion_rate:.3f} — recurring shadow operation is not stable enough"
            )
    if config.require_cost_known:
        if inputs.cost_known is not True:
            reasons.append("require_cost_known=true but cost_known is not True — no promotion when costs are unknown")
    if config.min_evidence_complete_sample_size is not None:
        if inputs.evidence_complete_sample_size is None:
            reasons.append("evidence_complete_sample_size is unknown but min_evidence_complete_sample_size is configured — fails closed")
        elif inputs.evidence_complete_sample_size < config.min_evidence_complete_sample_size:
            reasons.append(
                f"evidence_complete_sample_size {inputs.evidence_complete_sample_size} < minimum "
                f"{config.min_evidence_complete_sample_size}"
            )

    if reasons:
        return PromotionDecision(status=STATUS_INSUFFICIENT_DATA, policy_version=config.policy_version, reasons=tuple(reasons))

    safety_regressions: list[str] = []
    if inputs.unsupported_claim_rate > config.max_unsupported_claim_rate:
        safety_regressions.append(f"unsupported_claim_rate {inputs.unsupported_claim_rate:.3f} > max {config.max_unsupported_claim_rate:.3f}")
    if inputs.provider_failure_rate > config.max_provider_failure_rate:
        safety_regressions.append(f"provider_failure_rate {inputs.provider_failure_rate:.3f} > max {config.max_provider_failure_rate:.3f}")
    if inputs.retry_rate > config.max_retry_rate:
        safety_regressions.append(f"retry_rate {inputs.retry_rate:.3f} > max {config.max_retry_rate:.3f}")
    if inputs.incomplete_analysis_rate > config.max_incomplete_analysis_rate:
        safety_regressions.append(f"incomplete_analysis_rate {inputs.incomplete_analysis_rate:.3f} > max {config.max_incomplete_analysis_rate:.3f}")
    if inputs.reproducibility_rate is not None and inputs.reproducibility_rate < config.min_reproducibility_rate:
        safety_regressions.append(f"reproducibility_rate {inputs.reproducibility_rate:.3f} < min {config.min_reproducibility_rate:.3f}")
    if (
        inputs.max_drawdown_enhanced.status == "OK" and inputs.max_drawdown_baseline.status == "OK"
        and inputs.max_drawdown_enhanced.value < inputs.max_drawdown_baseline.value  # drawdown is negative; more negative = worse
    ):
        safety_regressions.append(
            f"max_drawdown regressed: enhanced {inputs.max_drawdown_enhanced.value} worse than baseline {inputs.max_drawdown_baseline.value}"
        )

    if safety_regressions:
        status = STATUS_ROLLBACK_REQUIRED if inputs.currently_preferred else STATUS_REJECTED
        return PromotionDecision(status=status, policy_version=config.policy_version, reasons=tuple(safety_regressions))

    if inputs.excess_return_enhanced.status != "OK" or inputs.excess_return_baseline.status != "OK":
        return PromotionDecision(
            status=STATUS_SHADOW_ONLY, policy_version=config.policy_version,
            reasons=("benchmark-relative return is not yet computable for one or both arms",),
        )

    if inputs.excess_return_enhanced.value <= inputs.excess_return_baseline.value:
        return PromotionDecision(
            status=STATUS_SHADOW_ONLY, policy_version=config.policy_version,
            reasons=(f"enhanced excess return {inputs.excess_return_enhanced.value} does not exceed baseline {inputs.excess_return_baseline.value}",),
        )

    margin = float(inputs.excess_return_enhanced.value) - float(inputs.excess_return_baseline.value)
    if margin >= config.preferred_excess_return_margin:
        return PromotionDecision(
            status=STATUS_PREFERRED_FOR_PAPER, policy_version=config.policy_version,
            reasons=(f"enhanced excess return exceeds baseline by {margin:.4f} >= preferred margin {config.preferred_excess_return_margin:.4f}",),
        )
    return PromotionDecision(
        status=STATUS_ELIGIBLE_FOR_PAPER, policy_version=config.policy_version,
        reasons=(f"enhanced excess return exceeds baseline by {margin:.4f}, below preferred margin {config.preferred_excess_return_margin:.4f}",),
    )
