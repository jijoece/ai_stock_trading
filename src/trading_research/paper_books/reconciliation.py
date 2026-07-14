"""Deterministic, book-aware reconciliation (docs/milestone-8.md Step 17).

Every check here is scoped to exactly one `book_id` — no query ever spans
two books, so a mismatch in one book cannot be hidden by, or contaminate,
the other (ADR 0006 Decision 2/4). This module never mutates history; a
detected mismatch is only ever recorded, never silently corrected.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ..storage import paper_books_repositories as repo
from . import cash_ledger

RECONCILIATION_VERSION = "paper-books-reconciliation-v1"

STATUS_MATCHED = "MATCHED"
STATUS_MISSING_ORDER = "MISSING_ORDER"
STATUS_MISSING_FILL = "MISSING_FILL"
STATUS_DUPLICATE_FILL = "DUPLICATE_FILL"
STATUS_CASH_MISMATCH = "CASH_MISMATCH"
STATUS_POSITION_MISMATCH = "POSITION_MISMATCH"
STATUS_LOT_MISMATCH = "LOT_MISMATCH"
STATUS_BOOK_MISMATCH = "BOOK_MISMATCH"
STATUS_ARM_MISMATCH = "ARM_MISMATCH"
STATUS_PENDING_NOT_APPLICABLE = "PENDING_NOT_APPLICABLE"

_SEVERITY_ORDER = (
    STATUS_ARM_MISMATCH, STATUS_BOOK_MISMATCH, STATUS_DUPLICATE_FILL, STATUS_MISSING_ORDER,
    STATUS_MISSING_FILL, STATUS_CASH_MISMATCH, STATUS_POSITION_MISMATCH, STATUS_LOT_MISMATCH,
)


class ReconciliationBookMismatchError(RuntimeError):
    """Raised when the caller passes a book_id that does not match the
    record actually being reconciled against — a structural cross-book
    contamination attempt, rejected before any comparison runs."""


def reconcile_book(conn, book_id: str, as_of: datetime, *, expected_arm: str | None = None) -> dict:
    book = repo.load_book(conn, book_id)
    if book is None:
        raise ValueError(f"unknown book_id {book_id!r}")

    mismatches: list[dict] = []
    if expected_arm is not None and book.experiment_arm != expected_arm:
        mismatches.append({
            "type": STATUS_ARM_MISMATCH,
            "detail": f"book {book_id} experiment_arm={book.experiment_arm} != expected {expected_arm}",
        })

    orders = repo.list_order_intents(conn, book_id)
    fills = repo.list_fills(conn, book_id)

    order_ids = {o["paper_order_intent_id"] for o in orders}
    fills_by_intent: dict[str, list] = {}
    seen_fill_ids: set[str] = set()
    for f in fills:
        if f["book_id"] != book_id:
            # Structurally unreachable given repo.list_fills already filters
            # by book_id, but checked explicitly so a future refactor cannot
            # silently reintroduce cross-book leakage undetected.
            mismatches.append({
                "type": STATUS_BOOK_MISMATCH, "detail": f"fill {f['fill_id']} carries book_id {f['book_id']} != {book_id}",
            })
            continue
        if f["fill_id"] in seen_fill_ids:
            mismatches.append({"type": STATUS_DUPLICATE_FILL, "detail": f"duplicate fill_id {f['fill_id']}"})
        seen_fill_ids.add(f["fill_id"])
        if f["paper_order_intent_id"] not in order_ids:
            mismatches.append({
                "type": STATUS_MISSING_ORDER,
                "detail": f"fill {f['fill_id']} references unknown order {f['paper_order_intent_id']}",
            })
        fills_by_intent.setdefault(f["paper_order_intent_id"], []).append(f)

    for order in orders:
        flist = fills_by_intent.get(order["paper_order_intent_id"], [])
        if order["status"] == "FILLED" and not flist:
            mismatches.append({
                "type": STATUS_MISSING_FILL,
                "detail": f"order {order['paper_order_intent_id']} status=FILLED but no fill row exists",
            })
        elif flist:
            total_filled = sum(Decimal(f["fill_quantity"]) for f in flist)
            if total_filled != Decimal(order["quantity"]):
                mismatches.append({
                    "type": STATUS_POSITION_MISMATCH,
                    "detail": f"order {order['paper_order_intent_id']} filled_qty={total_filled} != order_qty={order['quantity']}",
                })

    # Cash cross-check: recompute settled cash independently from fills +
    # non-fill ledger entries, compare to the ledger-derived total.
    ledger_entries = repo.list_cash_ledger_entries(conn, book_id)
    non_fill_total = sum(
        Decimal(e["amount_usd"]) for e in ledger_entries
        if e["event_type"] in ("INITIAL_CAPITAL", "DIVIDEND", "CASH_ADJUSTMENT")
    )
    fills_cash_delta = Decimal("0")
    for f in fills:
        amount = Decimal(f["fill_price"]) * Decimal(f["fill_quantity"])
        costs = Decimal(f["fees_usd"]) + Decimal(f["slippage_usd"])
        fills_cash_delta += (-amount - costs) if f["side"] == "BUY" else (amount - costs)
    recomputed_settled = non_fill_total + fills_cash_delta
    ledger_settled = cash_ledger.settled_cash(conn, book_id)
    if recomputed_settled != ledger_settled:
        mismatches.append({
            "type": STATUS_CASH_MISMATCH,
            "detail": f"recomputed settled cash {recomputed_settled} != ledger-derived {ledger_settled}",
        })

    # Position cross-check: recompute average-cost position from fills
    # independently (FIFO-equivalent average-cost approximation) and
    # compare to the stored aggregate.
    by_symbol: dict[str, list] = {}
    for f in fills:
        by_symbol.setdefault(f["symbol"], []).append(f)
    for symbol, flist in by_symbol.items():
        qty = Decimal("0")
        cost = Decimal("0")
        for f in sorted(flist, key=lambda row: row["fill_timestamp"]):
            fill_qty = Decimal(f["fill_quantity"])
            if f["side"] == "BUY":
                qty += fill_qty
                cost += fill_qty * Decimal(f["fill_price"])
            elif qty > 0:
                avg = cost / qty
                qty -= fill_qty
                cost -= avg * fill_qty
        stored = repo.load_position(conn, book_id, symbol)
        stored_qty = Decimal(stored["quantity"]) if stored else Decimal("0")
        if qty != stored_qty:
            mismatches.append({
                "type": STATUS_POSITION_MISMATCH, "detail": f"{symbol}: recomputed qty {qty} != stored {stored_qty}",
            })

    if not mismatches:
        status = STATUS_MATCHED
    else:
        status = next((s for s in _SEVERITY_ORDER if any(m["type"] == s for m in mismatches)), mismatches[0]["type"])

    reconciliation_id = f"pb-recon-{book_id}-{as_of.isoformat()}"
    repo.save_reconciliation(conn, book_id, reconciliation_id, as_of, status, mismatches, RECONCILIATION_VERSION)
    return {"reconciliation_id": reconciliation_id, "book_id": book_id, "status": status, "mismatches": mismatches}
