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
