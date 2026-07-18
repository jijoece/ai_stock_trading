"""Milestone 11.2 Part 7/8/37 regression: BUY cash reservations and SELL
share reservations must be serialized at book (and book+symbol) scope
across *separate* `sqlite3.Connection` objects, not just within one
Python-level call. These tests use two real connections against the same
on-disk database and two real threads so the race is genuine, not
simulated sequentially."""
from __future__ import annotations

import tempfile
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.paper_books import cash_ledger, positions
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "paper_books_test.db"


def _run_both(fn_a, fn_b):
    """Runs `fn_a`/`fn_b` on separate threads. Each callable must open (and
    close) its own `sqlite3.Connection` internally — a connection created on
    one thread cannot be used from another (`check_same_thread` default)."""
    results = {}
    errors = {}

    def _wrap(key, fn):
        try:
            results[key] = fn()
        except Exception as exc:  # noqa: BLE001 - captured for assertion, not swallowed
            errors[key] = exc

    t_a = threading.Thread(target=_wrap, args=("a", fn_a))
    t_b = threading.Thread(target=_wrap, args=("b", fn_b))
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)
    return results, errors


def test_two_buy_intents_cannot_collectively_overreserve(db_path):
    """available cash = 100; two intents each require 80 — exactly one may succeed."""
    setup = connect(db_path)
    cash_ledger.open_book(setup, book_id="BASELINE", starting_cash_usd=Decimal("100"), config_hash="cfg1", clock=lambda: NOW)
    setup.close()

    def _attempt(intent_id):
        conn = connect(db_path)
        try:
            return cash_ledger.reserve_for_order(conn, "BASELINE", intent_id, Decimal("80"), NOW)
        finally:
            conn.close()

    results, errors = _run_both(lambda: _attempt("intent-a"), lambda: _attempt("intent-b"))
    successes = [v for v in results.values() if v is True]
    failures = [e for e in errors.values() if isinstance(e, cash_ledger.InsufficientCashError)]
    assert len(successes) == 1
    assert len(failures) == 1

    verify = connect(db_path)
    available = cash_ledger.available_cash(verify, "BASELINE")
    assert available == Decimal("20")
    assert available >= 0
    verify.close()


def test_two_sell_intents_cannot_collectively_overreserve(db_path):
    """position = 10; two intents each require 7 — exactly one may succeed,
    and quantity = available_quantity + reserved_quantity always holds."""
    setup = connect(db_path)
    cash_ledger.open_book(setup, book_id="BASELINE", starting_cash_usd=Decimal("100000"), config_hash="cfg1", clock=lambda: NOW)
    positions.apply_buy_fill(setup, "BASELINE", "AAPL", "fill-seed", Decimal("10"), Decimal("100"), NOW)
    setup.close()

    def _attempt(intent_id, client_id):
        conn = connect(db_path)
        try:
            return positions.reserve_shares_for_sell(conn, "BASELINE", "AAPL", intent_id, client_id, Decimal("7"), NOW)
        finally:
            conn.close()

    results, errors = _run_both(
        lambda: _attempt("intent-a", "client-a"), lambda: _attempt("intent-b", "client-b"),
    )
    successes = [v for v in results.values() if v is True]
    failures = [e for e in errors.values() if isinstance(e, positions.InsufficientPositionError)]
    assert len(successes) == 1
    assert len(failures) == 1

    verify = connect(db_path)
    position = repo.load_position(verify, "BASELINE", "AAPL")
    quantity = Decimal(position["quantity"])
    available = Decimal(position["available_quantity"])
    reserved = Decimal(position["reserved_quantity"])
    assert quantity == available + reserved
    assert available >= 0
    assert reserved == Decimal("7")
    verify.close()


def test_different_client_order_ids_still_serialize(db_path):
    """Different paper_order_intent_id/client_order_id values must not
    bypass the book-scoped lock (Part 7: 'different client order IDs cannot
    bypass the lock')."""
    setup = connect(db_path)
    cash_ledger.open_book(setup, book_id="BASELINE", starting_cash_usd=Decimal("100"), config_hash="cfg1", clock=lambda: NOW)
    setup.close()

    def _attempt(intent_id):
        conn = connect(db_path)
        try:
            return cash_ledger.reserve_for_order(conn, "BASELINE", intent_id, Decimal("60"), NOW)
        finally:
            conn.close()

    results, errors = _run_both(lambda: _attempt("intent-x"), lambda: _attempt("intent-y"))
    successes = [v for v in results.values() if v is True]
    assert len(successes) == 1
    # A third, later, distinct intent must see the correct remaining balance.
    conn_c = connect(db_path)
    try:
        with pytest.raises(cash_ledger.InsufficientCashError):
            cash_ledger.reserve_for_order(conn_c, "BASELINE", "intent-z", Decimal("60"), NOW)
    finally:
        conn_c.close()
