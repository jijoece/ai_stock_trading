"""Tests for the Milestone 7 additive extensions to
`evaluation/research_comparison.py` (docs/milestone-7.md Step 24):
cost-adjusted enhanced value, shadow-cycle completion rates,
evidence-completeness stratification, performance by research outcome, by
market regime, and performance excluding incomplete runs with excluded-count
reporting.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from trading_research.evaluation import metrics
from trading_research.evaluation.models import RecommendationEvaluation
from trading_research.evaluation.research_comparison import (
    COST_KNOWN,
    COST_UNKNOWN,
    cost_adjusted_enhanced_value,
    performance_by_completeness_status,
    performance_by_market_regime,
    performance_by_research_outcome,
    performance_excluding_incomplete_runs,
    shadow_cycle_completion_rates,
)

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _eval(rec_id: str, net_return: str, *, status: str = "COMPLETED", market_regime: str | None = None) -> RecommendationEvaluation:
    incomplete = status not in ("COMPLETED", "PARTIALLY_FILLED")
    return RecommendationEvaluation(
        recommendation_id=rec_id, horizon_trading_days=5, status=status, evaluation_date=date(2026, 7, 8),
        benchmark_symbol="SPY", gross_return=Decimal(net_return) if not incomplete else None,
        net_return=Decimal(net_return) if not incomplete else None,
        market_regime=market_regime, evaluated_at=NOW,
        missing_data_reasons=("test fixture: no data",) if status in ("INCOMPLETE_MISSING_DATA", "BENCHMARK_MISSING", "DELISTED_OR_UNAVAILABLE") else (),
    )


# --- cost_adjusted_enhanced_value ---------------------------------------------


def test_cost_adjusted_value_unknown_when_no_pricing_rows():
    excess = metrics.MetricsResult(status="OK", value=0.03, sample_size=20)
    result = cost_adjusted_enhanced_value(enhanced_excess_return=excess, usage_rows=[], enhanced_recommendation_count=20)
    assert result.cost_status == COST_UNKNOWN
    assert result.cost_adjusted_excess_return is None
    assert result.excess_return == Decimal("0.03")


def test_cost_adjusted_value_unknown_when_excess_return_not_ok():
    excess = metrics.MetricsResult(status="INSUFFICIENT_DATA", value=None, sample_size=1)
    result = cost_adjusted_enhanced_value(enhanced_excess_return=excess, usage_rows=[{"cost_status": "CALCULATED", "estimated_cost": "1.00"}], enhanced_recommendation_count=1)
    assert result.cost_status == COST_UNKNOWN
    assert result.excess_return is None


def test_cost_adjusted_value_known_when_pricing_configured():
    excess = metrics.MetricsResult(status="OK", value=0.10, sample_size=10)
    usage_rows = [{"cost_status": "CALCULATED", "estimated_cost": "1.00"} for _ in range(10)]
    result = cost_adjusted_enhanced_value(enhanced_excess_return=excess, usage_rows=usage_rows, enhanced_recommendation_count=10)
    assert result.cost_status == COST_KNOWN
    assert result.cost_per_recommendation_usd == Decimal("1.00")
    assert result.cost_adjusted_excess_return == Decimal("0.10") - Decimal("1.00")


def test_cost_adjusted_value_never_fabricates_cost_from_uncalculated_rows():
    excess = metrics.MetricsResult(status="OK", value=0.05, sample_size=5)
    usage_rows = [{"cost_status": "PRICING_NOT_CONFIGURED", "estimated_cost": None} for _ in range(5)]
    result = cost_adjusted_enhanced_value(enhanced_excess_return=excess, usage_rows=usage_rows, enhanced_recommendation_count=5)
    assert result.cost_status == COST_UNKNOWN
    assert result.cost_adjusted_excess_return is None


def test_cost_adjusted_value_unknown_when_zero_recommendations():
    excess = metrics.MetricsResult(status="OK", value=0.05, sample_size=0)
    usage_rows = [{"cost_status": "CALCULATED", "estimated_cost": "1.00"}]
    result = cost_adjusted_enhanced_value(enhanced_excess_return=excess, usage_rows=usage_rows, enhanced_recommendation_count=0)
    assert result.cost_status == COST_UNKNOWN


# --- shadow_cycle_completion_rates ---------------------------------------------


def test_shadow_cycle_completion_rates_empty():
    result = shadow_cycle_completion_rates([])
    assert result.total_runs == 0
    assert result.completion_rate is None
    assert result.failure_rate is None


def test_shadow_cycle_completion_rates_mixed():
    rows = (
        [{"status": "COMPLETED"}] * 6
        + [{"status": "PARTIALLY_COMPLETE"}] * 2
        + [{"status": "FAILED"}] * 1
        + [{"status": "NOT_DUE"}] * 1
    )
    result = shadow_cycle_completion_rates(rows)
    assert result.total_runs == 10
    assert result.completed_count == 6
    assert result.partially_complete_count == 2
    assert result.failed_count == 1
    assert result.no_op_count == 1
    assert result.completion_rate == 0.6
    assert result.failure_rate == 0.1


# --- performance_by_completeness_status ----------------------------------------


def test_performance_by_completeness_status_groups_and_excludes_unlabeled():
    evaluations = [_eval("rec-1", "0.02"), _eval("rec-2", "0.03"), _eval("rec-3", "0.01")]
    status_map = {"rec-1": "COMPLETE_FOR_SCREENING", "rec-2": "COMPLETE_FOR_SCREENING"}  # rec-3 unlabeled -> excluded
    buckets = performance_by_completeness_status(evaluations, status_map)
    assert len(buckets) == 1
    assert buckets[0].label == "COMPLETE_FOR_SCREENING"
    assert buckets[0].sample_size == 2


# --- performance_by_research_outcome --------------------------------------------


def test_performance_by_research_outcome_groups():
    evaluations = [_eval("rec-1", "0.02"), _eval("rec-2", "-0.01")]
    outcome_map = {"rec-1": "COMPLETED", "rec-2": "SCREENED_OUT"}
    buckets = performance_by_research_outcome(evaluations, outcome_map)
    labels = {b.label for b in buckets}
    assert labels == {"COMPLETED", "SCREENED_OUT"}


# --- performance_by_market_regime -----------------------------------------------


def test_performance_by_market_regime_excludes_unlabeled():
    evaluations = [
        _eval("rec-1", "0.02", market_regime="BULL"),
        _eval("rec-2", "0.01", market_regime="BULL"),
        _eval("rec-3", "-0.01", market_regime=None),
    ]
    buckets = performance_by_market_regime(evaluations)
    assert len(buckets) == 1
    assert buckets[0].label == "BULL"
    assert buckets[0].sample_size == 2


def test_performance_by_market_regime_no_regimes_labeled_returns_empty():
    evaluations = [_eval("rec-1", "0.02"), _eval("rec-2", "0.01")]
    buckets = performance_by_market_regime(evaluations)
    assert buckets == ()


# --- performance_excluding_incomplete_runs --------------------------------------


def test_performance_excluding_incomplete_runs_reports_excluded_count():
    evaluations = [
        _eval("rec-1", "0.02"),
        _eval("rec-2", "0.03"),
        _eval("rec-3", "0", status="INCOMPLETE_MISSING_DATA"),
        _eval("rec-4", "0", status="NEVER_EXECUTED"),
    ]
    result = performance_excluding_incomplete_runs(evaluations)
    assert result.included_count == 2
    assert result.excluded_count == 2
    assert "excluded 2" in result.excluded_reason


def test_performance_excluding_incomplete_runs_also_excludes_flagged_recommendation_ids():
    evaluations = [_eval("rec-1", "0.02"), _eval("rec-2", "0.03")]
    result = performance_excluding_incomplete_runs(evaluations, incomplete_recommendation_ids=frozenset({"rec-2"}))
    assert result.included_count == 1
    assert result.excluded_count == 1


def test_performance_excluding_incomplete_runs_never_silently_drops_without_reporting():
    evaluations = [_eval("rec-1", "0.02", status="DELISTED_OR_UNAVAILABLE")]
    result = performance_excluding_incomplete_runs(evaluations)
    assert result.excluded_count == 1
    assert result.included_count == 0
    assert result.excluded_reason  # never empty
