from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from trading_research.backtesting import (
    BacktestConfiguration, EntrySignal, HistoricalBar, run_backtest,
)
from trading_research.backtesting.data_provider import FixtureHistoricalDataProvider


def _bars():
    result = []
    start = date(2026, 1, 1)
    for index in range(8):
        day = start + timedelta(days=index)
        close = Decimal("100") + Decimal(index)
        result.append(HistoricalBar(
            symbol="AAPL", session_date=day, open=close, high=close + 2,
            low=close - 2, close=close, volume=1000,
            available_at=datetime.combine(day, time(21), tzinfo=timezone.utc),
            source_id=f"bar-{index}",
        ))
    return tuple(result)


def test_next_session_entry_whole_shares_and_deterministic_replay():
    bars = _bars()
    provider = FixtureHistoricalDataProvider({"AAPL": bars})
    config = BacktestConfiguration(
        start_date=bars[0].session_date, end_date=bars[-1].session_date,
        symbols=("AAPL",), initial_cash=Decimal("10000"), atr_period=2,
    )
    signal = EntrySignal(
        signal_id="s1", symbol="AAPL", generated_after_session=bars[2].session_date,
        limit_price=Decimal("104"), quantity_hint=Decimal("10"),
    )
    first = run_backtest(configuration=config, data_provider=provider, signals=(signal,))
    second = run_backtest(configuration=config, data_provider=provider, signals=(signal,))
    buy = next(fill for fill in first.fills if fill.side == "BUY")
    assert buy.market_date == bars[3].session_date
    assert buy.quantity == buy.quantity.to_integral_value()
    assert first == second
    assert first.metrics["ambiguity_policy"] == "CONSERVATIVE_STOP_FIRST"
