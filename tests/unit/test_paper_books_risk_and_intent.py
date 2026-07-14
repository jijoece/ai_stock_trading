"""Tests for risk.py + order_intent.py (docs/milestone-8.md Steps 10-12, 24-25)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.paper_books import cash_ledger, order_intent, risk, valuation
from trading_research.paper_books.config import RiskSection
from trading_research.paper_books.models import (
    RISK_APPROVED,
    RISK_APPROVED_REDUCED,
    RISK_REJECTED_ARM_MISMATCH,
    RISK_REJECTED_BOOK_PAUSED,
    RISK_REJECTED_INVALID_RECOMMENDATION,
    RISK_REJECTED_MAX_OPEN_POSITIONS,
    RISK_REJECTED_MISSING_PRICE,
    RISK_REJECTED_STALE_PRICE,
    PaperPortfolioContext,
    VALUATION_COMPLETE,
    PaperBookModelError,
)
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "paper_books_test.db")
        yield c
        c.close()


def _risk_config(**overrides) -> RiskSection:
    defaults = dict(
        max_position_weight=Decimal("0.10"), max_order_notional_usd=Decimal("1000.00"),
        max_daily_new_notional_usd=Decimal("5000.00"), minimum_cash_buffer_weight=Decimal("0.10"),
        max_open_positions=20, max_symbol_concentration_weight=Decimal("0.10"),
        reject_stale_market_price_seconds=900,
    )
    defaults.update(overrides)
    return RiskSection(**defaults)


def _context(**overrides) -> PaperPortfolioContext:
    defaults = dict(
        book_id="BASELINE", as_of=NOW, available_cash_usd=Decimal("100000"), reserved_cash_usd=Decimal("0"),
        net_liquidation_value_usd=Decimal("100000"), current_position_quantity=Decimal("0"),
        current_position_market_value_usd=None, current_position_weight=None, open_position_count=0,
        daily_new_notional_usd=Decimal("0"), valuation_status=VALUATION_COMPLETE,
    )
    defaults.update(overrides)
    return PaperPortfolioContext(**defaults)


# -- risk decisions ---------------------------------------------------------


def test_full_approval_when_within_all_caps():
    decision = risk.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="BASELINE", expected_arm="BASELINE", context=_context(),
        requested_quantity_hint=Decimal("5"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
        reference_price_point_in_time_safe=True, risk_config=_risk_config(),
    )
    assert decision.decision == RISK_APPROVED
    assert decision.approved_quantity == Decimal("5")


def test_reduced_by_max_order_notional():
    decision = risk.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="BASELINE", expected_arm="BASELINE", context=_context(),
        requested_quantity_hint=Decimal("100"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
        reference_price_point_in_time_safe=True, risk_config=_risk_config(),
    )
    assert decision.decision == RISK_APPROVED_REDUCED
    assert decision.approved_notional_usd <= Decimal("1000.00")


def test_rejected_insufficient_cash():
    context = _context(available_cash_usd=Decimal("10"), net_liquidation_value_usd=Decimal("10"))
    decision = risk.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="BASELINE", expected_arm="BASELINE", context=context,
        requested_quantity_hint=Decimal("5"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
        reference_price_point_in_time_safe=True, risk_config=_risk_config(),
    )
    assert decision.decision.startswith("REJECTED_")


def test_rejected_max_open_positions_for_new_position():
    context = _context(open_position_count=20, current_position_quantity=Decimal("0"))
    decision = risk.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="BASELINE", expected_arm="BASELINE", context=context,
        requested_quantity_hint=Decimal("1"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
        reference_price_point_in_time_safe=True, risk_config=_risk_config(max_open_positions=20),
    )
    assert decision.decision == RISK_REJECTED_MAX_OPEN_POSITIONS


def test_rejected_stale_price():
    decision = risk.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="BASELINE", expected_arm="BASELINE", context=_context(),
        requested_quantity_hint=Decimal("5"), reference_price=Decimal("150.00"), reference_price_age_seconds=10000,
        reference_price_point_in_time_safe=True, risk_config=_risk_config(),
    )
    assert decision.decision == RISK_REJECTED_STALE_PRICE


def test_rejected_missing_price():
    decision = risk.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="BASELINE", expected_arm="BASELINE", context=_context(),
        requested_quantity_hint=Decimal("5"), reference_price=None, reference_price_age_seconds=None,
        reference_price_point_in_time_safe=None, risk_config=_risk_config(),
    )
    assert decision.decision == RISK_REJECTED_MISSING_PRICE


def test_rejected_book_paused():
    decision = risk.evaluate_paper_risk(
        book_status="PAUSED", experiment_arm="BASELINE", expected_arm="BASELINE", context=_context(),
        requested_quantity_hint=Decimal("5"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
        reference_price_point_in_time_safe=True, risk_config=_risk_config(),
    )
    assert decision.decision == RISK_REJECTED_BOOK_PAUSED


def test_rejected_invalid_recommendation():
    decision = risk.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="BASELINE", expected_arm="BASELINE", context=_context(),
        requested_quantity_hint=Decimal("0"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
        reference_price_point_in_time_safe=True, risk_config=_risk_config(),
    )
    assert decision.decision == RISK_REJECTED_INVALID_RECOMMENDATION


def test_rejected_arm_mismatch():
    decision = risk.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="ENHANCED", expected_arm="BASELINE", context=_context(),
        requested_quantity_hint=Decimal("5"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
        reference_price_point_in_time_safe=True, risk_config=_risk_config(),
    )
    assert decision.decision == RISK_REJECTED_ARM_MISMATCH


def test_partial_valuation_blocks_sizing():
    from trading_research.paper_books.models import VALUATION_PARTIAL_MISSING_PRICE

    context = _context(valuation_status=VALUATION_PARTIAL_MISSING_PRICE, net_liquidation_value_usd=None)
    decision = risk.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="BASELINE", expected_arm="BASELINE", context=context,
        requested_quantity_hint=Decimal("5"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
        reference_price_point_in_time_safe=True, risk_config=_risk_config(),
    )
    assert decision.decision == RISK_REJECTED_MISSING_PRICE


def test_deterministic_repeatable_decision():
    ctx = _context()
    cfg = _risk_config()
    d1 = risk.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="BASELINE", expected_arm="BASELINE", context=ctx,
        requested_quantity_hint=Decimal("5"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
        reference_price_point_in_time_safe=True, risk_config=cfg,
    )
    d2 = risk.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="BASELINE", expected_arm="BASELINE", context=ctx,
        requested_quantity_hint=Decimal("5"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
        reference_price_point_in_time_safe=True, risk_config=cfg,
    )
    assert d1 == d2


# -- order intents -----------------------------------------------------------


def test_same_recommendation_creates_different_book_aware_intent_ids(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    intent_ids = {}
    for book_id in ("BASELINE", "ENHANCED"):
        snap = valuation.build_portfolio_snapshot(conn, book_id, NOW, maximum_price_age_seconds=900)
        context = risk.build_portfolio_context(conn, book_id, NOW, snap, "AAPL", Decimal("0"))
        decision = risk.evaluate_paper_risk(
            book_status="ACTIVE", experiment_arm=book_id, expected_arm=book_id, context=context,
            requested_quantity_hint=Decimal("5"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
            reference_price_point_in_time_safe=True, risk_config=_risk_config(),
        )
        rd_id = order_intent.persist_risk_decision(conn, book_id, "cyc1", "rec-shared", "AAPL", decision, snap.snapshot_id, lambda: NOW)
        intent = order_intent.build_order_intent(
            book_id=book_id, experiment_arm=book_id, cycle_id="cyc1", recommendation_id="rec-shared",
            symbol="AAPL", risk_decision=decision, risk_decision_id=rd_id, portfolio_snapshot_id=snap.snapshot_id,
            config_hash="cfg1", as_of=NOW, clock=lambda: NOW,
        )
        intent_ids[book_id] = intent.paper_order_intent_id
    assert intent_ids["BASELINE"] != intent_ids["ENHANCED"]


def test_rejected_decision_never_creates_intent():
    decision = risk.evaluate_paper_risk(
        book_status="PAUSED", experiment_arm="BASELINE", expected_arm="BASELINE", context=_context(),
        requested_quantity_hint=Decimal("5"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
        reference_price_point_in_time_safe=True, risk_config=_risk_config(),
    )
    intent = order_intent.build_order_intent(
        book_id="BASELINE", experiment_arm="BASELINE", cycle_id="cyc1", recommendation_id="rec1",
        symbol="AAPL", risk_decision=decision, risk_decision_id="rd1", portfolio_snapshot_id="snap1",
        config_hash="cfg1", as_of=NOW, clock=lambda: NOW,
    )
    assert intent is None


def test_persist_order_intent_is_idempotent(conn):
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash="cfg1", clock=lambda: NOW)
    snap = valuation.build_portfolio_snapshot(conn, "BASELINE", NOW, maximum_price_age_seconds=900)
    context = risk.build_portfolio_context(conn, "BASELINE", NOW, snap, "AAPL", Decimal("0"))
    decision = risk.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="BASELINE", expected_arm="BASELINE", context=context,
        requested_quantity_hint=Decimal("5"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
        reference_price_point_in_time_safe=True, risk_config=_risk_config(),
    )
    rd_id = order_intent.persist_risk_decision(conn, "BASELINE", "cyc1", "rec1", "AAPL", decision, snap.snapshot_id, lambda: NOW)
    intent = order_intent.build_order_intent(
        book_id="BASELINE", experiment_arm="BASELINE", cycle_id="cyc1", recommendation_id="rec1", symbol="AAPL",
        risk_decision=decision, risk_decision_id=rd_id, portfolio_snapshot_id=snap.snapshot_id, config_hash="cfg1",
        as_of=NOW, clock=lambda: NOW,
    )
    assert order_intent.persist_order_intent(conn, intent) is True
    assert order_intent.persist_order_intent(conn, intent) is False


def test_enhanced_order_cannot_target_baseline_book_via_arm_mismatch():
    """An enhanced-arm recommendation evaluated against a BASELINE book's
    expected_arm is rejected structurally — there is no fallback."""
    decision = risk.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="ENHANCED", expected_arm="BASELINE", context=_context(book_id="BASELINE"),
        requested_quantity_hint=Decimal("5"), reference_price=Decimal("150.00"), reference_price_age_seconds=10,
        reference_price_point_in_time_safe=True, risk_config=_risk_config(),
    )
    assert decision.decision == RISK_REJECTED_ARM_MISMATCH
    intent = order_intent.build_order_intent(
        book_id="BASELINE", experiment_arm="ENHANCED", cycle_id="cyc1", recommendation_id="rec1", symbol="AAPL",
        risk_decision=decision, risk_decision_id="rd1", portfolio_snapshot_id="snap1", config_hash="cfg1",
        as_of=NOW, clock=lambda: NOW,
    )
    assert intent is None
