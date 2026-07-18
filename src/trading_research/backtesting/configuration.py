"""Strict configuration for the minimum historical control harness."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..evidence_providers.economic_calendar import EconomicEventBlackoutConfiguration
from ..paper_books.config import PartialProfitSection
from .models import BacktestError

AMBIGUITY_CONSERVATIVE_STOP_FIRST = "CONSERVATIVE_STOP_FIRST"


@dataclass(frozen=True)
class BacktestConfiguration:
    start_date: date
    end_date: date
    symbols: tuple[str, ...]
    initial_cash: Decimal
    atr_period: int = 14
    initial_stop_multiple: Decimal = Decimal("2")
    initial_target_multiple: Decimal = Decimal("3")
    risk_fraction: Decimal = Decimal("0.01")
    max_daily_loss_fraction: Decimal = Decimal("0.03")
    max_drawdown_fraction: Decimal = Decimal("0.15")
    slippage_bps: Decimal = Decimal("0")
    fee_per_order: Decimal = Decimal("0")
    maximum_holding_market_days: int = 20
    ambiguity_policy: str = AMBIGUITY_CONSERVATIVE_STOP_FIRST
    partial_profit: PartialProfitSection = PartialProfitSection()
    economic_event_blackout: EconomicEventBlackoutConfiguration = EconomicEventBlackoutConfiguration(
        enabled=False, before_minutes=30, after_minutes=30, minimum_importance="HIGH",
        markets=("US",), blocked_categories=("FOMC", "CPI", "NONFARM_PAYROLLS"),
    )
    benchmark_symbol: str | None = None

    def __post_init__(self) -> None:
        if self.end_date < self.start_date or not self.symbols:
            raise BacktestError("backtest date range and symbols are required")
        if self.initial_cash <= 0 or self.atr_period <= 0:
            raise BacktestError("initial_cash and atr_period must be positive")
        for value in (self.risk_fraction, self.max_daily_loss_fraction, self.max_drawdown_fraction):
            if not (Decimal("0") < value < Decimal("1")):
                raise BacktestError("risk fractions must be in (0,1)")
        if self.slippage_bps < 0 or self.fee_per_order < 0:
            raise BacktestError("slippage and fees must be non-negative")
        if self.ambiguity_policy != AMBIGUITY_CONSERVATIVE_STOP_FIRST:
            raise BacktestError("only conservative stop-first ambiguity handling is supported")
