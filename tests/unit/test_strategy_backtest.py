from datetime import datetime, time, timezone
from decimal import Decimal

import pytest

from trading_research.backtesting.configuration import BacktestConfiguration
from trading_research.backtesting.data_provider import FixtureHistoricalDataProvider
from trading_research.backtesting.models import BacktestError
from trading_research.strategies.backtest_adapter import (
    run_strategy_backtest,
    strategy_signal_to_entry_signal,
)
from trading_research.strategies.contracts import StrategySignal, StrategyStatus

from tests.unit._strategy_test_helpers import NOW, build_bars


def _signal(*, data_as_of, limit_reference=Decimal("104"), status=StrategyStatus.ELIGIBLE, strategy_id="momentum_breakout") -> StrategySignal:
    is_eligible = status == StrategyStatus.ELIGIBLE
    entry = limit_reference if is_eligible else None
    stop = limit_reference * Decimal("0.5") if is_eligible else None
    target = limit_reference * Decimal("1.5") if is_eligible else None
    return StrategySignal(
        strategy_id=strategy_id, strategy_version="1.0.0", symbol="AAPL",
        signal_timestamp=NOW, data_as_of=data_as_of, status=status, signal_strength=0.7,
        entry_reference=entry, limit_reference=entry, invalidation_price=stop,
        initial_stop_reference=stop, target_reference=target, expected_holding_period=10,
        reason_codes=("breakout_confirmed",), factor_values={"x": 1.0}, data_quality="complete",
        configuration_hash="cfg-hash",
    )


def _bars():
    closes = [100.0 + i for i in range(10)]
    return build_bars(closes, symbol="AAPL")


def test_strategy_signal_to_entry_signal_maps_point_in_time_fields():
    bars = _bars()
    signal = _signal(data_as_of=datetime.combine(bars[2].session_date, time(21), tzinfo=timezone.utc))
    entry = strategy_signal_to_entry_signal(signal)
    assert entry.symbol == "AAPL"
    assert entry.generated_after_session == bars[2].session_date
    assert entry.limit_price == Decimal("104")


def test_only_eligible_signal_can_be_backtested():
    bars = _bars()
    signal = _signal(
        data_as_of=datetime.combine(bars[2].session_date, time(21), tzinfo=timezone.utc),
        status=StrategyStatus.NOT_ELIGIBLE,
    )
    with pytest.raises(BacktestError):
        strategy_signal_to_entry_signal(signal)


def test_run_strategy_backtest_fills_within_engine_and_reports_metrics():
    bars = _bars()
    provider = FixtureHistoricalDataProvider({"AAPL": bars})
    config = BacktestConfiguration(
        start_date=bars[0].session_date, end_date=bars[-1].session_date,
        symbols=("AAPL",), initial_cash=Decimal("10000"), atr_period=2,
    )
    signal = _signal(data_as_of=datetime.combine(bars[2].session_date, time(21), tzinfo=timezone.utc))
    result = run_strategy_backtest(
        strategy_id="momentum_breakout", signals=(signal,), configuration=config, data_provider=provider,
    )
    assert result.strategy_id == "momentum_breakout"
    assert result.metrics.number_of_signals == 1
    buys = [f for f in result.backtest_result.fills if f.side == "BUY"]
    assert len(buys) == 1
    assert result.metrics.percentage_unfilled_signals == Decimal("0")
    assert result.metrics.time_to_fill_sessions is not None


def test_run_strategy_backtest_only_considers_matching_strategy_id():
    bars = _bars()
    provider = FixtureHistoricalDataProvider({"AAPL": bars})
    config = BacktestConfiguration(
        start_date=bars[0].session_date, end_date=bars[-1].session_date,
        symbols=("AAPL",), initial_cash=Decimal("10000"), atr_period=2,
    )
    momentum_signal = _signal(
        data_as_of=datetime.combine(bars[2].session_date, time(21), tzinfo=timezone.utc),
        strategy_id="momentum_breakout",
    )
    reversion_signal = _signal(
        data_as_of=datetime.combine(bars[2].session_date, time(21), tzinfo=timezone.utc),
        strategy_id="mean_reversion",
    )
    result = run_strategy_backtest(
        strategy_id="mean_reversion", signals=(momentum_signal, reversion_signal),
        configuration=config, data_provider=provider,
    )
    assert result.metrics.number_of_signals == 1


def test_unfilled_signal_is_reflected_in_percentage_unfilled():
    bars = _bars()
    provider = FixtureHistoricalDataProvider({"AAPL": bars})
    config = BacktestConfiguration(
        start_date=bars[0].session_date, end_date=bars[-1].session_date,
        symbols=("AAPL",), initial_cash=Decimal("10000"), atr_period=2,
    )
    # limit price far below any traded low -> never fills
    signal = _signal(
        data_as_of=datetime.combine(bars[2].session_date, time(21), tzinfo=timezone.utc),
        limit_reference=Decimal("1"),
    )
    result = run_strategy_backtest(
        strategy_id="momentum_breakout", signals=(signal,), configuration=config, data_provider=provider,
    )
    assert result.metrics.percentage_unfilled_signals == Decimal("1")
    assert result.metrics.number_of_trades == 0
