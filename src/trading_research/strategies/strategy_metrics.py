"""Deterministic per-strategy backtest metrics (Milestone 23, B8).

Reconstructs round-trip trades from `BacktestResult.fills` — already
point-in-time-safe output of the shared `backtesting.engine` — and reports
the minimum metric set required before any strategy's signals are allowed
to seed a paper candidate. Every value here is arithmetic over already
computed, persisted fills and rejections; zero LLM calls.

Metric definitions (Milestone 24 Part C4):
- `exposure`: fraction of test-session days with an open position, counting
  both completed trades and any position still open at the end of the test.
- `time_to_fill_sessions`: trading-session gap (not calendar days) between
  signal generation and fill — a weekend counts as one session step.
- `profit_factor`: gross profit / gross loss; `None` when there are no
  losing trades (an undefined ratio, never a nonfinite JSON number).
- realized P/L and cost basis both include the entry (BUY) fee exactly
  once, charged at trade open; each exit's own fee is added on top.
- `average_holding_days` remains explicit calendar-day compatibility data;
  `average_holding_sessions` is the strategy-evaluation metric.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..backtesting.models import BacktestFill, BacktestResult, EntrySignal

_UNFILLED_REASONS = ("LIMIT_NOT_FILLED", "NO_NEXT_SESSION", "ATR_UNAVAILABLE", "ZERO_SHARES_AT_RISK_CAP")


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_date: date
    exit_date: date
    entry_price: Decimal
    quantity: Decimal
    realized_pnl: Decimal
    return_fraction: Decimal
    holding_days: int
    holding_sessions: int
    exit_reason: str | None


@dataclass(frozen=True)
class StrategyBacktestMetrics:
    number_of_signals: int
    number_of_trades: int
    win_rate: Decimal | None
    average_return: Decimal | None
    median_return: Decimal | None
    maximum_drawdown: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal | None
    average_holding_days: Decimal | None
    average_holding_sessions: Decimal | None
    turnover: Decimal
    exposure: Decimal
    time_to_fill_sessions: Decimal | None
    percentage_unfilled_signals: Decimal
    by_regime: dict = field(default_factory=dict)


def _reconstruct_trades(
    fills: tuple[BacktestFill, ...], session_dates: tuple[date, ...] = (),
) -> tuple[tuple[Trade, ...], tuple[dict, ...]]:
    """One open lot per symbol at a time (the shared engine enforces this),
    so a BUY opens a trade and the SELL(s) that bring its remaining
    quantity to zero close it. The entry (BUY) fee is charged once, at
    open, into `realized_pnl` and into cost basis (Milestone 24 Part C4) —
    a partial exit's SELL fee is added on top of that, never replacing it.
    Any lot still open at the end of the test (no closing SELL yet) is
    returned separately so callers can still count its exposure days."""
    trades: list[Trade] = []
    open_lot: dict[str, dict] = {}
    # New fills carry a global monotonic sequence. Legacy fills keep their
    # supplied tuple order within a session; side is never used as a tie
    # breaker because that can move a later BUY ahead of an earlier SELL.
    ordered = [
        fill for _, fill in sorted(
            enumerate(fills),
            key=lambda item: (
                item[1].market_date,
                item[1].fill_sequence if item[1].fill_sequence is not None else item[0],
                item[0],
            ),
        )
    ]
    session_position = {market_date: index for index, market_date in enumerate(sorted(session_dates))}
    for fill in ordered:
        if fill.side == "BUY":
            lot_key = fill.position_id or f"legacy:{fill.symbol}"
            open_lot[lot_key] = {
                "position_id": fill.position_id, "symbol": fill.symbol,
                "entry_price": fill.fill_price, "entry_date": fill.market_date,
                "entry_fee": fill.fees,
                "original_quantity": fill.quantity, "remaining_quantity": fill.quantity,
                "realized_pnl": -fill.fees, "exit_reason": None, "exit_date": fill.market_date,
            }
            continue
        lot_key = fill.position_id
        if lot_key is None:
            lot_key = next(
                (key for key, candidate in open_lot.items() if candidate["symbol"] == fill.symbol), None,
            )
        lot = open_lot.get(lot_key) if lot_key is not None else None
        if lot is None:
            continue
        lot["realized_pnl"] += (fill.fill_price - lot["entry_price"]) * fill.quantity - fill.fees
        lot["remaining_quantity"] -= fill.quantity
        lot["exit_reason"] = fill.exit_reason
        lot["exit_date"] = fill.market_date
        if lot["remaining_quantity"] <= 0:
            cost_basis = lot["entry_price"] * lot["original_quantity"] + lot["entry_fee"]
            trades.append(Trade(
                symbol=fill.symbol, entry_date=lot["entry_date"], exit_date=lot["exit_date"],
                entry_price=lot["entry_price"], quantity=lot["original_quantity"],
                realized_pnl=lot["realized_pnl"],
                return_fraction=(lot["realized_pnl"] / cost_basis) if cost_basis > 0 else Decimal("0"),
                holding_days=(lot["exit_date"] - lot["entry_date"]).days,
                holding_sessions=max(
                    0,
                    session_position.get(lot["exit_date"], 0) - session_position.get(lot["entry_date"], 0),
                ),
                exit_reason=lot["exit_reason"],
            ))
            del open_lot[lot_key]
    return tuple(trades), tuple(open_lot.values())


def _median(values: list[Decimal]) -> Decimal | None:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return None
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _trade_metrics(trades: tuple[Trade, ...]) -> dict:
    n = len(trades)
    wins = [t for t in trades if t.realized_pnl > 0]
    losses = [t for t in trades if t.realized_pnl < 0]
    gross_profit = sum((t.realized_pnl for t in wins), Decimal("0"))
    gross_loss = -sum((t.realized_pnl for t in losses), Decimal("0"))
    # Milestone 24 Part C4: profit_factor must stay JSON-serializable. With
    # no losing trades the ratio is undefined (division by zero), not
    # infinite — represented as `None` rather than a nonfinite Decimal that
    # `json.dumps` cannot encode.
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    return {
        "number_of_trades": n,
        "win_rate": (Decimal(len(wins)) / n) if n else None,
        "average_return": (sum((t.return_fraction for t in trades), Decimal("0")) / n) if n else None,
        "median_return": _median([t.return_fraction for t in trades]),
        "profit_factor": profit_factor,
        "expectancy": (sum((t.realized_pnl for t in trades), Decimal("0")) / n) if n else None,
        "average_holding_days": (Decimal(sum(t.holding_days for t in trades)) / n) if n else None,
        "average_holding_sessions": (
            Decimal(sum(t.holding_sessions for t in trades)) / n
        ) if n else None,
    }


def compute_strategy_metrics(
    result: BacktestResult,
    entry_signals: tuple[EntrySignal, ...],
    *,
    regime_by_date: dict | None = None,
) -> StrategyBacktestMetrics:
    session_dates = tuple(state.market_date for state in result.daily_states)
    trades, open_lots = _reconstruct_trades(result.fills, session_dates)
    buy_fills = {f.order_id: f for f in result.fills if f.side == "BUY"}
    signal_by_id = {s.signal_id: s for s in entry_signals}

    # Milestone 24 Part C4: true trading-session gap, not calendar days — a
    # weekend between signal generation and fill must count as one session
    # step, not three calendar days.
    session_dates = {state.market_date for state in result.daily_states}
    ordered_sessions = sorted(session_dates)
    session_position = {d: i for i, d in enumerate(ordered_sessions)}
    import bisect
    time_to_fill_sessions: list[int] = []
    for order_id, fill in buy_fills.items():
        signal_id = order_id[len("bt-order-"):]
        signal = signal_by_id.get(signal_id)
        if signal is not None and fill.market_date in session_position:
            # Index of the last session at/before the signal's own
            # generation session (normally that session itself).
            generation_index = bisect.bisect_right(ordered_sessions, signal.generated_after_session) - 1
            if generation_index >= 0:
                time_to_fill_sessions.append(session_position[fill.market_date] - generation_index)
    time_to_fill = (
        Decimal(sum(time_to_fill_sessions)) / len(time_to_fill_sessions)
    ) if time_to_fill_sessions else None

    number_of_signals = len(entry_signals)
    unfilled_count = sum(1 for row in result.rejected_entries if row["reason"] in _UNFILLED_REASONS)
    percentage_unfilled = (Decimal(unfilled_count) / number_of_signals) if number_of_signals else Decimal("0")

    # Milestone 24 Part C4: exposure must count both completed trades and
    # any position still open at the end of the test — a strategy that was
    # in the market on the final day was still exposed that day.
    final_date = max(session_dates) if session_dates else None
    closed_days = {d for t in trades for d in session_dates if t.entry_date <= d <= t.exit_date}
    open_days = {
        d for lot in open_lots for d in session_dates
        if final_date is not None and lot["entry_date"] <= d <= final_date
    }
    days_with_position = len(closed_days | open_days)
    total_days = len(result.daily_states)
    exposure = (Decimal(days_with_position) / total_days) if total_days else Decimal("0")

    buy_notional = sum((f.fill_price * f.quantity for f in buy_fills.values()), Decimal("0"))
    initial_cash = result.metrics.get("initial_equity", Decimal("0"))
    turnover = (buy_notional / initial_cash) if initial_cash else Decimal("0")

    by_regime: dict = {}
    if regime_by_date:
        buckets: dict[str, list[Trade]] = {}
        for trade in trades:
            label = regime_by_date.get(trade.entry_date, "UNLABELED")
            buckets.setdefault(label, []).append(trade)
        by_regime = {label: _trade_metrics(tuple(bucket_trades)) for label, bucket_trades in buckets.items()}

    base = _trade_metrics(trades)
    return StrategyBacktestMetrics(
        number_of_signals=number_of_signals,
        number_of_trades=base["number_of_trades"],
        win_rate=base["win_rate"],
        average_return=base["average_return"],
        median_return=base["median_return"],
        maximum_drawdown=result.metrics.get("maximum_drawdown", Decimal("0")),
        profit_factor=base["profit_factor"],
        expectancy=base["expectancy"],
        average_holding_days=base["average_holding_days"],
        average_holding_sessions=base["average_holding_sessions"],
        turnover=turnover,
        exposure=exposure,
        time_to_fill_sessions=time_to_fill,
        percentage_unfilled_signals=percentage_unfilled,
        by_regime=by_regime,
    )
