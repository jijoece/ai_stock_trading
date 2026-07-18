"""Typed, immutable domain models for the isolated paper-book subsystem
(docs/milestone-8.md Steps 4, 8, 10, 11, 12).

Mirrors the existing repository convention of `@dataclass(frozen=True)` with
`__post_init__` validation (see `execution/models.py`, `models/trading_models.py`)
rather than pydantic. None of these types are ever constructed from Claude
output — every field here is either configuration, a deterministic
computation, or a value copied from an already-frozen recommendation
(docs/milestone-8.md "Authority model").
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..hashing import hash_config
from ..research.models import EXPERIMENT_ARMS

BOOK_STATUS_ACTIVE = "ACTIVE"
BOOK_STATUS_PAUSED = "PAUSED"
BOOK_STATUS_CLOSED = "CLOSED"
KNOWN_BOOK_STATUSES = (BOOK_STATUS_ACTIVE, BOOK_STATUS_PAUSED, BOOK_STATUS_CLOSED)

VALUATION_COMPLETE = "COMPLETE"
VALUATION_PARTIAL_MISSING_PRICE = "PARTIAL_MISSING_PRICE"
VALUATION_PARTIAL_STALE_PRICE = "PARTIAL_STALE_PRICE"
VALUATION_POINT_IN_TIME_UNSAFE = "POINT_IN_TIME_UNSAFE"
VALUATION_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
KNOWN_VALUATION_STATUSES = (
    VALUATION_COMPLETE, VALUATION_PARTIAL_MISSING_PRICE, VALUATION_PARTIAL_STALE_PRICE,
    VALUATION_POINT_IN_TIME_UNSAFE, VALUATION_SOURCE_UNAVAILABLE,
)

RISK_APPROVED = "APPROVED"
RISK_APPROVED_REDUCED = "APPROVED_REDUCED"
RISK_REJECTED_INSUFFICIENT_CASH = "REJECTED_INSUFFICIENT_CASH"
RISK_REJECTED_MAX_POSITION_WEIGHT = "REJECTED_MAX_POSITION_WEIGHT"
RISK_REJECTED_MAX_SYMBOL_CONCENTRATION = "REJECTED_MAX_SYMBOL_CONCENTRATION"
RISK_REJECTED_MAX_OPEN_POSITIONS = "REJECTED_MAX_OPEN_POSITIONS"
RISK_REJECTED_DAILY_NOTIONAL_LIMIT = "REJECTED_DAILY_NOTIONAL_LIMIT"
RISK_REJECTED_STALE_PRICE = "REJECTED_STALE_PRICE"
RISK_REJECTED_MISSING_PRICE = "REJECTED_MISSING_PRICE"
RISK_REJECTED_INVALID_RECOMMENDATION = "REJECTED_INVALID_RECOMMENDATION"
RISK_REJECTED_BOOK_PAUSED = "REJECTED_BOOK_PAUSED"
RISK_REJECTED_ARM_MISMATCH = "REJECTED_ARM_MISMATCH"
RISK_REJECTED_DAILY_LOSS_LIMIT = "RISK_REJECTED_DAILY_LOSS_LIMIT"
RISK_REJECTED_DRAWDOWN_LIMIT = "RISK_REJECTED_DRAWDOWN_LIMIT"
RISK_REJECTED_RISK_STATE_UNAVAILABLE = "RISK_REJECTED_RISK_STATE_UNAVAILABLE"
RISK_REJECTED_RISK_STATE_STALE = "RISK_REJECTED_RISK_STATE_STALE"
RISK_REJECTED_RISK_STATE_UNRECONCILED = "RISK_REJECTED_RISK_STATE_UNRECONCILED"
RISK_REJECTED_ECONOMIC_EVENT_BLACKOUT = "RISK_REJECTED_ECONOMIC_EVENT_BLACKOUT"
KNOWN_RISK_DECISIONS = (
    RISK_APPROVED, RISK_APPROVED_REDUCED, RISK_REJECTED_INSUFFICIENT_CASH,
    RISK_REJECTED_MAX_POSITION_WEIGHT, RISK_REJECTED_MAX_SYMBOL_CONCENTRATION,
    RISK_REJECTED_MAX_OPEN_POSITIONS, RISK_REJECTED_DAILY_NOTIONAL_LIMIT,
    RISK_REJECTED_STALE_PRICE, RISK_REJECTED_MISSING_PRICE, RISK_REJECTED_INVALID_RECOMMENDATION,
    RISK_REJECTED_BOOK_PAUSED, RISK_REJECTED_ARM_MISMATCH,
    RISK_REJECTED_DAILY_LOSS_LIMIT, RISK_REJECTED_DRAWDOWN_LIMIT,
    RISK_REJECTED_RISK_STATE_UNAVAILABLE, RISK_REJECTED_RISK_STATE_STALE,
    RISK_REJECTED_RISK_STATE_UNRECONCILED, RISK_REJECTED_ECONOMIC_EVENT_BLACKOUT,
)
APPROVED_RISK_DECISIONS = (RISK_APPROVED, RISK_APPROVED_REDUCED)

ORDER_SIDE_BUY = "BUY"
ORDER_SIDE_SELL = "SELL"
KNOWN_ORDER_SIDES = (ORDER_SIDE_BUY, ORDER_SIDE_SELL)

ORDER_TYPE_LIMIT = "LIMIT"
KNOWN_ORDER_TYPES = (ORDER_TYPE_LIMIT,)  # limit orders only — docs/milestone-8.md Step 12

INTENT_STATUS_PENDING_SUBMISSION = "PENDING_SUBMISSION"
INTENT_STATUS_SUBMITTED = "SUBMITTED"
INTENT_STATUS_FILLED = "FILLED"
INTENT_STATUS_PARTIALLY_FILLED = "PARTIALLY_FILLED"
INTENT_STATUS_CANCELLED = "CANCELLED"
INTENT_STATUS_EXPIRED = "EXPIRED"
INTENT_STATUS_REJECTED = "REJECTED"
KNOWN_INTENT_STATUSES = (
    INTENT_STATUS_PENDING_SUBMISSION, INTENT_STATUS_SUBMITTED, INTENT_STATUS_FILLED,
    INTENT_STATUS_PARTIALLY_FILLED, INTENT_STATUS_CANCELLED, INTENT_STATUS_EXPIRED,
    INTENT_STATUS_REJECTED,
)

POLICY_VERSION = "paper-books-risk-v1"


class PaperBookModelError(ValueError):
    """A paper-book domain type failed a fail-closed validation check."""


def _require_tz_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise PaperBookModelError(f"{name} must be timezone-aware")


def derive_paper_order_intent_id(recommendation_id: str, book_id: str, execution_version: str) -> str:
    """Deterministic idempotency key that includes book_id, so the same
    recommendation submitted to two different books always produces two
    different intent IDs (docs/milestone-8.md Step 12)."""
    digest = hashlib.sha256(f"{recommendation_id}:{book_id}:{execution_version}".encode()).hexdigest()[:32]
    return f"pb-intent-{digest}"




@dataclass(frozen=True)
class PaperBook:
    book_id: str
    experiment_arm: str
    currency: str
    starting_cash_usd: Decimal
    status: str
    created_at: datetime
    config_hash: str

    def __post_init__(self) -> None:
        if self.experiment_arm not in EXPERIMENT_ARMS:
            raise PaperBookModelError(f"experiment_arm {self.experiment_arm!r} not one of {EXPERIMENT_ARMS}")
        if self.currency != "USD":
            raise PaperBookModelError(f"currency {self.currency!r} not supported — only USD")
        if self.starting_cash_usd <= 0:
            raise PaperBookModelError("starting_cash_usd must be > 0")
        if self.status not in KNOWN_BOOK_STATUSES:
            raise PaperBookModelError(f"status {self.status!r} not one of {KNOWN_BOOK_STATUSES}")
        _require_tz_aware(self.created_at, "created_at")
        if not self.config_hash:
            raise PaperBookModelError("config_hash is required")
        if not self.book_id:
            raise PaperBookModelError("book_id is required")


@dataclass(frozen=True)
class PaperPortfolioSnapshot:
    snapshot_id: str
    book_id: str
    as_of: datetime
    cash_available_usd: Decimal
    cash_reserved_usd: Decimal
    gross_market_value_usd: Decimal | None
    net_liquidation_value_usd: Decimal | None
    total_cost_basis_usd: Decimal
    unrealized_pnl_usd: Decimal | None
    realized_pnl_usd: Decimal
    position_count: int
    unvalued_position_count: int
    stale_position_count: int
    valuation_status: str
    source_hash: str

    def __post_init__(self) -> None:
        _require_tz_aware(self.as_of, "as_of")
        if self.valuation_status not in KNOWN_VALUATION_STATUSES:
            raise PaperBookModelError(f"valuation_status {self.valuation_status!r} not known")
        if self.cash_available_usd < 0 or self.cash_reserved_usd < 0:
            raise PaperBookModelError("cash values must not be negative")
        if self.valuation_status == VALUATION_COMPLETE and self.net_liquidation_value_usd is None:
            raise PaperBookModelError("COMPLETE valuation must have a net_liquidation_value_usd")
        if self.position_count < 0 or self.unvalued_position_count < 0 or self.stale_position_count < 0:
            raise PaperBookModelError("position counts must not be negative")
        if self.unvalued_position_count > self.position_count:
            raise PaperBookModelError("unvalued_position_count cannot exceed position_count")
        if self.stale_position_count > self.position_count:
            raise PaperBookModelError("stale_position_count cannot exceed position_count")
        if not self.snapshot_id:
            raise PaperBookModelError("snapshot_id is required")
        if not self.source_hash:
            raise PaperBookModelError("source_hash is required")


def compute_snapshot_id(payload: dict) -> str:
    """Content hash over the full canonical snapshot-identity payload
    (`valuation.py::_snapshot_identity_payload` — book_id, as_of, and every
    economically material cash/ledger/position/price input feeding the
    snapshot). Identical inputs always reproduce the same ID; a changed
    input always produces a different one (docs/milestone-8.md Step 8;
    Milestone 11.3.1 Item 3 widened the payload to cover cash and ledger
    state, not just position/price). Uses `hash_config` rather than
    `json.dumps(..., default=str)`: an unsupported value fails loudly
    instead of being silently stringified, and sorted keys make the result
    independent of the caller's dict/insertion ordering."""
    return f"pb-snap-{hash_config(payload)}"


@dataclass(frozen=True)
class PaperPortfolioContext:
    book_id: str
    as_of: datetime
    available_cash_usd: Decimal
    reserved_cash_usd: Decimal
    net_liquidation_value_usd: Decimal | None
    current_position_quantity: Decimal
    current_position_market_value_usd: Decimal | None
    current_position_weight: Decimal | None
    open_position_count: int
    daily_new_notional_usd: Decimal
    valuation_status: str

    def __post_init__(self) -> None:
        _require_tz_aware(self.as_of, "as_of")
        if self.valuation_status not in KNOWN_VALUATION_STATUSES:
            raise PaperBookModelError(f"valuation_status {self.valuation_status!r} not known")
        if self.available_cash_usd < 0 or self.reserved_cash_usd < 0:
            raise PaperBookModelError("cash values must not be negative")
        if self.current_position_quantity < 0:
            raise PaperBookModelError("current_position_quantity must not be negative (long-only)")
        if self.open_position_count < 0:
            raise PaperBookModelError("open_position_count must not be negative")
        if self.daily_new_notional_usd < 0:
            raise PaperBookModelError("daily_new_notional_usd must not be negative")


@dataclass(frozen=True)
class PaperRiskDecision:
    decision: str
    requested_notional_usd: Decimal | None
    approved_notional_usd: Decimal | None
    approved_quantity: Decimal | None
    reasons: tuple[str, ...]
    policy_version: str

    def __post_init__(self) -> None:
        if self.decision not in KNOWN_RISK_DECISIONS:
            raise PaperBookModelError(f"decision {self.decision!r} not one of {KNOWN_RISK_DECISIONS}")
        if self.decision in APPROVED_RISK_DECISIONS:
            if self.approved_notional_usd is None or self.approved_quantity is None:
                raise PaperBookModelError("an approved decision must carry approved_notional_usd/approved_quantity")
            if self.approved_notional_usd <= 0 or self.approved_quantity <= 0:
                raise PaperBookModelError("an approved decision must have positive approved amounts")
        else:
            if self.approved_notional_usd is not None or self.approved_quantity is not None:
                raise PaperBookModelError("a rejected decision must not carry approved amounts")
            if not self.reasons:
                raise PaperBookModelError("a rejected decision must record at least one reason")
        if not self.policy_version:
            raise PaperBookModelError("policy_version is required")


@dataclass(frozen=True)
class PaperBookOrderIntent:
    paper_order_intent_id: str
    book_id: str
    experiment_arm: str
    cycle_id: str
    recommendation_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal
    notional_usd: Decimal
    time_in_force: str
    as_of: datetime
    risk_decision_id: str
    portfolio_snapshot_id: str
    config_hash: str
    created_at: datetime
    status: str = INTENT_STATUS_PENDING_SUBMISSION

    def __post_init__(self) -> None:
        if self.experiment_arm not in EXPERIMENT_ARMS:
            raise PaperBookModelError(f"experiment_arm {self.experiment_arm!r} not one of {EXPERIMENT_ARMS}")
        if self.side not in KNOWN_ORDER_SIDES:
            raise PaperBookModelError(f"side {self.side!r} not one of {KNOWN_ORDER_SIDES}")
        if self.order_type not in KNOWN_ORDER_TYPES:
            raise PaperBookModelError(f"order_type {self.order_type!r} not one of {KNOWN_ORDER_TYPES} (limit-only)")
        if self.quantity <= 0:
            raise PaperBookModelError("quantity must be positive")
        if self.quantity % 1 != 0:
            raise PaperBookModelError("quantity must be a whole number (no fractional shares in this milestone)")
        if self.limit_price <= 0:
            raise PaperBookModelError("limit_price must be positive")
        if self.notional_usd <= 0:
            raise PaperBookModelError("notional_usd must be positive")
        expected_notional = self.quantity * self.limit_price
        if self.notional_usd != expected_notional:
            raise PaperBookModelError(
                f"notional_usd {self.notional_usd} does not reconstruct from "
                f"quantity * limit_price ({expected_notional})"
            )
        if self.status not in KNOWN_INTENT_STATUSES:
            raise PaperBookModelError(f"status {self.status!r} not one of {KNOWN_INTENT_STATUSES}")
        for name, value in (("as_of", self.as_of), ("created_at", self.created_at)):
            _require_tz_aware(value, name)
        for name, value in (
            ("paper_order_intent_id", self.paper_order_intent_id), ("book_id", self.book_id),
            ("cycle_id", self.cycle_id), ("recommendation_id", self.recommendation_id),
            ("symbol", self.symbol), ("time_in_force", self.time_in_force),
            ("risk_decision_id", self.risk_decision_id), ("portfolio_snapshot_id", self.portfolio_snapshot_id),
            ("config_hash", self.config_hash),
        ):
            if not value:
                raise PaperBookModelError(f"{name} is required")


@dataclass(frozen=True)
class CashLedgerEntry:
    book_id: str
    ledger_entry_id: str
    event_type: str
    amount_usd: Decimal
    event_timestamp: datetime
    idempotency_key: str
    reference_id: str | None = None
    operator: str | None = None
    reason: str | None = None
    cycle_id: str | None = None
    symbol: str | None = None

    def __post_init__(self) -> None:
        _require_tz_aware(self.event_timestamp, "event_timestamp")
        if not self.book_id or not self.ledger_entry_id or not self.idempotency_key:
            raise PaperBookModelError("book_id, ledger_entry_id, and idempotency_key are required")
        if self.event_type == "CASH_ADJUSTMENT" and (not self.operator or not self.reason):
            raise PaperBookModelError("CASH_ADJUSTMENT requires operator and reason")


CASH_EVENT_INITIAL_CAPITAL = "INITIAL_CAPITAL"
CASH_EVENT_BUY_RESERVATION = "BUY_RESERVATION"
CASH_EVENT_BUY_SETTLEMENT = "BUY_SETTLEMENT"
CASH_EVENT_SELL_SETTLEMENT = "SELL_SETTLEMENT"
CASH_EVENT_FEE = "FEE"
CASH_EVENT_SLIPPAGE = "SLIPPAGE"
CASH_EVENT_DIVIDEND = "DIVIDEND"
CASH_EVENT_CASH_ADJUSTMENT = "CASH_ADJUSTMENT"
CASH_EVENT_ORDER_RELEASE = "ORDER_RELEASE"
KNOWN_CASH_EVENT_TYPES = (
    CASH_EVENT_INITIAL_CAPITAL, CASH_EVENT_BUY_RESERVATION, CASH_EVENT_BUY_SETTLEMENT,
    CASH_EVENT_SELL_SETTLEMENT, CASH_EVENT_FEE, CASH_EVENT_SLIPPAGE, CASH_EVENT_DIVIDEND,
    CASH_EVENT_CASH_ADJUSTMENT, CASH_EVENT_ORDER_RELEASE,
)
