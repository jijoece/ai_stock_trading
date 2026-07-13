"""Overlay <-> existing recommendation builder integration (docs/milestone-5.md Step 16)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_research.recommendations.builder import FrozenRecommendation
from trading_research.research.configuration import ResearchConfiguration
from trading_research.research.models import ResearchDecision
from trading_research.research.overlay import apply_research_overlay
from trading_research.research.recommendation_overlay import apply_overlay_to_recommendation

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _config() -> ResearchConfiguration:
    return ResearchConfiguration(
        version=1, enabled=True, provider="deterministic", model=None, max_attempts_per_role=2,
        request_timeout_seconds=30, max_input_characters=100_000, max_evidence_items=100,
        max_items_per_source_category=25, max_claims_per_role=20, max_output_tokens=2000,
        require_point_in_time_safe=True, require_evidence_for_material_claims=True,
        fail_on_stale_required_evidence=True, allow_parallel_roles=False, roles=("fundamental", "manager"),
        overlay_policy_version="test.v1", overlay_allow_score_increase=False,
        overlay_allow_position_size_increase=False, overlay_incomplete_action="ANALYSIS_INCOMPLETE",
        overlay_critical_risk_action="FORCE_NO_ACTION", config_hash="c" * 64, raw={},
    )


def _baseline_buy_candidate() -> FrozenRecommendation:
    payload = {
        "rec_id": "rec-baseline-1", "run_id": "run-1", "symbol": "AAPL", "side": "buy_candidate",
        "ts": NOW.isoformat(), "price_at_rec": 190.0, "score": 82.0, "confidence": "high", "status": "active",
        "acted": False, "rationale_text": "baseline", "factors": [], "risk_plan": {
            "shares": 10, "entry_price": 190.0, "stop_price": 180.0, "target_price": 210.0,
            "risk_per_share": 10.0, "dollars_at_risk": 100.0, "position_value": 1900.0, "reward_risk": 2.0,
            "warnings": [],
        },
        "warnings": [], "missing_data_reasons": [], "data_timestamps": {}, "reddit_component": None,
        "model_version": "m1", "prompt_version": "none-deterministic", "config_hash": "c" * 64,
        "git_sha": "deadbeef", "frozen": True, "disclaimer": "Research output only. Not financial advice. Not an instruction to trade.",
    }
    return FrozenRecommendation(payload=payload)


def _decision(rating: str) -> ResearchDecision:
    incomplete = rating == "ANALYSIS_INCOMPLETE"
    return ResearchDecision(
        decision_id="d1", research_run_id="run-1", symbol="AAPL", snapshot_id="snap-1", rating=rating,
        confidence=Decimal("0.7"), thesis="t", bull_case="" if incomplete else "bull", bear_case="" if incomplete else "bear",
        catalysts=(), risks=("risk",), invalidation_conditions=(), claims=(), evidence_ids=(),
        missing_data_reasons=("thin evidence",) if incomplete else (), model_name="m", prompt_version="v1",
    )


def test_allow_baseline_reproduces_baseline_side_and_risk_plan():
    baseline = _baseline_buy_candidate()
    overlay = apply_research_overlay(_decision("BUY"), orchestration_status="COMPLETED", baseline_score=Decimal("82"), configuration=_config())
    enhanced = apply_overlay_to_recommendation(baseline, overlay)
    assert enhanced.side == "buy_candidate"
    assert enhanced.payload["risk_plan"] is not None
    assert enhanced.rec_id != baseline.rec_id  # distinct persisted record


def test_critical_risk_forces_no_action_and_strips_risk_plan():
    baseline = _baseline_buy_candidate()
    overlay = apply_research_overlay(_decision("SELL"), orchestration_status="COMPLETED", baseline_score=Decimal("82"), configuration=_config())
    enhanced = apply_overlay_to_recommendation(baseline, overlay)
    assert enhanced.side == "no_action"
    assert enhanced.payload["risk_plan"] is None
    assert enhanced.status == "active"


def test_hold_downgrades_to_watch_and_strips_risk_plan():
    baseline = _baseline_buy_candidate()
    overlay = apply_research_overlay(_decision("HOLD"), orchestration_status="COMPLETED", baseline_score=Decimal("82"), configuration=_config())
    enhanced = apply_overlay_to_recommendation(baseline, overlay)
    assert enhanced.side == "watch"
    assert enhanced.payload["risk_plan"] is None


def test_analysis_incomplete_produces_no_executable_risk_plan():
    baseline = _baseline_buy_candidate()
    overlay = apply_research_overlay(None, orchestration_status="ANALYSIS_INCOMPLETE", baseline_score=Decimal("82"), configuration=_config())
    enhanced = apply_overlay_to_recommendation(baseline, overlay)
    assert enhanced.side == "analysis_incomplete"
    assert enhanced.status == "analysis_incomplete"
    assert enhanced.payload["risk_plan"] is None
    assert enhanced.payload["missing_data_reasons"]


def test_idempotent_overlay_application_same_rec_id():
    baseline = _baseline_buy_candidate()
    overlay = apply_research_overlay(_decision("BUY"), orchestration_status="COMPLETED", baseline_score=Decimal("82"), configuration=_config())
    enhanced1 = apply_overlay_to_recommendation(baseline, overlay)
    enhanced2 = apply_overlay_to_recommendation(baseline, overlay)
    assert enhanced1.rec_id == enhanced2.rec_id
