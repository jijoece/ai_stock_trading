from datetime import date, datetime, timezone
from decimal import Decimal

from trading_research.evaluation import metrics
from trading_research.evaluation.models import RecommendationEvaluation

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _completed(rec_id: str, net_return: str, excess_return: str | None = None, **overrides) -> RecommendationEvaluation:
    base = dict(
        recommendation_id=rec_id, horizon_trading_days=1, status="COMPLETED", evaluation_date=date(2026, 7, 14),
        benchmark_symbol="SPY", recommendation_price=Decimal("10"), execution_price=Decimal("10"),
        ending_symbol_price=Decimal("11"), ending_benchmark_price=Decimal("11"),
        benchmark_price_at_execution=Decimal("10"), gross_return=Decimal(net_return),
        net_return=Decimal(net_return), benchmark_return=Decimal("0"),
        excess_return=Decimal(excess_return) if excess_return is not None else Decimal(net_return),
        evaluated_at=NOW,
    )
    base.update(overrides)
    return RecommendationEvaluation(**base)


def test_hit_rate_insufficient_data_below_minimum_sample():
    evaluations = [_completed(f"rec-{i}", "0.05") for i in range(3)]
    result = metrics.hit_rate(evaluations)
    assert result.status == "INSUFFICIENT_DATA"
    assert result.value is None


def test_hit_rate_with_sufficient_sample():
    evaluations = (
        [_completed(f"rec-win-{i}", "0.05") for i in range(3)]
        + [_completed(f"rec-loss-{i}", "-0.02") for i in range(2)]
    )
    result = metrics.hit_rate(evaluations)
    assert result.status == "OK"
    assert result.value == Decimal("3") / Decimal("5")


def test_average_and_median_return():
    evaluations = [
        _completed("rec-1", "0.10"), _completed("rec-2", "0.20"), _completed("rec-3", "0.30"),
        _completed("rec-4", "0.40"), _completed("rec-5", "0.50"),
    ]
    avg = metrics.average_return(evaluations)
    median = metrics.median_return(evaluations)
    assert avg.value == Decimal("0.30")
    assert median.value == Decimal("0.30")


def test_gain_loss_ratio():
    evaluations = (
        [_completed(f"rec-win-{i}", "0.10") for i in range(3)]
        + [_completed(f"rec-loss-{i}", "-0.05") for i in range(3)]
    )
    result = metrics.gain_loss_ratio(evaluations)
    assert result.status == "OK"
    assert result.value == Decimal("2")


def test_gain_loss_ratio_undefined_without_any_losses():
    evaluations = [_completed(f"rec-{i}", "0.10") for i in range(6)]
    result = metrics.gain_loss_ratio(evaluations)
    assert result.status == "UNDEFINED"


def test_cumulative_return_compounds():
    evaluations = [_completed("rec-1", "0.10"), _completed("rec-2", "0.10")]
    result = metrics.cumulative_return(evaluations)
    expected = (Decimal("1.10") * Decimal("1.10")) - 1
    assert result.value == expected


def test_max_drawdown_and_calmar():
    evaluations = [
        _completed("rec-1", "0.10"), _completed("rec-2", "-0.20"), _completed("rec-3", "0.05"),
        _completed("rec-4", "0.05"), _completed("rec-5", "0.05"),
    ]
    drawdown = metrics.max_drawdown(evaluations)
    assert drawdown.status == "OK"
    assert drawdown.value < 0

    calmar = metrics.calmar_ratio(evaluations)
    assert calmar.status == "OK"


def test_sharpe_ratio_undefined_with_zero_variance():
    evaluations = [_completed(f"rec-{i}", "0.05") for i in range(6)]
    result = metrics.sharpe_ratio(evaluations)
    assert result.status == "UNDEFINED"


def test_sharpe_ratio_ok_with_variance():
    values = ["0.05", "-0.02", "0.03", "0.01", "-0.01", "0.04"]
    evaluations = [_completed(f"rec-{i}", v) for i, v in enumerate(values)]
    result = metrics.sharpe_ratio(evaluations, annualize=False)
    assert result.status == "OK"
    assert isinstance(result.value, float)


def test_sortino_ratio_undefined_with_no_downside():
    evaluations = [_completed(f"rec-{i}", "0.05") for i in range(6)]
    result = metrics.sortino_ratio(evaluations)
    assert result.status == "UNDEFINED"


def test_recommendation_to_fill_rate():
    evaluations = [_completed(f"rec-{i}", "0.05") for i in range(3)]
    result = metrics.recommendation_to_fill_rate(5, evaluations)
    assert result.status == "OK"
    assert result.value == Decimal("3") / Decimal("5")


def test_recommendation_to_fill_rate_zero_recommendations_is_insufficient():
    result = metrics.recommendation_to_fill_rate(0, [])
    assert result.status == "INSUFFICIENT_DATA"


def test_group_by_model_version():
    evaluations = [
        _completed("rec-1", "0.05", model_version="m1"),
        _completed("rec-2", "0.10", model_version="m1"),
        _completed("rec-3", "0.03", model_version="m2"),
    ]
    groups = metrics.group_by(evaluations, key="model_version")
    assert set(groups.keys()) == {"m1", "m2"}
    assert len(groups["m1"]) == 2
    assert len(groups["m2"]) == 1


def test_pending_evaluations_excluded_from_return_metrics():
    pending = RecommendationEvaluation(
        recommendation_id="rec-pending", horizon_trading_days=1, status="PENDING",
        evaluation_date=date(2026, 7, 20), benchmark_symbol="SPY", evaluated_at=NOW,
    )
    evaluations = [_completed(f"rec-{i}", "0.05") for i in range(5)] + [pending]
    result = metrics.average_return(evaluations)
    assert result.sample_size == 5
