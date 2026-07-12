from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from trading_research.evaluation.evaluation_service import (
    evaluate_recommendation,
    evaluate_recommendation_all_horizons,
)
from trading_research.evaluation.price_provider import DeterministicPriceProvider

# Monday, a trading day.
EXECUTION_AT = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)


def _provider_with_1_and_5_day_prices() -> DeterministicPriceProvider:
    provider = DeterministicPriceProvider()
    provider.register("SOFI", date(2026, 7, 13), "14.25")  # anchor (execution date)
    provider.register("SOFI", date(2026, 7, 14), "14.80")  # +1 trading day
    provider.register("SOFI", date(2026, 7, 20), "15.50")  # +5 trading days
    provider.register("SPY", date(2026, 7, 13), "550.00")
    provider.register("SPY", date(2026, 7, 14), "552.00")
    provider.register("SPY", date(2026, 7, 20), "560.00")
    return provider


def _base_kwargs(provider, now, **overrides):
    base = dict(
        recommendation_id="rec-1", symbol="SOFI", recommendation_price=Decimal("14.25"),
        execution_price=Decimal("14.30"), filled_quantity=70, requested_quantity=70,
        execution_completed_at=EXECUTION_AT, price_provider=provider, now=now,
    )
    base.update(overrides)
    return base


def test_one_trading_day_horizon_completed():
    provider = _provider_with_1_and_5_day_prices()
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)  # after the +1 target date
    result = evaluate_recommendation(horizon_trading_days=1, **_base_kwargs(provider, now))

    assert result.status == "COMPLETED"
    assert result.evaluation_date == date(2026, 7, 14)
    assert result.ending_symbol_price == Decimal("14.80")
    expected_gross = (Decimal("14.80") - Decimal("14.30")) / Decimal("14.30")
    assert result.gross_return == expected_gross
    assert result.net_return == expected_gross  # no fees
    expected_bench = (Decimal("552.00") - Decimal("550.00")) / Decimal("550.00")
    assert result.benchmark_return == expected_bench
    assert result.excess_return == expected_gross - expected_bench
    assert result.slippage == Decimal("14.30") - Decimal("14.25")


def test_five_trading_day_horizon_completed():
    provider = _provider_with_1_and_5_day_prices()
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    result = evaluate_recommendation(horizon_trading_days=5, **_base_kwargs(provider, now))
    assert result.status == "COMPLETED"
    assert result.evaluation_date == date(2026, 7, 20)


def test_horizon_not_yet_reached_is_pending():
    provider = _provider_with_1_and_5_day_prices()
    now = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)  # same day as execution
    result = evaluate_recommendation(horizon_trading_days=1, **_base_kwargs(provider, now))
    assert result.status == "PENDING"
    assert result.gross_return is None


def test_no_look_ahead_pending_horizon_never_queries_price_provider():
    class _TrackingProvider(DeterministicPriceProvider):
        def get_close(self, symbol, as_of):
            calls.append((symbol, as_of))
            return super().get_close(symbol, as_of)

    calls = []
    provider = _TrackingProvider()
    now = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
    evaluate_recommendation(horizon_trading_days=60, **_base_kwargs(provider, now))
    assert calls == []


def test_weekend_execution_anchors_to_next_trading_session():
    provider = DeterministicPriceProvider()
    provider.register("SOFI", date(2026, 7, 13), "14.25")  # Monday after the Saturday execution
    provider.register("SOFI", date(2026, 7, 14), "14.80")
    provider.register("SPY", date(2026, 7, 13), "550.00")
    provider.register("SPY", date(2026, 7, 14), "552.00")

    saturday_execution = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    result = evaluate_recommendation(
        horizon_trading_days=1,
        **_base_kwargs(provider, now, execution_completed_at=saturday_execution),
    )
    assert result.status == "COMPLETED"
    assert result.evaluation_date == date(2026, 7, 14)  # Monday + 1 trading day = Tuesday


def test_missing_symbol_price_is_delisted_or_unavailable():
    provider = DeterministicPriceProvider()
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    result = evaluate_recommendation(horizon_trading_days=1, **_base_kwargs(provider, now))
    assert result.status == "DELISTED_OR_UNAVAILABLE"
    assert result.missing_data_reasons


def test_missing_benchmark_price_is_benchmark_missing():
    provider = DeterministicPriceProvider()
    provider.register("SOFI", date(2026, 7, 14), "14.80")
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    result = evaluate_recommendation(horizon_trading_days=1, **_base_kwargs(provider, now))
    assert result.status == "BENCHMARK_MISSING"
    assert result.missing_data_reasons


def test_never_executed_recommendation():
    provider = _provider_with_1_and_5_day_prices()
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    result = evaluate_recommendation(
        horizon_trading_days=1, **_base_kwargs(provider, now, execution_completed_at=None, filled_quantity=0),
    )
    assert result.status == "NEVER_EXECUTED"
    assert result.gross_return is None


def test_partially_filled_recommendation_still_computes_return():
    provider = _provider_with_1_and_5_day_prices()
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    result = evaluate_recommendation(
        horizon_trading_days=1, **_base_kwargs(provider, now, filled_quantity=30, requested_quantity=70),
    )
    assert result.status == "PARTIALLY_FILLED"
    assert result.gross_return is not None


def test_fees_reduce_net_return_relative_to_gross():
    provider = _provider_with_1_and_5_day_prices()
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    result = evaluate_recommendation(horizon_trading_days=1, **_base_kwargs(provider, now, fees=Decimal("5.00")))
    assert result.net_return < result.gross_return


def test_idempotent_recomputation_produces_identical_result():
    provider = _provider_with_1_and_5_day_prices()
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    first = evaluate_recommendation(horizon_trading_days=1, **_base_kwargs(provider, now))
    second = evaluate_recommendation(horizon_trading_days=1, **_base_kwargs(provider, now))
    assert first == second


def test_evaluate_all_horizons_returns_one_per_configured_horizon():
    provider = _provider_with_1_and_5_day_prices()
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    results = evaluate_recommendation_all_horizons(
        recommendation_id="rec-1", symbol="SOFI", recommendation_price=Decimal("14.25"),
        execution_price=Decimal("14.30"), filled_quantity=70, requested_quantity=70,
        execution_completed_at=EXECUTION_AT, price_provider=provider, now=now, horizons=(1, 5, 10, 20, 60),
    )
    assert [r.horizon_trading_days for r in results] == [1, 5, 10, 20, 60]
    assert results[0].status == "COMPLETED"  # 1-day: has data
    assert results[1].status == "COMPLETED"  # 5-day: has data
    assert results[2].status == "PENDING"  # 10-day target date (2026-07-27) is after `now`
