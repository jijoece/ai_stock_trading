"""Deterministic, long-only paper-book exit policy (Milestone 9,
docs/milestone-9.md Section 2-3).

Pure functions over already-computed, typed inputs — never touches Claude
output, never sizes a position, never decides an *entry*. Stop and safety
exits close the full remainder; an explicitly calculated partial stage may
carry a smaller whole-share quantity. The same inputs always produce the
same `PaperExitDecision` (deterministic, persisted, versioned).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from ..evaluation.market_calendar import is_trading_day

EXIT_POLICY_VERSION = "paper-books-exit-policy-v1"

DECISION_HOLD = "HOLD"
DECISION_EXIT_STOP_LOSS = "EXIT_STOP_LOSS"
DECISION_EXIT_PROFIT_TARGET = "EXIT_PROFIT_TARGET"
DECISION_EXIT_MAX_HOLDING_PERIOD = "EXIT_MAX_HOLDING_PERIOD"
DECISION_EXIT_RECOMMENDATION_REVERSAL = "EXIT_RECOMMENDATION_REVERSAL"
DECISION_EXIT_MANUAL_REQUEST = "EXIT_MANUAL_REQUEST"
DECISION_EXIT_TRAILING_STOP = "EXIT_TRAILING_STOP"
DECISION_EXIT_BREAKEVEN_STOP = "EXIT_BREAKEVEN_STOP"
DECISION_EXIT_SAFETY = "EXIT_SAFETY"
DECISION_EXIT_PARTIAL_PROFIT = "EXIT_PARTIAL_PROFIT"
DECISION_SKIPPED_MISSING_PRICE = "SKIPPED_MISSING_PRICE"
DECISION_SKIPPED_STALE_PRICE = "SKIPPED_STALE_PRICE"
DECISION_SKIPPED_POINT_IN_TIME_UNSAFE = "SKIPPED_POINT_IN_TIME_UNSAFE"
DECISION_SKIPPED_NO_POSITION = "SKIPPED_NO_POSITION"

KNOWN_EXIT_DECISIONS = (
    DECISION_HOLD, DECISION_EXIT_STOP_LOSS, DECISION_EXIT_PROFIT_TARGET,
    DECISION_EXIT_MAX_HOLDING_PERIOD, DECISION_EXIT_RECOMMENDATION_REVERSAL,
    DECISION_EXIT_MANUAL_REQUEST, DECISION_SKIPPED_MISSING_PRICE, DECISION_SKIPPED_STALE_PRICE,
    DECISION_SKIPPED_POINT_IN_TIME_UNSAFE, DECISION_SKIPPED_NO_POSITION,
    DECISION_EXIT_TRAILING_STOP, DECISION_EXIT_BREAKEVEN_STOP,
    DECISION_EXIT_SAFETY, DECISION_EXIT_PARTIAL_PROFIT,
)

# A position exits, never a HOLD/SKIPPED outcome.
EXIT_DECISIONS = (
    DECISION_EXIT_STOP_LOSS, DECISION_EXIT_PROFIT_TARGET, DECISION_EXIT_MAX_HOLDING_PERIOD,
    DECISION_EXIT_RECOMMENDATION_REVERSAL, DECISION_EXIT_MANUAL_REQUEST,
    DECISION_EXIT_TRAILING_STOP, DECISION_EXIT_BREAKEVEN_STOP,
    DECISION_EXIT_SAFETY, DECISION_EXIT_PARTIAL_PROFIT,
)

# Recommendation side/status combination that constitutes a "reversal" for
# an already-open long position (docs/milestone-9.md Section 3 "Define and
# document..."): the newest post-entry, in-window, frozen/active
# recommendation for this exact symbol is no longer an actionable
# buy_candidate — it was actively screened out or sized to no_action. A
# missing recommendation, or one that is still `watch`/`analysis_incomplete`,
# is never treated as a sell signal (explicit non-goal below).
REVERSAL_SIDES = ("screened_out", "no_action")
REVERSAL_STATUS = "active"


class ExitPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperExitDecision:
    decision: str
    book_id: str
    symbol: str
    quantity: Decimal
    reference_price: Decimal | None
    reasons: tuple[str, ...]
    policy_version: str
    partial_stage_id: int | None = None

    def __post_init__(self) -> None:
        if self.decision not in KNOWN_EXIT_DECISIONS:
            raise ExitPolicyError(f"decision {self.decision!r} not one of {KNOWN_EXIT_DECISIONS}")
        if not self.book_id or not self.symbol:
            raise ExitPolicyError("book_id and symbol are required")
        if self.quantity < 0:
            raise ExitPolicyError("quantity must not be negative")
        if self.decision in EXIT_DECISIONS and self.quantity <= 0:
            raise ExitPolicyError(f"decision {self.decision!r} must carry a positive exit quantity")
        if not self.policy_version:
            raise ExitPolicyError("policy_version is required")


def market_days_held(opened_on: date, as_of_date: date) -> int:
    """Number of *elapsed* market sessions strictly after `opened_on` up to
    and including `as_of_date` (docs/milestone-9.md Section 3 "market days,
    not raw calendar days"). `opened_on` itself is day zero, matching
    `evaluation/market_calendar.py::add_trading_days`'s own convention."""
    if as_of_date < opened_on:
        raise ExitPolicyError("as_of_date must not precede opened_on")
    count = 0
    current = opened_on
    while current < as_of_date:
        current += timedelta(days=1)
        if is_trading_day(current):
            count += 1
    return count


def evaluate_exit_decision(
    *,
    book_id: str,
    symbol: str,
    position_quantity: Decimal,
    cost_basis_per_share: Decimal | None,
    position_opened_at: datetime | None,
    as_of: datetime,
    reference_price: Decimal | None,
    price_point_in_time_safe: bool | None,
    price_is_stale: bool,
    stop_loss_percent: Decimal,
    profit_target_percent: Decimal,
    maximum_holding_market_days: int,
    exit_on_recommendation_reversal: bool,
    reversal_recommendation: dict | None = None,
    manual_request: dict | None = None,
    policy_version: str = EXIT_POLICY_VERSION,
    current_stop_price: Decimal | None = None,
    initial_target_price: Decimal | None = None,
    trailing_stop_active: bool = False,
    breakeven_active: bool = False,
    safety_full_exit: bool = False,
    partial_stage_id: int | None = None,
    partial_close_quantity: Decimal | None = None,
) -> PaperExitDecision:
    """Fixed, documented check order — same inputs always produce the same
    decision:

    1. no open long position -> SKIPPED_NO_POSITION
    2. price unavailable -> SKIPPED_MISSING_PRICE
    3. price not point-in-time safe -> SKIPPED_POINT_IN_TIME_UNSAFE
    4. price stale -> SKIPPED_STALE_PRICE
    5. an unconsumed manual exit request exists -> EXIT_MANUAL_REQUEST
       (an explicit, audited human instruction outranks the automatic rules)
    6. hard, trailing, or breakeven stop
    7. safety-mandated full exit
    8. maximum holding period (market days held >= configured maximum)
    9. recommendation reversal (only when enabled and a qualifying newer
       recommendation was supplied by the caller)
    10. deterministic partial-profit stage
    11. final profit target
    12. otherwise HOLD

    Missing/stale/unsafe prices never reach the trigger rules, so they can
    never fabricate an exit. Partial quantity is supplied by the deterministic
    lifecycle policy and every other EXIT carries the full remainder.
    """

    def hold_or_skip(decision: str, *reasons: str) -> PaperExitDecision:
        return PaperExitDecision(
            decision=decision, book_id=book_id, symbol=symbol, quantity=Decimal("0"),
            reference_price=reference_price, reasons=tuple(reasons), policy_version=policy_version,
        )

    def exit_(decision: str, *reasons: str) -> PaperExitDecision:
        quantity = (
            partial_close_quantity
            if decision == DECISION_EXIT_PARTIAL_PROFIT and partial_close_quantity is not None
            else position_quantity
        )
        return PaperExitDecision(
            decision=decision, book_id=book_id, symbol=symbol, quantity=quantity,
            reference_price=reference_price, reasons=tuple(reasons), policy_version=policy_version,
            partial_stage_id=partial_stage_id if decision == DECISION_EXIT_PARTIAL_PROFIT else None,
        )

    if position_quantity is None or position_quantity <= 0:
        return hold_or_skip(DECISION_SKIPPED_NO_POSITION, "no open long position for this book/symbol")
    if reference_price is None or reference_price <= 0:
        return hold_or_skip(DECISION_SKIPPED_MISSING_PRICE, "no point-in-time-safe reference price is available")
    if price_point_in_time_safe is False:
        return hold_or_skip(DECISION_SKIPPED_POINT_IN_TIME_UNSAFE, "reference price is not point-in-time safe")
    if price_is_stale:
        return hold_or_skip(DECISION_SKIPPED_STALE_PRICE, "reference price is stale")

    if manual_request is not None:
        return exit_(
            DECISION_EXIT_MANUAL_REQUEST,
            f"manual exit requested by {manual_request['operator']!r}: {manual_request['reason']}",
        )

    if cost_basis_per_share is not None and cost_basis_per_share > 0:
        fixed_stop = cost_basis_per_share * (Decimal("1") - stop_loss_percent)
        stop_threshold = max(fixed_stop, current_stop_price or fixed_stop)
        if reference_price <= stop_threshold:
            stop_decision = (
                DECISION_EXIT_TRAILING_STOP if trailing_stop_active
                else DECISION_EXIT_BREAKEVEN_STOP if breakeven_active
                else DECISION_EXIT_STOP_LOSS
            )
            return exit_(
                stop_decision,
                f"reference_price {reference_price} <= stop threshold {stop_threshold} "
                f"(deterministic long stop; fixed baseline {fixed_stop})",
            )

    if safety_full_exit:
        return exit_(DECISION_EXIT_SAFETY, "deterministic safety policy requires a full risk-reducing exit")

    if position_opened_at is not None:
        held = market_days_held(position_opened_at.date(), as_of.date())
        if held >= maximum_holding_market_days:
            return exit_(
                DECISION_EXIT_MAX_HOLDING_PERIOD,
                f"held {held} market days >= maximum_holding_market_days {maximum_holding_market_days}",
            )

    if exit_on_recommendation_reversal and reversal_recommendation is not None:
        return exit_(
            DECISION_EXIT_RECOMMENDATION_REVERSAL,
            f"newer recommendation {reversal_recommendation['rec_id']!r} at "
            f"{reversal_recommendation['ts']} reversed to side={reversal_recommendation['side']!r} "
            f"status={reversal_recommendation['status']!r}",
        )

    if partial_stage_id is not None and partial_close_quantity is not None and partial_close_quantity > 0:
        return exit_(
            DECISION_EXIT_PARTIAL_PROFIT,
            f"partial-profit stage {partial_stage_id} approved {partial_close_quantity} whole shares",
        )

    if cost_basis_per_share is not None and cost_basis_per_share > 0:
        profit_threshold = initial_target_price or (
            cost_basis_per_share * (Decimal("1") + profit_target_percent)
        )
        if reference_price >= profit_threshold:
            return exit_(
                DECISION_EXIT_PROFIT_TARGET,
                f"reference_price {reference_price} >= profit threshold {profit_threshold}",
            )

    return hold_or_skip(DECISION_HOLD, "no exit condition met")


def is_reversal_recommendation(row: dict) -> bool:
    """A recommendation row (`rec_id`/`side`/`status`/`ts`) qualifies as a
    reversal signal — see `REVERSAL_SIDES`/`REVERSAL_STATUS` module docstring
    above for the documented definition."""
    return row["status"] == REVERSAL_STATUS and row["side"] in REVERSAL_SIDES
