"""Tests for cash_ledger.py + positions.py (docs/milestone-8.md Steps 6-7, 24-25)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.paper_books import cash_ledger, corporate_actions, positions
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "paper_books_test.db")
        yield c
        c.close()


def _open_both_books(conn, baseline_cash="100000.00", enhanced_cash="100000.00"):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal(baseline_cash), config_hash="cfg1", clock=lambda: NOW)
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=Decimal(enhanced_cash), config_hash="cfg1", clock=lambda: NOW)


# -- initial capital / cash model ------------------------------------------


def test_open_book_creates_initial_capital_exactly_once(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    entries = repo.list_cash_ledger_entries(conn, "BASELINE")
    initial_entries = [e for e in entries if e["event_type"] == "INITIAL_CAPITAL"]
    assert len(initial_entries) == 1
    assert cash_ledger.available_cash(conn, "BASELINE") == Decimal("100000.00")


def test_available_vs_reserved_cash(conn):
    _open_both_books(conn)
    cash_ledger.reserve_for_order(conn, "BASELINE", "intent1", Decimal("1500.00"), NOW)
    assert cash_ledger.reserved_cash(conn, "BASELINE") == Decimal("1500.00")
    assert cash_ledger.available_cash(conn, "BASELINE") == Decimal("98500.00")
    assert cash_ledger.settled_cash(conn, "BASELINE") == Decimal("100000.00")


def test_insufficient_cash_raises_before_writing_a_row(conn):
    _open_both_books(conn, enhanced_cash="50.00")
    with pytest.raises(cash_ledger.InsufficientCashError):
        cash_ledger.reserve_for_order(conn, "ENHANCED", "intentX", Decimal("1000.00"), NOW)
    assert repo.list_cash_ledger_entries(conn, "ENHANCED") == [
        e for e in repo.list_cash_ledger_entries(conn, "ENHANCED") if e["event_type"] == "INITIAL_CAPITAL"
    ]


def test_negative_available_cash_never_occurs(conn):
    _open_both_books(conn, baseline_cash="100.00")
    with pytest.raises(cash_ledger.InsufficientCashError):
        cash_ledger.reserve_for_order(conn, "BASELINE", "intent1", Decimal("101.00"), NOW)
    assert cash_ledger.available_cash(conn, "BASELINE") >= 0


def test_baseline_cash_cannot_satisfy_enhanced_order(conn):
    _open_both_books(conn, baseline_cash="100000.00", enhanced_cash="10.00")
    # BASELINE has plenty of cash, but ENHANCED's own reservation must fail
    # using only ENHANCED's own balance — no fallback to BASELINE's cash.
    with pytest.raises(cash_ledger.InsufficientCashError):
        cash_ledger.reserve_for_order(conn, "ENHANCED", "intentE", Decimal("500.00"), NOW)
    # BASELINE is unaffected and can still reserve normally.
    assert cash_ledger.reserve_for_order(conn, "BASELINE", "intentB", Decimal("500.00"), NOW) is True


def test_buy_settlement_and_sell_settlement(conn):
    _open_both_books(conn)
    cash_ledger.reserve_for_order(conn, "BASELINE", "intent1", Decimal("1500.00"), NOW)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fill1", Decimal("10"), Decimal("150.00"), NOW)
    cash_ledger.settle_buy(conn, "BASELINE", "fill1", Decimal("1500.00"), Decimal("1.00"), Decimal("0.50"), NOW)
    cash_ledger.release_reservation(conn, "BASELINE", "intent1", Decimal("1500.00"), NOW, reason="filled")
    assert cash_ledger.available_cash(conn, "BASELINE") == Decimal("100000.00") - Decimal("1500.00") - Decimal("1.50")

    realized = positions.apply_sell_fill(conn, "BASELINE", "AAPL", "fill2", Decimal("5"), Decimal("160.00"), NOW)
    cash_ledger.settle_sell(conn, "BASELINE", "fill2", Decimal("800.00"), Decimal("0"), Decimal("0"), NOW)
    assert realized == Decimal("50.00")


def test_duplicate_settlement_is_idempotent(conn):
    _open_both_books(conn)
    cash_ledger.settle_buy(conn, "BASELINE", "fillX", Decimal("100.00"), Decimal("0"), Decimal("0"), NOW)
    before = cash_ledger.available_cash(conn, "BASELINE")
    cash_ledger.settle_buy(conn, "BASELINE", "fillX", Decimal("100.00"), Decimal("0"), Decimal("0"), NOW)
    after = cash_ledger.available_cash(conn, "BASELINE")
    assert before == after


def test_compensating_cash_adjustment_requires_operator_and_reason(conn):
    _open_both_books(conn)
    with pytest.raises(ValueError):
        cash_ledger.cash_adjustment(
            conn, "BASELINE", Decimal("10.00"), operator="", reason="", idempotency_key="adj1", now=NOW,
        )
    ok = cash_ledger.cash_adjustment(
        conn, "BASELINE", Decimal("10.00"), operator="ops", reason="correcting a bug", idempotency_key="adj1", now=NOW,
    )
    assert ok is True
    assert cash_ledger.available_cash(conn, "BASELINE") == Decimal("100010.00")


# -- FIFO lot accounting / position isolation ------------------------------


def test_fifo_lot_consumption_oldest_first(conn):
    _open_both_books(conn)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fillA", Decimal("10"), Decimal("100.00"), NOW)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fillB", Decimal("10"), Decimal("200.00"), NOW)
    # Selling 10 should consume the first (cheaper) lot entirely.
    realized = positions.apply_sell_fill(conn, "BASELINE", "AAPL", "fillC", Decimal("10"), Decimal("250.00"), NOW)
    assert realized == Decimal("10") * (Decimal("250.00") - Decimal("100.00"))
    lots = repo.list_all_lots(conn, "BASELINE", "AAPL")
    first_lot = next(l for l in lots if l["opening_fill_id"] == "fillA")
    assert Decimal(first_lot["remaining_quantity"]) == 0


def test_no_sell_quantity_greater_than_available(conn):
    _open_both_books(conn)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fillA", Decimal("2"), Decimal("100.00"), NOW)
    with pytest.raises(positions.InsufficientPositionError):
        positions.apply_sell_fill(conn, "BASELINE", "AAPL", "fillB", Decimal("3"), Decimal("100.00"), NOW)


def test_baseline_position_cannot_satisfy_enhanced_sell(conn):
    _open_both_books(conn)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fillA", Decimal("10"), Decimal("100.00"), NOW)
    # ENHANCED never bought AAPL — selling from ENHANCED must fail even
    # though BASELINE holds plenty.
    with pytest.raises(positions.InsufficientPositionError):
        positions.apply_sell_fill(conn, "ENHANCED", "AAPL", "fillB", Decimal("1"), Decimal("100.00"), NOW)


def test_realized_pnl_recomputable_from_fifo(conn):
    _open_both_books(conn)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fillA", Decimal("10"), Decimal("100.00"), NOW)
    realized1 = positions.apply_sell_fill(conn, "BASELINE", "AAPL", "fillB", Decimal("4"), Decimal("120.00"), NOW)
    realized2 = positions.apply_sell_fill(conn, "BASELINE", "AAPL", "fillC", Decimal("6"), Decimal("90.00"), NOW)
    position = repo.load_position(conn, "BASELINE", "AAPL")
    assert Decimal(position["realized_pnl_usd"]) == realized1 + realized2


def test_forward_split_preserves_cost_basis(conn):
    _open_both_books(conn)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fillA", Decimal("10"), Decimal("150.00"), NOW)
    corporate_actions.apply_corporate_action(
        conn, "BASELINE", action_id="split1", symbol="AAPL", action_type="forward_split",
        effective_date="2026-07-14", ratio=Decimal("2"), now=NOW,
    )
    position = repo.load_position(conn, "BASELINE", "AAPL")
    assert Decimal(position["quantity"]) == Decimal("20")
    assert Decimal(position["quantity"]) * Decimal(position["average_cost_usd"]) == Decimal("1500.00")


def test_reverse_split_preserves_cost_basis(conn):
    _open_both_books(conn)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fillA", Decimal("10"), Decimal("150.00"), NOW)
    corporate_actions.apply_corporate_action(
        conn, "BASELINE", action_id="rsplit1", symbol="AAPL", action_type="reverse_split",
        effective_date="2026-07-14", ratio=Decimal("0.5"), now=NOW,
    )
    position = repo.load_position(conn, "BASELINE", "AAPL")
    assert Decimal(position["quantity"]) == Decimal("5")
    assert Decimal(position["quantity"]) * Decimal(position["average_cost_usd"]) == Decimal("1500.00")


def test_corporate_action_applied_once_per_book(conn):
    _open_both_books(conn)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fillA", Decimal("10"), Decimal("150.00"), NOW)
    r1 = corporate_actions.apply_corporate_action(
        conn, "BASELINE", action_id="split1", symbol="AAPL", action_type="forward_split",
        effective_date="2026-07-14", ratio=Decimal("2"), now=NOW,
    )
    r2 = corporate_actions.apply_corporate_action(
        conn, "BASELINE", action_id="split1", symbol="AAPL", action_type="forward_split",
        effective_date="2026-07-14", ratio=Decimal("2"), now=NOW,
    )
    assert r1["applied"] is True
    assert r2["applied"] is False


def test_cash_dividend_credits_correct_book_only(conn):
    _open_both_books(conn)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fillA", Decimal("20"), Decimal("100.00"), NOW)
    before_baseline = cash_ledger.available_cash(conn, "BASELINE")
    before_enhanced = cash_ledger.available_cash(conn, "ENHANCED")
    corporate_actions.apply_corporate_action(
        conn, "BASELINE", action_id="div1", symbol="AAPL", action_type="cash_dividend",
        effective_date="2026-07-14", dividend_per_share_usd=Decimal("0.50"), now=NOW,
    )
    corporate_actions.apply_corporate_action(
        conn, "ENHANCED", action_id="div1", symbol="AAPL", action_type="cash_dividend",
        effective_date="2026-07-14", dividend_per_share_usd=Decimal("0.50"), now=NOW,
    )
    assert cash_ledger.available_cash(conn, "BASELINE") == before_baseline + Decimal("10.00")
    assert cash_ledger.available_cash(conn, "ENHANCED") == before_enhanced  # held nothing, $0 dividend


def test_unsupported_corporate_action_type_rejected(conn):
    _open_both_books(conn)
    with pytest.raises(corporate_actions.UnsupportedCorporateActionError):
        corporate_actions.apply_corporate_action(
            conn, "BASELINE", action_id="x1", symbol="AAPL", action_type="spin_off",
            effective_date="2026-07-14", now=NOW,
        )
