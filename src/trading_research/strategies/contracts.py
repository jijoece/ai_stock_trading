"""Deterministic strategy-signal contracts (Milestone 23, B1).

A `StrategySignal` is the persisted output of one `CandidateStrategy`
evaluating one symbol. It carries only numeric factors, timestamps, and
reason codes — never free-form text — so it can be produced, audited, and
replayed with zero LLM involvement (architecture: deterministic candidate
generation runs ahead of and separate from the AI research committee).
"""
from __future__ import annotations

import hashlib
import json
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
        # Milestone 24 Part B6: an ELIGIBLE signal must be fully specified —
        # every downstream execution-boundary/backtest consumer trusts these
        # fields to be present and internally consistent without its own
        # re-derivation. A strategy with an incomplete ELIGIBLE result must
        # fail closed to NOT_ELIGIBLE/INCOMPLETE at its own evaluation site
        # rather than construct a half-specified ELIGIBLE signal here.
        if self.status == StrategyStatus.ELIGIBLE:
            if self.data_as_of is None:
                raise StrategyContractError("an ELIGIBLE StrategySignal must have data_as_of")
            if self.entry_reference is None or self.entry_reference <= 0:
                raise StrategyContractError("an ELIGIBLE StrategySignal must have a positive entry_reference")
            if self.limit_reference is None or self.limit_reference <= 0:
                raise StrategyContractError("an ELIGIBLE StrategySignal must have a positive limit_reference")
            if self.initial_stop_reference is None or self.initial_stop_reference <= 0:
                raise StrategyContractError("an ELIGIBLE StrategySignal must have a positive initial_stop_reference")
            if self.invalidation_price is None or self.invalidation_price <= 0:
                raise StrategyContractError("an ELIGIBLE StrategySignal must have a positive invalidation_price")
            if self.initial_stop_reference >= self.entry_reference:
                raise StrategyContractError("an ELIGIBLE StrategySignal's initial_stop_reference must be below entry_reference")
            if self.expected_holding_period is None or self.expected_holding_period <= 0:
                raise StrategyContractError("an ELIGIBLE StrategySignal must have a positive expected_holding_period")
            if self.target_reference is not None and self.target_reference <= self.entry_reference:
                raise StrategyContractError("an ELIGIBLE StrategySignal's target_reference must exceed entry_reference")


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


def _content_payload(signal: StrategySignal) -> dict:
    """Milestone 25 Part B10: every field that represents unchanged
    *economic signal content* — excludes `signal_timestamp` (the moment of
    one evaluation invocation), which lives only in the evaluation ID."""
    return {
        "strategy_id": signal.strategy_id,
        "strategy_version": signal.strategy_version,
        "symbol": signal.symbol,
        "data_as_of": signal.data_as_of.isoformat() if signal.data_as_of is not None else None,
        "status": signal.status.value,
        "entry_reference": str(signal.entry_reference) if signal.entry_reference is not None else None,
        "limit_reference": str(signal.limit_reference) if signal.limit_reference is not None else None,
        "initial_stop_reference": (
            str(signal.initial_stop_reference) if signal.initial_stop_reference is not None else None
        ),
        "invalidation_price": str(signal.invalidation_price) if signal.invalidation_price is not None else None,
        "target_reference": str(signal.target_reference) if signal.target_reference is not None else None,
        "expected_holding_period": signal.expected_holding_period,
        "reason_codes": list(signal.reason_codes),
        "factor_values": {key: signal.factor_values[key] for key in sorted(signal.factor_values)},
        "configuration_hash": signal.configuration_hash,
    }


def derive_strategy_signal_content_id(signal: StrategySignal) -> str:
    """Milestone 25 Part B10: the content identity of a strategy signal —
    represents unchanged economic signal content, independent of *when* the
    signal was evaluated. An unchanged signal re-evaluated five minutes
    later produces the same content ID; changing any economically
    meaningful field (status, references, factors, reason codes,
    configuration) changes it. Use this ID for research reuse, candidate
    deduplication, strategy signal persistence identity, and future
    order-intent deduplication.
    """
    encoded = json.dumps(_content_payload(signal), sort_keys=True, separators=(",", ":")).encode()
    return "strat-sig-" + hashlib.sha256(encoded).hexdigest()[:32]


def derive_strategy_evaluation_id(signal: StrategySignal) -> str:
    """Milestone 25 Part B10: the evaluation identity of a strategy signal —
    the audit record for one evaluation invocation. Includes
    `signal_timestamp`, so two structurally identical signals evaluated at
    different times get different evaluation IDs even though they share the
    same content ID. Use this ID for audit history and individual scanner
    invocation tracing — never for deduplication (use
    `derive_strategy_signal_content_id` instead).
    """
    payload = {**_content_payload(signal), "signal_timestamp": signal.signal_timestamp.isoformat()}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "strat-eval-" + hashlib.sha256(encoded).hexdigest()[:32]
