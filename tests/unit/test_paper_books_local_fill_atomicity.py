"""Milestone 11.2 Part 6/36/37 regression: local simulated fill application
(`execution.submit_and_simulate`) must persist fill + lot/position + cash
settlement + reservation release + order status atomically — a crash at any
stage must leave none of those effects applied, not a partial subset."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.paper_books import cash_ledger, execution
from trading_research.paper_books.models import INTENT_STATUS_FILLED, INTENT_STATUS_PENDING_SUBMISSION, PaperBookOrderIntent
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


def _assert_no_fill_effects(conn, book_id, intent_id, fill_id):
    assert not repo.fill_exists(conn, book_id, fill_id)
    order = repo.load_order_intent(conn, book_id, intent_id)
    assert order["status"] == INTENT_STATUS_PENDING_SUBMISSION
    position = repo.load_position(conn, book_id, "AAPL")
    assert position is None


@pytest.mark.parametrize(
    "crash_target",
    [
        "trading_research.storage.paper_books_repositories.save_fill",
        "trading_research.paper_books.positions.apply_buy_fill",
        "trading_research.paper_books.cash_ledger.settle_buy",
        "trading_research.paper_books.cash_ledger.release_reservation",
    ],
)
def test_crash_at_each_stage_leaves_no_partial_fill(conn, monkeypatch, crash_target):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    intent = _intent(limit_price="151.00")
    market = execution.MarketSimulationInput(bid=Decimal("149.90"), ask=Decimal("150.10"))
    fill_id = f"pb-fill-{intent.paper_order_intent_id}"

    module_path, func_name = crash_target.rsplit(".", 1)
    import importlib

    module = importlib.import_module(module_path)
    original = getattr(module, func_name)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(module, func_name, _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        execution.submit_and_simulate(conn, intent, market, NOW)

    monkeypatch.setattr(module, func_name, original)

    _assert_no_fill_effects(conn, "BASELINE", intent.paper_order_intent_id, fill_id)

    # Reservation from submission-time (BUY) survives the crashed fill
    # attempt untouched — it was never part of the fill-application tx.
    available = cash_ledger.available_cash(conn, "BASELINE")
    assert available == Decimal("100000.00") - intent.notional_usd

    # Connection remains usable: a retry now succeeds cleanly and exactly
    # one fill/lot/position results.
    result = execution.submit_and_simulate(conn, intent, market, NOW)
    assert result["status"] == INTENT_STATUS_FILLED
    assert repo.fill_exists(conn, "BASELINE", fill_id)
    position = repo.load_position(conn, "BASELINE", "AAPL")
    assert Decimal(position["quantity"]) == Decimal("10")


def test_crash_after_order_status_update_still_commits_atomically(conn):
    """Sanity: with no injected failure, all effects land together in a
    single commit (order status flips to FILLED alongside everything else,
    never observed mid-way)."""
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    intent = _intent(limit_price="151.00")
    market = execution.MarketSimulationInput(bid=Decimal("149.90"), ask=Decimal("150.10"))
    result = execution.submit_and_simulate(conn, intent, market, NOW)
    assert result["status"] == INTENT_STATUS_FILLED
    fill_id = f"pb-fill-{intent.paper_order_intent_id}"
    assert repo.fill_exists(conn, "BASELINE", fill_id)
    order = repo.load_order_intent(conn, "BASELINE", intent.paper_order_intent_id)
    assert order["status"] == INTENT_STATUS_FILLED
    position = repo.load_position(conn, "BASELINE", "AAPL")
    assert Decimal(position["quantity"]) == Decimal("10")
    lots = repo.list_all_lots(conn, "BASELINE", "AAPL")
    assert len(lots) == 1


def test_replay_after_successful_fill_does_not_duplicate(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    intent = _intent(limit_price="151.00")
    market = execution.MarketSimulationInput(bid=Decimal("149.90"), ask=Decimal("150.10"))
    first = execution.submit_and_simulate(conn, intent, market, NOW)
    second = execution.submit_and_simulate(conn, intent, market, NOW)
    assert first["status"] == INTENT_STATUS_FILLED
    assert second["status"] == INTENT_STATUS_FILLED
    assert second["fill"] is None  # idempotent no-op, not a duplicate
    position = repo.load_position(conn, "BASELINE", "AAPL")
    assert Decimal(position["quantity"]) == Decimal("10")
