"""Category I: experiment tests (docs/milestone-5.md Step 20.I)."""
from __future__ import annotations

from datetime import datetime, timezone

from trading_research.research.experiment import build_experiment_assignments, derive_experiment_id

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_both_arms_recorded_together_no_survivorship_filtering():
    baseline, enhanced = build_experiment_assignments(
        candidate_run_id="cand-1", symbol="AAPL", as_of=NOW,
        baseline_recommendation_id="rec-baseline", enhanced_recommendation_id="rec-enhanced",
    )
    assert baseline.arm == "BASELINE"
    assert enhanced.arm == "ENHANCED"
    assert baseline.experiment_id == enhanced.experiment_id


def test_non_executed_recommendation_still_gets_an_assignment():
    """A screened-out / no-action / incomplete recommendation_id is a
    perfectly valid input — the function has no filtering on outcome."""
    baseline, enhanced = build_experiment_assignments(
        candidate_run_id="cand-2", symbol="XXXX", as_of=NOW,
        baseline_recommendation_id="rec-screened-out", enhanced_recommendation_id="rec-incomplete",
    )
    assert baseline.baseline_recommendation_id == "rec-screened-out"
    assert enhanced.enhanced_recommendation_id == "rec-incomplete"


def test_same_as_of_and_symbol_for_both_arms():
    baseline, enhanced = build_experiment_assignments(
        candidate_run_id="cand-3", symbol="MSFT", as_of=NOW,
        baseline_recommendation_id="rec-a", enhanced_recommendation_id="rec-b",
    )
    assert baseline.symbol == enhanced.symbol == "MSFT"
    assert baseline.as_of == enhanced.as_of == NOW


def test_idempotent_experiment_id_construction():
    id1 = derive_experiment_id("cand-1", "research-experiment.v1")
    id2 = derive_experiment_id("cand-1", "research-experiment.v1")
    assert id1 == id2
    id3 = derive_experiment_id("cand-1", "research-experiment.v2")
    assert id1 != id3


def test_enhanced_arm_can_record_incomplete_baseline_survives():
    """Baseline recommendation is still assigned even if the enhanced arm's
    research failed (enhanced_recommendation_id may be an ANALYSIS_INCOMPLETE
    recommendation, never None just because research failed — the caller is
    expected to always freeze *some* enhanced-arm recommendation, even an
    ANALYSIS_INCOMPLETE one)."""
    baseline, enhanced = build_experiment_assignments(
        candidate_run_id="cand-4", symbol="AAPL", as_of=NOW,
        baseline_recommendation_id="rec-baseline-buy", enhanced_recommendation_id="rec-enhanced-incomplete",
    )
    assert baseline.baseline_recommendation_id is not None
    assert enhanced.enhanced_recommendation_id is not None
