"""Tests for paper_books/controlled_soak_readiness.py (Milestone 9.1).

Deliberately does not re-test Milestone 9's own `evaluate_paper_soak_readiness`
internals (reconciliation/valuation/cycle-count/market-day/lifecycle-failure
logic — already covered by test_paper_books_lifecycle_cli.py) or Milestone
7.2's own `evaluate_activation_readiness` internals (already covered by
test_shadow_readiness.py). This file tests only the NEW combining logic:
fail-closed ordering, the two checks neither sub-function owns (unresolved
CRITICAL alerts, the cross-book MISSING signal), and that the final status
is always one of the documented Milestone 9.1 vocabulary."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.paper_books import controlled_soak_readiness as csr
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
from trading_research.shadow import pause as pause_mod
from trading_research.shadow.alerts import OperationalAlert, SEVERITY_CRITICAL, raise_alert
from trading_research.shadow.config import load_shadow_operations_config
from trading_research.shadow.readiness import (
    ACTIVATION_NOT_READY_INSUFFICIENT_HISTORY,
    ACTIVATION_READY_FOR_LIMITED_RECURRING_SHADOW,
    ActivationReadinessResult,
)
from trading_research.storage.database import connect

BASE_TIME = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "controlled_soak_readiness_test.db")
        yield c
        c.close()


@pytest.fixture
def paper_cfg() -> PaperBooksConfiguration:
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
        config_hash="csr-test-hash", raw={},
    )


@pytest.fixture
def shadow_cfg():
    return load_shadow_operations_config()


def _ready_paper_soak(**overrides) -> dict:
    result = {
        "result": "READY_FOR_MORE_MANUAL_SOAK", "reasons": [], "as_of": BASE_TIME.isoformat(),
        "completed_cycles": 1, "market_days_covered": 1, "both_books_enabled": True,
    }
    result.update(overrides)
    return result


def _ready_activation(*, readiness_conn, **overrides) -> ActivationReadinessResult:
    from trading_research.shadow.readiness import build_readiness_report

    kwargs = dict(status=ACTIVATION_READY_FOR_LIMITED_RECURRING_SHADOW, reasons=("ok",))
    kwargs.update(overrides)
    # readiness_report is only read for real_provider_cycle_count in csr — a
    # zero-history report (built against the SAME test `conn`, whose schema
    # `apply_schema` already initialized) is fine unless a test overrides it.
    if "readiness_report" not in kwargs:
        kwargs["readiness_report"] = build_readiness_report(readiness_conn, BASE_TIME, load_shadow_operations_config())
    return ActivationReadinessResult(**kwargs)


def test_shadow_killed_blocks_before_anything_else(conn, paper_cfg, shadow_cfg, monkeypatch):
    pause_mod.kill(conn, "operator emergency stop", "alice", clock=lambda: BASE_TIME)
    result = csr.evaluate_controlled_soak_readiness(conn, BASE_TIME, paper_cfg, shadow_cfg)
    assert result.status == csr.STATUS_NOT_READY_SHADOW_KILLED
    assert result.checks[0].name == "shadow_kill_state"
    assert result.checks[0].passed is False


def test_shadow_paused_blocks(conn, paper_cfg, shadow_cfg):
    pause_mod.request_pause(conn, "budget breach", pause_mod.SOURCE_OPERATOR, clock=lambda: BASE_TIME)
    result = csr.evaluate_controlled_soak_readiness(conn, BASE_TIME, paper_cfg, shadow_cfg)
    assert result.status == csr.STATUS_NOT_READY_SHADOW_PAUSED


def test_unexplained_pause_required_blocks(conn, paper_cfg, shadow_cfg):
    from trading_research.storage.shadow_alerts_repositories import save_run_summary

    save_run_summary(conn, {
        "scheduler_run_id": "run-unexplained", "intended_schedule_id": "intended-1", "policy_version": "health/v2",
        "health_status": "PAUSE_REQUIRED", "health_reasons_json": "[]", "provider_success_rate": 0.9,
        "evidence_completeness_rate": 0.9, "claude_role_success_rate": 0.9, "retry_rate": 0.0,
        "retry_exhaustion_rate": 0.0, "unsupported_claim_rate": 0.0, "output_truncation_rate": 0.0,
        "latency_seconds": 1.0, "input_tokens": 1, "output_tokens": 1, "cost_usd": "0.1",
        "paper_reconciliation_mismatch": 0, "duplicate_prevention_violation": 0, "cycle_duration_seconds": 1.0,
        "created_at": BASE_TIME.isoformat(),
    })
    result = csr.evaluate_controlled_soak_readiness(conn, BASE_TIME, paper_cfg, shadow_cfg)
    assert result.status == csr.STATUS_NOT_READY_HEALTH_UNEXPLAINED


def test_unresolved_critical_alert_blocks(conn, paper_cfg, shadow_cfg):
    alert = OperationalAlert(
        severity=SEVERITY_CRITICAL, alert_type="PROVIDER_UNAVAILABLE", message="provider down",
        context={}, created_at=BASE_TIME,
    )
    raise_alert(conn, alert, (), clock=lambda: BASE_TIME)
    result = csr.evaluate_controlled_soak_readiness(conn, BASE_TIME, paper_cfg, shadow_cfg)
    assert result.status == csr.STATUS_NOT_READY_CRITICAL_ALERTS


def test_resolved_critical_alert_does_not_block(conn, paper_cfg, shadow_cfg, monkeypatch):
    from trading_research.storage.shadow_alerts_repositories import resolve_alert

    alert = OperationalAlert(
        severity=SEVERITY_CRITICAL, alert_type="PROVIDER_UNAVAILABLE", message="provider down",
        context={}, created_at=BASE_TIME,
    )
    raise_alert(conn, alert, (), clock=lambda: BASE_TIME)
    resolve_alert(conn, alert.alert_id, resolved_by="alice", reason="handled", resolved_at=BASE_TIME.isoformat())

    monkeypatch.setattr(csr, "evaluate_paper_soak_readiness", lambda c, a, cfg: _ready_paper_soak())
    monkeypatch.setattr(
        "trading_research.shadow.readiness.evaluate_activation_readiness",
        lambda c, a, cfg, thresholds=None, environmentally_blocked_reason=None: _ready_activation(readiness_conn=c),
    )
    result = csr.evaluate_controlled_soak_readiness(conn, BASE_TIME, paper_cfg, shadow_cfg)
    assert result.status != csr.STATUS_NOT_READY_CRITICAL_ALERTS


def test_paper_soak_reconciliation_mismatch_maps_to_dedicated_status(conn, paper_cfg, shadow_cfg, monkeypatch):
    monkeypatch.setattr(
        csr, "evaluate_paper_soak_readiness",
        lambda c, a, cfg: _ready_paper_soak(result="NOT_READY_RECONCILIATION", reasons=["BASELINE: MISMATCHED"]),
    )
    result = csr.evaluate_controlled_soak_readiness(conn, BASE_TIME, paper_cfg, shadow_cfg)
    assert result.status == csr.STATUS_NOT_READY_RECONCILIATION
    assert result.paper_soak_status == "NOT_READY_RECONCILIATION"


def test_paper_soak_valuation_incomplete_maps_to_dedicated_status(conn, paper_cfg, shadow_cfg, monkeypatch):
    monkeypatch.setattr(
        csr, "evaluate_paper_soak_readiness",
        lambda c, a, cfg: _ready_paper_soak(result="NOT_READY_VALUATION", reasons=["BASELINE: PARTIAL_UNVALUED"]),
    )
    result = csr.evaluate_controlled_soak_readiness(conn, BASE_TIME, paper_cfg, shadow_cfg)
    assert result.status == csr.STATUS_NOT_READY_VALUATION


@pytest.mark.parametrize("paper_result", [
    "NOT_READY_INSUFFICIENT_CYCLES", "NOT_READY_INSUFFICIENT_MARKET_DAYS", "NOT_READY_LIFECYCLE_FAILURES",
])
def test_paper_soak_generic_failures_map_to_paper_soak_status(conn, paper_cfg, shadow_cfg, monkeypatch, paper_result):
    monkeypatch.setattr(
        csr, "evaluate_paper_soak_readiness", lambda c, a, cfg: _ready_paper_soak(result=paper_result, reasons=["x"]),
    )
    result = csr.evaluate_controlled_soak_readiness(conn, BASE_TIME, paper_cfg, shadow_cfg)
    assert result.status == csr.STATUS_NOT_READY_PAPER_SOAK
    assert result.paper_soak_status == paper_result


def test_insufficient_real_provider_history_blocks(conn, paper_cfg, shadow_cfg, monkeypatch):
    monkeypatch.setattr(csr, "evaluate_paper_soak_readiness", lambda c, a, cfg: _ready_paper_soak())
    monkeypatch.setattr(
        "trading_research.shadow.readiness.evaluate_activation_readiness",
        lambda c, a, cfg, thresholds=None, environmentally_blocked_reason=None: _ready_activation(
            readiness_conn=c, status=ACTIVATION_NOT_READY_INSUFFICIENT_HISTORY,
            reasons=("not enough real-provider cycles",),
        ),
    )
    result = csr.evaluate_controlled_soak_readiness(conn, BASE_TIME, paper_cfg, shadow_cfg)
    assert result.status == csr.STATUS_NOT_READY_PROVIDER_HISTORY
    assert result.shadow_activation_status == ACTIVATION_NOT_READY_INSUFFICIENT_HISTORY


def test_manual_soak_ready_when_all_gates_clear_but_market_days_below_double_minimum(conn, paper_cfg, shadow_cfg, monkeypatch):
    from trading_research.shadow.readiness import ReadinessThresholds

    monkeypatch.setattr(
        csr, "evaluate_paper_soak_readiness",
        lambda c, a, cfg: _ready_paper_soak(result="READY_FOR_MORE_MANUAL_SOAK", market_days_covered=1),
    )
    monkeypatch.setattr(
        "trading_research.shadow.readiness.evaluate_activation_readiness",
        lambda c, a, cfg, thresholds=None, environmentally_blocked_reason=None: _ready_activation(readiness_conn=c),
    )
    # Milestone 9.2: real_provider_cycle_count now comes from persisted
    # provenance, not cost_usd — zeroed out here (min=0) since this test's
    # own focus is market-days tiering, not provider-history gating (that is
    # covered separately by test_provenance-driven tests below).
    result = csr.evaluate_controlled_soak_readiness(
        conn, BASE_TIME, paper_cfg, shadow_cfg, shadow_thresholds=ReadinessThresholds(min_real_provider_cycles_for_ready=0),
    )
    assert result.status == csr.STATUS_READY_FOR_MANUAL_SOAK


def test_extended_manual_soak_ready_but_recurring_review_withheld_for_missing_cross_book_signal(
    conn, paper_cfg, shadow_cfg, monkeypatch,
):
    from trading_research.shadow.readiness import ReadinessThresholds

    monkeypatch.setattr(
        csr, "evaluate_paper_soak_readiness",
        lambda c, a, cfg: _ready_paper_soak(result="READY_FOR_RECURRING_ACTIVATION_REVIEW", market_days_covered=100),
    )
    monkeypatch.setattr(
        "trading_research.shadow.readiness.evaluate_activation_readiness",
        lambda c, a, cfg, thresholds=None, environmentally_blocked_reason=None: _ready_activation(readiness_conn=c),
    )
    result = csr.evaluate_controlled_soak_readiness(
        conn, BASE_TIME, paper_cfg, shadow_cfg, shadow_thresholds=ReadinessThresholds(min_real_provider_cycles_for_ready=0),
    )
    assert result.status == csr.STATUS_READY_FOR_EXTENDED_MANUAL_SOAK
    assert result.status != csr.STATUS_READY_FOR_RECURRING_ACTIVATION_REVIEW
    cross_book_check = next(c for c in result.checks if c.name == "cross_book_violation_signal")
    assert cross_book_check.classification == csr.CLASSIFICATION_MISSING


def test_never_returns_recurring_activation_review_today(conn, paper_cfg, shadow_cfg, monkeypatch):
    """Documents the known gap (Section 4): with no persisted cross-book
    signal, this status is structurally unreachable — never fabricated."""
    monkeypatch.setattr(
        csr, "evaluate_paper_soak_readiness",
        lambda c, a, cfg: _ready_paper_soak(result="READY_FOR_RECURRING_ACTIVATION_REVIEW", market_days_covered=10_000),
    )
    monkeypatch.setattr(
        "trading_research.shadow.readiness.evaluate_activation_readiness",
        lambda c, a, cfg, thresholds=None, environmentally_blocked_reason=None: _ready_activation(readiness_conn=c),
    )
    result = csr.evaluate_controlled_soak_readiness(conn, BASE_TIME, paper_cfg, shadow_cfg)
    assert result.status != csr.STATUS_READY_FOR_RECURRING_ACTIVATION_REVIEW


def test_cross_book_verification_failed_blocks_in_isolation(conn, paper_cfg, shadow_cfg, monkeypatch):
    from trading_research.paper_books.cross_book_verification import CrossBookCheck, CrossBookVerificationResult, persist_verification

    monkeypatch.setattr(csr, "evaluate_paper_soak_readiness", lambda c, a, cfg: _ready_paper_soak())
    monkeypatch.setattr(
        "trading_research.shadow.readiness.evaluate_activation_readiness",
        lambda c, a, cfg, thresholds=None, environmentally_blocked_reason=None: _ready_activation(readiness_conn=c),
    )
    result = CrossBookVerificationResult(
        verification_id="cbv-test-failed", as_of=BASE_TIME, status="FAILED",
        checks=(CrossBookCheck("orders_arm_matches_book", "FAILED", "1", "0 violations", "paper_book_orders", "bad"),),
        violation_count=1, policy_version="cross-book-verification/v1",
    )
    persist_verification(conn, result, operator_run_id=None, lifecycle_run_id=None, created_at=BASE_TIME)

    readiness_result = csr.evaluate_controlled_soak_readiness(conn, BASE_TIME, paper_cfg, shadow_cfg)
    assert readiness_result.status == csr.STATUS_NOT_READY_CROSS_BOOK


def test_cross_book_verification_passed_allows_activation_review_when_everything_else_clears(
    conn, paper_cfg, shadow_cfg, monkeypatch,
):
    from trading_research.paper_books.cross_book_verification import CrossBookCheck, CrossBookVerificationResult, persist_verification
    from trading_research.shadow.readiness import ReadinessThresholds

    monkeypatch.setattr(
        csr, "evaluate_paper_soak_readiness",
        lambda c, a, cfg: _ready_paper_soak(result="READY_FOR_RECURRING_ACTIVATION_REVIEW", market_days_covered=100),
    )
    monkeypatch.setattr(
        "trading_research.shadow.readiness.evaluate_activation_readiness",
        lambda c, a, cfg, thresholds=None, environmentally_blocked_reason=None: _ready_activation(readiness_conn=c),
    )
    result = CrossBookVerificationResult(
        verification_id="cbv-test-passed", as_of=BASE_TIME, status="PASSED", checks=(), violation_count=0,
        policy_version="cross-book-verification/v1",
    )
    persist_verification(conn, result, operator_run_id=None, lifecycle_run_id=None, created_at=BASE_TIME)

    readiness_result = csr.evaluate_controlled_soak_readiness(
        conn, BASE_TIME, paper_cfg, shadow_cfg, shadow_thresholds=ReadinessThresholds(min_real_provider_cycles_for_ready=0),
    )
    assert readiness_result.status == csr.STATUS_READY_FOR_RECURRING_ACTIVATION_REVIEW


def test_result_status_always_in_documented_vocabulary(conn, paper_cfg, shadow_cfg):
    result = csr.evaluate_controlled_soak_readiness(conn, BASE_TIME, paper_cfg, shadow_cfg)
    assert result.status in csr.CONTROLLED_SOAK_STATUSES
    for check in result.checks:
        assert check.classification in csr.CHECK_CLASSIFICATIONS


def _persist_completed_cycle_with_provider(conn, *, cycle_id: str, provider_mode: str, real: bool) -> None:
    from trading_research.research.provider_provenance import evidence_provider_row, record_cycle_provider_provenance
    conn.execute(
        "INSERT INTO research_cycles (cycle_id, universe_id, as_of, configuration_hash, experiment_policy, "
        "provider_mode, status, started_at, completed_at) VALUES (?, 'u', ?, 'h', 'OBSERVE_ONLY', ?, 'COMPLETED', ?, ?)",
        (cycle_id, BASE_TIME.isoformat(), provider_mode, BASE_TIME.isoformat(), BASE_TIME.isoformat()),
    )
    conn.commit()
    record_cycle_provider_provenance(conn, [evidence_provider_row(
        cycle_id=cycle_id, research_run_id=None, symbol="AAPL", provider_category="market",
        provider_name="real-market" if real else "fixture-market", request_or_source_id="s", status="ok",
        cycle_provider_mode="real" if real else "fixture", observed_at=BASE_TIME,
    )])


def test_zero_cost_successful_real_provider_satisfies_controlled_gate(conn, paper_cfg, shadow_cfg, monkeypatch):
    from trading_research.shadow.readiness import ReadinessThresholds
    _persist_completed_cycle_with_provider(conn, cycle_id="real-zero-cost", provider_mode="real", real=True)
    monkeypatch.setattr(csr, "evaluate_paper_soak_readiness", lambda c, a, cfg: _ready_paper_soak())
    monkeypatch.setattr(
        "trading_research.shadow.readiness.evaluate_activation_readiness",
        lambda c, a, cfg, thresholds=None, environmentally_blocked_reason=None: _ready_activation(readiness_conn=c),
    )
    result = csr.evaluate_controlled_soak_readiness(
        conn, BASE_TIME, paper_cfg, shadow_cfg,
        shadow_thresholds=ReadinessThresholds(min_real_provider_cycles_for_ready=1),
    )
    check = next(c for c in result.checks if c.name == "real_provider_success_cycle_count")
    assert check.passed is True
    assert result.status != csr.STATUS_NOT_READY_PROVIDER_HISTORY


def test_positive_cost_fixture_does_not_satisfy_controlled_gate(conn, paper_cfg, shadow_cfg, monkeypatch):
    from trading_research.shadow.readiness import ReadinessThresholds
    from trading_research.storage.shadow_alerts_repositories import save_run_summary
    _persist_completed_cycle_with_provider(conn, cycle_id="fixture-with-cost", provider_mode="fixture", real=False)
    save_run_summary(conn, {
        "scheduler_run_id": "fixture-cost-run", "intended_schedule_id": "fixture-cost", "policy_version": "health/v2",
        "health_status": "HEALTHY", "health_reasons_json": "[]", "provider_success_rate": 1.0,
        "evidence_completeness_rate": 1.0, "claude_role_success_rate": 1.0, "retry_rate": 0.0,
        "retry_exhaustion_rate": 0.0, "unsupported_claim_rate": 0.0, "output_truncation_rate": 0.0,
        "latency_seconds": 1.0, "input_tokens": 1, "output_tokens": 1, "cost_usd": "99.00",
        "paper_reconciliation_mismatch": 0, "duplicate_prevention_violation": 0,
        "cycle_duration_seconds": 1.0, "created_at": BASE_TIME.isoformat(),
    })
    monkeypatch.setattr(csr, "evaluate_paper_soak_readiness", lambda c, a, cfg: _ready_paper_soak())
    monkeypatch.setattr(
        "trading_research.shadow.readiness.evaluate_activation_readiness",
        lambda c, a, cfg, thresholds=None, environmentally_blocked_reason=None: _ready_activation(readiness_conn=c),
    )
    result = csr.evaluate_controlled_soak_readiness(
        conn, BASE_TIME, paper_cfg, shadow_cfg,
        shadow_thresholds=ReadinessThresholds(min_real_provider_cycles_for_ready=1),
    )
    assert result.status == csr.STATUS_NOT_READY_PROVIDER_HISTORY
    assert next(c for c in result.checks if c.name == "real_provider_success_cycle_count").observed_value == "0"


def test_all_simultaneous_failures_visible_with_deterministic_primary(conn, paper_cfg, shadow_cfg, monkeypatch):
    pause_mod.kill(conn, "operator emergency stop", "alice", clock=lambda: BASE_TIME)
    raise_alert(conn, OperationalAlert(
        severity=SEVERITY_CRITICAL, alert_type="PROVIDER_UNAVAILABLE", message="provider down",
        context={}, created_at=BASE_TIME,
    ), (), clock=lambda: BASE_TIME)
    monkeypatch.setattr(csr, "evaluate_paper_soak_readiness", lambda c, a, cfg: _ready_paper_soak(
        result="NOT_READY_LIFECYCLE_FAILURES", reasons=["lifecycle failed"], failed_checks=[
            {"name": "lifecycle_failures", "result": "NOT_READY_LIFECYCLE_FAILURES", "reasons": ["failed"]},
            {"name": "paper_reconciliation", "result": "NOT_READY_RECONCILIATION", "reasons": ["mismatch"]},
            {"name": "paper_valuation", "result": "NOT_READY_VALUATION", "reasons": ["unsafe"]},
        ],
    ))
    result = csr.evaluate_controlled_soak_readiness(conn, BASE_TIME, paper_cfg, shadow_cfg)
    failed = {c.name for c in result.checks if c.passed is False}
    assert result.status == csr.STATUS_NOT_READY_SHADOW_KILLED
    assert {"shadow_kill_state", "unresolved_critical_alerts", "lifecycle_failures",
            "paper_reconciliation", "paper_valuation"} <= failed


def test_stale_verification_cannot_satisfy_recurring_review(conn, paper_cfg, shadow_cfg, monkeypatch):
    from trading_research.paper_books import cash_ledger
    from trading_research.paper_books.cross_book_verification import CrossBookVerificationResult, persist_verification
    from trading_research.shadow.readiness import ReadinessThresholds
    persisted = CrossBookVerificationResult(
        verification_id="cbv-before-change", as_of=BASE_TIME, status="PASSED", checks=(),
        violation_count=0, policy_version="cross-book-verification/v2",
    )
    persist_verification(conn, persisted, operator_run_id=None, lifecycle_run_id=None, created_at=BASE_TIME)
    cash_ledger.open_book(
        conn, book_id="BASELINE", starting_cash_usd=Decimal("100000"), config_hash="h", clock=lambda: BASE_TIME,
    )
    monkeypatch.setattr(csr, "evaluate_paper_soak_readiness", lambda c, a, cfg: _ready_paper_soak(
        result="READY_FOR_RECURRING_ACTIVATION_REVIEW", market_days_covered=100,
    ))
    monkeypatch.setattr(
        "trading_research.shadow.readiness.evaluate_activation_readiness",
        lambda c, a, cfg, thresholds=None, environmentally_blocked_reason=None: _ready_activation(readiness_conn=c),
    )
    result = csr.evaluate_controlled_soak_readiness(
        conn, BASE_TIME, paper_cfg, shadow_cfg,
        shadow_thresholds=ReadinessThresholds(min_real_provider_cycles_for_ready=0),
    )
    cross = next(c for c in result.checks if c.name == "cross_book_violation_signal")
    assert cross.observed_value == "STALE"
    assert cross.classification == csr.CLASSIFICATION_MISSING
    assert result.status != csr.STATUS_READY_FOR_RECURRING_ACTIVATION_REVIEW
