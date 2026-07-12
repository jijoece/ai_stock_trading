"""Framework-neutral broker submission/account/position snapshots for the
credentialed paper-broker path (docs/milestone-4.md Step 8/10).

These are new, additive contracts — nothing in `execution/models.py` (the
Milestone 3 contracts) is modified. `BrokerOrderSubmission` tracks the
submission/idempotency state machine across the main-process/runtime
boundary; `BrokerAccountSnapshot`/`BrokerPositionSnapshot` are the read
side used by account/position reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .models import _require_tz_aware

# Submission/order lifecycle across the process boundary (docs/milestone-
# 4.md Step 8). `PENDING_SUBMISSION` and `SUBMISSION_UNKNOWN` are new states
# that do not exist in Milestone 3's synchronous `PaperExecutionResult`
# (docs/milestone-3.md's adapter.submit() always returns a resolved
# outcome) — a real broker submission is asynchronous, so this state
# machine tracks the interval between "we asked the broker" and "we know
# what happened."
SUBMISSION_STATES = (
    "PENDING_SUBMISSION",
    "SUBMISSION_UNKNOWN",
    "SUBMITTED",
    "ACCEPTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELLED",
    "REJECTED",
    "ERROR",
)

TERMINAL_SUBMISSION_STATES = ("FILLED", "CANCELLED", "REJECTED", "ERROR")

ACCOUNT_RECONCILIATION_STATUSES = ("MATCHED", "CASH_MISMATCH", "MISSING_BROKER_ACCOUNT", "UNKNOWN")
POSITION_RECONCILIATION_STATUSES = (
    "MATCHED", "POSITION_MISMATCH", "MISSING_INTERNAL_POSITION", "MISSING_BROKER_POSITION", "UNKNOWN",
)


class BrokerSnapshotValidationError(ValueError):
    pass


@dataclass(frozen=True)
class BrokerOrderSubmission:
    intent_id: str
    client_order_id: str
    broker_order_id: str | None
    submission_status: str
    attempt_count: int
    last_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.submission_status not in SUBMISSION_STATES:
            raise BrokerSnapshotValidationError(f"unknown submission_status {self.submission_status!r} — fail closed")
        if self.attempt_count < 0:
            raise BrokerSnapshotValidationError("attempt_count must not be negative")
        if not self.client_order_id:
            raise BrokerSnapshotValidationError("client_order_id is required")
        _require_tz_aware(self.created_at, "created_at")
        _require_tz_aware(self.updated_at, "updated_at")
        if self.last_attempt_at is not None:
            _require_tz_aware(self.last_attempt_at, "last_attempt_at")

    def is_terminal(self) -> bool:
        return self.submission_status in TERMINAL_SUBMISSION_STATES


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    cash: Decimal
    equity: Decimal
    currency: str
    as_of: datetime

    def __post_init__(self) -> None:
        _require_tz_aware(self.as_of, "as_of")
        if not self.currency:
            raise BrokerSnapshotValidationError("currency is required")


@dataclass(frozen=True)
class BrokerPositionSnapshot:
    symbol: str
    quantity: Decimal
    average_entry_price: Decimal
    market_value: Decimal | None
    as_of: datetime

    def __post_init__(self) -> None:
        _require_tz_aware(self.as_of, "as_of")
        if not self.symbol:
            raise BrokerSnapshotValidationError("symbol is required")


@dataclass(frozen=True)
class AccountReconciliationResult:
    status: str
    broker_cash: Decimal
    ledger_cash: Decimal
    difference: Decimal
    tolerance: Decimal
    reasons: tuple[str, ...]
    broker_as_of: datetime
    reconciled_at: datetime

    def __post_init__(self) -> None:
        if self.status not in ACCOUNT_RECONCILIATION_STATUSES:
            raise BrokerSnapshotValidationError(f"unknown account reconciliation status {self.status!r}")
        if self.status != "MATCHED" and not self.reasons:
            raise BrokerSnapshotValidationError("a non-MATCHED result must record at least one reason")
        _require_tz_aware(self.broker_as_of, "broker_as_of")
        _require_tz_aware(self.reconciled_at, "reconciled_at")


@dataclass(frozen=True)
class PositionReconciliationResult:
    symbol: str
    status: str
    broker_quantity: Decimal
    ledger_quantity: Decimal
    tolerance: Decimal
    reasons: tuple[str, ...]
    broker_as_of: datetime
    reconciled_at: datetime

    def __post_init__(self) -> None:
        if self.status not in POSITION_RECONCILIATION_STATUSES:
            raise BrokerSnapshotValidationError(f"unknown position reconciliation status {self.status!r}")
        if self.status != "MATCHED" and not self.reasons:
            raise BrokerSnapshotValidationError("a non-MATCHED result must record at least one reason")
        _require_tz_aware(self.broker_as_of, "broker_as_of")
        _require_tz_aware(self.reconciled_at, "reconciled_at")
