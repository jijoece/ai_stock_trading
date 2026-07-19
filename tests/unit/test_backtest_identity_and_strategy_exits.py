from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.backtesting import BacktestConfiguration, EntrySignal, HistoricalBar, run_backtest
from trading_research.backtesting.data_provider import FixtureHistoricalDataProvider
from trading_research.backtesting.models import BacktestError
from trading_research.storage.database import connect
from trading_research.strategies.backtest_adapter import run_strategy_backtest
from trading_research.strategies.contracts import StrategySignal, StrategyStatus

from tests.unit._strategy_test_helpers import NOW


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


def _config(bars, **overrides):
    defaults = dict(
        start_date=bars[0].session_date, end_date=bars[-1].session_date,
        symbols=("AAPL",), initial_cash=Decimal("10000"), atr_period=2,
    )
    defaults.update(overrides)
    return BacktestConfiguration(**defaults)


def test_different_signal_sets_get_different_run_ids():
    bars = _bars()
    provider = FixtureHistoricalDataProvider({"AAPL": bars})
    config = _config(bars)
    signal_a = EntrySignal(signal_id="a", symbol="AAPL", generated_after_session=bars[2].session_date,
                            limit_price=Decimal("104"), quantity_hint=Decimal("10"))
    signal_b = EntrySignal(signal_id="b", symbol="AAPL", generated_after_session=bars[2].session_date,
                            limit_price=Decimal("104"), quantity_hint=Decimal("20"))
    result_a = run_backtest(configuration=config, data_provider=provider, signals=(signal_a,))
    result_b = run_backtest(configuration=config, data_provider=provider, signals=(signal_b,))
    assert result_a.backtest_run_id != result_b.backtest_run_id


def test_identical_signal_sets_replay_idempotently(tmp_path):
    bars = _bars()
    provider = FixtureHistoricalDataProvider({"AAPL": bars})
    config = _config(bars)
    signal = EntrySignal(signal_id="a", symbol="AAPL", generated_after_session=bars[2].session_date,
                          limit_price=Decimal("104"), quantity_hint=Decimal("10"))
    conn = connect(tmp_path / "bt.sqlite3")
    first = run_backtest(configuration=config, data_provider=provider, signals=(signal,), conn=conn)
    second = run_backtest(configuration=config, data_provider=provider, signals=(signal,), conn=conn)
    assert first.backtest_run_id == second.backtest_run_id
    rows = conn.execute("SELECT COUNT(*) FROM backtest_runs WHERE backtest_run_id = ?", (first.backtest_run_id,)).fetchone()
    assert rows[0] == 1


def test_run_id_collision_with_different_input_hash_fails_closed(tmp_path):
    bars = _bars()
    provider = FixtureHistoricalDataProvider({"AAPL": bars})
    config = _config(bars)
    signal = EntrySignal(signal_id="a", symbol="AAPL", generated_after_session=bars[2].session_date,
                          limit_price=Decimal("104"), quantity_hint=Decimal("10"))
    conn = connect(tmp_path / "bt.sqlite3")
    result = run_backtest(configuration=config, data_provider=provider, signals=(signal,), conn=conn)
    # Simulate a corrupted/foreign row sharing this run ID with a different
    # input hash — the persist path must refuse to silently accept it.
    conn.execute(
        "UPDATE backtest_runs SET input_hash = 'tampered-hash' WHERE backtest_run_id = ?",
        (result.backtest_run_id,),
    )
    with pytest.raises(BacktestError):
        run_backtest(configuration=config, data_provider=provider, signals=(signal,), conn=conn)


def test_strategy_stop_is_used_instead_of_global_stop():
    bars = _bars()
    provider = FixtureHistoricalDataProvider({"AAPL": bars})
    config = _config(bars, initial_cash=Decimal("10000000"))
    huge_hint = Decimal("1000000")
    base_signal = EntrySignal(signal_id="a", symbol="AAPL", generated_after_session=bars[2].session_date,
                               limit_price=Decimal("104"), quantity_hint=huge_hint)
    base_result = run_backtest(configuration=config, data_provider=provider, signals=(base_signal,))
    base_buy = next(f for f in base_result.fills if f.side == "BUY")

    # A much tighter stop than the engine's own ATR default shrinks the
    # risk-sized quantity — proving the strategy's stop, not the generic
    # ATR default, actually drove position sizing (quantity_hint and cash
    # are both made large enough that risk sizing is the binding
    # constraint in both runs).
    tight_signal = EntrySignal(
        signal_id="a", symbol="AAPL", generated_after_session=bars[2].session_date,
        limit_price=Decimal("104"), quantity_hint=huge_hint, initial_stop_reference=Decimal("102.9"),
    )
    tight_result = run_backtest(configuration=config, data_provider=provider, signals=(tight_signal,))
    tight_buy = next(f for f in tight_result.fills if f.side == "BUY")
    assert tight_buy.quantity != base_buy.quantity


def test_strategy_holding_period_is_used_instead_of_global_holding_period():
    bars = _bars()
    provider = FixtureHistoricalDataProvider({"AAPL": bars})
    config = _config(bars, maximum_holding_market_days=20)
    signal = EntrySignal(
        signal_id="a", symbol="AAPL", generated_after_session=bars[2].session_date,
        limit_price=Decimal("104"), quantity_hint=Decimal("10"), maximum_holding_sessions=1,
    )
    result = run_backtest(configuration=config, data_provider=provider, signals=(signal,))
    sells = [f for f in result.fills if f.side == "SELL"]
    assert sells and sells[0].exit_reason == "MAXIMUM_HOLDING_PERIOD"


def test_invalid_strategy_stop_rejects_the_signal():
    bars = _bars()
    provider = FixtureHistoricalDataProvider({"AAPL": bars})
    config = _config(bars)
    signal = EntrySignal(
        signal_id="a", symbol="AAPL", generated_after_session=bars[2].session_date,
        limit_price=Decimal("104"), quantity_hint=Decimal("10"),
        initial_stop_reference=Decimal("999"),  # above any realistic fill price
    )
    result = run_backtest(configuration=config, data_provider=provider, signals=(signal,))
    assert not any(f.side == "BUY" for f in result.fills)
    assert any(r["reason"] == "INVALID_STRATEGY_STOP" for r in result.rejected_entries)


def _strategy_signal(**overrides) -> StrategySignal:
    defaults = dict(
        strategy_id="momentum_breakout", strategy_version="1.0.0", symbol="AAPL",
        signal_timestamp=NOW, data_as_of=None, status=StrategyStatus.ELIGIBLE, signal_strength=0.7,
        entry_reference=Decimal("104"), limit_reference=Decimal("104"), invalidation_price=Decimal("90"),
        initial_stop_reference=Decimal("92"), target_reference=Decimal("120"), expected_holding_period=10,
        reason_codes=("breakout_confirmed",), factor_values={"x": 1.0}, data_quality="complete",
        configuration_hash="cfg-hash",
    )
    defaults.update(overrides)
    return StrategySignal(**defaults)


def test_mixed_strategy_configuration_hashes_fail_closed():
    bars = _bars()
    provider = FixtureHistoricalDataProvider({"AAPL": bars})
    config = _config(bars)
    when = datetime.combine(bars[2].session_date, time(21), tzinfo=timezone.utc)
    signal_a = _strategy_signal(data_as_of=when, configuration_hash="hash-a")
    signal_b = _strategy_signal(data_as_of=when, configuration_hash="hash-b")
    with pytest.raises(BacktestError):
        run_strategy_backtest(
            strategy_id="momentum_breakout", signals=(signal_a, signal_b),
            configuration=config, data_provider=provider,
        )


def test_mixed_strategy_versions_fail_closed():
    bars = _bars()
    provider = FixtureHistoricalDataProvider({"AAPL": bars})
    config = _config(bars)
    when = datetime.combine(bars[2].session_date, time(21), tzinfo=timezone.utc)
    signal_a = _strategy_signal(data_as_of=when, strategy_version="1.0.0")
    signal_b = _strategy_signal(data_as_of=when, strategy_version="2.0.0")
    with pytest.raises(BacktestError):
        run_strategy_backtest(
            strategy_id="momentum_breakout", signals=(signal_a, signal_b),
            configuration=config, data_provider=provider,
        )
