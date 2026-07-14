"""Baseline-versus-enhanced paper-book comparison (docs/milestone-8.md Step 20).

Comparability fails closed whenever valuation windows/evidence cutoffs
differ, either book has unsafe valuation, a cycle is missing an arm's
recommendation, or starting cash differs unexpectedly. Never automatically
declares the enhanced arm better — `comparable=False` produces zero metric
deltas, and even a comparable, positive delta is only ever evidence (see
`promotion_evidence.py`), never an authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..storage import paper_books_repositories as repo
from . import metrics as metrics_module

COMPARISON_POLICY_VERSION = "paper-books-comparison-v1"

_COMPARABLE_METRIC_KEYS = (
    "total_return", "cumulative_return", "maximum_drawdown", "volatility", "realized_pnl_usd",
    "unrealized_pnl_usd", "net_liquidation_value_usd", "fees_usd", "slippage_usd", "win_rate",
    "profit_factor", "turnover",
)


@dataclass(frozen=True)
class PaperExperimentComparison:
    comparison_id: str
    experiment_id: str
    baseline_book_id: str
    enhanced_book_id: str
    window_start: datetime
    window_end: datetime
    baseline_metrics_id: str
    enhanced_metrics_id: str
    comparable: bool
    comparability_reasons: tuple[str, ...]
    metric_deltas: dict
    policy_version: str = COMPARISON_POLICY_VERSION


def build_comparison(
    conn, experiment_id: str, baseline_book_id: str, enhanced_book_id: str,
    window_start: datetime, window_end: datetime, *, min_comparable_cycles: int = 1, clock,
) -> PaperExperimentComparison:
    baseline_metrics_id = metrics_module.save_book_metrics(conn, baseline_book_id, window_start, window_end, clock=clock)
    enhanced_metrics_id = metrics_module.save_book_metrics(conn, enhanced_book_id, window_start, window_end, clock=clock)
    baseline_metrics = repo.load_daily_metrics(conn, baseline_book_id, baseline_metrics_id)["metrics"]
    enhanced_metrics = repo.load_daily_metrics(conn, enhanced_book_id, enhanced_metrics_id)["metrics"]

    reasons: list[str] = []

    baseline_book = repo.load_book(conn, baseline_book_id)
    enhanced_book = repo.load_book(conn, enhanced_book_id)
    if baseline_book is None or enhanced_book is None:
        reasons.append("one or both books do not exist")
    else:
        if baseline_book.experiment_arm != "BASELINE" or enhanced_book.experiment_arm != "ENHANCED":
            reasons.append("book arm identity does not match its expected role")
        if Decimal(str(baseline_metrics["starting_cash_usd"])) != Decimal(str(enhanced_metrics["starting_cash_usd"])):
            reasons.append("starting cash differs unexpectedly between baseline and enhanced books")

    if baseline_metrics.get("net_liquidation_value_usd") is None:
        reasons.append("baseline book valuation is incomplete/unsafe for this window")
    if enhanced_metrics.get("net_liquidation_value_usd") is None:
        reasons.append("enhanced book valuation is incomplete/unsafe for this window")

    assignments = repo.list_experiment_assignments(conn, experiment_id)
    windowed = [
        a for a in assignments
        if window_start <= _parse(a["as_of"]) <= window_end
    ]
    missing_enhanced = [a for a in windowed if not a.get("enhanced_recommendation_id")]
    missing_baseline = [a for a in windowed if not a.get("baseline_recommendation_id")]
    if missing_enhanced:
        reasons.append(f"{len(missing_enhanced)} cycle(s) in this window are missing an enhanced recommendation")
    if missing_baseline:
        reasons.append(f"{len(missing_baseline)} cycle(s) in this window are missing a baseline recommendation")
    if len(windowed) < min_comparable_cycles:
        reasons.append(f"insufficient comparable cycles: {len(windowed)} < required {min_comparable_cycles}")

    comparable = not reasons
    metric_deltas: dict = {}
    if comparable:
        for key in _COMPARABLE_METRIC_KEYS:
            b = baseline_metrics.get(key)
            e = enhanced_metrics.get(key)
            metric_deltas[key] = (Decimal(str(e)) - Decimal(str(b))) if (b is not None and e is not None) else None
    else:
        metric_deltas = {key: None for key in _COMPARABLE_METRIC_KEYS}

    comparison_id = f"pb-cmp-{experiment_id}-{window_start.isoformat()}-{window_end.isoformat()}"
    comparison = PaperExperimentComparison(
        comparison_id=comparison_id, experiment_id=experiment_id, baseline_book_id=baseline_book_id,
        enhanced_book_id=enhanced_book_id, window_start=window_start, window_end=window_end,
        baseline_metrics_id=baseline_metrics_id, enhanced_metrics_id=enhanced_metrics_id,
        comparable=comparable, comparability_reasons=tuple(reasons), metric_deltas=metric_deltas,
    )
    repo.save_experiment_comparison(conn, {
        "comparison_id": comparison.comparison_id, "experiment_id": comparison.experiment_id,
        "baseline_book_id": comparison.baseline_book_id, "enhanced_book_id": comparison.enhanced_book_id,
        "window_start": comparison.window_start, "window_end": comparison.window_end,
        "baseline_metrics_id": comparison.baseline_metrics_id, "enhanced_metrics_id": comparison.enhanced_metrics_id,
        "comparable": comparison.comparable, "comparability_reasons": comparison.comparability_reasons,
        "metric_deltas": comparison.metric_deltas, "policy_version": comparison.policy_version,
        "created_at": clock(),
    })
    return comparison


def _parse(iso_ts: str) -> datetime:
    dt = datetime.fromisoformat(iso_ts)
    return dt
