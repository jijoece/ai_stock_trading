"""Aggregate portfolio/strategy metrics over completed evaluations
(docs/milestone-4.md Step 12).

Every metric fails safe with an explicit `MetricsResult.status` rather than
returning a misleading zero when the sample is too small or a metric is
undefined for the data available — see `MetricsResult.insufficient_data`.
Annualization uses 252 trading days/year (documented below); this is a
convention, not a measured constant.

Deterministic Decimal arithmetic throughout except where the standard
statistical definitions (Sharpe/Sortino standard deviation, square roots)
are only practical in `float` — matching this repository's existing
convention of Decimal at storage/monetary boundaries and float for derived
statistics (e.g. `analysis/scorer.py`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from .models import RecommendationEvaluation

ANNUALIZATION_TRADING_DAYS = 252
MIN_SAMPLE_SIZE = 5


class InsufficientDataError(ValueError):
    pass


@dataclass(frozen=True)
class MetricsResult:
    status: str  # "OK" | "INSUFFICIENT_DATA" | "UNDEFINED"
    value: float | Decimal | None
    sample_size: int
    reason: str | None = None


def _completed_returns(evaluations: list[RecommendationEvaluation]) -> list[Decimal]:
    return [e.net_return for e in evaluations if e.status in ("COMPLETED", "PARTIALLY_FILLED") and e.net_return is not None]


def _insufficient(sample_size: int, reason: str) -> MetricsResult:
    return MetricsResult(status="INSUFFICIENT_DATA", value=None, sample_size=sample_size, reason=reason)


def hit_rate(evaluations: list[RecommendationEvaluation], *, min_sample_size: int = MIN_SAMPLE_SIZE) -> MetricsResult:
    returns = _completed_returns(evaluations)
    if len(returns) < min_sample_size:
        return _insufficient(len(returns), f"need at least {min_sample_size} completed evaluations")
    wins = sum(1 for r in returns if r > 0)
    return MetricsResult(status="OK", value=Decimal(wins) / Decimal(len(returns)), sample_size=len(returns))


def average_return(evaluations: list[RecommendationEvaluation], *, min_sample_size: int = MIN_SAMPLE_SIZE) -> MetricsResult:
    returns = _completed_returns(evaluations)
    if len(returns) < min_sample_size:
        return _insufficient(len(returns), f"need at least {min_sample_size} completed evaluations")
    return MetricsResult(status="OK", value=sum(returns) / Decimal(len(returns)), sample_size=len(returns))


def median_return(evaluations: list[RecommendationEvaluation], *, min_sample_size: int = MIN_SAMPLE_SIZE) -> MetricsResult:
    returns = sorted(_completed_returns(evaluations))
    if len(returns) < min_sample_size:
        return _insufficient(len(returns), f"need at least {min_sample_size} completed evaluations")
    mid = len(returns) // 2
    if len(returns) % 2 == 1:
        value = returns[mid]
    else:
        value = (returns[mid - 1] + returns[mid]) / 2
    return MetricsResult(status="OK", value=value, sample_size=len(returns))


def gain_loss_ratio(evaluations: list[RecommendationEvaluation], *, min_sample_size: int = MIN_SAMPLE_SIZE) -> MetricsResult:
    returns = _completed_returns(evaluations)
    if len(returns) < min_sample_size:
        return _insufficient(len(returns), f"need at least {min_sample_size} completed evaluations")
    gains = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    if not gains or not losses:
        return MetricsResult(
            status="UNDEFINED", value=None, sample_size=len(returns),
            reason="gain/loss ratio is undefined without at least one gain and one loss",
        )
    avg_gain = sum(gains) / Decimal(len(gains))
    avg_loss = abs(sum(losses) / Decimal(len(losses)))
    return MetricsResult(status="OK", value=avg_gain / avg_loss, sample_size=len(returns))


def cumulative_return(evaluations: list[RecommendationEvaluation], *, min_sample_size: int = 1) -> MetricsResult:
    returns = _completed_returns(evaluations)
    if len(returns) < min_sample_size:
        return _insufficient(len(returns), f"need at least {min_sample_size} completed evaluations")
    total = Decimal("1")
    for r in returns:
        total *= (Decimal("1") + r)
    return MetricsResult(status="OK", value=total - 1, sample_size=len(returns))


def benchmark_relative_cumulative_return(
    evaluations: list[RecommendationEvaluation], *, min_sample_size: int = 1,
) -> MetricsResult:
    strategy = cumulative_return(evaluations, min_sample_size=min_sample_size)
    if strategy.status != "OK":
        return strategy
    excess_returns = [
        e.excess_return for e in evaluations
        if e.status in ("COMPLETED", "PARTIALLY_FILLED") and e.excess_return is not None
    ]
    if len(excess_returns) < min_sample_size:
        return _insufficient(len(excess_returns), f"need at least {min_sample_size} evaluations with excess_return")
    total = Decimal("1")
    for r in excess_returns:
        total *= (Decimal("1") + r)
    return MetricsResult(status="OK", value=total - 1, sample_size=len(excess_returns))


def sharpe_ratio(
    evaluations: list[RecommendationEvaluation], *, risk_free_rate: float = 0.0, min_sample_size: int = MIN_SAMPLE_SIZE,
    annualize: bool = True,
) -> MetricsResult:
    returns = [float(r) for r in _completed_returns(evaluations)]
    if len(returns) < min_sample_size:
        return _insufficient(len(returns), f"need at least {min_sample_size} completed evaluations")
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1) if len(returns) > 1 else 0.0
    std = math.sqrt(variance)
    if math.isclose(std, 0.0, abs_tol=1e-12):
        return MetricsResult(status="UNDEFINED", value=None, sample_size=len(returns), reason="zero return variance")
    ratio = (mean - risk_free_rate) / std
    if annualize:
        ratio *= math.sqrt(ANNUALIZATION_TRADING_DAYS)
    return MetricsResult(status="OK", value=ratio, sample_size=len(returns))


def sortino_ratio(
    evaluations: list[RecommendationEvaluation], *, risk_free_rate: float = 0.0, min_sample_size: int = MIN_SAMPLE_SIZE,
    annualize: bool = True,
) -> MetricsResult:
    returns = [float(r) for r in _completed_returns(evaluations)]
    if len(returns) < min_sample_size:
        return _insufficient(len(returns), f"need at least {min_sample_size} completed evaluations")
    mean = sum(returns) / len(returns)
    downside = [min(0.0, r - risk_free_rate) for r in returns]
    downside_variance = sum(d ** 2 for d in downside) / len(downside)
    downside_std = math.sqrt(downside_variance)
    if math.isclose(downside_std, 0.0, abs_tol=1e-12):
        return MetricsResult(
            status="UNDEFINED", value=None, sample_size=len(returns), reason="no downside deviation observed",
        )
    ratio = (mean - risk_free_rate) / downside_std
    if annualize:
        ratio *= math.sqrt(ANNUALIZATION_TRADING_DAYS)
    return MetricsResult(status="OK", value=ratio, sample_size=len(returns))


def max_drawdown(evaluations: list[RecommendationEvaluation], *, min_sample_size: int = 1) -> MetricsResult:
    """Drawdown of the cumulative-return equity curve built purely from
    sequential completed-evaluation returns (ordered as given — callers
    must pass evaluations in chronological order)."""
    returns = _completed_returns(evaluations)
    if len(returns) < min_sample_size:
        return _insufficient(len(returns), f"need at least {min_sample_size} completed evaluations")
    equity = Decimal("1")
    peak = equity
    worst = Decimal("0")
    for r in returns:
        equity *= (Decimal("1") + r)
        peak = max(peak, equity)
        drawdown = (equity - peak) / peak if peak != 0 else Decimal("0")
        worst = min(worst, drawdown)
    return MetricsResult(status="OK", value=worst, sample_size=len(returns))


def calmar_ratio(evaluations: list[RecommendationEvaluation], *, min_sample_size: int = MIN_SAMPLE_SIZE) -> MetricsResult:
    cumulative = cumulative_return(evaluations, min_sample_size=min_sample_size)
    drawdown = max_drawdown(evaluations, min_sample_size=min_sample_size)
    if cumulative.status != "OK" or drawdown.status != "OK":
        return _insufficient(cumulative.sample_size, "need sufficient data for both cumulative return and drawdown")
    if drawdown.value == 0:
        return MetricsResult(
            status="UNDEFINED", value=None, sample_size=cumulative.sample_size,
            reason="Calmar ratio is undefined with zero max drawdown",
        )
    return MetricsResult(status="OK", value=cumulative.value / abs(drawdown.value), sample_size=cumulative.sample_size)


def recommendation_to_fill_rate(
    total_recommendations: int, evaluations: list[RecommendationEvaluation],
) -> MetricsResult:
    if total_recommendations <= 0:
        return _insufficient(0, "no recommendations to compute a fill rate from")
    filled = len({e.recommendation_id for e in evaluations if e.status in ("COMPLETED", "PARTIALLY_FILLED")})
    return MetricsResult(status="OK", value=Decimal(filled) / Decimal(total_recommendations), sample_size=total_recommendations)


def group_by(evaluations: list[RecommendationEvaluation], *, key: str) -> dict[str, list[RecommendationEvaluation]]:
    """Groups completed/partially-filled evaluations by an attribute name
    (docs/milestone-4.md Step 12: "performance by model" / "by prompt
    version" / "by configuration hash" / "by market regime")."""
    groups: dict[str, list[RecommendationEvaluation]] = {}
    for e in evaluations:
        if e.status not in ("COMPLETED", "PARTIALLY_FILLED"):
            continue
        value = getattr(e, key, None)
        if value is None:
            continue
        groups.setdefault(str(value), []).append(e)
    return groups
