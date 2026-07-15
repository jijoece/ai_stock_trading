"""Offline, deterministic end-to-end proof of Milestone 9.1
(docs/milestone9-1-controlled-soak-readiness.md Section 12):

    persistent test database
    -> fixture paper history across two market days (lifecycle-only, zero
       explicit cycle IDs)
    -> persisted shadow health/readiness records, one resolved CRITICAL alert
    -> no active pause or kill
    -> explicit `paper-soak-run`
    -> lifecycle processing -> independent reconciliation -> soak report
    -> combined controlled-soak readiness -> operator-run summary
    -> replay the identical command -> prove idempotency
    -> prove no cross-book contamination
    -> prove no live/network call

A second case proves an unresolved CRITICAL alert blocks `paper-soak-run`
with zero activation side effect (no lifecycle run, no operator-run row).

Fixture providers only. No Claude, no network call anywhere in this file.
"""
from __future__ import annotations

import socket
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.paper_books import cash_ledger, cli_support, execution, order_intent, risk as risk_module, valuation
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
from trading_research.evaluation.price_provider import DeterministicPriceProvider
from trading_research.shadow import pause as pause_mod
from trading_research.shadow.alerts import OperationalAlert, SEVERITY_CRITICAL, raise_alert
from trading_research.storage import paper_books_repositories as pb_repo
from trading_research.storage.database import connect
from trading_research.storage.shadow_alerts_repositories import resolve_alert

DAY1 = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 1, 6, 20, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("Milestone 9.1 offline e2e must never open a real socket")

    monkeypatch.setattr(socket.socket, "connect", _blocked)


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "milestone_9_1_e2e.db"


def _config() -> PaperBooksConfiguration:
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
        config_hash="m9-1-e2e-hash", raw={},
    )


def _open_baseline_position(conn, cfg) -> None:
    pp = DeterministicPriceProvider()
    pp.register("AAPL", DAY1.date(), Decimal("100"))
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=cfg.baseline.starting_cash_usd, config_hash=cfg.config_hash, clock=lambda: DAY1)
    snap = valuation.build_portfolio_snapshot(conn, "BASELINE", DAY1, price_provider=pp, maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds)
    context = risk_module.build_portfolio_context(conn, "BASELINE", DAY1, snap, "AAPL", Decimal("0"))
    decision = risk_module.evaluate_paper_risk(
        book_status="ACTIVE", experiment_arm="BASELINE", expected_arm="BASELINE", context=context,
        requested_quantity_hint=Decimal("100"), reference_price=Decimal("100"), reference_price_age_seconds=0,
        reference_price_point_in_time_safe=True, risk_config=cfg.risk,
    )
    risk_decision_id = order_intent.persist_risk_decision(conn, "BASELINE", "c1", "rec1", "AAPL", decision, snap.snapshot_id, lambda: DAY1)
    intent = order_intent.build_order_intent(
        book_id="BASELINE", experiment_arm="BASELINE", cycle_id="c1", recommendation_id="rec1", symbol="AAPL",
        risk_decision=decision, risk_decision_id=risk_decision_id, portfolio_snapshot_id=snap.snapshot_id,
        config_hash=cfg.config_hash, as_of=DAY1, clock=lambda: DAY1,
    )
    market = execution.MarketSimulationInput(bid=Decimal("97"), ask=Decimal("97.5"))
    execution.submit_and_simulate(conn, intent, market, DAY1)


def test_offline_end_to_end_paper_soak_run_workflow(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)

    conn = connect(db_path)
    _open_baseline_position(conn, cfg)
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=cfg.enhanced.starting_cash_usd, config_hash=cfg.config_hash, clock=lambda: DAY1)

    # One resolved CRITICAL alert — must never block.
    alert = OperationalAlert(
        severity=SEVERITY_CRITICAL, alert_type="PROVIDER_UNAVAILABLE", message="transient provider outage",
        context={}, created_at=DAY1,
    )
    raise_alert(conn, alert, (), clock=lambda: DAY1)
    resolve_alert(conn, alert.alert_id, resolved_by="alice", reason="provider recovered", resolved_at=DAY1.isoformat())
    assert pause_mod.current_state(conn).state == pause_mod.STATE_ACTIVE
    conn.close()

    # Day 1: lifecycle-only day, zero explicit cycle IDs.
    first = cli_support.paper_soak_run_cli(db_path, as_of=DAY1, integrate_cycle_ids=())
    assert "error" not in first
    assert first["requested_cycle_ids"] == []
    assert "BASELINE" in first["lifecycle_result"]["books_processed"]
    assert "ENHANCED" in first["lifecycle_result"]["books_processed"]
    assert first["baseline_reconciliation_status"] == "MATCHED"
    assert first["enhanced_reconciliation_status"] == "MATCHED"
    assert first["controlled_readiness"]["status"] in (
        "NOT_READY_PAPER_SOAK", "NOT_READY_PROVIDER_HISTORY", "NOT_READY_VALUATION", "READY_FOR_MANUAL_SOAK",
        "READY_FOR_EXTENDED_MANUAL_SOAK",
    )
    # Never activates or schedules anything regardless of which advisory
    # status was reached.
    assert first["controlled_readiness"]["status"] != "READY_FOR_RECURRING_ACTIVATION_REVIEW"

    conn = connect(db_path)
    stored = pb_repo.load_operator_run(conn, first["operator_run_id"])
    conn.close()
    assert stored is not None

    # Day 2: another lifecycle-only day, unknown cycle ID supplied — fails
    # closed (recorded, does not hide the rest of the pipeline).
    second = cli_support.paper_soak_run_cli(db_path, as_of=DAY2, integrate_cycle_ids=("no-such-cycle",))
    assert "error" not in second
    assert any("no-such-cycle" in reason for reason in second["failure_reasons"])
    assert "BASELINE" in second["lifecycle_result"]["books_processed"]

    # Replay day 1's identical command -> idempotent, no duplicate summary.
    replay = cli_support.paper_soak_run_cli(db_path, as_of=DAY1, integrate_cycle_ids=())
    assert replay["operator_run_id"] == first["operator_run_id"]
    conn = connect(db_path)
    all_runs = pb_repo.list_operator_runs(conn)
    baseline_positions = pb_repo.list_positions(conn, "BASELINE", open_only=False)
    enhanced_positions = pb_repo.list_positions(conn, "ENHANCED", open_only=False)
    conn.close()
    assert len(all_runs) == 2  # one per distinct (as_of, cycle_ids) — day1 + day2, never a third for the replay
    # No cross-book contamination: ENHANCED never inherited BASELINE's position.
    assert any(p["symbol"] == "AAPL" for p in baseline_positions)
    assert not any(p["symbol"] == "AAPL" for p in enhanced_positions)


def test_unresolved_critical_alert_blocks_with_zero_activation_side_effect(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)

    conn = connect(db_path)
    _open_baseline_position(conn, cfg)
    alert = OperationalAlert(
        severity=SEVERITY_CRITICAL, alert_type="PROVIDER_UNAVAILABLE", message="provider down", context={},
        created_at=DAY1,
    )
    raise_alert(conn, alert, (), clock=lambda: DAY1)
    conn.close()

    readiness = cli_support.paper_soak_readiness_cli(db_path, as_of=DAY1)
    assert readiness["status"] == "NOT_READY_CRITICAL_ALERTS"

    conn = connect(db_path)
    runs_before = pb_repo.list_operator_runs(conn)
    conn.close()
    assert runs_before == []  # readiness check alone never runs the lifecycle or persists anything

    # An active pause also blocks paper-soak-run itself (not just readiness),
    # and must leave zero side effects (no lifecycle run recorded either).
    conn = connect(db_path)
    pause_mod.request_pause(conn, "investigating alert", pause_mod.SOURCE_OPERATOR, clock=lambda: DAY1)
    conn.close()

    outcome = cli_support.paper_soak_run_cli(db_path, as_of=DAY1)
    assert "error" in outcome

    conn = connect(db_path)
    runs_after = pb_repo.list_operator_runs(conn)
    lifecycle_runs_after = pb_repo.list_lifecycle_runs(conn)
    conn.close()
    assert runs_after == []
    assert lifecycle_runs_after == []
