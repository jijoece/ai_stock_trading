"""Milestone 11.3.1 Item 3: portfolio snapshot identity (`snapshot_id` +
`source_hash`) must change whenever any economically material cash, ledger,
position, or price input changes -- not just quantity/price. Complements
`test_paper_books_valuation.py`'s point-in-time price-selection tests.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.paper_books import cash_ledger, positions, valuation
from trading_research.storage.database import connect
from trading_research.storage.paper_books_repositories import SnapshotIdentityConflictError

NOW = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "snapshot_identity_test.db")
        yield c
        c.close()


class _FakePriceProvider:
    def __init__(self, price: Decimal | None, *, source: str = "fake"):
        self.price = price
        self.source = source

    def get_close(self, symbol, as_of):
        if self.price is None:
            return None
        from trading_research.evaluation.price_provider import PricePoint

        return PricePoint(symbol=symbol, as_of=as_of, close=self.price, source=self.source, available_at=None)


def _seeded(conn, *, starting_cash="100000.00"):
    cash_ledger.open_book(
        conn, book_id="BASELINE", starting_cash_usd=Decimal(starting_cash), config_hash="cfg1", clock=lambda: NOW,
    )
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fill1", Decimal("10"), Decimal("150.00"), NOW)


def _snapshot(conn, **overrides):
    kwargs = dict(price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900, persist=False)
    kwargs.update(overrides)
    return valuation.build_portfolio_snapshot(conn, "BASELINE", NOW, **kwargs)


# --- 1. cash adjustment -------------------------------------------------------


def test_identity_changes_after_cash_adjustment(conn):
    _seeded(conn)
    before = _snapshot(conn)
    cash_ledger.cash_adjustment(
        conn, "BASELINE", Decimal("500"), operator="alice", reason="correction", idempotency_key="adj-1", now=NOW,
    )
    after = _snapshot(conn)
    assert before.snapshot_id != after.snapshot_id
    assert before.source_hash != after.source_hash


# --- 2. reservation creation ---------------------------------------------------


def test_identity_changes_after_reservation_creation(conn):
    _seeded(conn)
    before = _snapshot(conn)
    cash_ledger.reserve_for_order(conn, "BASELINE", "intent-1", Decimal("1000"), NOW)
    after = _snapshot(conn)
    assert before.snapshot_id != after.snapshot_id


# --- 3. reservation release -----------------------------------------------------


def test_identity_changes_after_reservation_release(conn):
    _seeded(conn)
    cash_ledger.reserve_for_order(conn, "BASELINE", "intent-1", Decimal("1000"), NOW)
    reserved = _snapshot(conn)
    cash_ledger.release_reservation(conn, "BASELINE", "intent-1", Decimal("1000"), NOW, reason="cancelled")
    released = _snapshot(conn)
    assert reserved.snapshot_id != released.snapshot_id


# --- 4. fee change ---------------------------------------------------------------


def test_identity_changes_with_fee(conn):
    _seeded(conn)
    no_fee = _snapshot(conn)
    cash_ledger.settle_buy(conn, "BASELINE", "fee-fill", Decimal("100"), Decimal("5"), Decimal("0"), NOW)
    with_fee = _snapshot(conn)
    assert no_fee.snapshot_id != with_fee.snapshot_id


# --- 5. slippage change ------------------------------------------------------------


def test_identity_changes_with_slippage(conn):
    _seeded(conn)
    no_slippage = _snapshot(conn)
    cash_ledger.settle_buy(conn, "BASELINE", "slip-fill", Decimal("100"), Decimal("0"), Decimal("3"), NOW)
    with_slippage = _snapshot(conn)
    assert no_slippage.snapshot_id != with_slippage.snapshot_id


# --- 6. late fill -------------------------------------------------------------------


def test_identity_changes_after_late_fill(conn):
    _seeded(conn)
    before = _snapshot(conn)
    positions.apply_buy_fill(conn, "BASELINE", "MSFT", "fill2", Decimal("5"), Decimal("300.00"), NOW)
    after = _snapshot(
        conn, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900, persist=False,
    )
    assert before.snapshot_id != after.snapshot_id


# --- 7. cost-basis change ------------------------------------------------------------


def test_identity_changes_after_cost_basis_change(conn):
    _seeded(conn)
    before = _snapshot(conn)
    # A second BUY fill at a different price changes the position's average
    # cost basis without changing which symbols are held.
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fill2", Decimal("10"), Decimal("170.00"), NOW)
    after = _snapshot(conn)
    assert before.snapshot_id != after.snapshot_id


# --- 8. realized P&L change -----------------------------------------------------------


def test_identity_changes_after_realized_pnl_change(conn):
    _seeded(conn)
    before = _snapshot(conn)
    positions.apply_sell_fill(conn, "BASELINE", "AAPL", "fill-sell", Decimal("4"), Decimal("160.00"), NOW)
    after = _snapshot(conn)
    assert before.snapshot_id != after.snapshot_id
    assert before.snapshot_id  # sanity: still non-empty after a partial close


# --- 9. selected price provenance change --------------------------------------------


def test_identity_changes_with_different_price_provenance(conn):
    _seeded(conn)
    same_price_different_source = _snapshot(
        conn, price_provider=_FakePriceProvider(Decimal("150.00"), source="alternate_provider"),
        maximum_price_age_seconds=900, persist=False,
    )
    baseline = _snapshot(conn)
    assert baseline.snapshot_id != same_price_different_source.snapshot_id


# --- 10. settlement-policy version change ---------------------------------------------


def test_identity_changes_with_settlement_policy_version(conn, monkeypatch):
    _seeded(conn)
    before = _snapshot(conn)
    monkeypatch.setattr(valuation, "SETTLEMENT_POLICY_VERSION", "paper-books-settlement-v999")
    after = _snapshot(conn)
    assert before.snapshot_id != after.snapshot_id


# --- 11. valuation/snapshot methodology version change ---------------------------------


def test_identity_changes_with_snapshot_methodology_version(conn, monkeypatch):
    _seeded(conn)
    before = _snapshot(conn)
    monkeypatch.setattr(valuation, "SNAPSHOT_METHODOLOGY_VERSION", "paper-books-valuation-v999")
    after = _snapshot(conn)
    assert before.snapshot_id != after.snapshot_id


# --- exact replay is idempotent -------------------------------------------------------


def test_exact_replay_is_idempotent(conn):
    _seeded(conn)
    first = _snapshot(conn)
    second = _snapshot(conn)
    assert first.snapshot_id == second.snapshot_id
    assert first.source_hash == second.source_hash


# --- different source content cannot silently collide ---------------------------------


def test_same_snapshot_id_different_source_hash_raises(conn):
    """Structurally, snapshot_id and source_hash are both derived from the
    same canonical payload -- a real collision without a hash break is not
    reproducible. This proves the persistence-layer defense: a stored row
    later found with a mismatched source_hash under the same snapshot_id is
    a fail-closed integrity error, never silently discarded/overwritten."""
    from trading_research.storage import paper_books_repositories as repo

    _seeded(conn)
    snap = valuation.build_portfolio_snapshot(
        conn, "BASELINE", NOW, price_provider=_FakePriceProvider(Decimal("150.00")),
        maximum_price_age_seconds=900, persist=True,
    )
    import dataclasses

    tampered = dataclasses.replace(snap, source_hash="deliberately-different-hash")
    with pytest.raises(SnapshotIdentityConflictError):
        repo.save_snapshot(conn, tampered, [])


def test_corrected_snapshot_gets_its_own_row_not_silently_ignored(conn):
    _seeded(conn)
    from trading_research.storage import paper_books_repositories as repo

    original = valuation.build_portfolio_snapshot(
        conn, "BASELINE", NOW, price_provider=_FakePriceProvider(Decimal("150.00")),
        maximum_price_age_seconds=900, persist=True,
    )
    cash_ledger.cash_adjustment(
        conn, "BASELINE", Decimal("77"), operator="alice", reason="late correction",
        idempotency_key="adj-2", now=NOW,
    )
    corrected = valuation.build_portfolio_snapshot(
        conn, "BASELINE", NOW, price_provider=_FakePriceProvider(Decimal("150.00")),
        maximum_price_age_seconds=900, persist=True,
    )
    assert corrected.snapshot_id != original.snapshot_id
    assert repo.load_snapshot(conn, "BASELINE", original.snapshot_id) is not None
    assert repo.load_snapshot(conn, "BASELINE", corrected.snapshot_id) is not None


# --- ordering of positions/ledger inputs does not affect the hash ---------------------


def test_position_and_ledger_ordering_does_not_affect_hash(conn):
    cash_ledger.open_book(
        conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW,
    )
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fill1", Decimal("10"), Decimal("150.00"), NOW)
    positions.apply_buy_fill(conn, "BASELINE", "MSFT", "fill2", Decimal("5"), Decimal("300.00"), NOW)
    price_provider = _FakePriceProvider(Decimal("150.00"))
    snap = valuation.build_portfolio_snapshot(
        conn, "BASELINE", NOW, price_provider=price_provider, maximum_price_age_seconds=900, persist=False,
    )

    conn2 = connect(":memory:")
    cash_ledger.open_book(
        conn2, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW,
    )
    # Insert the same two fills in the opposite order.
    positions.apply_buy_fill(conn2, "BASELINE", "MSFT", "fill2", Decimal("5"), Decimal("300.00"), NOW)
    positions.apply_buy_fill(conn2, "BASELINE", "AAPL", "fill1", Decimal("10"), Decimal("150.00"), NOW)
    snap2 = valuation.build_portfolio_snapshot(
        conn2, "BASELINE", NOW, price_provider=price_provider, maximum_price_age_seconds=900, persist=False,
    )
    assert snap.snapshot_id == snap2.snapshot_id
    assert snap.source_hash == snap2.source_hash
    conn2.close()


# --- no unsupported object is silently stringified -------------------------------------


def test_unsupported_object_in_payload_raises_instead_of_stringifying():
    from trading_research.hashing import ConfigHashError
    from trading_research.paper_books.models import compute_snapshot_id

    class _Unsupported:
        def __str__(self) -> str:
            return "not-a-canonical-value"

    with pytest.raises(ConfigHashError):
        compute_snapshot_id({"book_id": "BASELINE", "weird": _Unsupported()})
