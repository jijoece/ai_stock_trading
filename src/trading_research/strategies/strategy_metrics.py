"""Deterministic per-strategy backtest metrics (Milestone 23, B8).

Reconstructs round-trip trades from `BacktestResult.fills` — already
point-in-time-safe output of the shared `backtesting.engine` — and reports
the minimum metric set required before any strategy's signals are allowed
to seed a paper candidate. Every value here is arithmetic over already
computed, persisted fills and rejections; zero LLM calls.
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
    turnover: Decimal
    exposure: Decimal
    time_to_fill_sessions: Decimal | None
    percentage_unfilled_signals: Decimal
    by_regime: dict = field(default_factory=dict)


def _reconstruct_trades(fills: tuple[BacktestFill, ...]) -> tuple[Trade, ...]:
    """One open lot per symbol at a time (the shared engine enforces this),
    so a BUY opens a trade and the SELL(s) that bring its remaining
    quantity to zero close it."""
    trades: list[Trade] = []
    open_lot: dict[str, dict] = {}
    ordered = sorted(fills, key=lambda f: (f.symbol, f.market_date, 0 if f.side == "BUY" else 1))
    for fill in ordered:
        if fill.side == "BUY":
            open_lot[fill.symbol] = {
                "entry_price": fill.fill_price, "entry_date": fill.market_date,
                "original_quantity": fill.quantity, "remaining_quantity": fill.quantity,
                "realized_pnl": Decimal("0"), "exit_reason": None, "exit_date": fill.market_date,
            }
            continue
        lot = open_lot.get(fill.symbol)
        if lot is None:
            continue
        lot["realized_pnl"] += (fill.fill_price - lot["entry_price"]) * fill.quantity - fill.fees
        lot["remaining_quantity"] -= fill.quantity
        lot["exit_reason"] = fill.exit_reason
        lot["exit_date"] = fill.market_date
        if lot["remaining_quantity"] <= 0:
            cost_basis = lot["entry_price"] * lot["original_quantity"]
            trades.append(Trade(
                symbol=fill.symbol, entry_date=lot["entry_date"], exit_date=lot["exit_date"],
                entry_price=lot["entry_price"], quantity=lot["original_quantity"],
                realized_pnl=lot["realized_pnl"],
                return_fraction=(lot["realized_pnl"] / cost_basis) if cost_basis > 0 else Decimal("0"),
                holding_days=(lot["exit_date"] - lot["entry_date"]).days,
                exit_reason=lot["exit_reason"],
            ))
            del open_lot[fill.symbol]
    return tuple(trades)


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
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = Decimal("Infinity")
    else:
        profit_factor = None
    return {
        "number_of_trades": n,
        "win_rate": (Decimal(len(wins)) / n) if n else None,
        "average_return": (sum((t.return_fraction for t in trades), Decimal("0")) / n) if n else None,
        "median_return": _median([t.return_fraction for t in trades]),
        "profit_factor": profit_factor,
        "expectancy": (sum((t.realized_pnl for t in trades), Decimal("0")) / n) if n else None,
        "average_holding_days": (Decimal(sum(t.holding_days for t in trades)) / n) if n else None,
    }


def compute_strategy_metrics(
    result: BacktestResult,
    entry_signals: tuple[EntrySignal, ...],
    *,
    regime_by_date: dict | None = None,
) -> StrategyBacktestMetrics:
    trades = _reconstruct_trades(result.fills)
    buy_fills = {f.order_id: f for f in result.fills if f.side == "BUY"}
    signal_by_id = {s.signal_id: s for s in entry_signals}

    time_to_fill_days: list[int] = []
    for order_id, fill in buy_fills.items():
        signal_id = order_id[len("bt-order-"):]
        signal = signal_by_id.get(signal_id)
        if signal is not None:
            time_to_fill_days.append((fill.market_date - signal.generated_after_session).days)
    time_to_fill = (Decimal(sum(time_to_fill_days)) / len(time_to_fill_days)) if time_to_fill_days else None

    number_of_signals = len(entry_signals)
    unfilled_count = sum(1 for row in result.rejected_entries if row["reason"] in _UNFILLED_REASONS)
    percentage_unfilled = (Decimal(unfilled_count) / number_of_signals) if number_of_signals else Decimal("0")

    session_dates = {state.market_date for state in result.daily_states}
    days_with_position = len({
        d for t in trades for d in session_dates if t.entry_date <= d <= t.exit_date
    })
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
        turnover=turnover,
        exposure=exposure,
        time_to_fill_sessions=time_to_fill,
        percentage_unfilled_signals=percentage_unfilled,
        by_regime=by_regime,
    )
