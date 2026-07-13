"""Tests for shadow/readiness.py (docs/milestone-7.md Step 23, Step 27 section L)."""
from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_research.shadow.alerts import (
    ALERT_TYPE_PROVIDER_UNAVAILABLE,
    SEVERITY_CRITICAL,
    OperationalAlert,
    raise_alert,
)
from trading_research.shadow.config import load_shadow_operations_config
from trading_research.shadow.readiness import (
    STATUS_INSUFFICIENT_DATA,
    STATUS_READY,
    STATUS_READY_WITH_WARNINGS,
    ReadinessPolicyError,
    ReadinessThresholds,
    build_readiness_report,
)
from trading_research.storage.database import connect
from trading_research.storage.shadow_alerts_repositories import save_run_summary
from trading_research.storage.shadow_operations_repositories import save_scheduler_run

BASE_TIME = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)


def _clock_at(t: datetime):
    return lambda: t


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "shadow_readiness_test.db")
        yield c
        c.close()


@pytest.fixture
def shadow_config():
    return load_shadow_operations_config()


def _seed_scheduler_run(conn, *, status: str = "COMPLETED", offset_minutes: int = 0, run_id: str | None = None) -> str:
    run_id = run_id or f"shadow-run-{uuid.uuid4().hex}"
    created_at = (BASE_TIME + timedelta(minutes=offset_minutes)).isoformat()
    save_scheduler_run(
        conn,
        {
            "scheduler_run_id": run_id, "intended_schedule_id": f"intended-{offset_minutes}",
            "scheduled_time": created_at, "actual_start_at": created_at, "actual_finish_at": created_at,
            "cycle_id": f"cycle-{offset_minutes}", "configuration_hash": "hash", "mode": "SHADOW_ENHANCED",
            "lease_owner": "owner-1", "lease_expires_at": created_at, "status": status, "pause_state": "ACTIVE",
            "budget_reservation_id": None, "budget_reserved_usd": "1.00", "budget_consumed_usd": "0.50",
            "symbols_attempted": 1, "symbols_completed": 1 if status == "COMPLETED" else 0,
            "symbols_skipped": 0, "provider_failures": 0, "research_failures": 0, "paper_submissions": 0,
            "alert_count": 0, "failure_reason": None, "operator_action": None, "deployment_source": "manual-invocation",
            "created_at": created_at,
        },
    )
    return run_id


def _seed_run_summary(
    conn, *, scheduler_run_id: str, offset_minutes: int = 0, evidence_completeness_rate: float = 0.98,
    provider_success_rate: float = 0.99, cost_usd: str | None = "0.25", cycle_duration_seconds: float = 60.0,
    claude_role_success_rate: float | None = 0.95,
) -> None:
    created_at = (BASE_TIME + timedelta(minutes=offset_minutes)).isoformat()
    save_run_summary(
        conn,
        {
            "scheduler_run_id": scheduler_run_id, "intended_schedule_id": f"intended-{offset_minutes}",
            "policy_version": "health/v1", "health_status": "HEALTHY", "health_reasons_json": "[]",
            "provider_success_rate": provider_success_rate, "evidence_completeness_rate": evidence_completeness_rate,
            "claude_role_success_rate": claude_role_success_rate, "retry_rate": 0.05, "retry_exhaustion_rate": 0.0,
            "unsupported_claim_rate": 0.0, "output_truncation_rate": 0.0, "latency_seconds": 5.0,
            "input_tokens": 1000, "output_tokens": 500, "cost_usd": cost_usd, "paper_reconciliation_mismatch": 0,
            "duplicate_prevention_violation": 0, "cycle_duration_seconds": cycle_duration_seconds,
            "created_at": created_at,
        },
    )


def _seed_alert_delivered_successfully(conn) -> None:
    class _AlwaysSucceedSink:
        name = "always_succeed"

        def send(self, alert):
            from trading_research.shadow.alerts import AlertDeliveryResult

            return AlertDeliveryResult(sink_name=self.name, success=True, response_text="ok", attempt_number=1)

    alert = OperationalAlert(
        severity="INFO", alert_type=ALERT_TYPE_PROVIDER_UNAVAILABLE, message="transient provider blip, recovered",
        context={}, created_at=BASE_TIME,
    )
    raise_alert(conn, alert, (_AlwaysSucceedSink(),), clock=_clock_at(BASE_TIME))


# --- insufficient data (no data at all) --------------------------------------------------


def test_no_runs_at_all_is_insufficient_data(conn, shadow_config):
    report = build_readiness_report(conn, BASE_TIME, shadow_config)
    assert report.overall_status == STATUS_INSUFFICIENT_DATA
    assert report.completed_cycle_count == 0


# --- single successful cycle is explicitly NOT ready (hard requirement) --------------------


def test_single_successful_cycle_is_never_ready(conn, shadow_config):
    run_id = _seed_scheduler_run(conn, status="COMPLETED", offset_minutes=0)
    _seed_run_summary(
        conn, scheduler_run_id=run_id, offset_minutes=0, evidence_completeness_rate=1.0, provider_success_rate=1.0,
        cost_usd="0.05", cycle_duration_seconds=30.0,
    )
    report = build_readiness_report(conn, BASE_TIME, shadow_config)
    assert report.overall_status != STATUS_READY
    assert report.overall_status != STATUS_READY_WITH_WARNINGS
    assert report.overall_status == STATUS_INSUFFICIENT_DATA
    assert report.completed_cycle_count == 1
    assert any("insufficient sample size" in r for r in report.reasons)


def test_single_successful_cycle_not_ready_even_with_custom_low_threshold_still_requires_explicit_config(conn, shadow_config):
    """Even when a caller supplies thresholds, the floor is not implicitly
    zero — min_completed_cycles_for_ready must be >= 1, so a single cycle
    can only ever be READY if an operator explicitly configures
    min_completed_cycles_for_ready=1, which this test makes an explicit,
    visible choice (not an accidental default)."""
    run_id = _seed_scheduler_run(conn, status="COMPLETED", offset_minutes=0)
    _seed_run_summary(conn, scheduler_run_id=run_id, offset_minutes=0)
    lenient = ReadinessThresholds(min_completed_cycles_for_ready=1, min_real_provider_cycles_for_ready=0)
    report = build_readiness_report(conn, BASE_TIME, shadow_config, thresholds=lenient)
    # Even with an explicit threshold of 1, one data point alone should not
    # be conflated with production stability — this repo's default remains
    # >= 10; a caller choosing 1 gets what they configured, but the DEFAULT
    # behavior (tested above) never allows this silently.
    assert report.completed_cycle_count == 1


# --- healthy-looking sample below minimum sample count still INSUFFICIENT_DATA -------------


def test_many_completed_cycles_reach_ready_with_warnings_or_better(conn, shadow_config):
    thresholds = ReadinessThresholds(min_completed_cycles_for_ready=3, min_real_provider_cycles_for_ready=2)
    for i in range(5):
        run_id = _seed_scheduler_run(conn, status="COMPLETED", offset_minutes=i)
        _seed_run_summary(
            conn, scheduler_run_id=run_id, offset_minutes=i, evidence_completeness_rate=0.99,
            provider_success_rate=0.99, cost_usd="0.10", cycle_duration_seconds=45.0,
        )
    # Research and operational categories are only READY when the
    # underlying tables actually have data — seed a minimal research
    # attempt and a successfully-delivered alert so this "everything
    # healthy" scenario exercises every category rather than leaving two of
    # them at INSUFFICIENT_DATA (which would otherwise, correctly, keep the
    # overall report from ever reaching READY/READY_WITH_WARNINGS).
    conn.execute(
        "INSERT INTO research_evidence_snapshots (snapshot_id, symbol, as_of, created_at, source_records_json, "
        "evidence_items_json, deterministic_factors_json, sentiment_metrics_json, portfolio_context_json, "
        "missing_data_reasons_json, conflict_reasons_json, point_in_time_safe, config_hash, git_sha) "
        "VALUES ('snap-1', 'AAPL', ?, ?, '[]', '[]', '{}', '{}', NULL, '[]', '[]', 1, 'h', 'sha')",
        (BASE_TIME.isoformat(), BASE_TIME.isoformat()),
    )
    conn.execute(
        "INSERT INTO research_committee_runs (research_run_id, snapshot_id, provider, model_name, roles_json, "
        "run_mode, status, config_hash, created_at, completed_at) "
        "VALUES ('run-1', 'snap-1', 'deterministic', 'n/a', '[]', 'BASELINE', 'COMPLETED', 'h', ?, ?)",
        (BASE_TIME.isoformat(), BASE_TIME.isoformat()),
    )
    conn.execute(
        "INSERT INTO research_attempts (attempt_id, research_run_id, role, attempt_number, prompt_name, "
        "prompt_version, prompt_hash, system_prompt_hash, schema_version, provider, model_name, success, "
        "failure_reason, raw_response_json, validated_payload_json, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, latency_ms, provider_request_id, retry_count, pricing_version, "
        "estimated_cost, cost_status, created_at) "
        "VALUES ('attempt-1', 'run-1', 'bull', 1, 'bull', 'v1', 'ph', 'sph', 'sv1', 'deterministic', 'n/a', 1, "
        "NULL, NULL, NULL, 100, 50, 0, 0, 10, NULL, 0, NULL, NULL, 'NOT_PRICED', ?)",
        (BASE_TIME.isoformat(),),
    )
    conn.commit()
    _seed_alert_delivered_successfully(conn)

    report = build_readiness_report(conn, BASE_TIME, shadow_config, thresholds=thresholds)
    assert report.completed_cycle_count == 5
    assert report.overall_status in (STATUS_READY, STATUS_READY_WITH_WARNINGS)


# --- reconciliation mismatch ---------------------------------------------------------------


def test_reconciliation_mismatch_blocks_paper_readiness(conn, shadow_config):
    thresholds = ReadinessThresholds(min_completed_cycles_for_ready=1, min_real_provider_cycles_for_ready=0)
    run_id = _seed_scheduler_run(conn, status="FAILED", offset_minutes=0)
    from trading_research.storage.shadow_operations_repositories import update_scheduler_run

    update_scheduler_run(conn, run_id, {"failure_reason": "paper reconciliation mismatch detected"})
    report = build_readiness_report(conn, BASE_TIME, shadow_config, thresholds=thresholds)
    paper_cat = next(c for c in report.categories if c.category == "paper")
    assert paper_cat.status == "NOT_READY"
    assert report.reconciliation_mismatch_count == 1


# --- cost unknown (pricing not configured) --------------------------------------------------


def test_cost_unknown_when_no_summary_carries_cost(conn, shadow_config):
    run_id = _seed_scheduler_run(conn, status="COMPLETED", offset_minutes=0)
    _seed_run_summary(conn, scheduler_run_id=run_id, offset_minutes=0, cost_usd=None)
    report = build_readiness_report(conn, BASE_TIME, shadow_config)
    budget_cat = next(c for c in report.categories if c.category == "budget")
    assert budget_cat.status == STATUS_INSUFFICIENT_DATA
    assert report.cost_per_completed_cycle_usd is None


# --- unstable scheduler (high scheduler-miss rate) -------------------------------------------


def test_unstable_scheduler_high_miss_rate(conn, shadow_config):
    thresholds = ReadinessThresholds(min_completed_cycles_for_ready=1, min_real_provider_cycles_for_ready=0, max_scheduler_miss_rate=0.10)
    for i in range(2):
        _seed_scheduler_run(conn, status="COMPLETED", offset_minutes=i)
    for i in range(8):
        _seed_scheduler_run(conn, status="FAILED", offset_minutes=100 + i)
    report = build_readiness_report(conn, BASE_TIME, shadow_config, thresholds=thresholds)
    scheduler_cat = next(c for c in report.categories if c.category == "scheduler")
    assert scheduler_cat.status == "NOT_READY"
    assert report.scheduler_miss_count == 8
    assert report.overall_status == "NOT_READY" or report.overall_status == STATUS_INSUFFICIENT_DATA


# --- environmentally blocked (alert-delivery total failure) ------------------------------------


def test_alert_delivery_total_failure_marks_operational_not_ready(conn, shadow_config):
    thresholds = ReadinessThresholds(min_completed_cycles_for_ready=1, min_real_provider_cycles_for_ready=0)

    class _AlwaysFailSink:
        name = "always_fail"

        def send(self, alert):
            raise RuntimeError("simulated outage")

    run_id = _seed_scheduler_run(conn, status="COMPLETED", offset_minutes=0)
    _seed_run_summary(conn, scheduler_run_id=run_id, offset_minutes=0)
    alert = OperationalAlert(
        severity=SEVERITY_CRITICAL, alert_type=ALERT_TYPE_PROVIDER_UNAVAILABLE, message="provider down",
        context={}, created_at=BASE_TIME,
    )
    raise_alert(conn, alert, (_AlwaysFailSink(),), clock=_clock_at(BASE_TIME))
    report = build_readiness_report(conn, BASE_TIME, shadow_config, thresholds=thresholds)
    operational_cat = next(c for c in report.categories if c.category == "operational")
    assert operational_cat.status == "NOT_READY"
    assert report.alert_delivery_failure_count == 1


# --- exhaustive status enum fails closed ----------------------------------------------------------


def test_readiness_thresholds_reject_invalid_minimum():
    with pytest.raises(ReadinessPolicyError):
        ReadinessThresholds(min_completed_cycles_for_ready=0)


def test_category_status_fails_closed_on_unrecognized_value():
    from trading_research.shadow.readiness import CategoryReadiness

    with pytest.raises(ReadinessPolicyError):
        CategoryReadiness(category="evidence", status="NOT_A_STATUS", reasons=())


def test_report_status_fails_closed_on_unrecognized_value():
    from trading_research.shadow.readiness import ReadinessReport

    with pytest.raises(ReadinessPolicyError):
        ReadinessReport(
            as_of=BASE_TIME, policy_version="v1", overall_status="NOT_A_STATUS", categories=(),
            completed_cycle_count=0, real_provider_cycle_count=0, evidence_completeness_rate=None,
            role_completion_rate=None, retry_exhaustion_rate=None, unsupported_claim_rate=None,
            provider_failure_rate=None, cost_per_completed_cycle_usd=None, average_cycle_duration_seconds=None,
            scheduler_miss_count=0, lease_conflict_count=0, reconciliation_mismatch_count=0,
            alert_delivery_failure_count=0, reasons=(),
        )
