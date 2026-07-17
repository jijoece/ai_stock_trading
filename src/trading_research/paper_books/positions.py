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
    *, commit: bool = True,
) -> Decimal:
    """Consumes open lots oldest-first (FIFO). Returns the realized P&L
    (price-basis only; fees/slippage are tracked separately in the cash
    ledger and the position's own `fees_usd` aggregate — see
    docs/adr/0006...md). Raises `InsufficientPositionError` rather than ever
    allowing a sell quantity greater than the book's available position."""
    existing = repo.load_position(conn, book_id, symbol)
    available = Decimal(existing["available_quantity"]) if existing else Decimal("0")
    if quantity > available:
        raise InsufficientPositionError(
            f"book {book_id} symbol {symbol}: cannot sell {quantity} — only {available} available"
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
