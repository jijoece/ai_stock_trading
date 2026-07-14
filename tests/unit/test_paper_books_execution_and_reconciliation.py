"""Tests for execution.py + reconciliation.py (docs/milestone-8.md Steps 15-17, 24)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.paper_books import cash_ledger, execution, reconciliation
from trading_research.paper_books.models import (
    INTENT_STATUS_FILLED,
    INTENT_STATUS_PENDING_SUBMISSION,
    INTENT_STATUS_REJECTED,
    PaperBookOrderIntent,
)
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "paper_books_test.db")
        yield c
        c.close()


def _intent(book_id="BASELINE", symbol="AAPL", side="BUY", qty="10", limit_price="151.00", intent_id=None, rec_id="rec1") -> PaperBookOrderIntent:
    quantity = Decimal(qty)
    price = Decimal(limit_price)
    return PaperBookOrderIntent(
        paper_order_intent_id=intent_id or f"pb-intent-{book_id}-{rec_id}", book_id=book_id, experiment_arm=book_id,
        cycle_id="cyc1", recommendation_id=rec_id, symbol=symbol, side=side, order_type="LIMIT",
        quantity=quantity, limit_price=price, notional_usd=quantity * price, time_in_force="DAY",
        as_of=NOW, risk_decision_id="rd1", portfolio_snapshot_id="snap1", config_hash="cfg1", created_at=NOW,
    )


def test_marketable_limit_order_fills(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    intent = _intent(limit_price="151.00")
    market = execution.MarketSimulationInput(bid=Decimal("149.90"), ask=Decimal("150.10"))
    result = execution.submit_and_simulate(conn, intent, market, NOW)
    assert result["status"] == INTENT_STATUS_FILLED
    assert result["fill"]["fill_price"] <= Decimal("151.00")


def test_unmarketable_limit_order_stays_pending(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    intent = _intent(limit_price="100.00")
    market = execution.MarketSimulationInput(bid=Decimal("149.90"), ask=Decimal("150.10"))
    result = execution.submit_and_simulate(conn, intent, market, NOW)
    assert result["status"] == INTENT_STATUS_PENDING_SUBMISSION
    assert result["fill"] is None
    order = repo.load_order_intent(conn, "BASELINE", intent.paper_order_intent_id)
    assert order["status"] == INTENT_STATUS_PENDING_SUBMISSION


def test_duplicate_fill_is_idempotent_within_one_book(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    intent = _intent(limit_price="151.00")
    market = execution.MarketSimulationInput(bid=Decimal("149.90"), ask=Decimal("150.10"))
    execution.submit_and_simulate(conn, intent, market, NOW)
    cash_after_first = cash_ledger.available_cash(conn, "BASELINE")
    execution.submit_and_simulate(conn, intent, market, NOW)
    cash_after_second = cash_ledger.available_cash(conn, "BASELINE")
    assert cash_after_first == cash_after_second
    fills = repo.list_fills(conn, "BASELINE")
    assert len(fills) == 1


def test_fill_id_reused_in_another_book_does_not_collide(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    intent_b = _intent(book_id="BASELINE", limit_price="151.00", intent_id="shared-intent-id", rec_id="rec1")
    intent_e = _intent(book_id="ENHANCED", limit_price="151.00", intent_id="shared-intent-id", rec_id="rec1")
    market = execution.MarketSimulationInput(bid=Decimal("149.90"), ask=Decimal("150.10"))
    result_b = execution.submit_and_simulate(conn, intent_b, market, NOW)
    result_e = execution.submit_and_simulate(conn, intent_e, market, NOW)
    assert result_b["status"] == INTENT_STATUS_FILLED
    assert result_e["status"] == INTENT_STATUS_FILLED
    baseline_fills = repo.list_fills(conn, "BASELINE")
    enhanced_fills = repo.list_fills(conn, "ENHANCED")
    assert len(baseline_fills) == 1
    assert len(enhanced_fills) == 1
    # Both books independently opened a position — no cross-book bleed.
    assert repo.load_position(conn, "BASELINE", "AAPL")["quantity"] == "10"
    assert repo.load_position(conn, "ENHANCED", "AAPL")["quantity"] == "10"


def test_sell_exceeding_available_position_is_rejected_not_simulated(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    intent = _intent(side="SELL", qty="5", limit_price="100.00")
    market = execution.MarketSimulationInput(bid=Decimal("99.90"), ask=Decimal("100.10"))
    result = execution.submit_and_simulate(conn, intent, market, NOW)
    assert result["status"] == INTENT_STATUS_REJECTED


def test_no_fallback_from_enhanced_to_baseline_book(conn):
    """Submitting an ENHANCED-arm intent always writes to the ENHANCED book's
    own tables — never BASELINE's, even implicitly."""
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    intent = _intent(book_id="ENHANCED", limit_price="151.00", rec_id="rec-enh")
    market = execution.MarketSimulationInput(bid=Decimal("149.90"), ask=Decimal("150.10"))
    execution.submit_and_simulate(conn, intent, market, NOW)
    assert repo.load_position(conn, "ENHANCED", "AAPL") is not None
    assert repo.load_position(conn, "BASELINE", "AAPL") is None


# -- reconciliation ----------------------------------------------------------


def test_clean_book_reconciles_matched(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    intent = _intent(limit_price="151.00")
    market = execution.MarketSimulationInput(bid=Decimal("149.90"), ask=Decimal("150.10"))
    execution.submit_and_simulate(conn, intent, market, NOW)
    result = reconciliation.reconcile_book(conn, "BASELINE", NOW)
    assert result["status"] == reconciliation.STATUS_MATCHED
    assert result["mismatches"] == []


def test_tampered_position_produces_mismatch(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    intent = _intent(limit_price="151.00")
    market = execution.MarketSimulationInput(bid=Decimal("149.90"), ask=Decimal("150.10"))
    execution.submit_and_simulate(conn, intent, market, NOW)
    conn.execute("UPDATE paper_book_positions SET quantity = '999' WHERE book_id = 'BASELINE' AND symbol = 'AAPL'")
    conn.commit()
    result = reconciliation.reconcile_book(conn, "BASELINE", NOW)
    assert result["status"] == reconciliation.STATUS_POSITION_MISMATCH


def test_arm_mismatch_detected(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    result = reconciliation.reconcile_book(conn, "BASELINE", NOW, expected_arm="ENHANCED")
    assert result["status"] == reconciliation.STATUS_ARM_MISMATCH


def test_reconciling_unknown_book_fails(conn):
    with pytest.raises(ValueError):
        reconciliation.reconcile_book(conn, "NOT_A_BOOK", NOW)


def test_one_book_mismatch_does_not_hide_in_the_other(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    intent_b = _intent(book_id="BASELINE", limit_price="151.00", rec_id="recb")
    intent_e = _intent(book_id="ENHANCED", limit_price="151.00", rec_id="rece")
    market = execution.MarketSimulationInput(bid=Decimal("149.90"), ask=Decimal("150.10"))
    execution.submit_and_simulate(conn, intent_b, market, NOW)
    execution.submit_and_simulate(conn, intent_e, market, NOW)
    conn.execute("UPDATE paper_book_positions SET quantity = '999' WHERE book_id = 'BASELINE' AND symbol = 'AAPL'")
    conn.commit()
    baseline_result = reconciliation.reconcile_book(conn, "BASELINE", NOW)
    enhanced_result = reconciliation.reconcile_book(conn, "ENHANCED", NOW)
    assert baseline_result["status"] == reconciliation.STATUS_POSITION_MISMATCH
    assert enhanced_result["status"] == reconciliation.STATUS_MATCHED
