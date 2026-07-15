"""Tests for the `paper-book-lifecycle-run` / `paper-book-exit-request` /
`paper-book-soak-report` / `paper-book-soak-readiness` CLI support functions
(docs/milestone-9.md Section 11)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
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
from trading_research.storage.database import connect

DAY1 = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 1, 6, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "cli_test.db"


def _config(*, lifecycle_enabled=True, exits_enabled=True, paper_books_enabled=True) -> PaperBooksConfiguration:
    return PaperBooksConfiguration(
        version=1, enabled=paper_books_enabled,
        baseline=PaperBookDefinition(enabled=True, book_id="BASELINE", starting_cash_usd=Decimal("100000")),
        enhanced=PaperBookDefinition(enabled=False, book_id="ENHANCED", starting_cash_usd=Decimal("100000")),
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
            enabled=lifecycle_enabled, pending_orders=PendingOrdersSection(expire_after_market_days=3),
            exits=ExitsSection(
                enabled=exits_enabled, stop_loss_percent=Decimal("0.08"), profit_target_percent=Decimal("0.15"),
                maximum_holding_market_days=20, exit_on_recommendation_reversal=True,
            ),
            soak=SoakSection(minimum_completed_cycles=1, minimum_market_days=1),
        ),
        config_hash="cli-test-hash", raw={},
    )


def _open_position(conn, cfg):
    from trading_research.evaluation.price_provider import DeterministicPriceProvider

    pp = DeterministicPriceProvider()
    pp.register("AAPL", DAY1.date(), Decimal("100"))
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
    market = execution.MarketSimulationInput(bid=Decimal("97"), ask=Decimal("97.5"))
    execution.submit_and_simulate(conn, intent, market, DAY1)
    return pp


# --- paper-book-lifecycle-run -------------------------------------------------


def test_lifecycle_run_fails_closed_when_disabled(db_path, monkeypatch):
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: _config(lifecycle_enabled=False))
    outcome = cli_support.paper_book_lifecycle_run_cli(db_path, as_of=DAY1)
    assert "error" in outcome


def test_lifecycle_run_fails_closed_when_paper_books_disabled(db_path, monkeypatch):
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: _config(paper_books_enabled=False))
    outcome = cli_support.paper_book_lifecycle_run_cli(db_path, as_of=DAY1)
    assert "error" in outcome


def test_valid_lifecycle_run_returns_sanitized_json(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    _open_position(conn, cfg)
    conn.close()

    outcome = cli_support.paper_book_lifecycle_run_cli(db_path, as_of=DAY2)
    assert "error" not in outcome
    assert outcome["lifecycle_run_id"]
    assert "BASELINE" in outcome["books_processed"]
    assert isinstance(outcome["exit_decisions"], list)
    import json
    json.dumps(outcome)  # must be JSON-serializable (sanitized, deterministic)


# --- paper-book-exit-request ---------------------------------------------------


def test_manual_exit_request_requires_operator_and_reason(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    _open_position(conn, cfg)
    conn.close()

    missing_operator = cli_support.paper_book_exit_request_cli(db_path, book_id="BASELINE", symbol="AAPL", operator="", reason="risk-off")
    assert "error" in missing_operator
    missing_reason = cli_support.paper_book_exit_request_cli(db_path, book_id="BASELINE", symbol="AAPL", operator="alice", reason="")
    assert "error" in missing_reason


def test_manual_exit_request_unknown_book_fails_closed(db_path, monkeypatch):
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: _config())
    outcome = cli_support.paper_book_exit_request_cli(db_path, book_id="NOT_A_BOOK", symbol="AAPL", operator="alice", reason="risk-off")
    assert "error" in outcome


def test_manual_exit_request_creates_audited_row(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    _open_position(conn, cfg)
    conn.close()

    outcome = cli_support.paper_book_exit_request_cli(db_path, book_id="BASELINE", symbol="AAPL", operator="alice", reason="risk-off")
    assert "error" not in outcome
    assert outcome["operator"] == "alice"
    assert outcome["reason"] == "risk-off"
    assert outcome["created"] is True


# --- paper-book-soak-report / paper-book-soak-readiness -----------------------


def test_soak_report_shows_not_enough_history_when_no_lifecycle_runs(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    conn.close()
    outcome = cli_support.paper_book_soak_report_cli(db_path, as_of=DAY1)
    assert outcome["status"] == "NOT_ENOUGH_HISTORY"


def test_soak_report_never_declares_a_winner(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    pp = _open_position(conn, cfg)
    conn.close()

    cli_support.paper_book_lifecycle_run_cli(db_path, as_of=DAY2)
    outcome = cli_support.paper_book_soak_report_cli(db_path, as_of=DAY2)
    assert "error" not in outcome
    assert outcome["status"] in ("NOT_ENOUGH_HISTORY", "RUNNING", "ATTENTION_REQUIRED", "READY_FOR_ACTIVATION_REVIEW")
    text = str(outcome)
    assert "winner" not in text.lower()


def test_soak_readiness_insufficient_cycles(db_path, monkeypatch):
    cfg = _config(paper_books_enabled=True)
    object.__setattr__(cfg.lifecycle.soak, "minimum_completed_cycles", 100)
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    conn.close()
    outcome = cli_support.paper_book_soak_readiness_cli(db_path, as_of=DAY1)
    assert outcome["result"] == "NOT_READY_INSUFFICIENT_CYCLES"


def test_soak_readiness_never_activates_anything(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    conn.close()
    outcome = cli_support.paper_book_soak_readiness_cli(db_path, as_of=DAY1)
    assert outcome["result"].startswith("NOT_READY") or outcome["result"].startswith("READY_FOR")
    assert cfg.lifecycle.enabled is True  # unchanged — readiness is advisory only, never mutates config


def test_soak_readiness_fails_closed_when_paper_books_disabled(db_path, monkeypatch):
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: _config(paper_books_enabled=False))
    outcome = cli_support.paper_book_soak_readiness_cli(db_path, as_of=DAY1)
    assert "error" in outcome


def test_soak_report_fails_closed_when_paper_books_disabled(db_path, monkeypatch):
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: _config(paper_books_enabled=False))
    outcome = cli_support.paper_book_soak_report_cli(db_path, as_of=DAY1)
    assert "error" in outcome


# --- clock correction (Milestone 9.1 Section 5) --------------------------------


def test_lifecycle_run_default_clock_anchors_audit_timestamps_to_as_of(db_path, monkeypatch):
    from trading_research.storage import paper_books_repositories as pb_repo

    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    _open_position(conn, cfg)
    conn.close()

    outcome = cli_support.paper_book_lifecycle_run_cli(db_path, as_of=DAY2)
    assert "error" not in outcome

    conn = connect(db_path)
    results = pb_repo.list_lifecycle_symbol_results(conn, outcome["lifecycle_run_id"])
    conn.close()
    assert results
    for row in results:
        assert row["created_at"] == DAY2.isoformat()


def test_lifecycle_run_audit_time_now_stamps_real_wall_clock_but_not_effective_time(db_path, monkeypatch):
    from trading_research.storage import paper_books_repositories as pb_repo

    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    _open_position(conn, cfg)
    conn.close()

    outcome = cli_support.paper_book_lifecycle_run_cli(db_path, as_of=DAY2, audit_time_now=True)
    assert "error" not in outcome
    assert outcome["as_of"] == DAY2.isoformat()  # effective market/decision time unaffected

    conn = connect(db_path)
    results = pb_repo.list_lifecycle_symbol_results(conn, outcome["lifecycle_run_id"])
    conn.close()
    assert results
    for row in results:
        assert row["created_at"] != DAY2.isoformat()  # real wall-clock audit stamp, not as_of


def test_forced_wallclock_created_at_desyncs_a_later_historical_replay(db_path, monkeypatch):
    """Regression for the exact bug the Milestone 9 scratchpad documented:
    a pending order stamped with a `created_at` in the future relative to a
    later, historical `as_of` (exactly what the old always-wallclock CLI
    default would have produced for real historical replay dates) makes
    `market_days_held` raise once that order is reprocessed on that later
    date. This confirms the default fix (exercised by the two tests above,
    which anchor `created_at` to `as_of` instead) is not a vacuous change —
    the failure mode it prevents is real and reproducible."""
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    from trading_research.evaluation.price_provider import DeterministicPriceProvider

    pp = DeterministicPriceProvider()
    pp.register("AAPL", DAY1.date(), Decimal("100"))
    book = cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=cfg.baseline.starting_cash_usd, config_hash=cfg.config_hash, clock=lambda: DAY1)
    snap = valuation.build_portfolio_snapshot(conn, "BASELINE", DAY1, price_provider=pp, maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds)
    context = risk_module.build_portfolio_context(conn, "BASELINE", DAY1, snap, "AAPL", Decimal("0"))
    decision = risk_module.evaluate_paper_risk(
        book_status=book.status, experiment_arm="BASELINE", expected_arm="BASELINE", context=context,
        requested_quantity_hint=Decimal("100"), reference_price=Decimal("100"), reference_price_age_seconds=0,
        reference_price_point_in_time_safe=True, risk_config=cfg.risk,
    )
    risk_decision_id = order_intent.persist_risk_decision(conn, "BASELINE", "c1", "rec1", "AAPL", decision, snap.snapshot_id, lambda: DAY1)
    future_created_at = DAY2 + timedelta(days=365)
    intent = order_intent.build_order_intent(
        book_id="BASELINE", experiment_arm="BASELINE", cycle_id="c1", recommendation_id="rec1", symbol="AAPL",
        risk_decision=decision, risk_decision_id=risk_decision_id, portfolio_snapshot_id=snap.snapshot_id,
        config_hash=cfg.config_hash, as_of=DAY1, clock=lambda: future_created_at,
    )
    # Market priced above the limit -> BUY never crosses -> stays
    # PENDING_SUBMISSION (mirrors Milestone 8's own "limit == reference price
    # typically doesn't fill same day" convention).
    market = execution.MarketSimulationInput(bid=Decimal("101"), ask=Decimal("101.5"))
    result = execution.submit_and_simulate(conn, intent, market, future_created_at)
    assert result["status"] == "PENDING_SUBMISSION"
    conn.close()

    outcome = cli_support.paper_book_lifecycle_run_cli(db_path, as_of=DAY2)
    # A book-level exception (here: market_days_held's own "as_of_date must
    # not precede opened_on" guard) never raises out of the CLI — it is
    # caught and recorded in failure_reasons, per lifecycle.py's own "one
    # book's failure never mutates the other" contract. `books_processed`
    # stays empty for this book precisely because the exception fired.
    assert "BASELINE" not in outcome["books_processed"]
    assert any("BASELINE" in reason for reason in outcome["failure_reasons"])


# --- paper-soak-run / paper-soak-readiness (Milestone 9.1) ---------------------


def test_paper_soak_run_fails_closed_when_disabled(db_path, monkeypatch):
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: _config(lifecycle_enabled=False))
    outcome = cli_support.paper_soak_run_cli(db_path, as_of=DAY1)
    assert "error" in outcome


def test_paper_soak_run_fails_closed_when_shadow_killed(db_path, monkeypatch):
    from trading_research.shadow import pause as pause_mod

    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    pause_mod.kill(conn, "emergency", "alice", clock=lambda: DAY1)
    conn.close()

    outcome = cli_support.paper_soak_run_cli(db_path, as_of=DAY1)
    assert "error" in outcome


def test_paper_soak_run_fails_closed_when_shadow_paused(db_path, monkeypatch):
    from trading_research.shadow import pause as pause_mod

    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    pause_mod.request_pause(conn, "budget breach", pause_mod.SOURCE_OPERATOR, clock=lambda: DAY1)
    conn.close()

    outcome = cli_support.paper_soak_run_cli(db_path, as_of=DAY1)
    assert "error" in outcome


def test_paper_soak_run_zero_cycle_ids_allowed(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    _open_position(conn, cfg)
    conn.close()

    outcome = cli_support.paper_soak_run_cli(db_path, as_of=DAY2, integrate_cycle_ids=())
    assert "error" not in outcome
    assert outcome["requested_cycle_ids"] == []
    assert outcome["failure_reasons"] == []


def test_paper_soak_run_unknown_cycle_fails_closed_but_does_not_hide_other_book(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    _open_position(conn, cfg)
    conn.close()

    outcome = cli_support.paper_soak_run_cli(db_path, as_of=DAY2, integrate_cycle_ids=("does-not-exist",))
    assert "error" not in outcome  # command itself still completes
    assert any("does-not-exist" in reason for reason in outcome["failure_reasons"])
    assert "BASELINE" in outcome["lifecycle_result"]["books_processed"]


def test_paper_soak_run_persists_operator_summary_and_returns_sanitized_json(db_path, monkeypatch):
    import json

    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    _open_position(conn, cfg)
    conn.close()

    outcome = cli_support.paper_soak_run_cli(db_path, as_of=DAY2)
    assert "error" not in outcome
    assert outcome["operator_run_id"]
    assert outcome["lifecycle_run_id"]
    assert outcome["controlled_readiness"]["status"]
    json.dumps(outcome)  # must be JSON-serializable — sanitized, deterministic

    from trading_research.storage import paper_books_repositories as pb_repo

    conn = connect(db_path)
    stored = pb_repo.load_operator_run(conn, outcome["operator_run_id"])
    conn.close()
    assert stored is not None
    assert stored["lifecycle_run_id"] == outcome["lifecycle_run_id"]


def test_paper_soak_run_is_idempotent_on_replay(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    _open_position(conn, cfg)
    conn.close()

    first = cli_support.paper_soak_run_cli(db_path, as_of=DAY2)
    second = cli_support.paper_soak_run_cli(db_path, as_of=DAY2)
    assert first["operator_run_id"] == second["operator_run_id"]
    assert first["lifecycle_run_id"] == second["lifecycle_run_id"]

    from trading_research.storage import paper_books_repositories as pb_repo

    conn = connect(db_path)
    all_runs = pb_repo.list_operator_runs(conn)
    conn.close()
    assert len(all_runs) == 1  # never duplicated


def test_paper_soak_readiness_cli_matches_controlled_readiness_module(db_path, monkeypatch):
    cfg = _config()
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)
    conn = connect(db_path)
    conn.close()

    outcome = cli_support.paper_soak_readiness_cli(db_path, as_of=DAY1)
    assert "error" not in outcome
    assert outcome["status"] in (
        "NOT_READY_PAPER_SOAK", "NOT_READY_SHADOW_PAUSED", "NOT_READY_SHADOW_KILLED",
        "NOT_READY_HEALTH_UNEXPLAINED", "NOT_READY_CRITICAL_ALERTS", "NOT_READY_PROVIDER_HISTORY",
        "NOT_READY_RECONCILIATION", "NOT_READY_VALUATION", "READY_FOR_MANUAL_SOAK",
        "READY_FOR_EXTENDED_MANUAL_SOAK", "READY_FOR_RECURRING_ACTIVATION_REVIEW",
    )
    assert "checks" in outcome and isinstance(outcome["checks"], list)


def test_paper_soak_run_persists_and_uses_cross_book_verification(db_path, monkeypatch):
    """Milestone 9.2 Section 12: verification runs after lifecycle, is
    persisted, and its ID/status are threaded onto the operator run."""
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: _config())
    outcome = cli_support.paper_soak_run_cli(db_path, as_of=DAY1)
    assert "error" not in outcome
    assert outcome["cross_book_verification_id"] is not None
    assert outcome["cross_book_verification_status"] in ("PASSED", "FAILED", "INSUFFICIENT_DATA")
    assert outcome["cross_book_verification"]["verification_id"] == outcome["cross_book_verification_id"]

    from trading_research.storage import paper_books_repositories as pb_repo
    from trading_research.storage.database import connect

    conn = connect(db_path)
    stored = pb_repo.load_cross_book_verification(conn, outcome["cross_book_verification_id"])
    assert stored is not None
    conn.close()


def test_paper_book_cross_check_cli_fails_closed_when_disabled(db_path, monkeypatch):
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: _config(paper_books_enabled=False))
    outcome = cli_support.paper_book_cross_check_cli(db_path, as_of=DAY1)
    assert "error" in outcome


def test_paper_book_cross_check_cli_returns_bounded_sanitized_result(db_path, monkeypatch):
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: _config())
    outcome = cli_support.paper_book_cross_check_cli(db_path, as_of=DAY1)
    assert outcome["status"] in ("PASSED", "FAILED", "INSUFFICIENT_DATA")
    assert isinstance(outcome["checks"], list)
    serialized = str(outcome).lower()
    for forbidden in ("api_key", "authorization", "sk-ant-", "password", "secret"):
        assert forbidden not in serialized


def test_readiness_diagnostics_expose_all_failed_and_missing_checks(db_path, monkeypatch):
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: _config())
    outcome = cli_support.paper_soak_readiness_cli(db_path, as_of=DAY1)
    assert "error" not in outcome
    for key in ("all_failed_checks", "blocking_checks", "advisory_checks", "missing_checks"):
        assert key in outcome
        assert isinstance(outcome[key], list)


def test_paper_soak_readiness_cli_fails_closed_when_disabled(db_path, monkeypatch):
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: _config(paper_books_enabled=False))
    outcome = cli_support.paper_soak_readiness_cli(db_path, as_of=DAY1)
    assert "error" in outcome
