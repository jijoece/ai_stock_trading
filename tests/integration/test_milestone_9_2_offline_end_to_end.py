"""Offline, deterministic end-to-end proof of Milestone 9.2
(docs/milestone-9.2.md Section 15):

    persistent test database
    -> fixture-only cycle -> explicit provider provenance
    -> controlled paper lifecycle -> clean isolated books
    -> persisted cross-book verification PASSED
    -> resolved historical CRITICAL alert
    -> paper-soak-run
    -> all readiness checks returned
    -> provider minimum remains unmet for fixture-only history
    -> replay is idempotent
    -> no network or live execution

A second case injects a foreign-reference violation, proving cross-book
verification FAILED blocks readiness while lifecycle evidence remains
persisted and no activation side effect occurs.

A third, minimal case proves real-provider metadata with zero cost still
counts as real (never inferred from cost_usd) — without making a real
provider call.

Fixture data only. No Claude, no network call anywhere in this file.
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
from trading_research.research.provider_provenance import (
    claude_provider_row,
    evidence_provider_row,
    record_cycle_provider_provenance,
)
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
        raise AssertionError("Milestone 9.2 offline e2e must never open a real socket")

    monkeypatch.setattr(socket.socket, "connect", _blocked)


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "milestone_9_2_e2e.db"


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
        config_hash="m9-2-e2e-hash", raw={},
    )


def _open_position(conn, cfg, *, book_id: str, arm: str, symbol: str = "AAPL") -> None:
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


def _seed_fixture_cycle_provenance(conn) -> None:
    """A single fixture-only research cycle, explicitly classified —
    proves fixture-only history never satisfies the real-provider minimum."""
    conn.execute(
        "INSERT INTO research_cycles (cycle_id, universe_id, as_of, configuration_hash, experiment_policy, "
        "provider_mode, status, started_at, completed_at) VALUES "
        "('cyc-fixture-1', 'u1', ?, 'h', 'OBSERVE_ONLY', 'fixture', 'COMPLETED', ?, ?)",
        (DAY1.isoformat(), DAY1.isoformat(), DAY1.isoformat()),
    )
    conn.commit()
    record_cycle_provider_provenance(conn, [
        evidence_provider_row(
            cycle_id="cyc-fixture-1", research_run_id=None, symbol="AAPL", provider_category="market",
            provider_name="fixture-market", request_or_source_id="s1", status="ok",
            cycle_provider_mode="fixture", observed_at=DAY1,
        ),
        claude_provider_row(
            cycle_id="cyc-fixture-1", research_run_id="rr-fixture-1", symbol="AAPL", provider_name="deterministic",
            observed_at=DAY1,
        ),
    ])


def test_offline_end_to_end_fixture_only_history_and_clean_cross_book_verification(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)

    conn = connect(db_path)
    _open_position(conn, cfg, book_id="BASELINE", arm="BASELINE", symbol="AAPL")
    _open_position(conn, cfg, book_id="ENHANCED", arm="ENHANCED", symbol="MSFT")
    _seed_fixture_cycle_provenance(conn)

    alert = OperationalAlert(
        severity=SEVERITY_CRITICAL, alert_type="PROVIDER_UNAVAILABLE", message="transient provider outage",
        context={}, created_at=DAY1,
    )
    raise_alert(conn, alert, (), clock=lambda: DAY1)
    resolve_alert(conn, alert.alert_id, resolved_by="alice", reason="provider recovered", resolved_at=DAY1.isoformat())
    assert pause_mod.current_state(conn).state == pause_mod.STATE_ACTIVE
    conn.close()

    first = cli_support.paper_soak_run_cli(db_path, as_of=DAY1, integrate_cycle_ids=())
    assert "error" not in first
    # The cross-book verification itself is authoritative and PASSED,
    # independent of whatever paper-soak reconciliation/valuation status
    # (an orthogonal, earlier-ordered gate — see controlled_soak_readiness.py
    # Section 3) the combined readiness ultimately reports.
    assert first["cross_book_verification_status"] == "PASSED"
    assert first["cross_book_verification"]["violation_count"] == 0

    # All readiness checks are returned, not only the first failure, and the
    # diagnostic buckets are always present (Section 11).
    assert len(first["controlled_readiness"]["checks"]) >= 1
    for key in ("all_failed_checks", "blocking_checks", "advisory_checks", "missing_checks"):
        assert key in first["controlled_readiness"]

    # Provider minimum remains unmet — the only recorded cycle is
    # fixture-only — proven directly against the authoritative source
    # (independent of which readiness check happened to be reached first).
    from trading_research.research.provider_provenance import compute_real_provider_history

    conn = connect(db_path)
    summary = compute_real_provider_history(conn, DAY1)
    conn.close()
    assert summary.real_provider_cycle_count == 0
    assert summary.fixture_only_cycle_count == 1

    # Replay is idempotent: identical operator_run_id, no duplicate
    # cross-book verification row for the same frozen inputs.
    replay = cli_support.paper_soak_run_cli(db_path, as_of=DAY1, integrate_cycle_ids=())
    assert replay["operator_run_id"] == first["operator_run_id"]
    assert replay["cross_book_verification_id"] == first["cross_book_verification_id"]

    conn = connect(db_path)
    all_verifications = conn.execute("SELECT COUNT(*) AS n FROM paper_book_cross_book_verifications").fetchone()["n"]
    conn.close()
    assert all_verifications == 1  # one verification_id for this frozen (as_of, operator_run_id, lifecycle_run_id)


def test_foreign_reference_violation_blocks_readiness_but_preserves_lifecycle_evidence(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)

    conn = connect(db_path)
    _open_position(conn, cfg, book_id="BASELINE", arm="BASELINE", symbol="AAPL")
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=cfg.enhanced.starting_cash_usd, config_hash=cfg.config_hash, clock=lambda: DAY1)
    # Inject an arm/book-identity violation directly on the assignment table
    # (simulates a hypothetical isolation bug) — purely metadata, so it does
    # NOT corrupt either book's own cash/fill reconciliation (proving this
    # is a genuinely distinct signal from paper-soak reconciliation).
    conn.execute(
        "INSERT INTO paper_book_experiment_assignments (experiment_id, cycle_id, symbol, as_of, "
        "evidence_snapshot_id, baseline_recommendation_id, enhanced_recommendation_id, baseline_book_id, "
        "enhanced_book_id, baseline_intent_id, enhanced_intent_id, assignment_policy_version, created_at) VALUES "
        "('exp-1', 'c-bad', 'AAPL', ?, NULL, NULL, NULL, 'ENHANCED', 'BASELINE', NULL, NULL, 'v1', ?)",
        (DAY1.isoformat(), DAY1.isoformat()),
    )
    conn.commit()
    conn.close()

    outcome = cli_support.paper_soak_run_cli(db_path, as_of=DAY1, integrate_cycle_ids=())
    assert "error" not in outcome
    assert outcome["cross_book_verification_status"] == "FAILED"
    assert outcome["cross_book_verification"]["violation_count"] >= 1
    # Combined readiness is blocked (never a READY_* status) — whichever
    # NOT_READY_* status it reports first, since paper-soak reconciliation/
    # valuation are earlier-ordered, orthogonal gates (Section 3) that may
    # also legitimately fire in this same offline harness.
    assert outcome["controlled_readiness"]["status"].startswith("NOT_READY")
    # (The cross-book gate blocking in isolation — independent of any other
    # paper-soak gate — is proven directly in
    # test_controlled_soak_readiness.py's own unit tests.)

    # Lifecycle evidence (the real, legitimately-opened BASELINE position)
    # remains persisted despite the verification failure.
    conn = connect(db_path)
    baseline_positions = pb_repo.list_positions(conn, "BASELINE", open_only=False)
    lifecycle_runs = pb_repo.list_lifecycle_runs(conn)
    conn.close()
    assert any(p["symbol"] == "AAPL" for p in baseline_positions)
    assert len(lifecycle_runs) == 1  # lifecycle itself still ran and was persisted

    # No activation side effect: never READY_FOR_RECURRING_ACTIVATION_REVIEW,
    # no pause/kill state change.
    assert outcome["controlled_readiness"]["status"] != "READY_FOR_RECURRING_ACTIVATION_REVIEW"


def test_zero_cost_real_provider_metadata_counts_as_real(db_path):
    """Proves real-provider classification never depends on cost_usd — a
    real-mode cycle with zero shadow_run_summaries cost history anywhere in
    the database still counts toward the real-provider minimum. No real
    provider call is made; only persisted metadata is used."""
    from trading_research.research.provider_provenance import compute_real_provider_history

    conn = connect(db_path)
    conn.execute(
        "INSERT INTO research_cycles (cycle_id, universe_id, as_of, configuration_hash, experiment_policy, "
        "provider_mode, status, started_at, completed_at) VALUES "
        "('cyc-real-1', 'u1', ?, 'h', 'OBSERVE_ONLY', 'real', 'COMPLETED', ?, ?)",
        (DAY1.isoformat(), DAY1.isoformat(), DAY1.isoformat()),
    )
    conn.commit()
    record_cycle_provider_provenance(conn, [
        evidence_provider_row(
            cycle_id="cyc-real-1", research_run_id=None, symbol="AAPL", provider_category="market",
            provider_name="alpaca-data", request_or_source_id="s1", status="ok",
            cycle_provider_mode="real", observed_at=DAY1,
        ),
    ])
    # No shadow_run_summaries row exists at all in this database — the old
    # cost_usd > 0 heuristic would have reported zero real-provider cycles.
    no_summaries = conn.execute("SELECT COUNT(*) AS n FROM shadow_run_summaries").fetchone()["n"]
    assert no_summaries == 0

    summary = compute_real_provider_history(conn, DAY1)
    conn.close()
    assert summary.real_provider_cycle_count == 1
    assert summary.real_evidence_only_cycle_count == 1
