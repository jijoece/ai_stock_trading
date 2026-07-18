"""Deterministic ATR lifecycle state, stop advancement, and partial exits."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from ..analysis.indicators import atr_risk_levels

LIFECYCLE_POLICY_VERSION = "paper-book-position-lifecycle-v1"

STOP_REASON_INITIAL = "INITIAL_ATR_STOP"
STOP_REASON_BREAKEVEN = "BREAKEVEN_ACTIVATED"
STOP_REASON_TRAILING = "TRAILING_STOP_ADVANCED"
STOP_REASON_UNCHANGED = "UNCHANGED"
EVALUATION_INCOMPLETE_STALE_PRICE = "INCOMPLETE_STALE_OR_UNSAFE_PRICE"
EVALUATION_INCOMPLETE_ATR = "INCOMPLETE_ATR_UNAVAILABLE"


class PositionLifecycleError(ValueError):
    pass


@dataclass(frozen=True)
class PositionLifecycleState:
    lifecycle_state_id: str
    book_id: str
    symbol: str
    originating_intent_id: str
    entry_fill_id: str
    opened_at: datetime
    original_quantity: Decimal
    remaining_quantity: Decimal
    average_entry_price: Decimal
    entry_atr: Decimal
    atr_period: int
    initial_stop_price: Decimal
    current_stop_price: Decimal
    initial_target_price: Decimal
    highest_eligible_price_since_entry: Decimal
    trailing_stop_active: bool
    breakeven_active: bool
    partial_profit_stage: int
    policy_version: str
    config_hash: str
    last_evaluated_at: datetime
    source_market_data_id: str
    stop_change_reason: str = STOP_REASON_INITIAL

    def __post_init__(self) -> None:
        if self.opened_at.tzinfo is None or self.last_evaluated_at.tzinfo is None:
            raise PositionLifecycleError("lifecycle timestamps must be timezone-aware")
        if self.original_quantity <= 0 or self.remaining_quantity < 0:
            raise PositionLifecycleError("position quantities are invalid")
        if self.remaining_quantity > self.original_quantity:
            raise PositionLifecycleError("remaining quantity cannot exceed original quantity")
        if self.original_quantity != self.original_quantity.to_integral_value():
            raise PositionLifecycleError("fractional shares are unavailable")
        if self.remaining_quantity != self.remaining_quantity.to_integral_value():
            raise PositionLifecycleError("fractional shares are unavailable")
        if self.entry_atr <= 0 or self.atr_period <= 0:
            raise PositionLifecycleError("entry ATR and period must be positive")
        if not (Decimal("0") < self.current_stop_price <= self.average_entry_price or self.breakeven_active or self.trailing_stop_active):
            raise PositionLifecycleError("initial lifecycle stop is invalid")
        if self.current_stop_price < self.initial_stop_price:
            raise PositionLifecycleError("a long stop may never loosen below its initial value")


@dataclass(frozen=True)
class LifecycleTransition:
    previous_state_id: str
    state: PositionLifecycleState
    complete: bool
    reasons: tuple[str, ...]
    current_r_multiple: Decimal


def _state_id(
    *, book_id: str, symbol: str, source_id: str, evaluated_at: datetime,
    stop: Decimal, remaining: Decimal, stage: int,
) -> str:
    payload = f"{book_id}:{symbol}:{source_id}:{evaluated_at.isoformat()}:{stop}:{remaining}:{stage}"
    return "pb-lifecycle-state-" + hashlib.sha256(payload.encode()).hexdigest()[:40]


def create_entry_lifecycle_state(
    *, book_id: str, symbol: str, originating_intent_id: str, entry_fill_id: str,
    opened_at: datetime, quantity: Decimal, average_entry_price: Decimal,
    entry_atr: Decimal, atr_period: int, initial_stop_multiple: Decimal,
    initial_target_multiple: Decimal, policy_version: str,
    config_hash: str, source_market_data_id: str,
) -> PositionLifecycleState:
    if quantity <= 0 or quantity != quantity.to_integral_value(rounding=ROUND_FLOOR):
        raise PositionLifecycleError("entry quantity must be positive whole shares")
    stop, target = atr_risk_levels(
        entry_price=average_entry_price, atr=entry_atr,
        stop_multiple=initial_stop_multiple, target_multiple=initial_target_multiple,
    )
    return PositionLifecycleState(
        lifecycle_state_id=_state_id(
            book_id=book_id, symbol=symbol, source_id=source_market_data_id,
            evaluated_at=opened_at, stop=stop, remaining=quantity, stage=0,
        ),
        book_id=book_id, symbol=symbol, originating_intent_id=originating_intent_id,
        entry_fill_id=entry_fill_id, opened_at=opened_at, original_quantity=quantity,
        remaining_quantity=quantity, average_entry_price=average_entry_price,
        entry_atr=entry_atr, atr_period=atr_period, initial_stop_price=stop,
        current_stop_price=stop, initial_target_price=target,
        highest_eligible_price_since_entry=average_entry_price,
        trailing_stop_active=False, breakeven_active=False, partial_profit_stage=0,
        policy_version=policy_version, config_hash=config_hash,
        last_evaluated_at=opened_at, source_market_data_id=source_market_data_id,
    )


def advance_lifecycle_state(
    state: PositionLifecycleState, *, as_of: datetime,
    reference_price: Decimal | None, price_is_stale: bool,
    price_point_in_time_safe: bool | None, current_atr: Decimal | None,
    source_market_data_id: str,
    breakeven_enabled: bool, breakeven_activation_r_multiple: Decimal,
    breakeven_offset_bps: Decimal,
    trailing_enabled: bool, trailing_activation_r_multiple: Decimal,
    trailing_atr_multiple: Decimal,
) -> LifecycleTransition:
    if as_of.tzinfo is None:
        raise PositionLifecycleError("as_of must be timezone-aware")
    risk_per_share = state.average_entry_price - state.initial_stop_price
    if risk_per_share <= 0:
        raise PositionLifecycleError("initial risk per share must be positive")
    if reference_price is None or reference_price <= 0 or price_is_stale or price_point_in_time_safe is not True:
        return LifecycleTransition(
            previous_state_id=state.lifecycle_state_id, state=state, complete=False,
            reasons=(EVALUATION_INCOMPLETE_STALE_PRICE,), current_r_multiple=Decimal("0"),
        )

    current_r = (reference_price - state.average_entry_price) / risk_per_share
    highest = max(state.highest_eligible_price_since_entry, reference_price)
    stop = state.current_stop_price
    breakeven_active = state.breakeven_active
    trailing_active = state.trailing_stop_active
    reasons: list[str] = []

    if breakeven_enabled and current_r >= breakeven_activation_r_multiple:
        breakeven_active = True
        candidate = state.average_entry_price * (
            Decimal("1") + breakeven_offset_bps / Decimal("10000")
        )
        if candidate > stop:
            stop = candidate
            reasons.append(STOP_REASON_BREAKEVEN)

    complete = True
    if trailing_enabled and current_r >= trailing_activation_r_multiple:
        trailing_active = True
        if current_atr is None or current_atr <= 0:
            complete = False
            reasons.append(EVALUATION_INCOMPLETE_ATR)
        else:
            candidate = highest - current_atr * trailing_atr_multiple
            if candidate > stop:
                stop = candidate
                reasons.append(STOP_REASON_TRAILING)

    reason = reasons[-1] if reasons and reasons[-1] in (
        STOP_REASON_BREAKEVEN, STOP_REASON_TRAILING
    ) else STOP_REASON_UNCHANGED
    if not reasons:
        reasons.append(STOP_REASON_UNCHANGED)
    updated = replace(
        state,
        lifecycle_state_id=_state_id(
            book_id=state.book_id, symbol=state.symbol, source_id=source_market_data_id,
            evaluated_at=as_of, stop=stop, remaining=state.remaining_quantity,
            stage=state.partial_profit_stage,
        ),
        current_stop_price=max(state.current_stop_price, stop),
        highest_eligible_price_since_entry=highest,
        trailing_stop_active=trailing_active, breakeven_active=breakeven_active,
        last_evaluated_at=as_of, source_market_data_id=source_market_data_id,
        stop_change_reason=reason,
    )
    return LifecycleTransition(
        previous_state_id=state.lifecycle_state_id, state=updated, complete=complete,
        reasons=tuple(reasons), current_r_multiple=current_r,
    )


def calculate_partial_close_quantity(
    *, original_quantity: Decimal, close_fraction: Decimal,
    available_unreserved_quantity: Decimal, current_remaining_quantity: Decimal,
    minimum_remaining_quantity: Decimal,
) -> Decimal:
    """Whole-share stage sizing based on original, never reduced, quantity."""
    values = (
        original_quantity, available_unreserved_quantity,
        current_remaining_quantity, minimum_remaining_quantity,
    )
    if any(value < 0 or value != value.to_integral_value() for value in values):
        raise PositionLifecycleError("partial-exit quantities must be non-negative whole shares")
    if not (Decimal("0") < close_fraction <= Decimal("1")):
        raise PositionLifecycleError("close_fraction must be in (0,1]")
    target = (original_quantity * close_fraction).to_integral_value(rounding=ROUND_FLOOR)
    remaining_capacity = max(Decimal("0"), current_remaining_quantity - minimum_remaining_quantity)
    return max(Decimal("0"), min(target, available_unreserved_quantity, remaining_capacity))


def next_partial_stage(
    *, state: PositionLifecycleState, current_r_multiple: Decimal,
    stages: tuple[object, ...], available_unreserved_quantity: Decimal,
    minimum_remaining_quantity: Decimal,
) -> tuple[int, Decimal] | None:
    """Return the first not-yet-completed eligible stage and whole quantity."""
    for stage in stages:
        stage_id = int(getattr(stage, "stage"))
        if stage_id <= state.partial_profit_stage:
            continue
        if current_r_multiple < getattr(stage, "trigger_r_multiple"):
            return None
        quantity = calculate_partial_close_quantity(
            original_quantity=state.original_quantity,
            close_fraction=getattr(stage, "close_fraction"),
            available_unreserved_quantity=available_unreserved_quantity,
            current_remaining_quantity=state.remaining_quantity,
            minimum_remaining_quantity=minimum_remaining_quantity,
        )
        return stage_id, quantity
    return None


def lifecycle_state_from_row(row: dict) -> PositionLifecycleState:
    return PositionLifecycleState(
        lifecycle_state_id=row["lifecycle_state_id"], book_id=row["book_id"], symbol=row["symbol"],
        originating_intent_id=row["originating_intent_id"], entry_fill_id=row["entry_fill_id"],
        opened_at=datetime.fromisoformat(row["opened_at"]), original_quantity=Decimal(row["original_quantity"]),
        remaining_quantity=Decimal(row["remaining_quantity"]), average_entry_price=Decimal(row["average_entry_price"]),
        entry_atr=Decimal(row["entry_atr"]), atr_period=int(row["atr_period"]),
        initial_stop_price=Decimal(row["initial_stop_price"]), current_stop_price=Decimal(row["current_stop_price"]),
        initial_target_price=Decimal(row["initial_target_price"]),
        highest_eligible_price_since_entry=Decimal(row["highest_eligible_price_since_entry"]),
        trailing_stop_active=bool(row["trailing_stop_active"]), breakeven_active=bool(row["breakeven_active"]),
        partial_profit_stage=int(row["partial_profit_stage"]), policy_version=row["policy_version"],
        config_hash=row["config_hash"], last_evaluated_at=datetime.fromisoformat(row["last_evaluated_at"]),
        source_market_data_id=row["source_market_data_id"], stop_change_reason=row["stop_change_reason"],
    )


def apply_completed_partial_stage(
    state: PositionLifecycleState, *, stage_id: int, filled_quantity: Decimal,
    as_of: datetime, source_market_data_id: str,
) -> PositionLifecycleState:
    if stage_id <= state.partial_profit_stage:
        raise PositionLifecycleError("partial-profit stage cannot complete twice")
    if filled_quantity <= 0 or filled_quantity > state.remaining_quantity:
        raise PositionLifecycleError("partial-profit fill quantity is invalid")
    remaining = state.remaining_quantity - filled_quantity
    return replace(
        state,
        lifecycle_state_id=_state_id(
            book_id=state.book_id, symbol=state.symbol, source_id=source_market_data_id,
            evaluated_at=as_of, stop=state.current_stop_price, remaining=remaining, stage=stage_id,
        ),
        remaining_quantity=remaining, partial_profit_stage=stage_id,
        last_evaluated_at=as_of, source_market_data_id=source_market_data_id,
        stop_change_reason=STOP_REASON_UNCHANGED,
    )


def state_from_row(row: dict) -> PositionLifecycleState:
    return PositionLifecycleState(
        lifecycle_state_id=row["lifecycle_state_id"], book_id=row["book_id"], symbol=row["symbol"],
        originating_intent_id=row["originating_intent_id"], entry_fill_id=row["entry_fill_id"],
        opened_at=datetime.fromisoformat(row["opened_at"]), original_quantity=Decimal(row["original_quantity"]),
        remaining_quantity=Decimal(row["remaining_quantity"]),
        average_entry_price=Decimal(row["average_entry_price"]), entry_atr=Decimal(row["entry_atr"]),
        atr_period=int(row["atr_period"]), initial_stop_price=Decimal(row["initial_stop_price"]),
        current_stop_price=Decimal(row["current_stop_price"]),
        initial_target_price=Decimal(row["initial_target_price"]),
        highest_eligible_price_since_entry=Decimal(row["highest_eligible_price_since_entry"]),
        trailing_stop_active=bool(row["trailing_stop_active"]), breakeven_active=bool(row["breakeven_active"]),
        partial_profit_stage=int(row["partial_profit_stage"]), policy_version=row["policy_version"],
        config_hash=row["config_hash"], last_evaluated_at=datetime.fromisoformat(row["last_evaluated_at"]),
        source_market_data_id=row["source_market_data_id"], stop_change_reason=row["stop_change_reason"],
    )
