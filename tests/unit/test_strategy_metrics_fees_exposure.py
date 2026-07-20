import json
from datetime import date
from decimal import Decimal

from trading_research.backtesting.models import BacktestDailyState, BacktestFill, BacktestResult, EntrySignal
from trading_research.strategies.strategy_metrics import _reconstruct_trades, compute_strategy_metrics


def _fill(
    fill_id, order_id, side, symbol, quantity, price, fees, market_date, exit_reason=None,
    fill_sequence=None, position_id=None,
):
    return BacktestFill(
        fill_id=fill_id, order_id=order_id, symbol=symbol, side=side, quantity=Decimal(quantity),
        fill_price=Decimal(price), fees=Decimal(fees), slippage=Decimal("0"), market_date=market_date,
        exit_reason=exit_reason, fill_sequence=fill_sequence, position_id=position_id,
    )


def _daily_states(dates):
    return tuple(
        BacktestDailyState(
            market_date=d, cash=Decimal("1000"), equity=Decimal("1000"), realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"), drawdown_fraction=Decimal("0"),
        )
        for d in dates
    )


def _result(fills, daily_states, rejected=()):
    return BacktestResult(
        backtest_run_id="bt-test", configuration_hash="cfg", daily_states=daily_states, fills=tuple(fills),
        rejected_entries=tuple(rejected), metrics={"initial_equity": Decimal("1000")}, unresolved_evaluations=(),
    )


def test_entry_and_exit_fees_both_reduce_pnl():
    d0, d1 = date(2026, 1, 5), date(2026, 1, 6)
    fills = [
        _fill("f1", "bt-order-s1", "BUY", "AAPL", "10", "100", "1", d0),
        _fill("f2", "bt-order-s1", "SELL", "AAPL", "10", "110", "1", d1, "FINAL_TARGET"),
    ]
    result = _result(fills, _daily_states([d0, d1]))
    signal = EntrySignal(signal_id="s1", symbol="AAPL", generated_after_session=d0,
                          limit_price=Decimal("100"), quantity_hint=Decimal("10"))
    metrics = compute_strategy_metrics(result, (signal,))
    assert metrics.number_of_trades == 1
    # (110-100)*10 - buy_fee(1) - sell_fee(1) = 98
    assert metrics.expectancy == Decimal("98")


def test_partial_exits_allocate_fees_correctly():
    d0, d1, d2 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
    fills = [
        _fill("f1", "bt-order-s1", "BUY", "AAPL", "10", "100", "1", d0),
        _fill("f2", "bt-order-s1", "SELL", "AAPL", "4", "110", "1", d1, "PARTIAL_PROFIT"),
        _fill("f3", "bt-order-s1", "SELL", "AAPL", "6", "120", "1", d2, "FINAL_TARGET"),
    ]
    result = _result(fills, _daily_states([d0, d1, d2]))
    signal = EntrySignal(signal_id="s1", symbol="AAPL", generated_after_session=d0,
                          limit_price=Decimal("100"), quantity_hint=Decimal("10"))
    metrics = compute_strategy_metrics(result, (signal,))
    assert metrics.number_of_trades == 1
    # entry fee once (-1) + partial exit (110-100)*4-1=39 + final exit (120-100)*6-1=119 = 157
    assert metrics.expectancy == Decimal("157")


def test_open_end_of_test_position_contributes_to_exposure():
    d0, d1, d2 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
    fills = [_fill("f1", "bt-order-s1", "BUY", "AAPL", "10", "100", "1", d0)]
    result = _result(fills, _daily_states([d0, d1, d2]))
    signal = EntrySignal(signal_id="s1", symbol="AAPL", generated_after_session=d0,
                          limit_price=Decimal("100"), quantity_hint=Decimal("10"))
    metrics = compute_strategy_metrics(result, (signal,))
    assert metrics.number_of_trades == 0  # never closed
    assert metrics.exposure == Decimal("1")  # open the whole time through the end of the test


def test_weekend_gap_counts_as_one_market_session_not_three_days():
    # Friday session generates the signal; the next session (fill) is the
    # following Monday — three calendar days away but one trading session.
    friday = date(2026, 1, 2)
    monday = date(2026, 1, 5)
    fills = [_fill("f1", "bt-order-s1", "BUY", "AAPL", "10", "100", "0", monday)]
    result = _result(fills, _daily_states([friday, monday]))
    signal = EntrySignal(signal_id="s1", symbol="AAPL", generated_after_session=friday,
                          limit_price=Decimal("100"), quantity_hint=Decimal("10"))
    metrics = compute_strategy_metrics(result, (signal,))
    assert metrics.time_to_fill_sessions == Decimal("1")


def test_friday_to_monday_holding_reports_one_session_and_three_calendar_days():
    friday = date(2026, 1, 2)
    monday = date(2026, 1, 5)
    fills = [
        _fill("f1", "bt-order-s1", "BUY", "AAPL", "10", "100", "0", friday, fill_sequence=1, position_id="p1"),
        _fill("f2", "bt-order-exit", "SELL", "AAPL", "10", "110", "0", monday,
              "FINAL_TARGET", fill_sequence=2, position_id="p1"),
    ]
    result = _result(fills, _daily_states([friday, monday]))
    metrics = compute_strategy_metrics(result, ())
    assert metrics.average_holding_days == Decimal("3")
    assert metrics.average_holding_sessions == Decimal("1")


def test_same_day_exit_then_reentry_keeps_distinct_trade_lifecycles():
    d0, d1, d2 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
    # Deliberately provide a non-execution tuple order. fill_sequence must
    # reconstruct old BUY -> old SELL -> new BUY, never BUY-before-SELL.
    fills = [
        _fill("new-buy", "bt-order-new", "BUY", "AAPL", "5", "50", "3", d1,
              fill_sequence=3, position_id="new-buy"),
        _fill("old-sell", "bt-order-old-exit", "SELL", "AAPL", "10", "110", "2", d1,
              "MAXIMUM_HOLDING_PERIOD", fill_sequence=2, position_id="old-buy"),
        _fill("old-buy", "bt-order-old", "BUY", "AAPL", "10", "100", "1", d0,
              fill_sequence=1, position_id="old-buy"),
    ]
    result = _result(fills, _daily_states([d0, d1, d2]))
    metrics = compute_strategy_metrics(result, ())
    trades, open_lots = _reconstruct_trades(result.fills, (d0, d1, d2))
    assert metrics.number_of_trades == 1
    assert metrics.expectancy == Decimal("97")  # old lot only: 100 gain - 1 entry fee - 2 exit fee
    assert trades[0].quantity == Decimal("10")
    assert len(open_lots) == 1
    assert open_lots[0]["position_id"] == "new-buy"
    assert open_lots[0]["remaining_quantity"] == Decimal("5")
    assert metrics.exposure == Decimal("1")


def test_all_winning_result_serializes_safely():
    d0, d1 = date(2026, 1, 5), date(2026, 1, 6)
    fills = [
        _fill("f1", "bt-order-s1", "BUY", "AAPL", "10", "100", "0", d0),
        _fill("f2", "bt-order-s1", "SELL", "AAPL", "10", "110", "0", d1, "FINAL_TARGET"),
    ]
    result = _result(fills, _daily_states([d0, d1]))
    signal = EntrySignal(signal_id="s1", symbol="AAPL", generated_after_session=d0,
                          limit_price=Decimal("100"), quantity_hint=Decimal("10"))
    metrics = compute_strategy_metrics(result, (signal,))
    assert metrics.profit_factor is None  # undefined ratio (no losses), never a nonfinite literal
    payload = json.dumps(
        {"profit_factor": metrics.profit_factor}, default=lambda v: str(v) if isinstance(v, Decimal) else v,
    )
    assert json.loads(payload)["profit_factor"] is None
