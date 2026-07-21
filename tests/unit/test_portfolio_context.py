"""Unit tests for the authoritative portfolio-exposure snapshot pipeline
(docs/milestones/26.md PR B): `PortfolioAccountSnapshot` completeness
semantics in `models/trading_models.py::build_portfolio_state`, and the
fixture/ledger sources in `evidence_providers/portfolio_context.py`.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.evidence_providers.portfolio_context import (
    LedgerPortfolioAccountSnapshotProvider,
    fixture_portfolio_account_snapshot,
    unverified_portfolio_account_snapshot,
)
from trading_research.models.trading_models import (
    PortfolioAccountSnapshot,
    PortfolioPositionSnapshot,
    build_portfolio_state,
)
from trading_research.paper.ledger import FillModel, PaperLedger

NOW = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)

BUILDER_KWARGS = dict(
    maximum_account_age_seconds=300,
    maximum_positions_age_seconds=300,
    maximum_position_price_age_seconds=300,
)


def _complete_snapshot(**overrides) -> PortfolioAccountSnapshot:
    base = dict(
        account_equity=Decimal("10000"), settled_cash=Decimal("10000"),
        account_as_of=NOW, positions={}, positions_as_of=NOW,
        account_identity="paper_ledger:local", account_verified=True,
        account_query_complete=True, positions_query_complete=True,
        source="paper_ledger",
    )
    base.update(overrides)
    return PortfolioAccountSnapshot(**base)


# --- Completeness semantics -------------------------------------------------

def test_empty_positions_without_completion_proof_is_unknown_not_zero():
    snapshot = _complete_snapshot(positions_query_complete=False)
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False


def test_empty_positions_with_verified_complete_fresh_snapshots_is_known_zero():
    snapshot = _complete_snapshot()
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is True
    assert portfolio.symbol_exposure_fraction == {}
    assert portfolio.portfolio_exposure_fraction == 0.0


def test_account_query_incomplete_is_incomplete():
    snapshot = _complete_snapshot(account_query_complete=False)
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False


def test_positions_query_incomplete_is_incomplete():
    snapshot = _complete_snapshot(positions_query_complete=False)
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False


def test_missing_account_identity_is_incomplete():
    snapshot = _complete_snapshot(account_identity=None)
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False


# --- Freshness ---------------------------------------------------------------

def test_stale_account_snapshot_is_incomplete():
    snapshot = _complete_snapshot(account_as_of=NOW - timedelta(seconds=301))
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False


def test_stale_positions_snapshot_is_incomplete():
    snapshot = _complete_snapshot(positions_as_of=NOW - timedelta(seconds=301))
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False


def test_fresh_account_and_positions_but_stale_held_symbol_price_is_incomplete():
    snapshot = _complete_snapshot(
        positions={
            "AAA": PortfolioPositionSnapshot(
                quantity=5, market_price=Decimal("100"), price_as_of=NOW - timedelta(seconds=301),
            ),
        },
    )
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False


def test_future_account_timestamp_is_incomplete():
    snapshot = _complete_snapshot(account_as_of=NOW + timedelta(seconds=5))
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False


def test_future_position_price_timestamp_is_incomplete():
    snapshot = _complete_snapshot(
        positions={
            "AAA": PortfolioPositionSnapshot(
                quantity=5, market_price=Decimal("100"), price_as_of=NOW + timedelta(seconds=5),
            ),
        },
    )
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False


# --- Empty holdings ----------------------------------------------------------

def test_authoritative_complete_empty_positions_gives_unheld_symbol_known_zero():
    snapshot = _complete_snapshot()
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is True
    assert "AAA" not in portfolio.existing_positions


def test_empty_dict_without_completion_proof_is_unknown():
    snapshot = _complete_snapshot(account_verified=False)
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False


# --- Fixture vs. real (ledger) provenance ------------------------------------

def test_fixture_mode_snapshot_is_explicitly_labeled_and_complete():
    snapshot = fixture_portfolio_account_snapshot(NOW)
    assert snapshot.source == "fixture"
    assert snapshot.account_verified is True
    assert snapshot.account_query_complete is True
    assert snapshot.positions_query_complete is True
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is True
    assert portfolio.portfolio_source == "fixture"


@pytest.fixture
def ledger_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_real_mode_empty_ledger_yields_verified_zero_exposure(ledger_conn):
    ledger = PaperLedger(ledger_conn, starting_cash=100_000.0, fill_model=FillModel())
    provider = LedgerPortfolioAccountSnapshotProvider(ledger, clock=lambda: NOW)
    snapshot = provider.fetch(NOW)
    assert snapshot.source == "paper_ledger"
    assert snapshot.account_equity == Decimal("100000")
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is True
    assert portfolio.portfolio_source == "paper_ledger"


def test_real_mode_held_position_without_live_price_is_incomplete(ledger_conn):
    ledger = PaperLedger(ledger_conn, starting_cash=100_000.0, fill_model=FillModel())
    ledger.submit_and_fill("AAA", "buy", 10, bid=99.0, ask=101.0, idempotency_key="k1", now=NOW)
    provider = LedgerPortfolioAccountSnapshotProvider(ledger, clock=lambda: NOW)
    snapshot = provider.fetch(NOW)
    assert "AAA" in snapshot.positions
    assert snapshot.positions["AAA"].market_price is None  # no live mark from the local ledger
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False  # never a fabricated exposure number


def test_real_mode_with_no_portfolio_provider_yields_symbol_exposure_unknown():
    """B3: real mode with no authoritative source available must never fall
    back to a synthetic $100,000 account."""
    snapshot = unverified_portfolio_account_snapshot()
    assert snapshot.account_equity is None
    assert snapshot.account_verified is False
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False
    assert portfolio.account_equity is None


def test_real_mode_ledger_query_failure_is_incomplete(ledger_conn):
    class _BrokenLedger:
        def positions(self):
            raise RuntimeError("boom")

        def settled_cash(self, as_of):
            raise RuntimeError("boom")

        def total_cash(self):
            raise RuntimeError("boom")

    provider = LedgerPortfolioAccountSnapshotProvider(_BrokenLedger(), clock=lambda: NOW)  # type: ignore[arg-type]
    snapshot = provider.fetch(NOW)
    assert snapshot.account_verified is False
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False


# --- Historical / point-in-time behavior -------------------------------------

def test_ledger_snapshot_rejects_materially_historical_as_of(ledger_conn):
    """The local ledger has no historical point-in-time reconstruction — a
    materially past `as_of` cannot be proven and must not be represented as
    though today's live state existed back then (docs/milestones/26.md B7)."""
    ledger = PaperLedger(ledger_conn, starting_cash=100_000.0, fill_model=FillModel())
    provider = LedgerPortfolioAccountSnapshotProvider(ledger, clock=lambda: NOW)
    historical_as_of = NOW - timedelta(days=30)
    snapshot = provider.fetch(historical_as_of)
    assert snapshot.account_verified is False
    portfolio = build_portfolio_state(snapshot, as_of=historical_as_of, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False


def test_ledger_snapshot_accepts_current_as_of(ledger_conn):
    ledger = PaperLedger(ledger_conn, starting_cash=100_000.0, fill_model=FillModel())
    provider = LedgerPortfolioAccountSnapshotProvider(ledger, clock=lambda: NOW)
    snapshot = provider.fetch(NOW)
    assert snapshot.account_verified is True


# --- docs/milestones/28.md PR A: book-scoped read-only portfolio provider ----

from trading_research.evidence_providers.portfolio_context import (  # noqa: E402
    BookScopedPortfolioAccountSnapshotProvider,
    book_account_identity,
)
from trading_research.paper.ledger import PaperLedger as _LegacyPaperLedger  # noqa: E402
from trading_research.paper_books import cash_ledger as book_cash_ledger  # noqa: E402
from trading_research.paper_books import positions as book_positions  # noqa: E402
from trading_research.storage import paper_books_repositories as book_repo  # noqa: E402
from trading_research.storage.database import connect as db_connect  # noqa: E402

CONFIG_HASH = "test-config-hash"


def _open_book(conn, book_id: str, *, starting_cash=Decimal("10000")):
    return book_cash_ledger.open_book(
        conn, book_id=book_id, starting_cash_usd=starting_cash, config_hash=CONFIG_HASH, clock=lambda: NOW,
    )


def _mark_position(conn, book_id: str, symbol: str, price: Decimal, price_as_of: datetime) -> None:
    position = book_repo.load_position(conn, book_id, symbol)
    book_repo.upsert_position(conn, book_id, symbol, {
        "quantity": Decimal(position["quantity"]), "available_quantity": Decimal(position["available_quantity"]),
        "reserved_quantity": Decimal(position["reserved_quantity"]),
        "average_cost_usd": Decimal(position["average_cost_usd"]),
        "realized_pnl_usd": Decimal(position["realized_pnl_usd"]), "fees_usd": Decimal(position["fees_usd"]),
        "latest_valuation_price": price, "valuation_timestamp": price_as_of.isoformat(),
        "valuation_status": "COMPLETE", "updated_at": price_as_of.isoformat(),
    })


@pytest.fixture
def book_conn(tmp_path):
    conn = db_connect(tmp_path / "portfolio.db")
    yield conn
    conn.close()


def test_book_scoped_empty_database_yields_no_synthetic_account_and_no_writes(book_conn):
    provider = BookScopedPortfolioAccountSnapshotProvider(book_conn, book_id="BASELINE", clock=lambda: NOW)
    snapshot = provider.fetch(NOW)
    assert snapshot.account_verified is False
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False
    assert portfolio.account_equity is None
    assert book_conn.execute("SELECT COUNT(*) AS c FROM paper_books").fetchone()["c"] == 0
    assert book_conn.execute("SELECT COUNT(*) AS c FROM paper_book_cash_ledger").fetchone()["c"] == 0
    # legacy global cash table must never be created by a real-mode portfolio read
    legacy_table = book_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'paper_cash_state'"
    ).fetchone()
    assert legacy_table is None


def test_book_scoped_position_detected_and_legacy_ledger_ignored(book_conn):
    _open_book(book_conn, "BASELINE")
    book_positions.apply_buy_fill(
        book_conn, "BASELINE", "AAA", "fill-1", Decimal("10"), Decimal("100"), NOW,
    )
    _mark_position(book_conn, "BASELINE", "AAA", Decimal("100"), NOW)

    # A position seeded only in the legacy global ledger must never leak in.
    legacy_ledger = _LegacyPaperLedger(book_conn, starting_cash=100_000.0)
    legacy_ledger.submit_and_fill("MSFT", "buy", 5, bid=99.0, ask=101.0, idempotency_key="legacy-1", now=NOW)

    provider = BookScopedPortfolioAccountSnapshotProvider(book_conn, book_id="BASELINE", clock=lambda: NOW)
    snapshot = provider.fetch(NOW)
    assert "AAA" in snapshot.positions
    assert "MSFT" not in snapshot.positions
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.shares_held("AAA") == 10


def test_book_isolation_between_baseline_and_enhanced(book_conn):
    _open_book(book_conn, "BASELINE")
    _open_book(book_conn, "ENHANCED")
    book_positions.apply_buy_fill(book_conn, "BASELINE", "AAPL", "fill-b1", Decimal("10"), Decimal("100"), NOW)
    _mark_position(book_conn, "BASELINE", "AAPL", Decimal("100"), NOW)
    book_positions.apply_buy_fill(book_conn, "ENHANCED", "MSFT", "fill-e1", Decimal("5"), Decimal("200"), NOW)
    _mark_position(book_conn, "ENHANCED", "MSFT", Decimal("200"), NOW)

    provider = BookScopedPortfolioAccountSnapshotProvider(book_conn, book_id="BASELINE", clock=lambda: NOW)
    snapshot = provider.fetch(NOW)
    assert "AAPL" in snapshot.positions
    assert "MSFT" not in snapshot.positions
    assert snapshot.account_identity == book_account_identity("BASELINE")


def test_book_scoped_missing_mark_is_incomplete_then_complete_with_fresh_mark(book_conn):
    _open_book(book_conn, "BASELINE")
    book_positions.apply_buy_fill(book_conn, "BASELINE", "AAA", "fill-1", Decimal("10"), Decimal("100"), NOW)

    provider = BookScopedPortfolioAccountSnapshotProvider(book_conn, book_id="BASELINE", clock=lambda: NOW)
    snapshot = provider.fetch(NOW)
    assert snapshot.positions["AAA"].market_price is None
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False

    _mark_position(book_conn, "BASELINE", "AAA", Decimal("150"), NOW)
    snapshot = provider.fetch(NOW)
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is True
    # apply_buy_fill only updates positions/lots — cash settlement is a
    # separate step (paper_books/cash_ledger.py::settle_buy) this test does
    # not exercise, so settled cash stays at the book's opening balance.
    assert portfolio.account_equity == Decimal("10000") + Decimal("1500")
    assert portfolio.symbol_exposure_fraction["AAA"] == pytest.approx(
        float(Decimal("10") * Decimal("150") / portfolio.account_equity)
    )


def test_book_scoped_point_in_time_rejects_fill_after_cycle_as_of(book_conn):
    cycle_as_of = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    fill_committed_at = datetime(2026, 7, 20, 10, 2, tzinfo=timezone.utc)
    snapshot_read_at = datetime(2026, 7, 20, 10, 3, tzinfo=timezone.utc)

    _open_book(book_conn, "BASELINE")
    book_positions.apply_buy_fill(
        book_conn, "BASELINE", "AAA", "fill-1", Decimal("10"), Decimal("100"), fill_committed_at,
    )
    _mark_position(book_conn, "BASELINE", "AAA", Decimal("100"), fill_committed_at)

    provider = BookScopedPortfolioAccountSnapshotProvider(
        book_conn, book_id="BASELINE", clock=lambda: snapshot_read_at,
    )
    snapshot = provider.fetch(cycle_as_of)
    portfolio = build_portfolio_state(snapshot, as_of=cycle_as_of, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is False


def test_book_scoped_current_cycle_with_matching_read_time_is_accepted(book_conn):
    _open_book(book_conn, "BASELINE")
    provider = BookScopedPortfolioAccountSnapshotProvider(book_conn, book_id="BASELINE", clock=lambda: NOW)
    snapshot = provider.fetch(NOW)
    portfolio = build_portfolio_state(snapshot, as_of=NOW, **BUILDER_KWARGS)
    assert portfolio.symbol_exposure_complete is True


def test_book_scoped_read_never_mutates_database(book_conn):
    _open_book(book_conn, "BASELINE")
    book_positions.apply_buy_fill(book_conn, "BASELINE", "AAA", "fill-1", Decimal("10"), Decimal("100"), NOW)
    _mark_position(book_conn, "BASELINE", "AAA", Decimal("100"), NOW)

    def _row_counts():
        return {
            table: book_conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            for table in ("paper_books", "paper_book_cash_ledger", "paper_book_positions", "paper_book_position_lots")
        }

    before_counts = _row_counts()
    before_cash = book_cash_ledger.settled_cash(book_conn, "BASELINE")

    provider = BookScopedPortfolioAccountSnapshotProvider(book_conn, book_id="BASELINE", clock=lambda: NOW)
    provider.fetch(NOW)

    assert _row_counts() == before_counts
    assert book_cash_ledger.settled_cash(book_conn, "BASELINE") == before_cash


def test_book_scoped_concurrent_read_snapshot_is_never_mixed(tmp_path):
    from trading_research.evidence_providers.portfolio_context import _book_scoped_read_transaction

    db_path = tmp_path / "concurrency.db"
    conn_a = db_connect(db_path)
    conn_b = db_connect(db_path)
    try:
        _open_book(conn_a, "BASELINE")
        book_positions.apply_buy_fill(conn_a, "BASELINE", "AAA", "fill-1", Decimal("10"), Decimal("100"), NOW)

        with _book_scoped_read_transaction(conn_a):
            positions_before = book_repo.list_positions(conn_a, "BASELINE", open_only=True)
            cash_before = book_cash_ledger.settled_cash(conn_a, "BASELINE")

            # Connection B durably commits a second fill while A's read
            # transaction is still open — A's already-open snapshot must
            # not observe it.
            book_positions.apply_buy_fill(conn_b, "BASELINE", "AAA", "fill-2", Decimal("5"), Decimal("110"), NOW)

            positions_after = book_repo.list_positions(conn_a, "BASELINE", open_only=True)
            cash_after = book_cash_ledger.settled_cash(conn_a, "BASELINE")

        assert positions_before == positions_after
        assert cash_before == cash_after
    finally:
        conn_a.close()
        conn_b.close()
