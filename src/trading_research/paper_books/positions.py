"""Deterministic, long-only FIFO lot accounting for isolated paper books
(docs/milestone-8.md Step 7). BUY/SELL only — no SHORT, COVER, OPTION, or
MARGIN support anywhere in this module.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ..storage import paper_books_repositories as repo

COST_BASIS_METHOD = "FIFO"


class InsufficientPositionError(RuntimeError):
    """A sell would exceed the book's available long position — fails closed."""


def apply_buy_fill(
    conn, book_id: str, symbol: str, fill_id: str, quantity: Decimal, fill_price: Decimal, now: datetime,
    *, commit: bool = True,
) -> None:
    """Creates exactly one new lot (lot_id derived from fill_id, so a
    duplicate fill can never create a second lot) and updates the book's
    symbol-level position aggregate. Idempotency of "was this fill already
    applied" is the caller's (execution.py's) responsibility via
    `repo.fill_exists` — this function is not itself re-entrant-safe against
    being called twice for the same fill_id with different quantities, only
    safe against being called with the *same* fill_id twice (a no-op lot
    insert, but position aggregates would double-count if the caller doesn't
    guard — hence the caller-side fill_exists check is mandatory)."""
    lot_id = f"lot:{fill_id}"
    cost_basis_usd = quantity * fill_price
    repo.save_lot(conn, {
        "book_id": book_id, "lot_id": lot_id, "symbol": symbol, "opened_at": now,
        "quantity": quantity, "remaining_quantity": quantity, "cost_basis_usd": cost_basis_usd,
        "opening_fill_id": fill_id,
    }, commit=commit)

    existing = repo.load_position(conn, book_id, symbol)
    if existing is None:
        repo.upsert_position(conn, book_id, symbol, {
            "quantity": quantity, "available_quantity": quantity, "reserved_quantity": Decimal("0"),
            "average_cost_usd": fill_price, "realized_pnl_usd": Decimal("0"), "fees_usd": Decimal("0"),
            "updated_at": now.isoformat(),
        }, commit=commit)
    else:
        old_qty = Decimal(existing["quantity"])
        old_avg = Decimal(existing["average_cost_usd"])
        new_qty = old_qty + quantity
        new_avg = (old_qty * old_avg + quantity * fill_price) / new_qty if new_qty > 0 else Decimal("0")
        repo.upsert_position(conn, book_id, symbol, {
            "quantity": new_qty, "available_quantity": Decimal(existing["available_quantity"]) + quantity,
            "reserved_quantity": Decimal(existing["reserved_quantity"]), "average_cost_usd": new_avg,
            "realized_pnl_usd": Decimal(existing["realized_pnl_usd"]), "fees_usd": Decimal(existing["fees_usd"]),
            "updated_at": now.isoformat(),
        }, commit=commit)


def apply_sell_fill(
    conn, book_id: str, symbol: str, fill_id: str, quantity: Decimal, fill_price: Decimal, now: datetime,
    *, commit: bool = True, already_reserved: bool = False,
) -> Decimal:
    """Consumes open lots oldest-first (FIFO). Returns the realized P&L
    (price-basis only; fees/slippage are tracked separately in the cash
    ledger and the position's own `fees_usd` aggregate — see
    docs/adr/0006...md). Raises `InsufficientPositionError` rather than ever
    allowing a sell quantity greater than the book's confirmed long position.

    `already_reserved=True` (external order fills, Part 3): the caller
    already atomically reserved these shares before submission, which
    lowered `available_quantity` at reservation time — checking
    `available_quantity` again here would double-count that same
    reservation and wrongly reject the fill it was reserved for. The
    confirmed `quantity` remains the ceiling either way, so a true oversell
    (selling more than the book actually holds) is still impossible.
    """
    existing = repo.load_position(conn, book_id, symbol)
    ceiling = Decimal(
        (existing["quantity"] if already_reserved else existing["available_quantity"]) if existing else "0"
    )
    if quantity > ceiling:
        raise InsufficientPositionError(
            f"book {book_id} symbol {symbol}: cannot sell {quantity} — only {ceiling} available"
        )

    open_lots = repo.list_open_lots(conn, book_id, symbol)
    remaining_to_sell = quantity
    realized_pnl = Decimal("0")
    for lot in open_lots:
        if remaining_to_sell <= 0:
            break
        lot_remaining = Decimal(lot["remaining_quantity"])
        lot_qty_total = Decimal(lot["quantity"])
        lot_cost_total = Decimal(lot["cost_basis_usd"])
        cost_per_share = lot_cost_total / lot_qty_total
        consumed = min(lot_remaining, remaining_to_sell)
        realized_pnl += (fill_price - cost_per_share) * consumed
        new_remaining = lot_remaining - consumed
        repo.update_lot_remaining(
            conn, book_id, lot["lot_id"], new_remaining,
            closed_at=now if new_remaining == 0 else None, commit=commit,
        )
        remaining_to_sell -= consumed

    old_qty = Decimal(existing["quantity"])
    new_qty = old_qty - quantity
    new_realized = Decimal(existing["realized_pnl_usd"]) + realized_pnl
    repo.upsert_position(conn, book_id, symbol, {
        "quantity": new_qty, "available_quantity": Decimal(existing["available_quantity"]) - quantity,
        "reserved_quantity": Decimal(existing["reserved_quantity"]),
        "average_cost_usd": Decimal(existing["average_cost_usd"]) if new_qty > 0 else Decimal("0"),
        "realized_pnl_usd": new_realized, "fees_usd": Decimal(existing["fees_usd"]), "updated_at": now.isoformat(),
    }, commit=commit)
    return realized_pnl


def apply_forward_or_reverse_split(conn, book_id: str, symbol: str, ratio: Decimal, action_id: str, now: datetime) -> None:
    """A `ratio` > 1 is a forward split (more shares, proportionally lower
    cost per share); a `ratio` < 1 is a reverse split. Total cost basis per
    lot is preserved exactly; only quantity (and therefore average cost)
    changes (docs/milestone-8.md Step 18: "cost basis preserved correctly")."""
    if ratio <= 0:
        raise ValueError("split ratio must be positive")
    lots = repo.list_all_lots(conn, book_id, symbol)
    for lot in lots:
        new_remaining = Decimal(lot["remaining_quantity"]) * ratio
        repo.update_lot_remaining(
            conn, book_id, lot["lot_id"], new_remaining,
            closed_at=_iso(lot["closed_at"]) if lot["closed_at"] else None,
        )
    position = repo.load_position(conn, book_id, symbol)
    if position is not None:
        old_qty = Decimal(position["quantity"])
        new_qty = old_qty * ratio
        old_avg = Decimal(position["average_cost_usd"])
        new_avg = old_avg / ratio if new_qty > 0 else Decimal("0")
        repo.upsert_position(conn, book_id, symbol, {
            "quantity": new_qty, "available_quantity": Decimal(position["available_quantity"]) * ratio,
            "reserved_quantity": Decimal(position["reserved_quantity"]) * ratio, "average_cost_usd": new_avg,
            "realized_pnl_usd": Decimal(position["realized_pnl_usd"]), "fees_usd": Decimal(position["fees_usd"]),
            "updated_at": now.isoformat(),
        })


def _iso(value: str):
    from datetime import timezone
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_RESERVATION_EVENT = "RESERVED"
_CONSUMED_EVENT = "CONSUMED_BY_FILL"
_RELEASE_EVENTS = ("RELEASED_CANCELLED", "RELEASED_REJECTED", "RELEASED_EXPIRED")
_MANUAL_CORRECTION_EVENT = "MANUAL_CORRECTION"


def _write_position_reservation(conn, book_id: str, symbol: str, *, delta_reserved: Decimal, now, commit: bool) -> None:
    """Adjust reserved_quantity/available_quantity by `delta_reserved`, keeping
    quantity - reserved_quantity == available_quantity at every write."""
    position = repo.load_position(conn, book_id, symbol)
    repo.upsert_position(conn, book_id, symbol, {
        "quantity": Decimal(position["quantity"]),
        "available_quantity": Decimal(position["available_quantity"]) - delta_reserved,
        "reserved_quantity": Decimal(position["reserved_quantity"]) + delta_reserved,
        "average_cost_usd": Decimal(position["average_cost_usd"]),
        "realized_pnl_usd": Decimal(position["realized_pnl_usd"]), "fees_usd": Decimal(position["fees_usd"]),
        "updated_at": now.isoformat(),
    }, commit=commit)


def remaining_share_reservation(conn, book_id: str, paper_order_intent_id: str) -> Decimal:
    """Original reserved quantity minus every consume/release event recorded for this intent."""
    reserved = Decimal("0")
    resolved = Decimal("0")
    for e in repo.list_external_position_reservation_events(
        conn, book_id=book_id, paper_order_intent_id=paper_order_intent_id
    ):
        if e["event_type"] == _RESERVATION_EVENT:
            reserved += Decimal(e["quantity"])
        elif e["event_type"] == _CONSUMED_EVENT or e["event_type"] in _RELEASE_EVENTS:
            resolved += Decimal(e["quantity"])
        elif e["event_type"] == _MANUAL_CORRECTION_EVENT:
            reserved += Decimal(e["quantity"])
    return reserved - resolved


def reserve_shares_for_sell(
    conn, book_id: str, symbol: str, paper_order_intent_id: str, client_order_id: str, quantity: Decimal, now,
    *, commit: bool = True,
) -> bool:
    """Atomically reserve `quantity` shares for an external closing SELL before submission.

    Fails closed (`InsufficientPositionError`) if fewer shares are
    confirmed-and-unreserved than requested — this is what prevents a second
    SELL from reserving or submitting the same shares."""
    idem = f"reserve:{paper_order_intent_id}"
    if repo.list_external_position_reservation_events(
        conn, book_id=book_id, paper_order_intent_id=paper_order_intent_id
    ):
        return False
    position = repo.load_position(conn, book_id, symbol)
    available = Decimal(position["available_quantity"]) if position else Decimal("0")
    if quantity > available:
        raise InsufficientPositionError(
            f"book {book_id} symbol {symbol}: cannot reserve {quantity} shares for SELL — only {available} available"
        )
    inserted = repo.save_external_position_reservation_event(conn, {
        "reservation_event_id": idem, "book_id": book_id, "symbol": symbol,
        "paper_order_intent_id": paper_order_intent_id, "client_order_id": client_order_id,
        "quantity": quantity, "event_type": _RESERVATION_EVENT, "operator": None,
        "reason": "external closing SELL submission", "created_at": now.isoformat(),
    }, commit=commit)
    if inserted:
        _write_position_reservation(conn, book_id, symbol, delta_reserved=quantity, now=now, commit=commit)
    return inserted


def consume_share_reservation_for_fill(
    conn, book_id: str, symbol: str, paper_order_intent_id: str, fill_id: str, quantity: Decimal, now,
    *, commit: bool = True,
) -> bool:
    """Consume the reservation attributable to one durably-applied SELL fill.

    Keyed per `fill_id` so idempotent replay never double-consumes; clamped
    to whatever remains reserved so a malformed fill can never drive the
    reservation negative."""
    remaining = remaining_share_reservation(conn, book_id, paper_order_intent_id)
    if remaining <= 0:
        return False
    amount = quantity if quantity < remaining else remaining
    inserted = repo.save_external_position_reservation_event(conn, {
        "reservation_event_id": f"consume:{paper_order_intent_id}:fill:{fill_id}",
        "book_id": book_id, "symbol": symbol, "paper_order_intent_id": paper_order_intent_id,
        "client_order_id": None, "quantity": amount, "event_type": _CONSUMED_EVENT,
        "operator": None, "reason": "SELL fill applied", "created_at": now.isoformat(),
    }, commit=commit)
    if inserted:
        _write_position_reservation(conn, book_id, symbol, delta_reserved=-amount, now=now, commit=commit)
    return inserted


def release_remaining_share_reservation(
    conn, book_id: str, symbol: str, paper_order_intent_id: str, now, *, release_event_id: str, event_type: str,
    commit: bool = True,
) -> bool:
    """Release exactly what remains reserved once an order will receive no more fills.

    Idempotent per `release_event_id`: a second call after the reservation is
    already fully released is a no-op, so the released total can never
    exceed the original reservation."""
    remaining = remaining_share_reservation(conn, book_id, paper_order_intent_id)
    if remaining <= 0:
        return False
    inserted = repo.save_external_position_reservation_event(conn, {
        "reservation_event_id": f"release:{paper_order_intent_id}:{release_event_id}",
        "book_id": book_id, "symbol": symbol, "paper_order_intent_id": paper_order_intent_id,
        "client_order_id": None, "quantity": remaining, "event_type": event_type,
        "operator": None, "reason": "unresolved external SELL reservation released", "created_at": now.isoformat(),
    }, commit=commit)
    if inserted:
        _write_position_reservation(conn, book_id, symbol, delta_reserved=-remaining, now=now, commit=commit)
    return inserted


def manual_correct_share_reservation(
    conn, book_id: str, symbol: str, paper_order_intent_id: str, delta_quantity: Decimal, now,
    *, operator: str, reason: str, commit: bool = True,
) -> bool:
    """Explicit, audited operator correction of reservation drift. Never used
    by application code — every call site must be an audited operator action."""
    if not operator or not reason:
        raise ValueError("manual_correct_share_reservation requires a non-empty operator and reason")
    idem = f"manual:{paper_order_intent_id}:{now.isoformat()}"
    inserted = repo.save_external_position_reservation_event(conn, {
        "reservation_event_id": idem, "book_id": book_id, "symbol": symbol,
        "paper_order_intent_id": paper_order_intent_id, "client_order_id": None,
        "quantity": delta_quantity, "event_type": _MANUAL_CORRECTION_EVENT,
        "operator": operator, "reason": reason, "created_at": now.isoformat(),
    }, commit=commit)
    if inserted:
        _write_position_reservation(conn, book_id, symbol, delta_reserved=delta_quantity, now=now, commit=commit)
    return inserted
