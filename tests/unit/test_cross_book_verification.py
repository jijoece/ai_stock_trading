"""Tests for `paper_books/cross_book_verification.py` (Milestone 9.2 Sections
5-8): authoritative PASSED/FAILED/INSUFFICIENT_DATA cross-book isolation
verification, replacing the permanent MISSING signal from Milestone 9.1."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.paper_books import cash_ledger, cross_book_verification as cbv, execution, order_intent, risk as risk_module, valuation
from trading_research.paper_books.config import (
    ExecutionSection,
    ExitsSection,
    LifecycleSection,
    PaperBookDefinition,
    PaperBooksConfiguration,
    PendingOrdersSection,
    RiskSection,
    ScheduledIntegrationSection,
    SoakSection,
    ValuationSection,
)
from trading_research.storage import paper_books_repositories as pb_repo
from trading_research.storage.database import connect

DAY1 = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "cbv_test.db")
        yield c
        c.close()


@pytest.fixture
def cfg() -> PaperBooksConfiguration:
    return PaperBooksConfiguration(
        version=1, enabled=True,
        baseline=PaperBookDefinition(enabled=True, book_id="BASELINE", starting_cash_usd=Decimal("100000")),
        enhanced=PaperBookDefinition(enabled=True, book_id="ENHANCED", starting_cash_usd=Decimal("100000")),
        execution=ExecutionSection(provider="local_simulated", allow_external_paper_broker=False, allow_live_broker=False),
        risk=RiskSection(
            max_position_weight=Decimal("0.9"), max_order_notional_usd=Decimal("50000"),
            max_daily_new_notional_usd=Decimal("50000"), minimum_cash_buffer_weight=Decimal("0.02"),
            max_open_positions=20, max_symbol_concentration_weight=Decimal("0.9"),
            reject_stale_market_price_seconds=999999,
        ),
        valuation=ValuationSection(price_source="persisted_market_bar", maximum_price_age_seconds=999999, missing_price_policy="MARK_UNVALUED"),
        scheduled_integration=ScheduledIntegrationSection(enabled=False),
        lifecycle=LifecycleSection(
            enabled=True, pending_orders=PendingOrdersSection(expire_after_market_days=3),
            exits=ExitsSection(
                enabled=True, stop_loss_percent=Decimal("0.08"), profit_target_percent=Decimal("0.15"),
                maximum_holding_market_days=20, exit_on_recommendation_reversal=True,
            ),
            soak=SoakSection(minimum_completed_cycles=1, minimum_market_days=1),
        ),
        config_hash="cbv-test-hash", raw={},
    )


def _open_position(conn, cfg, *, book_id: str, arm: str, symbol: str = "AAPL"):
    from trading_research.evaluation.price_provider import DeterministicPriceProvider

    pp = DeterministicPriceProvider()
    pp.register(symbol, DAY1.date(), Decimal("100"))
    book_def = cfg.baseline if book_id == cfg.baseline.book_id else cfg.enhanced
    cash_ledger.open_book(conn, book_id=book_id, starting_cash_usd=book_def.starting_cash_usd, config_hash=cfg.config_hash, clock=lambda: DAY1)
    snap = valuation.build_portfolio_snapshot(conn, book_id, DAY1, price_provider=pp, maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds)
    context = risk_module.build_portfolio_context(conn, book_id, DAY1, snap, symbol, Decimal("0"))
    decision = risk_module.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm=arm, expected_arm=arm, context=context,
        requested_quantity_hint=Decimal("100"), reference_price=Decimal("100"), reference_price_age_seconds=0,
        reference_price_point_in_time_safe=True, risk_config=cfg.risk,
    )
    risk_decision_id = order_intent.persist_risk_decision(conn, book_id, "c1", "rec1", symbol, decision, snap.snapshot_id, lambda: DAY1)
    intent = order_intent.build_order_intent(
        book_id=book_id, experiment_arm=arm, cycle_id="c1", recommendation_id="rec1", symbol=symbol,
        risk_decision=decision, risk_decision_id=risk_decision_id, portfolio_snapshot_id=snap.snapshot_id,
        config_hash=cfg.config_hash, as_of=DAY1, clock=lambda: DAY1,
    )
    market = execution.MarketSimulationInput(bid=Decimal("97"), ask=Decimal("97.5"))
    execution.submit_and_simulate(conn, intent, market, DAY1)
    return intent


def test_insufficient_data_when_no_books_opened(conn, cfg):
    result = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert result.status == cbv.STATUS_INSUFFICIENT_DATA
    assert result.violation_count == 0


def test_clean_isolated_books_pass(conn, cfg):
    _open_position(conn, cfg, book_id="BASELINE", arm="BASELINE", symbol="AAPL")
    _open_position(conn, cfg, book_id="ENHANCED", arm="ENHANCED", symbol="MSFT")
    result = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert result.status == cbv.STATUS_PASSED
    assert result.violation_count == 0


def test_same_identifier_text_in_two_isolated_books_does_not_fail(conn, cfg):
    """Both books trade the exact same symbol with the exact same
    `cycle_id`/`recommendation_id` text — structurally distinct rows
    (book_id-scoped primary keys), never flagged as a foreign reference."""
    _open_position(conn, cfg, book_id="BASELINE", arm="BASELINE", symbol="AAPL")
    _open_position(conn, cfg, book_id="ENHANCED", arm="ENHANCED", symbol="AAPL")
    result = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert result.status == cbv.STATUS_PASSED
    assert result.violation_count == 0


def test_arm_book_mismatch_fails(conn, cfg):
    _open_position(conn, cfg, book_id="BASELINE", arm="BASELINE", symbol="AAPL")
    # Every existing paper_book_orders row is immutable (core-fields trigger)
    # — inject the violation via a second, directly-inserted bad order row
    # rather than mutating the real one, simulating a hypothetical bug.
    conn.execute(
        "INSERT INTO paper_book_orders (book_id, paper_order_intent_id, experiment_arm, cycle_id, "
        "recommendation_id, symbol, side, order_type, quantity, limit_price, notional_usd, time_in_force, "
        "as_of, risk_decision_id, portfolio_snapshot_id, config_hash, created_at, status) VALUES "
        "('BASELINE', 'bad-order-1', 'ENHANCED', 'c1', 'rec1', 'AAPL', 'BUY', 'LIMIT', '1', '100', '100', "
        "'DAY', ?, 'rd1', 'snap1', 'h', ?, 'PENDING_SUBMISSION')",
        (DAY1.isoformat(), DAY1.isoformat()),
    )
    conn.commit()
    result = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert result.status == cbv.STATUS_FAILED
    assert result.violation_count >= 1
    failed_names = {c.name for c in result.checks if c.status == cbv.CHECK_STATUS_FAILED}
    assert "orders_arm_matches_book" in failed_names


def test_fill_order_foreign_reference_fails(conn, cfg):
    _open_position(conn, cfg, book_id="BASELINE", arm="BASELINE", symbol="AAPL")
    conn.execute(
        "INSERT INTO paper_book_fills (book_id, fill_id, paper_order_intent_id, symbol, side, "
        "simulated_market_price, limit_price, fill_quantity, fill_price, fees_usd, slippage_usd, "
        "fill_timestamp, simulation_rule_version, created_at) VALUES "
        "('BASELINE', 'bad-fill-1', 'does-not-exist', 'AAPL', 'BUY', '100', '100', '1', '100', '0', '0', ?, 'v1', ?)",
        (DAY1.isoformat(), DAY1.isoformat()),
    )
    conn.commit()
    result = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert result.status == cbv.STATUS_FAILED
    failed_names = {c.name for c in result.checks if c.status == cbv.CHECK_STATUS_FAILED}
    assert "fills_reference_own_book_order" in failed_names


def test_cash_ledger_foreign_reference_fails(conn, cfg):
    _open_position(conn, cfg, book_id="BASELINE", arm="BASELINE", symbol="AAPL")
    _open_position(conn, cfg, book_id="ENHANCED", arm="ENHANCED", symbol="MSFT")
    enhanced_fill = pb_repo.list_fills(conn, "ENHANCED")[0]
    conn.execute(
        "INSERT INTO paper_book_cash_ledger (book_id, ledger_entry_id, event_type, amount_usd, event_timestamp, "
        "idempotency_key, cycle_id, symbol, reference_id, operator, reason, created_at) VALUES "
        "('BASELINE', 'bad-entry-1', 'FILL_SETTLEMENT', '-100', ?, 'bad-idem-1', 'c1', 'AAPL', ?, NULL, NULL, ?)",
        (DAY1.isoformat(), enhanced_fill["fill_id"], DAY1.isoformat()),
    )
    conn.commit()
    result = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert result.status == cbv.STATUS_FAILED
    failed_names = {c.name for c in result.checks if c.status == cbv.CHECK_STATUS_FAILED}
    assert "cash_ledger_foreign_reference" in failed_names


def test_lot_foreign_fill_reference_fails(conn, cfg):
    _open_position(conn, cfg, book_id="BASELINE", arm="BASELINE", symbol="AAPL")
    conn.execute(
        "INSERT INTO paper_book_position_lots (book_id, lot_id, symbol, opened_at, quantity, remaining_quantity, "
        "cost_basis_usd, opening_fill_id, closed_at, created_at) VALUES "
        "('BASELINE', 'bad-lot-1', 'AAPL', ?, '1', '1', '100', 'does-not-exist', NULL, ?)",
        (DAY1.isoformat(), DAY1.isoformat()),
    )
    conn.commit()
    result = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert result.status == cbv.STATUS_FAILED
    failed_names = {c.name for c in result.checks if c.status == cbv.CHECK_STATUS_FAILED}
    assert "lots_reference_own_book_fill" in failed_names


def test_deterministic_verification_id(conn, cfg):
    r1 = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg, operator_run_id="op-1", lifecycle_run_id="lc-1")
    r2 = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg, operator_run_id="op-1", lifecycle_run_id="lc-1")
    assert r1.verification_id == r2.verification_id
    r3 = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg, operator_run_id="op-2", lifecycle_run_id="lc-1")
    assert r3.verification_id != r1.verification_id


def test_persist_verification_is_idempotent(conn, cfg):
    result = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    first = cbv.persist_verification(conn, result, operator_run_id=None, lifecycle_run_id=None, created_at=DAY1)
    second = cbv.persist_verification(conn, result, operator_run_id=None, lifecycle_run_id=None, created_at=DAY1)
    assert first is True
    assert second is False
    stored = pb_repo.load_cross_book_verification(conn, result.verification_id)
    assert stored is not None
    assert stored["status"] == result.status
    assert len(stored["checks"]) == len(result.checks)


def test_never_fabricates_passed_from_zero_violations_and_insufficient_data(conn, cfg):
    """No books opened at all: zero violations trivially, but that must not
    become PASSED (Section 7's explicit requirement)."""
    result = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert result.violation_count == 0
    assert result.status == cbv.STATUS_INSUFFICIENT_DATA


def test_verified_state_change_generates_new_id_and_stale_signal(conn, cfg):
    _open_position(conn, cfg, book_id="BASELINE", arm="BASELINE")
    first = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    cbv.persist_verification(conn, first, operator_run_id=None, lifecycle_run_id=None, created_at=DAY1)
    cash_ledger.cash_adjustment(
        conn, "BASELINE", Decimal("1"), operator="auditor", reason="verified correction",
        idempotency_key="audit-adjustment", now=DAY1,
    )
    second = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert second.verification_id != first.verification_id
    assert cbv.verification_is_stale(conn, pb_repo.load_cross_book_verification(conn, first.verification_id), DAY1)


def test_future_rows_and_current_position_do_not_change_historical_verification(conn, cfg):
    _open_position(conn, cfg, book_id="BASELINE", arm="BASELINE")
    first = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    cbv.persist_verification(conn, first, operator_run_id=None, lifecycle_run_id=None, created_at=DAY1)
    future = DAY1.replace(day=6)
    cash_ledger.cash_adjustment(
        conn, "BASELINE", Decimal("1"), operator="auditor", reason="future correction",
        idempotency_key="future-adjustment", now=future,
    )
    conn.execute(
        "UPDATE paper_book_positions SET quantity='999', updated_at=? WHERE book_id='BASELINE' AND symbol='AAPL'",
        (future.isoformat(),),
    )
    conn.commit()
    rebuilt = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert rebuilt.verification_id == first.verification_id
    assert cbv.verification_is_stale(
        conn, pb_repo.load_cross_book_verification(conn, first.verification_id), future,
    ) is False


def test_failed_then_repaired_state_preserves_both_events(conn, cfg):
    _open_position(conn, cfg, book_id="BASELINE", arm="BASELINE")
    conn.execute("UPDATE paper_book_positions SET quantity = '999' WHERE book_id = 'BASELINE' AND symbol = 'AAPL'")
    conn.commit()
    failed = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert failed.status == cbv.STATUS_FAILED
    cbv.persist_verification(conn, failed, operator_run_id="op", lifecycle_run_id="lc", created_at=DAY1)
    lot_quantity = sum(Decimal(l["remaining_quantity"]) for l in pb_repo.list_all_lots(conn, "BASELINE", "AAPL"))
    conn.execute(
        "UPDATE paper_book_positions SET quantity = ? WHERE book_id = 'BASELINE' AND symbol = 'AAPL'",
        (str(lot_quantity),),
    )
    conn.commit()
    repaired = cbv.verify_cross_book_integrity(
        conn, as_of=DAY1, paper_books_config=cfg, operator_run_id="op", lifecycle_run_id="lc"
    )
    assert repaired.status == cbv.STATUS_PASSED
    cbv.persist_verification(conn, repaired, operator_run_id="op", lifecycle_run_id="lc", created_at=DAY1)
    assert pb_repo.load_cross_book_verification(conn, failed.verification_id)["status"] == cbv.STATUS_FAILED
    assert pb_repo.load_cross_book_verification(conn, repaired.verification_id)["status"] == cbv.STATUS_PASSED
    assert pb_repo.latest_cross_book_verification_upto(conn, DAY1.isoformat())["verification_id"] == repaired.verification_id


def test_unmatched_settlement_reference_fails(conn, cfg):
    _open_position(conn, cfg, book_id="BASELINE", arm="BASELINE")
    conn.execute(
        "INSERT INTO paper_book_cash_ledger (book_id, ledger_entry_id, event_type, amount_usd, event_timestamp, "
        "idempotency_key, reference_id, created_at) VALUES ('BASELINE', 'bad-settle', 'BUY_SETTLEMENT', '-1', ?, "
        "'bad-settle', 'missing-fill', ?)", (DAY1.isoformat(), DAY1.isoformat()),
    )
    conn.commit()
    result = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert "cash_ledger_foreign_reference" in {c.name for c in result.checks if c.status == cbv.CHECK_STATUS_FAILED}


def test_unexpected_book_namespace_fails(conn, cfg):
    conn.execute(
        "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
        "VALUES ('EXTRA', 'BASELINE', '1', ?, 'h')", (DAY1.isoformat(),),
    )
    conn.commit()
    result = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert "unexpected_book_namespaces" in {c.name for c in result.checks if c.status == cbv.CHECK_STATUS_FAILED}


def test_position_lot_quantity_inconsistency_fails(conn, cfg):
    _open_position(conn, cfg, book_id="BASELINE", arm="BASELINE")
    conn.execute("UPDATE paper_book_positions SET quantity = '2' WHERE book_id = 'BASELINE' AND symbol = 'AAPL'")
    conn.commit()
    result = cbv.verify_cross_book_integrity(conn, as_of=DAY1, paper_books_config=cfg)
    assert "position_lot_consistency" in {c.name for c in result.checks if c.status == cbv.CHECK_STATUS_FAILED}
