"""Tests for baseline-vs-Claude-enhanced evaluation comparison
(docs/milestone-5.md Step 22)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from trading_research.evaluation.models import RecommendationEvaluation
from trading_research.evaluation.research_comparison import (
    INSUFFICIENT_SAMPLE,
    compare_arms,
    research_contribution_report,
)
from trading_research.research.experiment import build_experiment_assignments

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _completed_eval(rec_id: str, net_return: str) -> RecommendationEvaluation:
    return RecommendationEvaluation(
        recommendation_id=rec_id, horizon_trading_days=5, status="COMPLETED", evaluation_date=date(2026, 7, 8),
        benchmark_symbol="SPY", gross_return=Decimal(net_return), net_return=Decimal(net_return),
        evaluated_at=NOW,
    )


def test_compare_arms_no_survivorship_filtering():
    assignments = []
    evals_by_rec = {}
    for i in range(3):
        baseline_id = f"rec-baseline-{i}"
        enhanced_id = f"rec-enhanced-{i}"
        baseline, enhanced = build_experiment_assignments(
            candidate_run_id=f"cand-{i}", symbol="AAPL", as_of=NOW,
            baseline_recommendation_id=baseline_id, enhanced_recommendation_id=enhanced_id,
        )
        assignments.extend([baseline, enhanced])
        evals_by_rec[baseline_id] = [_completed_eval(baseline_id, "0.02")]
        evals_by_rec[enhanced_id] = [_completed_eval(enhanced_id, "0.01")]

    comparison = compare_arms(tuple(assignments), evals_by_rec)
    assert comparison.baseline.recommendation_count == 3
    assert comparison.enhanced.recommendation_count == 3


def test_insufficient_sample_reported_honestly():
    baseline, enhanced = build_experiment_assignments(
        candidate_run_id="cand-1", symbol="AAPL", as_of=NOW,
        baseline_recommendation_id="rec-b", enhanced_recommendation_id="rec-e",
    )
    evals_by_rec = {"rec-b": [_completed_eval("rec-b", "0.02")], "rec-e": [_completed_eval("rec-e", "0.01")]}
    comparison = compare_arms((baseline, enhanced), evals_by_rec)
    report = research_contribution_report(
        experiment_id=baseline.experiment_id, baseline_sides={"rec-b": "buy_candidate"},
        enhanced_sides={"rec-e": "watch"}, assignments=(baseline, enhanced), arm_comparison=comparison,
        usage_rows=[], evidence_complete_flags=[True], replay_match_flags=[True],
    )
    assert report.directionally_helpful == INSUFFICIENT_SAMPLE  # only 1 paired sample, far below threshold
    assert report.decisions_changed == 1
    assert report.decisions_compared == 1


def test_cost_status_reported_when_no_usage_configured():
    baseline, enhanced = build_experiment_assignments(
        candidate_run_id="cand-1", symbol="AAPL", as_of=NOW,
        baseline_recommendation_id="rec-b", enhanced_recommendation_id="rec-e",
    )
    comparison = compare_arms((baseline, enhanced), {})
    report = research_contribution_report(
        experiment_id=baseline.experiment_id, baseline_sides={}, enhanced_sides={}, assignments=(baseline, enhanced),
        arm_comparison=comparison, usage_rows=[], evidence_complete_flags=[], replay_match_flags=[],
    )
    assert report.total_estimated_cost is None
    assert report.cost_status == "PRICING_NOT_CONFIGURED_OR_NO_USAGE"


def test_cost_aggregated_when_configured():
    baseline, enhanced = build_experiment_assignments(
        candidate_run_id="cand-1", symbol="AAPL", as_of=NOW,
        baseline_recommendation_id="rec-b", enhanced_recommendation_id="rec-e",
    )
    comparison = compare_arms((baseline, enhanced), {})
    usage_rows = [
        {"cost_status": "CALCULATED", "estimated_cost": "0.01"},
        {"cost_status": "CALCULATED", "estimated_cost": "0.02"},
        {"cost_status": "PRICING_NOT_CONFIGURED", "estimated_cost": None},
    ]
    report = research_contribution_report(
        experiment_id=baseline.experiment_id, baseline_sides={}, enhanced_sides={}, assignments=(baseline, enhanced),
        arm_comparison=comparison, usage_rows=usage_rows, evidence_complete_flags=[], replay_match_flags=[],
    )
    assert report.total_estimated_cost == Decimal("0.03")
    assert report.cost_status == "CALCULATED"
