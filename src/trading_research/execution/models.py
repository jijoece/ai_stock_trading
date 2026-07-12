"""Framework-neutral paper-execution contracts (Milestone 3, docs/milestone-3.md Step 3).

Implemented as `@dataclass(frozen=True)` with `__post_init__` validation —
matching every existing domain model in this repository
(`models/trading_models.py`, `analysis/screener.py::ScreeningConfig`,
`recommendations/builder.py::FrozenRecommendation`) — rather than the
`pydantic.BaseModel` shown illustratively in docs/milestone-3.md. Pydantic is
not a dependency anywhere else in this codebase; introducing it solely for
this slice would break an established convention for no behavioral benefit
(see docs/adr/0001-lumibot-paper-runtime.md).

None of these types ever appear in `schemas/recommendation.schema.json` or
touch a LumiBot object — LumiBot-specific types stay inside
`runtime/lumibot/`.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

ORDER_SIDE_BUY = "BUY"
ORDER_TYPE_MARKET = "MARKET"
ORDER_TYPE_LIMIT = "LIMIT"

EVENT_TYPES = (
    "SUBMITTED",
    "ACCEPTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELLED",
    "REJECTED",
    "ERROR",
)

RESULT_STATUSES = ("FILLED", "PARTIALLY_FILLED", "CANCELLED", "REJECTED", "ERROR")

RECONCILIATION_STATUSES = ("MATCHED", "PENDING", "MISMATCH", "MISSING_INTERNAL_EVENT", "MISSING_BROKER_EVENT")


class IntentValidationError(ValueError):
    """A PaperOrderIntent (or a related contract) failed a fail-closed check."""


def _require_tz_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise IntentValidationError(f"{name} must be timezone-aware")


def derive_intent_id(recommendation_id: str, execution_version: str) -> str:
    """Deterministic, stable idempotency key — never wall-clock or random.

    Same (recommendation_id, execution_version) always yields the same
    intent_id, mirroring `recommendations/builder.py::derive_rec_id` so a
    retried service invocation naturally maps to the same intent instead of
    minting a new one.
    """
    digest = hashlib.sha256(f"{recommendation_id}:{execution_version}".encode()).hexdigest()[:32]
    return f"intent-{digest}"


@dataclass(frozen=True)
class PaperOrderIntent:
    intent_id: str
    recommendation_id: str
    symbol: str
    side: str  # long-only in this milestone: always "BUY"
    quantity: int
    order_type: str  # "MARKET" | "LIMIT"
    limit_price: Decimal | None
    reference_price: Decimal
    expected_notional: Decimal
    recommendation_created_at: datetime
    recommendation_frozen_at: datetime
    expires_at: datetime
    config_hash: str
    git_sha: str
    policy_version: str
    execution_version: str

    def __post_init__(self) -> None:
        if self.side != ORDER_SIDE_BUY:
            raise IntentValidationError(f"side must be {ORDER_SIDE_BUY!r} (long-only) — got {self.side!r}")
        if not isinstance(self.quantity, int) or isinstance(self.quantity, bool):
            raise IntentValidationError("quantity must be an int (no fractional shares in this milestone)")
        if self.quantity <= 0:
            raise IntentValidationError(f"quantity must be a positive whole number — got {self.quantity}")
        if self.order_type not in (ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT):
            raise IntentValidationError(f"order_type must be MARKET or LIMIT — got {self.order_type!r}")
        if self.order_type == ORDER_TYPE_LIMIT:
            if self.limit_price is None or self.limit_price <= 0:
                raise IntentValidationError("LIMIT orders require a positive limit_price")
        if self.order_type == ORDER_TYPE_MARKET and self.limit_price is not None:
            raise IntentValidationError("MARKET orders must have limit_price=None")
        if self.reference_price is None or self.reference_price <= 0:
            raise IntentValidationError("reference_price must be a positive Decimal from persisted evidence")
        expected = self.reference_price * self.quantity
        if self.expected_notional != expected:
            raise IntentValidationError(
                f"expected_notional {self.expected_notional} does not reconstruct from "
                f"quantity * reference_price ({expected})"
            )
        for name, value in (
            ("recommendation_created_at", self.recommendation_created_at),
            ("recommendation_frozen_at", self.recommendation_frozen_at),
            ("expires_at", self.expires_at),
        ):
            _require_tz_aware(value, name)
        if self.expires_at <= self.recommendation_frozen_at:
            raise IntentValidationError("expires_at must be after recommendation_frozen_at")
        if not self.config_hash:
            raise IntentValidationError("config_hash is required")
        if not self.git_sha:
            raise IntentValidationError("git_sha is required")
        if not self.policy_version:
            raise IntentValidationError("policy_version is required")
        if not self.execution_version:
            raise IntentValidationError("execution_version is required")
        if not self.symbol:
            raise IntentValidationError("symbol is required")

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True)
class PaperExecutionEvent:
    event_id: str
    intent_id: str
    recommendation_id: str
    symbol: str
    event_type: str
    broker_order_id: str | None
    quantity: int
    filled_quantity: int
    fill_price: Decimal | None
    occurred_at: datetime
    raw_status: str | None
    source: str = "LUMIBOT_PAPER"

    def __post_init__(self) -> None:
        if self.event_type not in EVENT_TYPES:
            raise IntentValidationError(f"unknown event_type {self.event_type!r} — fail closed")
        if self.source != "LUMIBOT_PAPER":
            raise IntentValidationError(f"unsupported event source {self.source!r}")
        if self.filled_quantity < 0 or self.filled_quantity > self.quantity:
            raise IntentValidationError(
                f"filled_quantity {self.filled_quantity} out of range for quantity {self.quantity}"
            )
        # A positive filled_quantity always needs a real fill price; a
        # zero-quantity PARTIALLY_FILLED/FILLED event (a degenerate/no-op
        # broker status update) carries none, and the ledger must treat it
        # as a no-op — never invent a price to satisfy validation.
        if self.filled_quantity > 0 and (self.fill_price is None or self.fill_price <= 0):
            raise IntentValidationError("a positive filled_quantity requires a positive fill_price")
        _require_tz_aware(self.occurred_at, "occurred_at")


@dataclass(frozen=True)
class PaperExecutionResult:
    intent_id: str
    recommendation_id: str
    final_status: str
    requested_quantity: int
    filled_quantity: int
    average_fill_price: Decimal | None
    fees: Decimal
    event_ids: tuple[str, ...]
    completed_at: datetime

    def __post_init__(self) -> None:
        if self.final_status not in RESULT_STATUSES:
            raise IntentValidationError(f"unknown final_status {self.final_status!r} — fail closed")
        if self.filled_quantity < 0 or self.filled_quantity > self.requested_quantity:
            raise IntentValidationError("filled_quantity out of range for requested_quantity")
        if self.final_status == "FILLED" and self.filled_quantity != self.requested_quantity:
            raise IntentValidationError("FILLED result must have filled_quantity == requested_quantity")
        if self.filled_quantity > 0 and (self.average_fill_price is None or self.average_fill_price <= 0):
            raise IntentValidationError("a positive fill quantity requires a positive average_fill_price")
        if self.fees < 0:
            raise IntentValidationError("fees must not be negative")
        _require_tz_aware(self.completed_at, "completed_at")


@dataclass(frozen=True)
class ReconciliationResult:
    intent_id: str
    status: str
    broker_quantity: int
    ledger_quantity: int
    broker_notional: Decimal
    ledger_notional: Decimal
    reasons: tuple[str, ...]
    reconciled_at: datetime

    def __post_init__(self) -> None:
        if self.status not in RECONCILIATION_STATUSES:
            raise IntentValidationError(f"unknown reconciliation status {self.status!r} — fail closed")
        if self.status == "MISMATCH" and not self.reasons:
            raise IntentValidationError("MISMATCH reconciliation must record at least one reason")
        _require_tz_aware(self.reconciled_at, "reconciled_at")


@dataclass(frozen=True)
class PaperExecutionEligibility:
    recommendation_id: str
    eligible: bool
    reasons: tuple[str, ...]
    evaluated_at: datetime
    policy_version: str

    def __post_init__(self) -> None:
        if self.eligible and self.reasons:
            raise IntentValidationError("an eligible result must not carry rejection reasons")
        if not self.eligible and not self.reasons:
            raise IntentValidationError("an ineligible result must record at least one reason")
        _require_tz_aware(self.evaluated_at, "evaluated_at")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
