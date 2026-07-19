"""Deterministic strategy-signal contracts (Milestone 23, B1).

A `StrategySignal` is the persisted output of one `CandidateStrategy`
evaluating one symbol. It carries only numeric factors, timestamps, and
reason codes — never free-form text — so it can be produced, audited, and
replayed with zero LLM involvement (architecture: deterministic candidate
generation runs ahead of and separate from the AI research committee).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol

from ..analysis.screener import ScreeningResult
from ..backtesting.models import HistoricalBar
from ..models.trading_models import (
    CatalystRiskFlags,
    MarketDataSnapshot,
    PortfolioState,
    TechnicalFactorInput,
)
from .events import MarketEvent


class StrategyContractError(ValueError):
    """A strategy contract object was constructed with invalid/inconsistent data."""


class StrategyStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    INCOMPLETE = "INCOMPLETE"
    STALE = "STALE"


@dataclass(frozen=True)
class StrategySignal:
    """Deterministic, persisted output of one strategy evaluating one symbol.

    `factor_values` holds only numeric factors (never strings) so nothing
    resembling free-form LLM prose can enter the core signal. `reason_codes`
    are short machine-readable tokens, not sentences.
    """

    strategy_id: str
    strategy_version: str
    symbol: str
    signal_timestamp: datetime
    data_as_of: datetime | None
    status: StrategyStatus
    signal_strength: float
    entry_reference: Decimal | None
    limit_reference: Decimal | None
    invalidation_price: Decimal | None
    initial_stop_reference: Decimal | None
    target_reference: Decimal | None
    expected_holding_period: int | None
    reason_codes: tuple[str, ...]
    factor_values: dict[str, float]
    data_quality: str
    configuration_hash: str

    def __post_init__(self) -> None:
        if self.signal_timestamp.tzinfo is None:
            raise StrategyContractError("StrategySignal.signal_timestamp must be timezone-aware")
        if self.data_as_of is not None and self.data_as_of.tzinfo is None:
            raise StrategyContractError("StrategySignal.data_as_of must be timezone-aware")
        if not (0.0 <= self.signal_strength <= 1.0):
            raise StrategyContractError("StrategySignal.signal_strength must be within [0.0, 1.0]")
        if self.status == StrategyStatus.ELIGIBLE and not self.reason_codes:
            raise StrategyContractError("an ELIGIBLE StrategySignal must record at least one reason code")
        for key, value in self.factor_values.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise StrategyContractError(f"factor_values[{key!r}] must be numeric, got {type(value).__name__}")
        if not self.configuration_hash:
            raise StrategyContractError("StrategySignal.configuration_hash is required")


@dataclass(frozen=True)
class StrategyMarketData:
    """Bars and factor snapshots handed to a strategy for one symbol.

    `bars` must be ordered oldest -> newest and reuses
    `backtesting.models.HistoricalBar` rather than introducing a second bar
    type.
    """

    symbol: str
    bars: tuple[HistoricalBar, ...] = ()
    market: MarketDataSnapshot | None = None
    technical: TechnicalFactorInput | None = None
    catalyst: CatalystRiskFlags | None = None
    events: tuple[MarketEvent, ...] = ()

    def __post_init__(self) -> None:
        for prior, nxt in zip(self.bars, self.bars[1:]):
            if nxt.session_date < prior.session_date:
                raise StrategyContractError("StrategyMarketData.bars must be ordered oldest -> newest")


@dataclass(frozen=True)
class StrategyContext:
    """Point-in-time context shared by every strategy for one evaluation pass."""

    now: datetime
    screening_result: ScreeningResult
    portfolio: PortfolioState | None = None

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise StrategyContractError("StrategyContext.now must be timezone-aware")


class CandidateStrategy(Protocol):
    strategy_id: str
    strategy_version: str

    def evaluate(
        self,
        symbol: str,
        market_data: StrategyMarketData,
        context: StrategyContext,
    ) -> StrategySignal:
        ...
