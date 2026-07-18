"""Tests for valuation.py (docs/milestone-8.md Steps 8-9, 26 — point-in-time tests)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.evaluation.price_provider import PricePoint
from trading_research.paper_books import cash_ledger, positions, valuation
from trading_research.paper_books.models import (
    VALUATION_COMPLETE,
    VALUATION_PARTIAL_MISSING_PRICE,
    VALUATION_PARTIAL_STALE_PRICE,
    VALUATION_POINT_IN_TIME_UNSAFE,
)
from trading_research.research.models import EvidenceItem, EvidenceSnapshot, SourceRecord
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "paper_books_test.db")
        yield c
        c.close()


class _FakePriceProvider:
    def __init__(self, price: Decimal | None):
        self.price = price
        self.calls = []

    def get_close(self, symbol, as_of):
        self.calls.append((symbol, as_of))
        if self.price is None:
            return None
        return PricePoint(symbol=symbol, as_of=as_of, close=self.price, source="fixture")


class _FuturePriceProvider:
    def get_close(self, symbol, as_of):
        return PricePoint(
            symbol=symbol, as_of=as_of, close=Decimal("155"), source="fixture",
            available_at=NOW + timedelta(seconds=1),
        )


def _make_evidence_snapshot(symbol: str, close: Decimal, available_at: datetime, as_of: datetime, point_in_time_safe: bool = True):
    source = SourceRecord(
        source_id=f"src-{symbol}", source_type="market", provider="fixture-market", source_locator=None,
        retrieved_at=as_of, published_at=available_at, effective_at=available_at, available_at=available_at,
        content_hash="hash", status="ok", is_stale=False, point_in_time_safe=point_in_time_safe, error_code=None,
    )
    item = EvidenceItem(
        evidence_id=f"{symbol}-close", source_id=source.source_id, category="market", title="close",
        summary="close", normalized_values={"latest_close": float(close)}, as_of=available_at,
        confidence="high", stale=False,
    )
    return EvidenceSnapshot(
        snapshot_id=f"snap-{symbol}", symbol=symbol, as_of=as_of, created_at=as_of,
        source_records=(source,), evidence_items=(item,), deterministic_factors={}, sentiment_metrics={},
        portfolio_context=None, missing_data_reasons=(), conflict_reasons=(), point_in_time_safe=point_in_time_safe,
        config_hash="cfg", git_sha="sha",
    )


def test_cash_only_book_is_complete(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    snap = valuation.build_portfolio_snapshot(conn, "BASELINE", NOW, maximum_price_age_seconds=900)
    assert snap.valuation_status == VALUATION_COMPLETE
    assert snap.net_liquidation_value_usd == Decimal("100000.00")


def test_priced_position_produces_correct_unrealized_pnl(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fill1", Decimal("10"), Decimal("150.00"), NOW)
    cash_ledger.settle_buy(conn, "BASELINE", "fill1", Decimal("1500.00"), Decimal("0"), Decimal("0"), NOW)
    provider = _FakePriceProvider(Decimal("155.00"))
    snap = valuation.build_portfolio_snapshot(conn, "BASELINE", NOW, price_provider=provider, maximum_price_age_seconds=900)
    assert snap.valuation_status == VALUATION_COMPLETE
    assert snap.unrealized_pnl_usd == Decimal("50.00")
    assert snap.net_liquidation_value_usd == Decimal("100050.00")


def test_missing_price_never_becomes_zero(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fill1", Decimal("10"), Decimal("150.00"), NOW)
    snap = valuation.build_portfolio_snapshot(conn, "BASELINE", NOW, maximum_price_age_seconds=900)
    assert snap.valuation_status == VALUATION_PARTIAL_MISSING_PRICE
    assert snap.net_liquidation_value_usd is None
    assert snap.gross_market_value_usd is None
    assert snap.unvalued_position_count == 1


def test_stale_price_is_explicit(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fill1", Decimal("10"), Decimal("150.00"), NOW)
    stale_available_at = NOW - timedelta(seconds=2000)
    snapshot = _make_evidence_snapshot("AAPL", Decimal("155.00"), stale_available_at, NOW)
    snap = valuation.build_portfolio_snapshot(
        conn, "BASELINE", NOW, evidence_snapshots_by_symbol={"AAPL": snapshot}, maximum_price_age_seconds=900,
    )
    assert snap.valuation_status == VALUATION_PARTIAL_STALE_PRICE
    assert snap.stale_position_count == 1
    # Stale != missing: the price value is still usable.
    assert snap.net_liquidation_value_usd is not None


def test_unsafe_source_blocks_complete_valuation(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fill1", Decimal("10"), Decimal("150.00"), NOW)
    future_available_at = NOW + timedelta(hours=1)
    snapshot = _make_evidence_snapshot("AAPL", Decimal("155.00"), future_available_at, NOW, point_in_time_safe=False)
    snap = valuation.build_portfolio_snapshot(
        conn, "BASELINE", NOW, evidence_snapshots_by_symbol={"AAPL": snapshot}, maximum_price_age_seconds=900,
    )
    assert snap.valuation_status == VALUATION_POINT_IN_TIME_UNSAFE
    assert snap.net_liquidation_value_usd is None


def test_historical_close_with_future_availability_fails_closed(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fill1", Decimal("10"), Decimal("150.00"), NOW)
    snap = valuation.build_portfolio_snapshot(
        conn, "BASELINE", NOW, price_provider=_FuturePriceProvider(), maximum_price_age_seconds=900,
    )
    assert snap.valuation_status == VALUATION_POINT_IN_TIME_UNSAFE


def test_current_quote_never_substitutes_for_historical_price(conn):
    """The price provider is only ever asked for `as_of.date()` — never a
    live/"current" query — proven by inspecting exactly what it was called
    with."""
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fill1", Decimal("10"), Decimal("150.00"), NOW)
    provider = _FakePriceProvider(Decimal("155.00"))
    valuation.build_portfolio_snapshot(conn, "BASELINE", NOW, price_provider=provider, maximum_price_age_seconds=900)
    assert provider.calls == [("AAPL", NOW.date())]


def test_same_as_of_used_across_comparison_books(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    snap_b = valuation.build_portfolio_snapshot(conn, "BASELINE", NOW, maximum_price_age_seconds=900)
    snap_e = valuation.build_portfolio_snapshot(conn, "ENHANCED", NOW, maximum_price_age_seconds=900)
    assert snap_b.as_of == snap_e.as_of == NOW


def test_snapshot_id_stable_for_identical_inputs(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fill1", Decimal("10"), Decimal("150.00"), NOW)
    provider = _FakePriceProvider(Decimal("155.00"))
    snap1 = valuation.build_portfolio_snapshot(conn, "BASELINE", NOW, price_provider=provider, maximum_price_age_seconds=900)
    snap2 = valuation.build_portfolio_snapshot(conn, "BASELINE", NOW, price_provider=provider, maximum_price_age_seconds=900)
    assert snap1.snapshot_id == snap2.snapshot_id


def test_snapshot_id_changes_when_price_input_changes(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "fill1", Decimal("10"), Decimal("150.00"), NOW)
    snap_low = valuation.build_portfolio_snapshot(
        conn, "BASELINE", NOW, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900,
    )
    # Same position, a different price input -- persist=False so this
    # doesn't collide with snap_low's own row for the same (book_id, as_of).
    snap_high = valuation.build_portfolio_snapshot(
        conn, "BASELINE", NOW, price_provider=_FakePriceProvider(Decimal("160.00")), maximum_price_age_seconds=900,
        persist=False,
    )
    assert snap_low.snapshot_id != snap_high.snapshot_id
