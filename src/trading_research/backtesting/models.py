"""Typed records for deterministic daily-bar control backtests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


class BacktestError(ValueError):
    pass


@dataclass(frozen=True)
class HistoricalBar:
    symbol: str
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    available_at: datetime
    point_in_time_safe: bool = True
    source_id: str = "fixture"

    def __post_init__(self) -> None:
        if self.available_at.tzinfo is None:
            raise BacktestError("bar available_at must be timezone-aware")
        if not self.point_in_time_safe:
            raise BacktestError("historical bar must be point-in-time safe")
        if min(self.open, self.high, self.low, self.close) <= 0 or self.high < self.low:
            raise BacktestError("bar OHLC values are invalid")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise BacktestError("open and close must lie within the daily range")


@dataclass(frozen=True)
class EntrySignal:
    signal_id: str
    symbol: str
    generated_after_session: date
    limit_price: Decimal
    quantity_hint: Decimal
    initial_stop_reference: Decimal | None = None
    target_reference: Decimal | None = None
    maximum_holding_sessions: int | None = None
    strategy_signal_id: str | None = None

    def __post_init__(self) -> None:
        if self.limit_price <= 0 or self.quantity_hint <= 0:
            raise BacktestError("entry signal price and quantity must be positive")
        if self.quantity_hint != self.quantity_hint.to_integral_value():
            raise BacktestError("backtests support whole-share signals only")
        if self.initial_stop_reference is not None and self.initial_stop_reference <= 0:
            raise BacktestError("entry signal initial_stop_reference must be positive")
        if self.target_reference is not None and self.target_reference <= 0:
            raise BacktestError("entry signal target_reference must be positive")
        if self.maximum_holding_sessions is not None and self.maximum_holding_sessions <= 0:
            raise BacktestError("entry signal maximum_holding_sessions must be positive")


@dataclass(frozen=True)
class BacktestFill:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    quantity: Decimal
    fill_price: Decimal
    fees: Decimal
    slippage: Decimal
    market_date: date
    exit_reason: str | None = None


@dataclass(frozen=True)
class BacktestDailyState:
    market_date: date
    cash: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    drawdown_fraction: Decimal


@dataclass(frozen=True)
class BacktestResult:
    backtest_run_id: str
    configuration_hash: str
    daily_states: tuple[BacktestDailyState, ...]
    fills: tuple[BacktestFill, ...]
    rejected_entries: tuple[dict, ...]
    metrics: dict
    unresolved_evaluations: tuple[str, ...]
