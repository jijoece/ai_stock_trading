"""Strategy-specific execution and risk boundaries (Milestone 23, B7).

Defines the fields every downstream order intent must retain from the
`StrategySignal` that produced it, and the only transformation an AI
research overlay is permitted to apply on top. Pure, deterministic
functions only — zero LLM calls, and nothing here constructs a new
signal or raises a strategy's own numbers.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .contracts import StrategySignal, StrategyStatus, derive_canonical_strategy_signal_id


class StrategyExecutionBoundaryError(ValueError):
    """A strategy order-intent boundary was violated; callers must fail closed."""


class OverlayDisposition(str, Enum):
    """The only outcomes an AI research overlay may assign to a strategy
    candidate (B7: "reduce confidence, reduce position size, change BUY to
    NO_ACTION, mark ANALYSIS_INCOMPLETE")."""

    ALLOW_ENTRY = "ALLOW_ENTRY"
    REDUCE_CONFIDENCE = "REDUCE_CONFIDENCE"
    REDUCE_SIZE = "REDUCE_SIZE"
    NO_ACTION = "NO_ACTION"
    ANALYSIS_INCOMPLETE = "ANALYSIS_INCOMPLETE"


ALLOWED_OVERLAY_DISPOSITIONS = frozenset(OverlayDisposition)


@dataclass(frozen=True)
class StrategyOrderIntentContext:
    """The strategy-identity fields an order intent must carry unchanged.

    Once built by `build_strategy_order_intent_context`, nothing downstream
    (including the research overlay) may edit these fields — only a fresh
    `StrategySignal` re-evaluation can.
    """

    strategy_id: str
    strategy_signal_id: str
    symbol: str
    entry_condition: str
    invalidation_condition: str
    expected_holding_period: int
    strategy_stop: Decimal

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.strategy_signal_id:
            raise StrategyExecutionBoundaryError("strategy_id and strategy_signal_id are required")
        if not self.entry_condition or not self.invalidation_condition:
            raise StrategyExecutionBoundaryError("entry_condition and invalidation_condition are required")
        if self.expected_holding_period is None or self.expected_holding_period <= 0:
            raise StrategyExecutionBoundaryError("expected_holding_period must be positive")
        if self.strategy_stop is None or self.strategy_stop <= 0:
            raise StrategyExecutionBoundaryError("strategy_stop must be positive")


def derive_strategy_signal_id(signal: StrategySignal) -> str:
    """Milestone 24 Part B7: delegates to the single canonical derivation in
    `contracts.py` — kept as a re-export here so existing call sites in this
    module do not need to change."""
    return derive_canonical_strategy_signal_id(signal)


def build_strategy_order_intent_context(signal: StrategySignal) -> StrategyOrderIntentContext:
    """Only a fully-specified `ELIGIBLE` signal may seed an order-intent
    context. Fails closed on any other status or missing stop/holding
    period/invalidation data — this is the contract B8 requires before any
    strategy signal is allowed to create a paper intent."""
    if signal.status != StrategyStatus.ELIGIBLE:
        raise StrategyExecutionBoundaryError(f"signal status {signal.status} is not ELIGIBLE")
    if signal.initial_stop_reference is None:
        raise StrategyExecutionBoundaryError("ELIGIBLE signal is missing initial_stop_reference")
    if signal.expected_holding_period is None:
        raise StrategyExecutionBoundaryError("ELIGIBLE signal is missing expected_holding_period")
    if signal.invalidation_price is None:
        raise StrategyExecutionBoundaryError("ELIGIBLE signal is missing invalidation_price")
    if not signal.reason_codes:
        raise StrategyExecutionBoundaryError("ELIGIBLE signal is missing entry reason codes")
    return StrategyOrderIntentContext(
        strategy_id=signal.strategy_id,
        strategy_signal_id=derive_strategy_signal_id(signal),
        symbol=signal.symbol,
        entry_condition=",".join(signal.reason_codes),
        invalidation_condition=f"price_at_or_below_{signal.invalidation_price}",
        expected_holding_period=signal.expected_holding_period,
        strategy_stop=signal.initial_stop_reference,
    )


@dataclass(frozen=True)
class StrategyOverlayResult:
    context: StrategyOrderIntentContext
    disposition: OverlayDisposition
    size_multiplier: Decimal
    reasons: tuple[str, ...]


def apply_overlay_disposition(
    context: StrategyOrderIntentContext,
    disposition: OverlayDisposition,
    *,
    requested_size_multiplier: Decimal = Decimal("1"),
    reasons: tuple[str, ...] = (),
) -> StrategyOverlayResult:
    """The only function permitted to fold an AI research disposition onto a
    strategy order-intent context.

    Hard boundaries (B7):
    * `requested_size_multiplier` may only ever shrink size, never exceed 1;
    * the returned `context` is always the exact object the strategy
      produced — no field of it is ever rewritten here, so the overlay
      cannot invent a strategy signal or change a failed strategy into an
      eligible one;
    * `NO_ACTION` / `ANALYSIS_INCOMPLETE` force size to zero regardless of
      the requested multiplier.
    """
    if disposition not in ALLOWED_OVERLAY_DISPOSITIONS:
        raise StrategyExecutionBoundaryError(f"unknown overlay disposition {disposition!r} — fails closed")
    if requested_size_multiplier > Decimal("1") or requested_size_multiplier < Decimal("0"):
        raise StrategyExecutionBoundaryError(
            "requested_size_multiplier must be within [0, 1] — an overlay may only shrink size"
        )
    if disposition in (OverlayDisposition.NO_ACTION, OverlayDisposition.ANALYSIS_INCOMPLETE):
        size_multiplier = Decimal("0")
    else:
        size_multiplier = requested_size_multiplier
    return StrategyOverlayResult(
        context=context, disposition=disposition, size_multiplier=size_multiplier, reasons=reasons,
    )
