"""Deterministic, long-only paper-book exit policy (Milestone 9,
docs/milestone-9.md Section 2-3).

Pure functions over already-computed, typed inputs — never touches Claude
output, never sizes a position, never decides an *entry*. Full-position
exits only (no partial exits) in this milestone. The same inputs always
produce the same `PaperExitDecision` (deterministic, persisted, versioned).
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
DECISION_SKIPPED_MISSING_PRICE = "SKIPPED_MISSING_PRICE"
DECISION_SKIPPED_STALE_PRICE = "SKIPPED_STALE_PRICE"
DECISION_SKIPPED_POINT_IN_TIME_UNSAFE = "SKIPPED_POINT_IN_TIME_UNSAFE"
DECISION_SKIPPED_NO_POSITION = "SKIPPED_NO_POSITION"

KNOWN_EXIT_DECISIONS = (
    DECISION_HOLD, DECISION_EXIT_STOP_LOSS, DECISION_EXIT_PROFIT_TARGET,
    DECISION_EXIT_MAX_HOLDING_PERIOD, DECISION_EXIT_RECOMMENDATION_REVERSAL,
    DECISION_EXIT_MANUAL_REQUEST, DECISION_SKIPPED_MISSING_PRICE, DECISION_SKIPPED_STALE_PRICE,
    DECISION_SKIPPED_POINT_IN_TIME_UNSAFE, DECISION_SKIPPED_NO_POSITION,
)

# A position exits, never a HOLD/SKIPPED outcome.
EXIT_DECISIONS = (
    DECISION_EXIT_STOP_LOSS, DECISION_EXIT_PROFIT_TARGET, DECISION_EXIT_MAX_HOLDING_PERIOD,
    DECISION_EXIT_RECOMMENDATION_REVERSAL, DECISION_EXIT_MANUAL_REQUEST,
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
) -> PaperExitDecision:
    """Fixed, documented check order — same inputs always produce the same
    decision:

    1. no open long position -> SKIPPED_NO_POSITION
    2. price unavailable -> SKIPPED_MISSING_PRICE
    3. price not point-in-time safe -> SKIPPED_POINT_IN_TIME_UNSAFE
    4. price stale -> SKIPPED_STALE_PRICE
    5. an unconsumed manual exit request exists -> EXIT_MANUAL_REQUEST
       (an explicit, audited human instruction outranks the automatic rules)
    6. stop loss (price <= cost_basis * (1 - stop_loss_percent))
    7. profit target (price >= cost_basis * (1 + profit_target_percent))
    8. maximum holding period (market days held >= configured maximum)
    9. recommendation reversal (only when enabled and a qualifying newer
       recommendation was supplied by the caller)
    10. otherwise HOLD

    Full-position exits only: `quantity` on any EXIT_* decision is always
    the entire `position_quantity`. Missing/stale/unsafe prices never reach
    the trigger rules, so they can never fabricate an exit.
    """

    def hold_or_skip(decision: str, *reasons: str) -> PaperExitDecision:
        return PaperExitDecision(
            decision=decision, book_id=book_id, symbol=symbol, quantity=Decimal("0"),
            reference_price=reference_price, reasons=tuple(reasons), policy_version=policy_version,
        )

    def exit_(decision: str, *reasons: str) -> PaperExitDecision:
        return PaperExitDecision(
            decision=decision, book_id=book_id, symbol=symbol, quantity=position_quantity,
            reference_price=reference_price, reasons=tuple(reasons), policy_version=policy_version,
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
        stop_threshold = cost_basis_per_share * (Decimal("1") - stop_loss_percent)
        if reference_price <= stop_threshold:
            return exit_(
                DECISION_EXIT_STOP_LOSS,
                f"reference_price {reference_price} <= stop threshold {stop_threshold} "
                f"(cost_basis {cost_basis_per_share} * (1 - {stop_loss_percent}))",
            )
        profit_threshold = cost_basis_per_share * (Decimal("1") + profit_target_percent)
        if reference_price >= profit_threshold:
            return exit_(
                DECISION_EXIT_PROFIT_TARGET,
                f"reference_price {reference_price} >= profit threshold {profit_threshold} "
                f"(cost_basis {cost_basis_per_share} * (1 + {profit_target_percent}))",
            )

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

    return hold_or_skip(DECISION_HOLD, "no exit condition met")


def is_reversal_recommendation(row: dict) -> bool:
    """A recommendation row (`rec_id`/`side`/`status`/`ts`) qualifies as a
    reversal signal — see `REVERSAL_SIDES`/`REVERSAL_STATUS` module docstring
    above for the documented definition."""
    return row["status"] == REVERSAL_STATUS and row["side"] in REVERSAL_SIDES
