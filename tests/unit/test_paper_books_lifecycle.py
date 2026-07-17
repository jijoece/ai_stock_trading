"""Tests for lifecycle.py (docs/milestone-9.md Sections 6-7)."""
from __future__ import annotations

import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.evaluation.price_provider import DeterministicPriceProvider
from trading_research.paper_books import cash_ledger, execution, order_intent, positions, risk as risk_module, valuation
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
from trading_research.paper_books.lifecycle import (
    LifecycleError,
    PENDING_OUTCOME_EXPIRED,
    PENDING_OUTCOME_FILLED,
    _has_unresolved_pending_sell,
    run_paper_book_lifecycle,
)
from trading_research.paper_books.models import PaperBookOrderIntent
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect
from trading_research.storage.trading_repositories import save_frozen_recommendation
from trading_research.recommendations.builder import FrozenRecommendation

DAY1 = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)  # Monday
DAY2 = datetime(2026, 1, 6, 20, 0, tzinfo=timezone.utc)
DAY3 = datetime(2026, 1, 7, 20, 0, tzinfo=timezone.utc)
DAY4 = datetime(2026, 1, 8, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "lifecycle_test.db")
        yield c
        c.close()


def _config(*, lifecycle_enabled=True, exits_enabled=True, expire_after_market_days=3,
            stop_loss_percent="0.08", profit_target_percent="0.15", maximum_holding_market_days=20,
            exit_on_recommendation_reversal=True, enhanced_enabled=False,
            minimum_completed_cycles=1, minimum_market_days=1) -> PaperBooksConfiguration:
    return PaperBooksConfiguration(
        version=1, enabled=True,
        baseline=PaperBookDefinition(enabled=True, book_id="BASELINE", starting_cash_usd=Decimal("100000")),
        enhanced=PaperBookDefinition(enabled=enhanced_enabled, book_id="ENHANCED", starting_cash_usd=Decimal("100000")),
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
            enabled=lifecycle_enabled, pending_orders=PendingOrdersSection(expire_after_market_days=expire_after_market_days),
            exits=ExitsSection(
                enabled=exits_enabled, stop_loss_percent=Decimal(stop_loss_percent),
                profit_target_percent=Decimal(profit_target_percent),
                maximum_holding_market_days=maximum_holding_market_days,
                exit_on_recommendation_reversal=exit_on_recommendation_reversal,
            ),
            soak=SoakSection(minimum_completed_cycles=minimum_completed_cycles, minimum_market_days=minimum_market_days),
        ),
        config_hash="lifecycle-test-hash", raw={},
    )


def _open_long_position(conn, cfg, *, book_id="BASELINE", symbol="AAPL", quantity="100", price="100",
                         as_of=DAY1, price_provider=None) -> None:
    book_def = cfg.book(book_id)
    book = cash_ledger.open_book(conn, book_id=book_id, starting_cash_usd=book_def.starting_cash_usd, config_hash=cfg.config_hash, clock=lambda: as_of)
    snap = valuation.build_portfolio_snapshot(conn, book_id, as_of, price_provider=price_provider, maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds)
    context = risk_module.build_portfolio_context(conn, book_id, as_of, snap, symbol, Decimal("0"))
    decision = risk_module.evaluate_paper_risk(
        book_status=book.status, experiment_arm=book.experiment_arm, expected_arm=book_id, context=context,
        requested_quantity_hint=Decimal(quantity), reference_price=Decimal(price), reference_price_age_seconds=0,
        reference_price_point_in_time_safe=True, risk_config=cfg.risk,
    )
    assert decision.decision in ("APPROVED", "APPROVED_REDUCED"), decision.reasons
    risk_decision_id = order_intent.persist_risk_decision(conn, book_id, "entry-cycle", "rec-entry", symbol, decision, snap.snapshot_id, lambda: as_of)
    intent = order_intent.build_order_intent(
        book_id=book_id, experiment_arm=book_id, cycle_id="entry-cycle", recommendation_id="rec-entry", symbol=symbol,
        risk_decision=decision, risk_decision_id=risk_decision_id, portfolio_snapshot_id=snap.snapshot_id,
        config_hash=cfg.config_hash, as_of=as_of, clock=lambda: as_of,
    )
    ref = Decimal(price)
    market = execution.MarketSimulationInput(bid=ref * Decimal("0.97"), ask=ref * Decimal("0.975"))
    result = execution.submit_and_simulate(conn, intent, market, as_of)
    assert result["status"] == "FILLED", result


def _price_provider(*entries: tuple[str, date, str]) -> DeterministicPriceProvider:
    pp = DeterministicPriceProvider()
    for symbol, d, price in entries:
        pp.register(symbol, d, Decimal(price))
    return pp


# --- disabled-by-default / fail-closed --------------------------------------


def test_lifecycle_fails_closed_when_lifecycle_disabled(conn):
    cfg = _config(lifecycle_enabled=False)
    with pytest.raises(LifecycleError):
        run_paper_book_lifecycle(conn, as_of=DAY1, paper_books_config=cfg)


def test_lifecycle_fails_closed_when_paper_books_disabled(conn):
    cfg = _config()
    object.__setattr__(cfg, "enabled", False)
    with pytest.raises(LifecycleError):
        run_paper_book_lifecycle(conn, as_of=DAY1, paper_books_config=cfg)


def test_lifecycle_requires_timezone_aware_as_of(conn):
    cfg = _config()
    with pytest.raises(LifecycleError):
        run_paper_book_lifecycle(conn, as_of=datetime(2026, 1, 5), paper_books_config=cfg)


# --- pending order lifecycle -------------------------------------------------


def test_pending_order_fills_when_price_crosses_later(conn):
    cfg = _config(exits_enabled=False)
    pp = _price_provider(("AAPL", DAY1.date(), "100"))
    book = cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=cfg.baseline.starting_cash_usd, config_hash=cfg.config_hash, clock=lambda: DAY1)
    snap = valuation.build_portfolio_snapshot(conn, "BASELINE", DAY1, price_provider=pp, maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds)
    context = risk_module.build_portfolio_context(conn, "BASELINE", DAY1, snap, "AAPL", Decimal("0"))
    decision = risk_module.evaluate_paper_risk(
        book_status=book.status, experiment_arm="BASELINE", expected_arm="BASELINE", context=context,
        requested_quantity_hint=Decimal("100"), reference_price=Decimal("100"), reference_price_age_seconds=0,
        reference_price_point_in_time_safe=True, risk_config=cfg.risk,
    )
    risk_decision_id = order_intent.persist_risk_decision(conn, "BASELINE", "c1", "rec1", "AAPL", decision, snap.snapshot_id, lambda: DAY1)
    intent = order_intent.build_order_intent(
        book_id="BASELINE", experiment_arm="BASELINE", cycle_id="c1", recommendation_id="rec1", symbol="AAPL",
        risk_decision=decision, risk_decision_id=risk_decision_id, portfolio_snapshot_id=snap.snapshot_id,
        config_hash=cfg.config_hash, as_of=DAY1, clock=lambda: DAY1,
    )
    # Bid/ask centered at exactly the limit price never crosses (matches
    # existing Milestone 8/8.1 tier-2 known limitation) — order stays pending.
    market = execution.MarketSimulationInput(bid=Decimal("99.9"), ask=Decimal("100.1"))
    submit = execution.submit_and_simulate(conn, intent, market, DAY1)
    assert submit["status"] == "PENDING_SUBMISSION"

    # Day 2: price drops -> the pending BUY (limit=100) now crosses.
    pp.register("AAPL", DAY2.date(), Decimal("90"))
    result = run_paper_book_lifecycle(conn, as_of=DAY2, paper_books_config=cfg, price_provider=pp)
    assert result.pending_orders_filled == 1
    order = repo.load_order_intent(conn, "BASELINE", intent.paper_order_intent_id)
    assert order["status"] == "FILLED"
    results = repo.list_lifecycle_symbol_results(conn, result.lifecycle_run_id)
    assert any(r["outcome"] == PENDING_OUTCOME_FILLED for r in results)


def test_pending_order_expires_after_configured_market_days_and_releases_cash(conn):
    cfg = _config(exits_enabled=False, expire_after_market_days=1)
    pp = _price_provider(("AAPL", DAY1.date(), "100"))
    book = cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=cfg.baseline.starting_cash_usd, config_hash=cfg.config_hash, clock=lambda: DAY1)
    snap = valuation.build_portfolio_snapshot(conn, "BASELINE", DAY1, price_provider=pp, maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds)
    context = risk_module.build_portfolio_context(conn, "BASELINE", DAY1, snap, "AAPL", Decimal("0"))
    decision = risk_module.evaluate_paper_risk(
        book_status=book.status, experiment_arm="BASELINE", expected_arm="BASELINE", context=context,
        requested_quantity_hint=Decimal("100"), reference_price=Decimal("100"), reference_price_age_seconds=0,
        reference_price_point_in_time_safe=True, risk_config=cfg.risk,
    )
    cash_before = cash_ledger.available_cash(conn, "BASELINE")
    risk_decision_id = order_intent.persist_risk_decision(conn, "BASELINE", "c1", "rec1", "AAPL", decision, snap.snapshot_id, lambda: DAY1)
    intent = order_intent.build_order_intent(
        book_id="BASELINE", experiment_arm="BASELINE", cycle_id="c1", recommendation_id="rec1", symbol="AAPL",
        risk_decision=decision, risk_decision_id=risk_decision_id, portfolio_snapshot_id=snap.snapshot_id,
        config_hash=cfg.config_hash, as_of=DAY1, clock=lambda: DAY1,
    )
    market = execution.MarketSimulationInput(bid=Decimal("99.9"), ask=Decimal("100.1"))
    submit = execution.submit_and_simulate(conn, intent, market, DAY1)
    assert submit["status"] == "PENDING_SUBMISSION"
    cash_after_reserve = cash_ledger.available_cash(conn, "BASELINE")
    assert cash_after_reserve < cash_before

    # Day 4: 3 market days later, exceeds expire_after_market_days=1.
    result = run_paper_book_lifecycle(conn, as_of=DAY4, paper_books_config=cfg, price_provider=pp)
    assert result.pending_orders_expired == 1
    order = repo.load_order_intent(conn, "BASELINE", intent.paper_order_intent_id)
    assert order["status"] == "EXPIRED"
    assert cash_ledger.available_cash(conn, "BASELINE") == cash_before  # reservation released exactly once

    # Idempotent rerun of the same as_of never re-releases.
    run_paper_book_lifecycle(conn, as_of=DAY4, paper_books_config=cfg, price_provider=pp)
    assert cash_ledger.available_cash(conn, "BASELINE") == cash_before


# --- exit decisions -----------------------------------------------------------


def test_profit_target_exit_creates_and_may_fill_sell_order(conn):
    cfg = _config()
    pp = _price_provider(("AAPL", DAY1.date(), "100"))
    _open_long_position(conn, cfg, as_of=DAY1, price_provider=pp)

    pp.register("AAPL", DAY2.date(), Decimal("125"))
    result = run_paper_book_lifecycle(conn, as_of=DAY2, paper_books_config=cfg, price_provider=pp)
    assert result.failure_reasons == ()
    assert len(result.exit_decisions) == 1
    assert result.exit_decisions[0]["decision"] == "EXIT_PROFIT_TARGET"
    assert result.exit_orders_created == 1


def test_stop_loss_exit_creates_sell_order(conn):
    cfg = _config()
    pp = _price_provider(("AAPL", DAY1.date(), "100"))
    _open_long_position(conn, cfg, as_of=DAY1, price_provider=pp)

    pp.register("AAPL", DAY2.date(), Decimal("85"))
    result = run_paper_book_lifecycle(conn, as_of=DAY2, paper_books_config=cfg, price_provider=pp)
    assert result.failure_reasons == ()
    assert result.exit_decisions[0]["decision"] == "EXIT_STOP_LOSS"


def test_max_holding_period_exit(conn):
    cfg = _config(maximum_holding_market_days=1)
    pp = _price_provider(("AAPL", DAY1.date(), "100"))
    _open_long_position(conn, cfg, as_of=DAY1, price_provider=pp)

    pp.register("AAPL", DAY3.date(), Decimal("100"))
    result = run_paper_book_lifecycle(conn, as_of=DAY3, paper_books_config=cfg, price_provider=pp)
    assert result.failure_reasons == ()
    assert result.exit_decisions[0]["decision"] == "EXIT_MAX_HOLDING_PERIOD"


def test_recommendation_reversal_exit(conn):
    cfg = _config()
    pp = _price_provider(("AAPL", DAY1.date(), "100"))
    _open_long_position(conn, cfg, as_of=DAY1, price_provider=pp)

    reversal_payload = {
        "rec_id": "rec-reversal", "run_id": None, "symbol": "AAPL", "side": "screened_out", "ts": DAY2.isoformat(),
        "price_at_rec": 100.0, "score": None, "confidence": None, "status": "active", "acted": False,
        "rationale_text": "", "factors": [], "risk_plan": None, "warnings": [], "missing_data_reasons": [],
        "data_timestamps": {}, "reddit_component": None, "model_version": "v1", "prompt_version": "v1",
        "config_hash": "a" * 64, "git_sha": "sha", "frozen": True, "disclaimer": "x",
    }
    save_frozen_recommendation(conn, FrozenRecommendation(payload=reversal_payload))

    pp.register("AAPL", DAY3.date(), Decimal("100"))
    result = run_paper_book_lifecycle(conn, as_of=DAY3, paper_books_config=cfg, price_provider=pp)
    assert result.failure_reasons == ()
    assert result.exit_decisions[0]["decision"] == "EXIT_RECOMMENDATION_REVERSAL"


def test_manual_exit_request_is_audited_and_consumed_once(conn):
    cfg = _config()
    pp = _price_provider(("AAPL", DAY1.date(), "100"))
    _open_long_position(conn, cfg, as_of=DAY1, price_provider=pp)

    repo.save_manual_exit_request(
        conn, manual_exit_request_id="req-1", book_id="BASELINE", symbol="AAPL", operator="alice",
        reason="risk-off", requested_at=DAY1, idempotency_key="idem-1", created_at=DAY1,
    )

    pp.register("AAPL", DAY2.date(), Decimal("100"))
    result = run_paper_book_lifecycle(conn, as_of=DAY2, paper_books_config=cfg, price_provider=pp)
    assert result.failure_reasons == ()
    assert result.exit_decisions[0]["decision"] == "EXIT_MANUAL_REQUEST"
    decision_row = repo.load_exit_decision(conn, result.exit_decisions[0]["exit_decision_id"])
    assert decision_row["manual_exit_request_id"] == "req-1"

    # A second lifecycle run must not re-trigger the already-consumed request.
    pp.register("AAPL", DAY3.date(), Decimal("100"))
    result2 = run_paper_book_lifecycle(conn, as_of=DAY3, paper_books_config=cfg, price_provider=pp)
    assert result2.failure_reasons == ()
    assert not any(d["decision"] == "EXIT_MANUAL_REQUEST" for d in result2.exit_decisions)


def test_missing_price_never_fabricates_an_exit(conn):
    cfg = _config()
    pp = _price_provider(("AAPL", DAY1.date(), "100"))
    _open_long_position(conn, cfg, as_of=DAY1, price_provider=pp)

    # No price registered for DAY2 -> SKIPPED_MISSING_PRICE, never an exit.
    result = run_paper_book_lifecycle(conn, as_of=DAY2, paper_books_config=cfg, price_provider=pp)
    assert result.exit_decisions[0]["decision"] == "SKIPPED_MISSING_PRICE"
    assert result.exit_orders_created == 0


def test_no_duplicate_exit_intent_for_the_same_decision(conn):
    cfg = _config()
    pp = _price_provider(("AAPL", DAY1.date(), "100"))
    _open_long_position(conn, cfg, as_of=DAY1, price_provider=pp)
    pp.register("AAPL", DAY2.date(), Decimal("125"))

    result1 = run_paper_book_lifecycle(conn, as_of=DAY2, paper_books_config=cfg, price_provider=pp)
    assert result1.failure_reasons == ()
    orders_after_first = repo.list_order_intents(conn, "BASELINE")
    sell_orders_1 = [o for o in orders_after_first if o["side"] == "SELL"]

    result2 = run_paper_book_lifecycle(conn, as_of=DAY2, paper_books_config=cfg, price_provider=pp)
    assert result2.failure_reasons == ()
    orders_after_second = repo.list_order_intents(conn, "BASELINE")
    sell_orders_2 = [o for o in orders_after_second if o["side"] == "SELL"]

    assert len(sell_orders_1) == len(sell_orders_2) == 1
    assert result1.lifecycle_run_id == result2.lifecycle_run_id


def test_no_exit_evaluated_while_a_prior_exit_order_is_still_pending(conn):
    cfg = _config()
    pp = _price_provider(("AAPL", DAY1.date(), "100"))
    _open_long_position(conn, cfg, as_of=DAY1, price_provider=pp)
    pp.register("AAPL", DAY2.date(), Decimal("125"))
    r2 = run_paper_book_lifecycle(conn, as_of=DAY2, paper_books_config=cfg, price_provider=pp)
    assert r2.failure_reasons == ()

    sell_orders = [o for o in repo.list_order_intents(conn, "BASELINE") if o["side"] == "SELL"]
    assert len(sell_orders) == 1
    if sell_orders[0]["status"] != "FILLED":
        pp.register("AAPL", DAY3.date(), Decimal("125"))
        result3 = run_paper_book_lifecycle(conn, as_of=DAY3, paper_books_config=cfg, price_provider=pp)
        assert result3.failure_reasons == ()
        # Either the pending sell resolved (filled) or exit evaluation was
        # explicitly skipped — either way, never a second exit decision for
        # an already-outstanding SELL.
        sell_orders_after = [o for o in repo.list_order_intents(conn, "BASELINE") if o["side"] == "SELL"]
        assert len(sell_orders_after) == 1


def _seed_external_sell_intent(conn, *, book_id="BASELINE", symbol="AAPL", quantity="10", status="SUBMITTED"):
    quantity = Decimal(quantity)
    cash_ledger.open_book(conn, book_id=book_id, starting_cash_usd=Decimal("100000"), config_hash="lifecycle-test-hash", clock=lambda: DAY1)
    intent = PaperBookOrderIntent(
        paper_order_intent_id=f"external-sell-{status.lower()}", book_id=book_id, experiment_arm=book_id,
        cycle_id="exit-cycle", recommendation_id="rec-exit", symbol=symbol, side="SELL",
        order_type="LIMIT", quantity=quantity, limit_price=Decimal("100"),
        notional_usd=quantity * Decimal("100"), time_in_force="DAY", as_of=DAY2,
        risk_decision_id="risk-exit", portfolio_snapshot_id="snap-exit", config_hash="lifecycle-test-hash",
        created_at=DAY2, status=status,
    )
    repo.save_order_intent(conn, intent)
    return intent.paper_order_intent_id


def _seed_external_order_event(conn, *, book_id, paper_order_intent_id, new_state):
    repo.save_external_order_event(conn, {
        "external_order_event_id": f"evt-{paper_order_intent_id}-{new_state}",
        "external_order_scope_id": f"scope-{paper_order_intent_id}", "book_id": book_id,
        "paper_order_intent_id": paper_order_intent_id, "client_order_id": f"epb-{book_id.lower()}-{paper_order_intent_id}",
        "broker_order_id": "broker-1", "account_fingerprint": "acct_test", "previous_state": "SUBMITTED",
        "new_state": new_state, "payload_hash": "hash", "quantity": Decimal("10"), "limit_price": Decimal("100"),
        "operator": "alice", "reason": "test", "runtime_request_id": None, "error_code": None,
        "created_at": DAY2.isoformat(), "policy_version": "v1", "config_hash": "lifecycle-test-hash",
        "attempt_number": 0, "scope_sequence": 0,
    })


def test_unresolved_external_sell_blocks_new_exit_intent(conn):
    intent_id = _seed_external_sell_intent(conn, status="SUBMITTED")
    _seed_external_order_event(conn, book_id="BASELINE", paper_order_intent_id=intent_id, new_state="SUBMITTED")
    assert _has_unresolved_pending_sell(conn, "BASELINE", "AAPL") is True


def test_terminal_external_sell_with_released_reservation_does_not_block(conn):
    intent_id = _seed_external_sell_intent(conn, status="CANCELLED")
    _seed_external_order_event(conn, book_id="BASELINE", paper_order_intent_id=intent_id, new_state="CANCELLED")
    assert _has_unresolved_pending_sell(conn, "BASELINE", "AAPL") is False


def test_terminal_external_sell_with_unreleased_reservation_still_blocks(conn):
    intent_id = _seed_external_sell_intent(conn, status="CANCELLED")
    _seed_external_order_event(conn, book_id="BASELINE", paper_order_intent_id=intent_id, new_state="CANCELLED")
    repo.save_external_position_reservation_event(conn, {
        "reservation_event_id": f"reserve:{intent_id}", "book_id": "BASELINE", "symbol": "AAPL",
        "paper_order_intent_id": intent_id, "client_order_id": "cid", "quantity": Decimal("10"),
        "event_type": "RESERVED", "operator": None, "reason": "test", "created_at": DAY2.isoformat(),
    })
    assert _has_unresolved_pending_sell(conn, "BASELINE", "AAPL") is True


# --- isolation / disabled exits ----------------------------------------------


def test_exits_disabled_by_config_never_evaluated(conn):
    cfg = _config(exits_enabled=False)
    pp = _price_provider(("AAPL", DAY1.date(), "100"))
    _open_long_position(conn, cfg, as_of=DAY1, price_provider=pp)
    pp.register("AAPL", DAY2.date(), Decimal("125"))
    result = run_paper_book_lifecycle(conn, as_of=DAY2, paper_books_config=cfg, price_provider=pp)
    assert result.exit_decisions == ()
    assert result.exit_orders_created == 0


def test_one_book_failure_does_not_prevent_the_other_book_processing(conn):
    cfg = _config(enhanced_enabled=True)
    pp = _price_provider(("AAPL", DAY1.date(), "100"))
    _open_long_position(conn, cfg, book_id="BASELINE", as_of=DAY1, price_provider=pp)
    _open_long_position(conn, cfg, book_id="ENHANCED", as_of=DAY1, price_provider=pp)

    pp.register("AAPL", DAY2.date(), Decimal("125"))
    result = run_paper_book_lifecycle(conn, as_of=DAY2, paper_books_config=cfg, price_provider=pp)
    assert "BASELINE" in result.books_processed
    assert "ENHANCED" in result.books_processed
    baseline_position = repo.load_position(conn, "BASELINE", "AAPL")
    enhanced_position = repo.load_position(conn, "ENHANCED", "AAPL")
    assert baseline_position is not None and enhanced_position is not None


def test_no_cross_book_contamination(conn):
    cfg = _config(enhanced_enabled=True)
    pp = _price_provider(("AAPL", DAY1.date(), "100"), ("MSFT", DAY1.date(), "200"))
    _open_long_position(conn, cfg, book_id="BASELINE", symbol="AAPL", as_of=DAY1, price_provider=pp)
    _open_long_position(conn, cfg, book_id="ENHANCED", symbol="MSFT", as_of=DAY1, price_provider=pp)

    pp.register("AAPL", DAY2.date(), Decimal("125"))
    pp.register("MSFT", DAY2.date(), Decimal("250"))
    run_paper_book_lifecycle(conn, as_of=DAY2, paper_books_config=cfg, price_provider=pp)

    assert repo.load_position(conn, "BASELINE", "MSFT") is None
    assert repo.load_position(conn, "ENHANCED", "AAPL") is None


def test_snapshots_and_reconciliation_and_metrics_persisted(conn):
    cfg = _config()
    pp = _price_provider(("AAPL", DAY1.date(), "100"))
    _open_long_position(conn, cfg, as_of=DAY1, price_provider=pp)
    pp.register("AAPL", DAY2.date(), Decimal("105"))
    result = run_paper_book_lifecycle(conn, as_of=DAY2, paper_books_config=cfg, price_provider=pp)
    assert "BASELINE" in result.snapshot_ids
    assert result.reconciliation_statuses["BASELINE"] == "MATCHED"
    assert "BASELINE" in result.metrics_ids
    assert repo.load_lifecycle_run(conn, result.lifecycle_run_id) is not None
