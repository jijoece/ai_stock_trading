"""The internal adapter boundary Milestone 3's orchestration service programs
against (docs/milestone-3.md Step 5). Both `runtime.deterministic_adapter`
and `runtime.lumibot.adapter` implement this Protocol — neither the
orchestration service nor this module ever imports LumiBot.

Deviates from the illustrative `docs/milestone-3.md` snippet in two
deliberate ways:

1. `submit()` returns `(events, result)` rather than only a
   `PaperExecutionResult` — the orchestration service must persist each
   individual normalized event (idempotently, by `event_id`) *before*
   applying it to the ledger (Step 8, "persist the intent before external
   submission" / "normalize and persist execution events"), so the event
   stream has to be available, not just the final rollup.
2. `reconcile()` returns a `BrokerExecutionSnapshot` (the adapter's own
   view of broker-side quantity/notional) rather than a full
   `ReconciliationResult` directly — an adapter only ever knows the broker
   side; it cannot honestly report `ledger_quantity`/`ledger_notional`
   without reaching into ledger internals it has no business touching.
   `reconciliation.reconcile_intent()` combines a `BrokerExecutionSnapshot`
   with the orchestration service's own ledger read to produce the
   persisted `ReconciliationResult`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .models import PaperExecutionEvent, PaperExecutionResult, PaperOrderIntent


@dataclass(frozen=True)
class BrokerExecutionSnapshot:
    intent_id: str
    broker_quantity: int
    broker_notional: Decimal
    broker_status: str
    as_of: datetime


class PaperExecutionAdapter(Protocol):
    def submit(self, intent: PaperOrderIntent) -> tuple[tuple[PaperExecutionEvent, ...], PaperExecutionResult]:
        ...

    def reconcile(self, intent_id: str) -> BrokerExecutionSnapshot:
        ...
