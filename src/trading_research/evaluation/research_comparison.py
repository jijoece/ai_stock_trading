"""Baseline-versus-Claude-enhanced evaluation comparison (docs/milestone-5.md
Steps 14 and 22).

Extends the existing evaluation layer — reuses `evaluation/metrics.py`'s
functions unchanged, applying them independently to each experiment arm's
evaluations. Never filters by execution outcome: a screened-out, watch,
no-action, or ANALYSIS_INCOMPLETE recommendation's evaluation is included
exactly like an executed one (no survivorship bias, per Step 14).

Does not claim Claude improves the strategy — `research_contribution_report`
reports `INSUFFICIENT_SAMPLE` explicitly wherever the data cannot support a
directional claim, rather than guessing.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from . import metrics
from .models import RecommendationEvaluation
from ..research.models import ExperimentAssignment

INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
MIN_SAMPLE_FOR_DIRECTIONAL_CLAIM = 20


@dataclass(frozen=True)
class ArmMetrics:
    recommendation_count: int
    hit_rate: metrics.MetricsResult
    average_return: metrics.MetricsResult
    median_return: metrics.MetricsResult
    gain_loss_ratio: metrics.MetricsResult
    cumulative_return: metrics.MetricsResult
    benchmark_relative_cumulative_return: metrics.MetricsResult
    sharpe_ratio: metrics.MetricsResult
    sortino_ratio: metrics.MetricsResult
    max_drawdown: metrics.MetricsResult
    calmar_ratio: metrics.MetricsResult


@dataclass(frozen=True)
class ArmComparison:
    experiment_id: str
    baseline: ArmMetrics
    enhanced: ArmMetrics


def _arm_metrics(evaluations: list[RecommendationEvaluation]) -> ArmMetrics:
    return ArmMetrics(
        recommendation_count=len(evaluations),
        hit_rate=metrics.hit_rate(evaluations),
        average_return=metrics.average_return(evaluations),
        median_return=metrics.median_return(evaluations),
        gain_loss_ratio=metrics.gain_loss_ratio(evaluations),
        cumulative_return=metrics.cumulative_return(evaluations),
        benchmark_relative_cumulative_return=metrics.benchmark_relative_cumulative_return(evaluations),
        sharpe_ratio=metrics.sharpe_ratio(evaluations),
        sortino_ratio=metrics.sortino_ratio(evaluations),
        max_drawdown=metrics.max_drawdown(evaluations),
        calmar_ratio=metrics.calmar_ratio(evaluations),
    )


def compare_arms(
    assignments: tuple[ExperimentAssignment, ...],
    evaluations_by_recommendation_id: dict[str, list[RecommendationEvaluation]],
) -> ArmComparison:
    baseline_evals: list[RecommendationEvaluation] = []
    enhanced_evals: list[RecommendationEvaluation] = []
    for assignment in assignments:
        if assignment.baseline_recommendation_id:
            baseline_evals.extend(evaluations_by_recommendation_id.get(assignment.baseline_recommendation_id, []))
        if assignment.enhanced_recommendation_id:
            enhanced_evals.extend(evaluations_by_recommendation_id.get(assignment.enhanced_recommendation_id, []))

    experiment_id = assignments[0].experiment_id if assignments else ""
    return ArmComparison(experiment_id=experiment_id, baseline=_arm_metrics(baseline_evals), enhanced=_arm_metrics(enhanced_evals))


@dataclass(frozen=True)
class ResearchContributionReport:
    """Answers the five questions docs/milestone-5.md Step 22 requires."""

    experiment_id: str
    decisions_compared: int
    decisions_changed: int
    change_rate: float | None
    directionally_helpful: str  # "HELPFUL" | "NOT_HELPFUL" | "INSUFFICIENT_SAMPLE"
    total_estimated_cost: Decimal | None
    cost_status: str
    evidence_complete_rate: float | None
    reproducible_rate: float | None


def research_contribution_report(
    *,
    experiment_id: str,
    baseline_sides: dict[str, str],
    enhanced_sides: dict[str, str],
    assignments: tuple[ExperimentAssignment, ...],
    arm_comparison: ArmComparison,
    usage_rows: list[dict],
    evidence_complete_flags: list[bool],
    replay_match_flags: list[bool],
) -> ResearchContributionReport:
    # Each ExperimentAssignment row carries only one arm's recommendation_id
    # (see research/experiment.py::build_experiment_assignments) — pair the
    # BASELINE and ENHANCED rows for the same candidate_run_id back together.
    by_candidate: dict[str, dict[str, str]] = {}
    for a in assignments:
        slot = by_candidate.setdefault(a.candidate_run_id, {})
        if a.baseline_recommendation_id:
            slot["baseline"] = a.baseline_recommendation_id
        if a.enhanced_recommendation_id:
            slot["enhanced"] = a.enhanced_recommendation_id
    paired = [
        (slot["baseline"], slot["enhanced"]) for slot in by_candidate.values() if "baseline" in slot and "enhanced" in slot
    ]
    changed = sum(
        1 for baseline_id, enhanced_id in paired
        if baseline_sides.get(baseline_id) != enhanced_sides.get(enhanced_id)
    )
    change_rate = (changed / len(paired)) if paired else None

    sample_size = min(arm_comparison.baseline.recommendation_count, arm_comparison.enhanced.recommendation_count)
    if sample_size < MIN_SAMPLE_FOR_DIRECTIONAL_CLAIM:
        directionally_helpful = INSUFFICIENT_SAMPLE
    else:
        baseline_ok = arm_comparison.baseline.average_return.status == "OK"
        enhanced_ok = arm_comparison.enhanced.average_return.status == "OK"
        if not (baseline_ok and enhanced_ok):
            directionally_helpful = INSUFFICIENT_SAMPLE
        else:
            directionally_helpful = "HELPFUL" if arm_comparison.enhanced.average_return.value > arm_comparison.baseline.average_return.value else "NOT_HELPFUL"

    calculated_costs = [Decimal(r["estimated_cost"]) for r in usage_rows if r.get("cost_status") == "CALCULATED" and r.get("estimated_cost")]
    if calculated_costs:
        total_cost: Decimal | None = sum(calculated_costs, Decimal("0"))
        cost_status = "CALCULATED"
    else:
        total_cost = None
        cost_status = "PRICING_NOT_CONFIGURED_OR_NO_USAGE"

    evidence_complete_rate = (sum(evidence_complete_flags) / len(evidence_complete_flags)) if evidence_complete_flags else None
    reproducible_rate = (sum(replay_match_flags) / len(replay_match_flags)) if replay_match_flags else None

    return ResearchContributionReport(
        experiment_id=experiment_id, decisions_compared=len(paired), decisions_changed=changed,
        change_rate=change_rate, directionally_helpful=directionally_helpful, total_estimated_cost=total_cost,
        cost_status=cost_status, evidence_complete_rate=evidence_complete_rate, reproducible_rate=reproducible_rate,
    )
