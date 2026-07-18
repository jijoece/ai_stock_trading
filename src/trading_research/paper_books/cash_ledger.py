"""Append-only cash ledger for isolated paper books (docs/milestone-8.md Step 6).

Available cash is always derived from ledger entries, never stored as a
single overwritable balance (contrast with the legacy
`paper/ledger.py::PaperLedger`'s singleton `paper_cash_state` row — see
`docs/adr/0006...md` Decision 1/3). Reserved cash is tracked separately from
settled cash. No function here ever produces negative available cash; a
reservation that would do so raises `InsufficientCashError` instead.

Settlement policy (Milestone 11.3 Part 32, explicit by design):
`settle_buy`/`settle_sell` apply a fill's cash effect **immediately**, in
the same transaction as the fill itself — there is no separate T+1 (or any
other deferred) settlement step. This is `SETTLEMENT_POLICY_VERSION`
(`IMMEDIATE_SIMULATED_SETTLEMENT.v1`), a deliberate simulation
simplification, never real broker/regulatory settlement (which is
typically T+1 for US equities). Every buying-power, risk, and reservation
calculation in this subsystem (`available_cash`, `reserved_cash`,
`settled_cash`, `reserve_for_order`) reads from the *same* immediately-
settled ledger, so there is no internal inconsistency between "settled"
and "available" — they differ only by open reservations, never by a
settlement lag. Any future T+1 implementation would need to introduce a
genuinely separate pending-settlement state and thread
`SETTLEMENT_POLICY_VERSION` through every consumer that currently assumes
immediate settlement; until then this constant is the single source of
truth for which policy is active.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

# Milestone 11.3 Part 32: explicit, versioned settlement policy. Bump the
# version suffix (not the identifier) if the *mechanics* of immediate
# settlement ever change in a way that would affect a recomputed snapshot
# hash; introduce a new identifier entirely (e.g.
# "MARKET_DAY_T_PLUS_1.v1") only if/when true deferred settlement is built.
SETTLEMENT_POLICY_VERSION = "IMMEDIATE_SIMULATED_SETTLEMENT.v1"

from ..storage import paper_books_repositories as repo
from ..storage.database import begin_immediate
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
    """Milestone 11.2 Part 7: the settled-cash/existing-reservations read and
    the reservation insert must be serialized at book scope — otherwise two
    concurrent BUY intents can both observe enough available cash before
    either commits and together reserve more than exists. When `commit=True`
    (the only mode any caller currently uses) this function owns its own
    `BEGIN IMMEDIATE` book-scoped lock end-to-end. `commit=False` is reserved
    for a caller that has *already* acquired its own book-scoped write lock
    (e.g. is itself inside a `begin_immediate` block) and wants this
    reservation to participate in that outer transaction instead."""
    idem = f"reserve:{paper_order_intent_id}"
    if not commit:
        return _reserve_for_order_locked(conn, book_id, paper_order_intent_id, notional_usd, now, idem, commit=False)
    try:
        begin_immediate(conn)
        result = _reserve_for_order_locked(conn, book_id, paper_order_intent_id, notional_usd, now, idem, commit=False)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def _reserve_for_order_locked(
    conn, book_id: str, paper_order_intent_id: str, notional_usd: Decimal, now: datetime, idem: str,
    *, commit: bool,
) -> bool:
    if repo.cash_ledger_entry_exists(conn, book_id, idem):
        return False
    current_available = available_cash(conn, book_id)
    if notional_usd > current_available:
        raise InsufficientCashError(
            f"book {book_id}: cannot reserve {notional_usd} — only {current_available} available"
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


def remaining_buy_reservation(conn, book_id: str, paper_order_intent_id: str) -> Decimal:
    """Original reserved amount minus every release event recorded so far for this intent.

    Reservation and release are both append-only ledger events keyed by
    ``reference_id``; this never inspects settlement events directly so it
    stays correct even if a caller releases in several partial steps.
    """
    reserved = Decimal("0")
    released = Decimal("0")
    for e in repo.list_cash_ledger_entries(conn, book_id):
        if e.get("reference_id") != paper_order_intent_id:
            continue
        if e["event_type"] == CASH_EVENT_BUY_RESERVATION:
            reserved += Decimal(e["amount_usd"])
        elif e["event_type"] == CASH_EVENT_ORDER_RELEASE:
            released += -Decimal(e["amount_usd"])
    return reserved - released


def release_settled_buy_reservation(
    conn, book_id: str, paper_order_intent_id: str, fill_id: str, notional_usd: Decimal, now: datetime,
    *, commit: bool = True,
) -> bool:
    """Release the portion of the reservation attributable to one durably-applied BUY fill.

    Keyed per ``fill_id`` so repeated fill application (idempotent replay,
    reconciliation) never double-releases; the amount released is clamped to
    whatever remains reserved so a malformed or oversized fill can never
    drive the reservation, and therefore available cash, negative.
    """
    remaining = remaining_buy_reservation(conn, book_id, paper_order_intent_id)
    if remaining <= 0:
        return False
    amount = notional_usd if notional_usd < remaining else remaining
    idem = f"release:{paper_order_intent_id}:fill:{fill_id}"
    entry = CashLedgerEntry(
        book_id=book_id, ledger_entry_id=idem, event_type=CASH_EVENT_ORDER_RELEASE,
        amount_usd=-amount, event_timestamp=now, idempotency_key=idem, reference_id=paper_order_intent_id,
    )
    return repo.save_cash_ledger_entry(conn, entry, commit=commit)


def release_remaining_buy_reservation(
    conn, book_id: str, paper_order_intent_id: str, now: datetime, *, release_event_id: str, commit: bool = True,
) -> bool:
    """Release exactly what remains reserved once an order will receive no more fills.

    Safe to call repeatedly with the same ``release_event_id`` (e.g. once per
    terminal broker state observed) — the idempotency key is stable, so a
    second call after the reservation is already fully released is a no-op,
    and the released amount can never exceed the original reservation.
    """
    remaining = remaining_buy_reservation(conn, book_id, paper_order_intent_id)
    if remaining <= 0:
        return False
    idem = f"release:{paper_order_intent_id}:{release_event_id}"
    entry = CashLedgerEntry(
        book_id=book_id, ledger_entry_id=idem, event_type=CASH_EVENT_ORDER_RELEASE,
        amount_usd=-remaining, event_timestamp=now, idempotency_key=idem, reference_id=paper_order_intent_id,
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
