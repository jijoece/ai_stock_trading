"""Tests for comparison.py + promotion_evidence.py (docs/milestone-8.md Steps 20-21, 27)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.evaluation.price_provider import PricePoint
from trading_research.paper_books import cash_ledger, comparison, positions, promotion_evidence
from trading_research.paper_books.experiment_assignment import PaperBookExperimentAssignment, save_assignment
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect

T0 = datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=1)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "paper_books_test.db")
        yield c
        c.close()


class _FakePriceProvider:
    def __init__(self, price: Decimal):
        self.price = price

    def get_close(self, symbol, as_of):
        return PricePoint(symbol=symbol, as_of=as_of, close=self.price, source="fixture")


def _do_buy(conn, book_id, symbol, fill_id, qty, price, ts):
    repo.save_fill(conn, {
        "book_id": book_id, "fill_id": fill_id, "paper_order_intent_id": "intent-" + fill_id, "symbol": symbol,
        "side": "BUY", "simulated_market_price": price, "limit_price": price, "fill_quantity": qty,
        "fill_price": price, "fees_usd": Decimal("0"), "slippage_usd": Decimal("0"), "fill_timestamp": ts,
        "simulation_rule_version": "v1",
    })
    positions.apply_buy_fill(conn, book_id, symbol, fill_id, qty, price, ts)
    cash_ledger.settle_buy(conn, book_id, fill_id, qty * price, Decimal("0"), Decimal("0"), ts)


def _open_and_assign(conn, baseline_cash="100000.00", enhanced_cash="100000.00"):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal(baseline_cash), config_hash="cfg1", clock=lambda: T0)
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=Decimal(enhanced_cash), config_hash="cfg1", clock=lambda: T0)
    save_assignment(conn, PaperBookExperimentAssignment(
        experiment_id="exp1", cycle_id="cyc1", symbol="AAPL", as_of=T0, evidence_snapshot_id="snap1",
        baseline_recommendation_id="rec-b", enhanced_recommendation_id="rec-e", baseline_book_id="BASELINE",
        enhanced_book_id="ENHANCED", baseline_intent_id="ib", enhanced_intent_id="ie",
    ), clock=lambda: T0)


def test_comparable_books_produce_metric_deltas(conn):
    _open_and_assign(conn)
    _do_buy(conn, "BASELINE", "AAPL", "fillb1", Decimal("10"), Decimal("150.00"), T0)
    _do_buy(conn, "ENHANCED", "AAPL", "fille1", Decimal("10"), Decimal("150.00"), T0)
    from trading_research.paper_books import valuation
    valuation.build_portfolio_snapshot(conn, "BASELINE", T0, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", T0, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "BASELINE", T1, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", T1, price_provider=_FakePriceProvider(Decimal("170.00")), maximum_price_age_seconds=900)

    cmp = comparison.build_comparison(conn, "exp1", "BASELINE", "ENHANCED", T0 - timedelta(hours=1), T1 + timedelta(hours=1), clock=lambda: T1)
    assert cmp.comparable is True
    assert cmp.metric_deltas["cumulative_return"] > 0


def test_different_starting_cash_fails_closed(conn):
    _open_and_assign(conn, baseline_cash="100000.00", enhanced_cash="50000.00")
    cmp = comparison.build_comparison(conn, "exp1", "BASELINE", "ENHANCED", T0 - timedelta(hours=1), T1, clock=lambda: T1)
    assert cmp.comparable is False
    assert any("starting cash" in r for r in cmp.comparability_reasons)


def test_different_evaluation_windows_not_applicable_since_shared_window(conn):
    """Both books are always evaluated over the identical caller-supplied
    window — there is no way to pass a different window per arm."""
    _open_and_assign(conn)
    cmp = comparison.build_comparison(conn, "exp1", "BASELINE", "ENHANCED", T0, T1, clock=lambda: T1)
    assert cmp.window_start == T0


def test_missing_enhanced_cycle_fails_closed(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: T0)
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: T0)
    save_assignment(conn, PaperBookExperimentAssignment(
        experiment_id="exp1", cycle_id="cyc1", symbol="AAPL", as_of=T0, evidence_snapshot_id="snap1",
        baseline_recommendation_id="rec-b", enhanced_recommendation_id=None, baseline_book_id="BASELINE",
        enhanced_book_id="ENHANCED", baseline_intent_id="ib", enhanced_intent_id=None,
    ), clock=lambda: T0)
    cmp = comparison.build_comparison(conn, "exp1", "BASELINE", "ENHANCED", T0 - timedelta(hours=1), T1, clock=lambda: T1)
    assert cmp.comparable is False
    assert any("missing an enhanced recommendation" in r for r in cmp.comparability_reasons)


def test_unsafe_valuation_fails_closed(conn):
    _open_and_assign(conn)
    _do_buy(conn, "BASELINE", "AAPL", "fillb1", Decimal("10"), Decimal("150.00"), T0)
    # No price provider supplied for BASELINE -> unvalued/missing price -> unsafe.
    from trading_research.paper_books import valuation
    valuation.build_portfolio_snapshot(conn, "BASELINE", T0, maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", T0, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    cmp = comparison.build_comparison(conn, "exp1", "BASELINE", "ENHANCED", T0 - timedelta(hours=1), T0 + timedelta(hours=1), clock=lambda: T0)
    assert cmp.comparable is False
    assert any("unsafe" in r for r in cmp.comparability_reasons)


def test_insufficient_sample_size_fails_closed(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: T0)
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: T0)
    cmp = comparison.build_comparison(
        conn, "exp1", "BASELINE", "ENHANCED", T0 - timedelta(hours=1), T0 + timedelta(hours=1),
        min_comparable_cycles=5, clock=lambda: T0,
    )
    assert cmp.comparable is False
    assert any("insufficient comparable cycles" in r for r in cmp.comparability_reasons)


def test_no_automatic_promotion_ever(conn):
    """Even a strongly positive delta only ever produces a review-eligible
    result — never an automatic promotion status."""
    _open_and_assign(conn)
    _do_buy(conn, "BASELINE", "AAPL", "fillb1", Decimal("10"), Decimal("150.00"), T0)
    _do_buy(conn, "ENHANCED", "AAPL", "fille1", Decimal("10"), Decimal("150.00"), T0)
    from trading_research.paper_books import valuation
    valuation.build_portfolio_snapshot(conn, "BASELINE", T0, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", T0, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "BASELINE", T1, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", T1, price_provider=_FakePriceProvider(Decimal("300.00")), maximum_price_age_seconds=900)
    cmp = comparison.build_comparison(conn, "exp1", "BASELINE", "ENHANCED", T0 - timedelta(hours=1), T1 + timedelta(hours=1), clock=lambda: T1)
    enhanced_metrics = repo.load_daily_metrics(conn, "ENHANCED", cmp.enhanced_metrics_id)["metrics"]
    result, reasons = promotion_evidence.evaluate_promotion_evidence(
        cmp, enhanced_metrics, cycle_count=1, min_comparable_cycles=1, min_trading_days=1, min_closed_trades=0,
        operational_health_ok=True, reconciliation_ok=True,
    )
    assert result == promotion_evidence.RESULT_PROMOTION_REVIEW_ELIGIBLE
    assert "not an automatic promotion" in reasons[0]
    assert result != "PROMOTED"  # this value must never exist in the vocabulary


def test_baseline_outperformance_reported_honestly(conn):
    _open_and_assign(conn)
    _do_buy(conn, "BASELINE", "AAPL", "fillb1", Decimal("10"), Decimal("150.00"), T0)
    _do_buy(conn, "ENHANCED", "AAPL", "fille1", Decimal("10"), Decimal("150.00"), T0)
    from trading_research.paper_books import valuation
    valuation.build_portfolio_snapshot(conn, "BASELINE", T0, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", T0, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "BASELINE", T1, price_provider=_FakePriceProvider(Decimal("170.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", T1, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    cmp = comparison.build_comparison(conn, "exp1", "BASELINE", "ENHANCED", T0 - timedelta(hours=1), T1 + timedelta(hours=1), clock=lambda: T1)
    enhanced_metrics = repo.load_daily_metrics(conn, "ENHANCED", cmp.enhanced_metrics_id)["metrics"]
    result, reasons = promotion_evidence.evaluate_promotion_evidence(
        cmp, enhanced_metrics, cycle_count=1, min_comparable_cycles=1, min_trading_days=1, min_closed_trades=0,
        operational_health_ok=True, reconciliation_ok=True,
    )
    assert result == promotion_evidence.RESULT_BASELINE_OUTPERFORMS


def test_operational_health_block_prevents_promotion_eligibility(conn):
    _open_and_assign(conn)
    _do_buy(conn, "BASELINE", "AAPL", "fillb1", Decimal("10"), Decimal("150.00"), T0)
    _do_buy(conn, "ENHANCED", "AAPL", "fille1", Decimal("10"), Decimal("150.00"), T0)
    from trading_research.paper_books import valuation
    valuation.build_portfolio_snapshot(conn, "BASELINE", T0, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", T0, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "BASELINE", T1, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", T1, price_provider=_FakePriceProvider(Decimal("170.00")), maximum_price_age_seconds=900)
    cmp = comparison.build_comparison(conn, "exp1", "BASELINE", "ENHANCED", T0 - timedelta(hours=1), T1 + timedelta(hours=1), clock=lambda: T1)
    enhanced_metrics = repo.load_daily_metrics(conn, "ENHANCED", cmp.enhanced_metrics_id)["metrics"]
    result, reasons = promotion_evidence.evaluate_promotion_evidence(
        cmp, enhanced_metrics, cycle_count=1, min_comparable_cycles=1, min_trading_days=1, min_closed_trades=0,
        operational_health_ok=False, reconciliation_ok=True,
    )
    assert result == promotion_evidence.RESULT_ENHANCED_OUTPERFORMS_NOT_PROMOTABLE


def test_reconciliation_block_prevents_promotion_eligibility(conn):
    _open_and_assign(conn)
    _do_buy(conn, "BASELINE", "AAPL", "fillb1", Decimal("10"), Decimal("150.00"), T0)
    _do_buy(conn, "ENHANCED", "AAPL", "fille1", Decimal("10"), Decimal("150.00"), T0)
    from trading_research.paper_books import valuation
    valuation.build_portfolio_snapshot(conn, "BASELINE", T0, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", T0, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "BASELINE", T1, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", T1, price_provider=_FakePriceProvider(Decimal("170.00")), maximum_price_age_seconds=900)
    cmp = comparison.build_comparison(conn, "exp1", "BASELINE", "ENHANCED", T0 - timedelta(hours=1), T1 + timedelta(hours=1), clock=lambda: T1)
    enhanced_metrics = repo.load_daily_metrics(conn, "ENHANCED", cmp.enhanced_metrics_id)["metrics"]
    result, reasons = promotion_evidence.evaluate_promotion_evidence(
        cmp, enhanced_metrics, cycle_count=1, min_comparable_cycles=1, min_trading_days=1, min_closed_trades=0,
        operational_health_ok=True, reconciliation_ok=False,
    )
    assert result == promotion_evidence.RESULT_ENHANCED_OUTPERFORMS_NOT_PROMOTABLE


def test_insufficient_sample_but_positive_delta_is_observed_only(conn):
    _open_and_assign(conn)
    _do_buy(conn, "BASELINE", "AAPL", "fillb1", Decimal("10"), Decimal("150.00"), T0)
    _do_buy(conn, "ENHANCED", "AAPL", "fille1", Decimal("10"), Decimal("150.00"), T0)
    from trading_research.paper_books import valuation
    valuation.build_portfolio_snapshot(conn, "BASELINE", T0, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", T0, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "BASELINE", T1, price_provider=_FakePriceProvider(Decimal("150.00")), maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", T1, price_provider=_FakePriceProvider(Decimal("170.00")), maximum_price_age_seconds=900)
    cmp = comparison.build_comparison(conn, "exp1", "BASELINE", "ENHANCED", T0 - timedelta(hours=1), T1 + timedelta(hours=1), clock=lambda: T1)
    enhanced_metrics = repo.load_daily_metrics(conn, "ENHANCED", cmp.enhanced_metrics_id)["metrics"]
    result, reasons = promotion_evidence.evaluate_promotion_evidence(
        cmp, enhanced_metrics, cycle_count=1, min_comparable_cycles=10, min_trading_days=1, min_closed_trades=0,
        operational_health_ok=True, reconciliation_ok=True,
    )
    assert result == promotion_evidence.RESULT_ENHANCED_OUTPERFORMS_OBSERVED


def test_not_comparable_blocks_promotion(conn):
    _open_and_assign(conn, baseline_cash="100000.00", enhanced_cash="50000.00")
    cmp = comparison.build_comparison(conn, "exp1", "BASELINE", "ENHANCED", T0 - timedelta(hours=1), T1, clock=lambda: T1)
    enhanced_metrics = {"daily_returns": None, "trade_count": 0}
    result, reasons = promotion_evidence.evaluate_promotion_evidence(
        cmp, enhanced_metrics, cycle_count=1, min_comparable_cycles=1, min_trading_days=1, min_closed_trades=0,
        operational_health_ok=True, reconciliation_ok=True,
    )
    assert result == promotion_evidence.RESULT_NOT_COMPARABLE
