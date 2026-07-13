"""Category H: overlay tests (docs/milestone-5.md Step 20.H)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from trading_research.recommendations.builder import SIDE_ANALYSIS_INCOMPLETE, SIDE_BUY_CANDIDATE, SIDE_NO_ACTION, SIDE_SCREENED_OUT
from trading_research.research.configuration import ResearchConfiguration
from trading_research.research.errors import UnknownOverlayActionError
from trading_research.research.models import ResearchDecision
from trading_research.research.overlay import (
    ACTION_ALLOW_BASELINE,
    ACTION_DOWNGRADE_TO_WATCH,
    ACTION_FORCE_NO_ACTION,
    apply_research_overlay,
    resolve_side_after_overlay,
)


def _config(policy_version="test.v1") -> ResearchConfiguration:
    return ResearchConfiguration(
        version=1, enabled=True, provider="deterministic", model=None, max_attempts_per_role=2,
        request_timeout_seconds=30, max_input_characters=100_000, max_evidence_items=100,
        max_items_per_source_category=25, max_claims_per_role=20, max_output_tokens=2000,
        require_point_in_time_safe=True, require_evidence_for_material_claims=True,
        fail_on_stale_required_evidence=True, allow_parallel_roles=False, roles=("fundamental", "manager"),
        overlay_policy_version=policy_version, overlay_allow_score_increase=False,
        overlay_allow_position_size_increase=False, overlay_incomplete_action="ANALYSIS_INCOMPLETE",
        overlay_critical_risk_action="FORCE_NO_ACTION", config_hash="c" * 64, raw={},
    )


def _decision(rating="BUY", risks=("some risk",)) -> ResearchDecision:
    return ResearchDecision(
        decision_id="d1", research_run_id="run-1", symbol="AAPL", snapshot_id="snap-1", rating=rating,
        confidence=Decimal("0.7"), thesis="t", bull_case="bull" if rating != "ANALYSIS_INCOMPLETE" else "",
        bear_case="bear" if rating != "ANALYSIS_INCOMPLETE" else "", catalysts=(), risks=risks,
        invalidation_conditions=(), claims=(), evidence_ids=(),
        missing_data_reasons=() if rating != "ANALYSIS_INCOMPLETE" else ("thin evidence",),
        model_name="m", prompt_version="v1",
    )


def test_supportive_research_allows_baseline():
    overlay = apply_research_overlay(_decision(rating="BUY"), orchestration_status="COMPLETED", baseline_score=Decimal("80"), configuration=_config())
    assert overlay.action == ACTION_ALLOW_BASELINE
    assert resolve_side_after_overlay(SIDE_BUY_CANDIDATE, overlay) == SIDE_BUY_CANDIDATE


def test_critical_risk_downgrades_baseline_to_no_action():
    overlay = apply_research_overlay(_decision(rating="SELL"), orchestration_status="COMPLETED", baseline_score=Decimal("80"), configuration=_config())
    assert overlay.action == ACTION_FORCE_NO_ACTION
    assert resolve_side_after_overlay(SIDE_BUY_CANDIDATE, overlay) == SIDE_NO_ACTION


def test_hold_rating_downgrades_to_watch():
    overlay = apply_research_overlay(_decision(rating="HOLD"), orchestration_status="COMPLETED", baseline_score=Decimal("60"), configuration=_config())
    assert overlay.action == ACTION_DOWNGRADE_TO_WATCH
    assert resolve_side_after_overlay(SIDE_BUY_CANDIDATE, overlay) == "watch"


def test_incomplete_analysis_blocks_enhanced_recommendation():
    overlay = apply_research_overlay(None, orchestration_status="ANALYSIS_INCOMPLETE", baseline_score=Decimal("80"), configuration=_config())
    assert overlay.action == "ANALYSIS_INCOMPLETE"
    assert resolve_side_after_overlay(SIDE_BUY_CANDIDATE, overlay) == SIDE_ANALYSIS_INCOMPLETE


def test_screened_out_candidate_cannot_be_promoted_by_any_overlay_action():
    for rating in ("BUY", "OVERWEIGHT", "HOLD", "SELL", "UNDERWEIGHT"):
        overlay = apply_research_overlay(_decision(rating=rating), orchestration_status="COMPLETED", baseline_score=None, configuration=_config())
        assert resolve_side_after_overlay(SIDE_SCREENED_OUT, overlay) == SIDE_SCREENED_OUT


def test_already_incomplete_baseline_is_never_touched():
    overlay = apply_research_overlay(_decision(rating="BUY"), orchestration_status="COMPLETED", baseline_score=None, configuration=_config())
    assert resolve_side_after_overlay(SIDE_ANALYSIS_INCOMPLETE, overlay) == SIDE_ANALYSIS_INCOMPLETE


def test_overlay_cannot_increase_a_lesser_side_to_buy_candidate():
    """No overlay action, applied to any baseline side, ever produces
    SIDE_BUY_CANDIDATE unless the baseline already was — this is what
    'Claude cannot increase position size' reduces to at the side level."""
    for rating in ("BUY", "OVERWEIGHT", "HOLD", "SELL", "UNDERWEIGHT", "ANALYSIS_INCOMPLETE"):
        overlay = apply_research_overlay(_decision(rating=rating), orchestration_status="COMPLETED", baseline_score=None, configuration=_config())
        for baseline_side in (SIDE_NO_ACTION, "watch", SIDE_SCREENED_OUT, SIDE_ANALYSIS_INCOMPLETE):
            assert resolve_side_after_overlay(baseline_side, overlay) != SIDE_BUY_CANDIDATE


def test_model_confidence_does_not_alter_the_overlay_action():
    low_conf = _decision(rating="BUY")
    high_conf = ResearchDecision(
        decision_id="d2", research_run_id="run-1", symbol="AAPL", snapshot_id="snap-1", rating="BUY",
        confidence=Decimal("0.99"), thesis="t", bull_case="bull", bear_case="bear", catalysts=(), risks=(),
        invalidation_conditions=(), claims=(), evidence_ids=(), missing_data_reasons=(), model_name="m", prompt_version="v1",
    )
    overlay_low = apply_research_overlay(low_conf, orchestration_status="COMPLETED", baseline_score=None, configuration=_config())
    overlay_high = apply_research_overlay(high_conf, orchestration_status="COMPLETED", baseline_score=None, configuration=_config())
    assert overlay_low.action == overlay_high.action == ACTION_ALLOW_BASELINE


def test_deterministic_output_for_identical_inputs():
    decision = _decision(rating="BUY")
    overlay1 = apply_research_overlay(decision, orchestration_status="COMPLETED", baseline_score=Decimal("77"), configuration=_config())
    overlay2 = apply_research_overlay(decision, orchestration_status="COMPLETED", baseline_score=Decimal("77"), configuration=_config())
    assert overlay1 == overlay2
    assert overlay1.overlay_id == overlay2.overlay_id


def test_policy_version_change_creates_a_new_overlay_result():
    decision = _decision(rating="BUY")
    overlay_v1 = apply_research_overlay(decision, orchestration_status="COMPLETED", baseline_score=Decimal("77"), configuration=_config(policy_version="policy.v1"))
    overlay_v2 = apply_research_overlay(decision, orchestration_status="COMPLETED", baseline_score=Decimal("77"), configuration=_config(policy_version="policy.v2"))
    assert overlay_v1.overlay_id != overlay_v2.overlay_id
