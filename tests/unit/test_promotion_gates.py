"""Unit tests for research/promotion.py — Milestone 6 docs/milestone-6.md
Step 22 category K."""
from __future__ import annotations

import pytest

from trading_research.research.promotion import (
    PromotionConfigError,
    PromotionGateConfig,
    PromotionGateInputs,
    PromotionMetricInput,
    STATUS_ELIGIBLE_FOR_PAPER,
    STATUS_INSUFFICIENT_DATA,
    STATUS_PREFERRED_FOR_PAPER,
    STATUS_REJECTED,
    STATUS_ROLLBACK_REQUIRED,
    STATUS_SHADOW_ONLY,
    evaluate_promotion,
)


def _config(**overrides) -> PromotionGateConfig:
    base = dict(
        policy_version="v1", minimum_completed_evaluations=20, minimum_market_regimes=1,
        max_incomplete_analysis_rate=0.3, max_unsupported_claim_rate=0.05, max_provider_failure_rate=0.2,
        max_retry_rate=0.5, min_reproducibility_rate=0.9, preferred_excess_return_margin=0.02,
    )
    base.update(overrides)
    return PromotionGateConfig(**base)


def _inputs(**overrides) -> PromotionGateInputs:
    base = dict(
        completed_evaluations=50, market_regimes_observed=2,
        excess_return_enhanced=PromotionMetricInput(status="OK", value=0.05),
        excess_return_baseline=PromotionMetricInput(status="OK", value=0.01),
        max_drawdown_enhanced=PromotionMetricInput(status="OK", value=-0.05),
        max_drawdown_baseline=PromotionMetricInput(status="OK", value=-0.05),
        incomplete_analysis_rate=0.0, unsupported_claim_rate=0.0, provider_failure_rate=0.0,
        retry_rate=0.0, reproducibility_rate=1.0,
    )
    base.update(overrides)
    return PromotionGateInputs(**base)


def test_allow_live_promotion_true_fails_closed_at_construction():
    with pytest.raises(PromotionConfigError):
        _config(allow_live_promotion=True)


def test_insufficient_sample_below_minimum_evaluations():
    decision = evaluate_promotion(_inputs(completed_evaluations=5), _config())
    assert decision.status == STATUS_INSUFFICIENT_DATA


def test_insufficient_sample_below_minimum_regimes():
    decision = evaluate_promotion(_inputs(market_regimes_observed=0), _config())
    assert decision.status == STATUS_INSUFFICIENT_DATA


def test_safety_regression_rejected_when_not_currently_preferred():
    decision = evaluate_promotion(_inputs(unsupported_claim_rate=0.5, currently_preferred=False), _config())
    assert decision.status == STATUS_REJECTED


def test_safety_regression_rollback_required_when_currently_preferred():
    decision = evaluate_promotion(_inputs(unsupported_claim_rate=0.5, currently_preferred=True), _config())
    assert decision.status == STATUS_ROLLBACK_REQUIRED


def test_drawdown_regression_is_a_safety_failure():
    decision = evaluate_promotion(
        _inputs(
            max_drawdown_enhanced=PromotionMetricInput(status="OK", value=-0.30),
            max_drawdown_baseline=PromotionMetricInput(status="OK", value=-0.05),
        ),
        _config(),
    )
    assert decision.status == STATUS_REJECTED


def test_shadow_only_when_excess_return_not_computable():
    decision = evaluate_promotion(
        _inputs(excess_return_enhanced=PromotionMetricInput(status="INSUFFICIENT_DATA", value=None)), _config(),
    )
    assert decision.status == STATUS_SHADOW_ONLY


def test_shadow_only_when_enhanced_does_not_beat_baseline():
    decision = evaluate_promotion(
        _inputs(
            excess_return_enhanced=PromotionMetricInput(status="OK", value=0.01),
            excess_return_baseline=PromotionMetricInput(status="OK", value=0.02),
        ),
        _config(),
    )
    assert decision.status == STATUS_SHADOW_ONLY


def test_eligible_for_paper_when_margin_below_preferred_threshold():
    decision = evaluate_promotion(
        _inputs(
            excess_return_enhanced=PromotionMetricInput(status="OK", value=0.021),
            excess_return_baseline=PromotionMetricInput(status="OK", value=0.01),
        ),
        _config(preferred_excess_return_margin=0.05),
    )
    assert decision.status == STATUS_ELIGIBLE_FOR_PAPER


def test_preferred_for_paper_when_margin_meets_threshold():
    decision = evaluate_promotion(_inputs(), _config(preferred_excess_return_margin=0.02))
    assert decision.status == STATUS_PREFERRED_FOR_PAPER


def test_no_status_ever_implies_live_trading():
    from trading_research.research.promotion import PROMOTION_STATUSES
    for status in PROMOTION_STATUSES:
        assert "LIVE" not in status


def test_deterministic_reconstruction_same_inputs_same_decision():
    config = _config()
    inputs = _inputs()
    d1 = evaluate_promotion(inputs, config)
    d2 = evaluate_promotion(inputs, config)
    assert d1 == d2
