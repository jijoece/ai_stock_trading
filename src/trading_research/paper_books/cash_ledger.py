"""Append-only cash ledger for isolated paper books (docs/milestone-8.md Step 6).

Available cash is always derived from ledger entries, never stored as a
single overwritable balance (contrast with the legacy
`paper/ledger.py::PaperLedger`'s singleton `paper_cash_state` row — see
`docs/adr/0006...md` Decision 1/3). Reserved cash is tracked separately from
settled cash. No function here ever produces negative available cash; a
reservation that would do so raises `InsufficientCashError` instead.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ..storage import paper_books_repositories as repo
from .models import (
    CASH_EVENT_BUY_RESERVATION,
    CASH_EVENT_BUY_SETTLEMENT,
    CASH_EVENT_CASH_ADJUSTMENT,
    CASH_EVENT_DIVIDEND,
    CASH_EVENT_FEE,
    CASH_EVENT_INITIAL_CAPITAL,
    CASH_EVENT_ORDER_RELEASE,
    CASH_EVENT_SELL_SETTLEMENT,
    CASH_EVENT_SLIPPAGE,
    BOOK_STATUS_ACTIVE,
    CashLedgerEntry,
    PaperBook,
)

_SETTLED_EVENT_TYPES = frozenset(
    {
        CASH_EVENT_INITIAL_CAPITAL, CASH_EVENT_BUY_SETTLEMENT, CASH_EVENT_SELL_SETTLEMENT,
        CASH_EVENT_FEE, CASH_EVENT_SLIPPAGE, CASH_EVENT_DIVIDEND, CASH_EVENT_CASH_ADJUSTMENT,
    }
)
_RESERVED_EVENT_TYPES = frozenset({CASH_EVENT_BUY_RESERVATION, CASH_EVENT_ORDER_RELEASE})

_ARM_FOR_BOOK_ID = {"BASELINE": "BASELINE", "ENHANCED": "ENHANCED"}


class InsufficientCashError(RuntimeError):
    """A reservation or settlement would drive available cash negative — fails closed."""


def open_book(conn, *, book_id: str, starting_cash_usd: Decimal, config_hash: str, clock) -> PaperBook:
    """Idempotent: a second call for the same book_id is a no-op (the book
    row and its one INITIAL_CAPITAL ledger entry are both keyed to never
    duplicate)."""
    now = clock()
    experiment_arm = _ARM_FOR_BOOK_ID[book_id]
    book = PaperBook(
        book_id=book_id, experiment_arm=experiment_arm, currency="USD",
        starting_cash_usd=starting_cash_usd, status=BOOK_STATUS_ACTIVE, created_at=now, config_hash=config_hash,
    )
    repo.save_book(conn, book)
    idem = f"init:{book_id}"
    entry = CashLedgerEntry(
        book_id=book_id, ledger_entry_id=idem, event_type=CASH_EVENT_INITIAL_CAPITAL,
        amount_usd=starting_cash_usd, event_timestamp=now, idempotency_key=idem,
    )
    repo.save_cash_ledger_entry(conn, entry)
    return repo.load_book(conn, book_id)


def settled_cash(conn, book_id: str) -> Decimal:
    total = Decimal("0")
    for e in repo.list_cash_ledger_entries(conn, book_id):
        if e["event_type"] in _SETTLED_EVENT_TYPES:
            total += Decimal(e["amount_usd"])
    return total


def reserved_cash(conn, book_id: str) -> Decimal:
    total = Decimal("0")
    for e in repo.list_cash_ledger_entries(conn, book_id):
        if e["event_type"] in _RESERVED_EVENT_TYPES:
            total += Decimal(e["amount_usd"])
    return total


def available_cash(conn, book_id: str) -> Decimal:
    return settled_cash(conn, book_id) - reserved_cash(conn, book_id)


def reserve_for_order(
    conn, book_id: str, paper_order_intent_id: str, notional_usd: Decimal, now: datetime,
    *, commit: bool = True,
) -> bool:
    idem = f"reserve:{paper_order_intent_id}"
    if repo.cash_ledger_entry_exists(conn, book_id, idem):
        return False
    if notional_usd > available_cash(conn, book_id):
        raise InsufficientCashError(
            f"book {book_id}: cannot reserve {notional_usd} — only {available_cash(conn, book_id)} available"
        )
    entry = CashLedgerEntry(
        book_id=book_id, ledger_entry_id=idem, event_type=CASH_EVENT_BUY_RESERVATION,
        amount_usd=notional_usd, event_timestamp=now, idempotency_key=idem, reference_id=paper_order_intent_id,
    )
    return repo.save_cash_ledger_entry(conn, entry, commit=commit)


def release_reservation(
    conn, book_id: str, paper_order_intent_id: str, notional_usd: Decimal, now: datetime,
    *, reason: str = "released", commit: bool = True,
) -> bool:
    idem = f"release:{paper_order_intent_id}:{reason}"
    entry = CashLedgerEntry(
        book_id=book_id, ledger_entry_id=idem, event_type=CASH_EVENT_ORDER_RELEASE,
        amount_usd=-notional_usd, event_timestamp=now, idempotency_key=idem, reference_id=paper_order_intent_id,
    )
    return repo.save_cash_ledger_entry(conn, entry, commit=commit)


def settle_buy(
    conn, book_id: str, fill_id: str, cost_usd: Decimal, fees_usd: Decimal, slippage_usd: Decimal,
    now: datetime, *, commit: bool = True,
) -> None:
    idem = f"settle:{fill_id}"
    repo.save_cash_ledger_entry(conn, CashLedgerEntry(
        book_id=book_id, ledger_entry_id=idem, event_type=CASH_EVENT_BUY_SETTLEMENT,
        amount_usd=-cost_usd, event_timestamp=now, idempotency_key=idem, reference_id=fill_id,
    ), commit=commit)
    if fees_usd:
        fee_idem = f"fee:{fill_id}"
        repo.save_cash_ledger_entry(conn, CashLedgerEntry(
            book_id=book_id, ledger_entry_id=fee_idem, event_type=CASH_EVENT_FEE,
            amount_usd=-fees_usd, event_timestamp=now, idempotency_key=fee_idem, reference_id=fill_id,
        ), commit=commit)
    if slippage_usd:
        slip_idem = f"slippage:{fill_id}"
        repo.save_cash_ledger_entry(conn, CashLedgerEntry(
            book_id=book_id, ledger_entry_id=slip_idem, event_type=CASH_EVENT_SLIPPAGE,
            amount_usd=-slippage_usd, event_timestamp=now, idempotency_key=slip_idem, reference_id=fill_id,
        ), commit=commit)


def settle_sell(
    conn, book_id: str, fill_id: str, proceeds_usd: Decimal, fees_usd: Decimal, slippage_usd: Decimal,
    now: datetime, *, commit: bool = True,
) -> None:
    idem = f"settle:{fill_id}"
    repo.save_cash_ledger_entry(conn, CashLedgerEntry(
        book_id=book_id, ledger_entry_id=idem, event_type=CASH_EVENT_SELL_SETTLEMENT,
        amount_usd=proceeds_usd, event_timestamp=now, idempotency_key=idem, reference_id=fill_id,
    ), commit=commit)
    if fees_usd:
        fee_idem = f"fee:{fill_id}"
        repo.save_cash_ledger_entry(conn, CashLedgerEntry(
            book_id=book_id, ledger_entry_id=fee_idem, event_type=CASH_EVENT_FEE,
            amount_usd=-fees_usd, event_timestamp=now, idempotency_key=fee_idem, reference_id=fill_id,
        ), commit=commit)
    if slippage_usd:
        slip_idem = f"slippage:{fill_id}"
        repo.save_cash_ledger_entry(conn, CashLedgerEntry(
            book_id=book_id, ledger_entry_id=slip_idem, event_type=CASH_EVENT_SLIPPAGE,
            amount_usd=-slippage_usd, event_timestamp=now, idempotency_key=slip_idem, reference_id=fill_id,
        ), commit=commit)


def credit_dividend(conn, book_id: str, action_id: str, symbol: str, amount_usd: Decimal, now: datetime) -> bool:
    idem = f"dividend:{action_id}:{symbol}"
    entry = CashLedgerEntry(
        book_id=book_id, ledger_entry_id=idem, event_type=CASH_EVENT_DIVIDEND, amount_usd=amount_usd,
        event_timestamp=now, idempotency_key=idem, symbol=symbol, reference_id=action_id,
    )
    return repo.save_cash_ledger_entry(conn, entry)


def cash_adjustment(
    conn, book_id: str, amount_usd: Decimal, *, operator: str, reason: str, idempotency_key: str, now: datetime
) -> bool:
    """Requires operator + reason (Step 6). Never used by application code to
    fabricate cash silently — every call site must be an explicit, audited
    operator action."""
    if not operator or not reason:
        raise ValueError("cash_adjustment requires a non-empty operator and reason")
    entry = CashLedgerEntry(
        book_id=book_id, ledger_entry_id=idempotency_key, event_type=CASH_EVENT_CASH_ADJUSTMENT,
        amount_usd=amount_usd, event_timestamp=now, idempotency_key=idempotency_key, operator=operator, reason=reason,
    )
    return repo.save_cash_ledger_entry(conn, entry)
