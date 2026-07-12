"""Applies normalized `PaperExecutionEvent`s to the existing
`paper.ledger.PaperLedger` (Milestone 3, Step 6).

A narrow adapter around the ledger, not a rewrite of it: every accounting
decision (cash debit, T+1 settlement, position averaging) still happens
inside `PaperLedger._apply_fill`; this module only decides *whether* a given
event should reach the ledger at all, and guarantees that decision is
idempotent — `paper_execution_events.ledger_applied` is the durable record
of "have we already told the ledger about this event," independent of and
in addition to the ledger's own `simulated_orders.idempotency_key` UNIQUE
constraint (belt-and-suspenders: a crash between the ledger write and the
`ledger_applied` flag update is still safe, see `DuplicateOrderError`
handling below).
"""
from __future__ import annotations

from datetime import datetime

from ..paper.ledger import DuplicateOrderError, PaperLedger
from ..storage import execution_repositories as exec_repo
from .models import PaperExecutionEvent

FILL_EVENT_TYPES = ("FILLED", "PARTIALLY_FILLED")


def apply_paper_execution_event(
    conn, ledger: PaperLedger, event: PaperExecutionEvent, *, now: datetime | None = None
) -> bool:
    """Persist (idempotently) then apply one event to the ledger if (and
    only if) it represents a new, positive fill. Returns True if the ledger
    was mutated, False otherwise (already applied, non-fill event type, or
    a zero-quantity fill).

    Non-fill events (SUBMITTED, ACCEPTED, CANCELLED, REJECTED, ERROR) and
    zero-quantity fills never reach the ledger — cash/holdings are
    untouched, matching the ledger-invariant requirements in
    docs/milestone-3.md Step 6. Persisting the event row here (rather than
    requiring a separate prior call) keeps "normalize and persist" and
    "apply to ledger" atomic from a caller's point of view while still
    persisting strictly before applying.
    """
    exec_repo.save_event(conn, event, now=now or event.occurred_at)

    if exec_repo.is_event_ledger_applied(conn, event.event_id):
        return False

    if event.event_type not in FILL_EVENT_TYPES or event.filled_quantity <= 0:
        exec_repo.mark_event_ledger_applied(conn, event.event_id)
        return False

    try:
        ledger.apply_external_fill(
            symbol=event.symbol,
            side="buy",  # long-only in this milestone
            qty=event.filled_quantity,
            price=float(event.fill_price),
            idempotency_key=event.event_id,
            rec_id=event.recommendation_id,
            now=now,
        )
    except DuplicateOrderError:
        # The ledger itself already saw this event_id as an idempotency key
        # (e.g. a crash after the ledger write but before the flag update on
        # a prior attempt) — safe to treat as already-applied.
        exec_repo.mark_event_ledger_applied(conn, event.event_id)
        return False

    exec_repo.mark_event_ledger_applied(conn, event.event_id)
    return True


def apply_all_new_events(
    conn, ledger: PaperLedger, events: tuple[PaperExecutionEvent, ...], *, now: datetime | None = None
) -> int:
    """Apply every event in submission order; returns the count actually
    applied. Order matters for partial-fill sequences (each event carries an
    incremental fill quantity, not a cumulative one)."""
    applied = 0
    for event in events:
        if apply_paper_execution_event(conn, ledger, event, now=now):
            applied += 1
    return applied
