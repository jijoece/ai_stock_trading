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


# --- Milestone 7 additive extensions (docs/milestone-7.md Step 24) ------------
#
# Everything below is purely additive: new dataclasses/functions only, no
# change to any function/dataclass above. `research/promotion.py`'s decision
# authority and `allow_live_promotion=False` enforcement are untouched —
# these functions only *report* additional metrics; nothing here feeds a
# promotion decision automatically (a caller wiring one of these into
# `PromotionGateInputs` would be a separate, later choice).

COST_UNKNOWN = "COST_UNKNOWN"
COST_KNOWN = "COST_KNOWN"


@dataclass(frozen=True)
class CostAdjustedValueResult:
    """Excess return minus a cost-per-recommendation term, using
    `research/usage.py`'s pricing where available. `cost_status` is
    `COST_UNKNOWN` (never a fabricated cost) whenever pricing wasn't
    configured for the relevant provider/model/date, matching
    `research/usage.py`'s own "cost stays unset and cost_status explains
    why" convention."""

    excess_return: Decimal | None
    cost_per_recommendation_usd: Decimal | None
    cost_adjusted_excess_return: Decimal | None
    cost_status: str
    recommendation_count: int


def cost_adjusted_enhanced_value(
    *, enhanced_excess_return: metrics.MetricsResult, usage_rows: list[dict], enhanced_recommendation_count: int,
) -> CostAdjustedValueResult:
    """`usage_rows` is the same shape `research_promotion_status_cli` already
    queries via `list_attempt_usage_rows` — rows carrying `cost_status` and
    `estimated_cost` (docs/milestone-5.md `research/usage.py`). Only rows
    with `cost_status == "CALCULATED"` contribute; if none do, or if
    `enhanced_recommendation_count` is 0, the cost-adjusted value is
    `None`/`COST_UNKNOWN` — never a fabricated $0 cost."""
    calculated_costs = [Decimal(r["estimated_cost"]) for r in usage_rows if r.get("cost_status") == "CALCULATED" and r.get("estimated_cost") is not None]

    if enhanced_excess_return.status != "OK":
        return CostAdjustedValueResult(
            excess_return=None, cost_per_recommendation_usd=None, cost_adjusted_excess_return=None,
            cost_status=COST_UNKNOWN, recommendation_count=enhanced_recommendation_count,
        )

    excess_return = Decimal(str(enhanced_excess_return.value))

    if not calculated_costs or enhanced_recommendation_count <= 0:
        return CostAdjustedValueResult(
            excess_return=excess_return, cost_per_recommendation_usd=None, cost_adjusted_excess_return=None,
            cost_status=COST_UNKNOWN, recommendation_count=enhanced_recommendation_count,
        )

    total_cost = sum(calculated_costs, Decimal("0"))
    cost_per_recommendation = total_cost / Decimal(enhanced_recommendation_count)
    # `excess_return` is a fractional return (e.g. 0.03 = 3%), while cost is
    # an absolute USD amount — these are not directly subtractable without a
    # notional basis. This function reports both terms and their difference
    # under an explicit, documented convention: cost is expressed as a
    # fraction of a $1 notional per recommendation, matching how
    # `evaluation/metrics.py` already treats returns as fractional, dollar-
    # unit-free values. A caller wanting a true dollar-basis adjustment must
    # supply their own notional and is not this function's job to assume.
    cost_adjusted = excess_return - cost_per_recommendation

    return CostAdjustedValueResult(
        excess_return=excess_return, cost_per_recommendation_usd=cost_per_recommendation,
        cost_adjusted_excess_return=cost_adjusted, cost_status=COST_KNOWN,
        recommendation_count=enhanced_recommendation_count,
    )


@dataclass(frozen=True)
class ShadowCycleCompletionRates:
    """Completion-rate summary from `shadow_scheduler_runs`/
    `shadow_run_summaries` (docs/milestone-7.md Step 24: "shadow-cycle
    completion rates"). Pure aggregation over already-persisted rows — no DB
    access here, matching this module's existing "caller queries, this
    module computes" boundary."""

    total_runs: int
    completed_count: int
    partially_complete_count: int
    failed_count: int
    no_op_count: int
    completion_rate: float | None
    failure_rate: float | None


def shadow_cycle_completion_rates(scheduler_run_rows: list[dict]) -> ShadowCycleCompletionRates:
    """`scheduler_run_rows` is the shape `shadow_operations_repositories.py
    ::list_scheduler_runs` returns — each row's `status` is one of
    `shadow/scheduler.py`'s `ShadowCycleRunResult.status` values."""
    total = len(scheduler_run_rows)
    if total == 0:
        return ShadowCycleCompletionRates(
            total_runs=0, completed_count=0, partially_complete_count=0, failed_count=0, no_op_count=0,
            completion_rate=None, failure_rate=None,
        )
    completed = sum(1 for r in scheduler_run_rows if r["status"] == "COMPLETED")
    partial = sum(1 for r in scheduler_run_rows if r["status"] == "PARTIALLY_COMPLETE")
    failed = sum(1 for r in scheduler_run_rows if r["status"] == "FAILED")
    no_op = total - completed - partial - failed
    return ShadowCycleCompletionRates(
        total_runs=total, completed_count=completed, partially_complete_count=partial, failed_count=failed,
        no_op_count=no_op, completion_rate=completed / total, failure_rate=failed / total,
    )


@dataclass(frozen=True)
class StratifiedPerformanceBucket:
    label: str
    metrics: metrics.MetricsResult
    sample_size: int


def performance_by_completeness_status(
    evaluations: list[RecommendationEvaluation], completeness_status_by_recommendation_id: dict[str, str],
) -> tuple[StratifiedPerformanceBucket, ...]:
    """Groups evaluations by `research/evidence_completeness.py`'s
    screening/research-completeness status (docs/milestone-7.md Step 24:
    "evidence-completeness stratification"; "performance by completeness
    status"). `completeness_status_by_recommendation_id` is caller-supplied
    (this module does not query `evidence_completeness_results` directly —
    consistent with this file's existing "caller queries, this module
    aggregates" boundary)."""
    buckets: dict[str, list[RecommendationEvaluation]] = {}
    for e in evaluations:
        status = completeness_status_by_recommendation_id.get(e.recommendation_id)
        if status is None:
            continue
        buckets.setdefault(status, []).append(e)
    return tuple(
        StratifiedPerformanceBucket(label=status, metrics=metrics.average_return(evals), sample_size=len(evals))
        for status, evals in sorted(buckets.items())
    )


def performance_by_research_outcome(
    evaluations: list[RecommendationEvaluation], outcome_by_recommendation_id: dict[str, str],
) -> tuple[StratifiedPerformanceBucket, ...]:
    """Groups by research-run outcome (e.g. `COMPLETED`/`PARTIALLY_COMPLETE`/
    `FAILED`/`SCREENED_OUT` — whatever vocabulary the caller's outcome map
    uses; this function does not constrain the label set, matching
    `metrics.group_by`'s own unconstrained-key convention)."""
    buckets: dict[str, list[RecommendationEvaluation]] = {}
    for e in evaluations:
        outcome = outcome_by_recommendation_id.get(e.recommendation_id)
        if outcome is None:
            continue
        buckets.setdefault(outcome, []).append(e)
    return tuple(
        StratifiedPerformanceBucket(label=outcome, metrics=metrics.average_return(evals), sample_size=len(evals))
        for outcome, evals in sorted(buckets.items())
    )


def performance_by_market_regime(evaluations: list[RecommendationEvaluation]) -> tuple[StratifiedPerformanceBucket, ...]:
    """Reuses `RecommendationEvaluation.market_regime` (already an existing
    field on the model per `evaluation/models.py`, currently always `None`
    in practice since no caller populates it yet — docs/milestone-7.md Step
    24 says "reuse whatever regime concept evaluation/ already has ... check
    first"; this repository already has the field, so this function reuses
    it rather than adding a second, competing regime concept). Evaluations
    with `market_regime is None` are excluded from every bucket, not folded
    into a fabricated "UNKNOWN" bucket that would silently mix "regime not
    yet labeled" with a genuine label."""
    grouped = metrics.group_by(evaluations, key="market_regime")
    return tuple(
        StratifiedPerformanceBucket(label=regime, metrics=metrics.average_return(evals), sample_size=len(evals))
        for regime, evals in sorted(grouped.items())
    )


@dataclass(frozen=True)
class ExclusionAwarePerformance:
    """Performance computed after excluding incomplete runs, with the
    excluded sample count always reported alongside — docs/milestone-7.md
    Step 24: "never silently drop samples without reporting how many were
    excluded and why"."""

    included_metrics: metrics.MetricsResult
    included_count: int
    excluded_count: int
    excluded_reason: str


INCOMPLETE_RUN_EVALUATION_STATUSES = ("INCOMPLETE_MISSING_DATA", "BENCHMARK_MISSING", "DELISTED_OR_UNAVAILABLE", "NEVER_EXECUTED")


def performance_excluding_incomplete_runs(
    evaluations: list[RecommendationEvaluation],
    *,
    incomplete_recommendation_ids: frozenset[str] = frozenset(),
) -> ExclusionAwarePerformance:
    """Excludes two independent notions of "incomplete": (1) evaluations
    whose own `status` is one of `INCOMPLETE_RUN_EVALUATION_STATUSES`
    (evaluation-level incompleteness — missing price data, delisted symbol,
    never executed), and (2) evaluations for a recommendation the caller has
    separately identified as an incomplete *research* run via
    `incomplete_recommendation_ids` (e.g. `evidence_completeness.py`
    screening-blocked or `ANALYSIS_INCOMPLETE` outcomes) — always reported
    together as one combined excluded count, never silently merged into the
    included sample."""
    included: list[RecommendationEvaluation] = []
    excluded_count = 0
    for e in evaluations:
        if e.status in INCOMPLETE_RUN_EVALUATION_STATUSES or e.recommendation_id in incomplete_recommendation_ids:
            excluded_count += 1
            continue
        included.append(e)
    return ExclusionAwarePerformance(
        included_metrics=metrics.average_return(included), included_count=len(included),
        excluded_count=excluded_count,
        excluded_reason=(
            f"excluded {excluded_count} evaluation(s) with an incomplete evaluation status "
            f"({', '.join(INCOMPLETE_RUN_EVALUATION_STATUSES)}) or flagged as an incomplete research run"
        ),
    )
