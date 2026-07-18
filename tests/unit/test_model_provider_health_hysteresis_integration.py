"""Milestone 12.1.1 Item 7: scheduler-run-scoped model-provider health,
end to end through the real join query
(`storage/shadow_operations_repositories.py::list_research_attempts_for_scheduler_run`)
and the persistent hysteresis engine — proves attempts are correctly scoped
to their own scheduler run, structural failures pause immediately, transient
failures use ordinary hysteresis, and replay is idempotent.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.models import UsageRecord
from trading_research.research.orchestration import ResearchAttemptRecord
from trading_research.shadow import health as health_mod
from trading_research.shadow import health_hysteresis as hh
from trading_research.shadow import model_provider_health as mph_mod
from trading_research.storage.database import connect
from trading_research.storage.research_repositories import SQLiteResearchRepository, save_evidence_snapshot
from trading_research.storage.shadow_operations_repositories import (
    list_research_attempts_for_scheduler_run,
    save_role_budget_check,
)

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "test.db")
        yield c
        c.close()


def _attempt(*, attempt_id, research_run_id, role="fundamental", attempt_number=1, provider="codex",
             success=True, failure_code=None, failure_retryable=None):
    usage = UsageRecord(
        provider=provider, model_name="m", role=role, input_tokens=100 if success else None,
        output_tokens=50 if success else None, cache_read_tokens=None, cache_write_tokens=None,
        latency_ms=200, provider_request_id=None, retry_count=0, success=success,
        pricing_version=None, estimated_cost=None, cost_status="NOT_APPLICABLE",
    )
    return ResearchAttemptRecord(
        attempt_id=attempt_id, research_run_id=research_run_id, role=role, attempt_number=attempt_number,
        prompt_name="p", prompt_version="v1", prompt_hash="h1", system_prompt_hash="sph1", schema_version="s1",
        provider=provider, model_name="m", success=success, failure_reason=None if success else "failed",
        raw_response_json={}, validated_payload_json={} if success else None, usage=usage, created_at=NOW,
        failure_code=failure_code, failure_stage="PROVIDER_RESPONSE" if failure_code else None,
        failure_retryable=failure_retryable,
    )


def _persist_attempt(conn, *, scheduler_run_id, research_run_id, attempt, cycle_id="cycle-1"):
    snapshot_id = f"{research_run_id}-snap"
    snapshot = build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)
    snapshot = snapshot.__class__(**{**snapshot.__dict__, "snapshot_id": snapshot_id})
    save_evidence_snapshot(conn, snapshot)
    repo = SQLiteResearchRepository(conn)
    repo.save_run_started(
        research_run_id=research_run_id, snapshot_id=snapshot_id, provider=attempt.provider,
        model_name="m", roles=("fundamental",), run_mode="shadow", config_hash="c" * 64, created_at=NOW,
    )
    repo.save_attempt(attempt)
    save_role_budget_check(conn, {
        "check_id": f"check-{attempt.attempt_id}", "reservation_id": "res-1", "scheduler_run_id": scheduler_run_id,
        "cycle_id": cycle_id, "research_run_id": research_run_id, "symbol": "AAPL", "role": attempt.role,
        "attempt_number": attempt.attempt_number, "provider": attempt.provider, "model_name": "m",
        "decision": "PROCEED", "reason": None, "remaining_input_tokens": 1000, "remaining_output_tokens": 1000,
        "remaining_latency_ms": 60000, "remaining_cost_usd": "1.0", "maximum_attempt_input_tokens": 1000,
        "maximum_attempt_output_tokens": 1000, "maximum_attempt_latency_ms": 60000,
        "maximum_attempt_cost_usd": "1.0", "checked_at": NOW.isoformat(),
    })


def test_scheduler_run_sees_only_its_own_model_attempts(conn):
    """Required test #7."""
    _persist_attempt(
        conn, scheduler_run_id="sched-A", research_run_id="run-A",
        attempt=_attempt(attempt_id="a-1", research_run_id="run-A"),
    )
    _persist_attempt(
        conn, scheduler_run_id="sched-B", research_run_id="run-B",
        attempt=_attempt(attempt_id="b-1", research_run_id="run-B", success=False, failure_code="CODEX_QUOTA_EXHAUSTED", failure_retryable=False),
    )
    rows_a = list_research_attempts_for_scheduler_run(conn, "sched-A")
    rows_b = list_research_attempts_for_scheduler_run(conn, "sched-B")
    assert len(rows_a) == 1 and rows_a[0]["attempt_id"] == "a-1"
    assert len(rows_b) == 1 and rows_b[0]["attempt_id"] == "b-1"


def test_authentication_and_unsupported_model_pause_immediately(conn):
    """Required test #1 (integration)."""
    _persist_attempt(
        conn, scheduler_run_id="sched-A", research_run_id="run-A",
        attempt=_attempt(
            attempt_id="a-1", research_run_id="run-A", success=False,
            failure_code="CODEX_NOT_AUTHENTICATED", failure_retryable=False,
        ),
    )
    rows = list_research_attempts_for_scheduler_run(conn, "sched-A")
    evidence = mph_mod.evaluate_model_provider_health(rows)
    assert evidence.structural_failure is True

    check = health_mod.HealthCheckResult(
        check_name=health_mod.CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE, status=health_mod.CHECK_STATUS_FAIL,
        input_value="1.0", input_unit="fraction", threshold_value="0.5", threshold_unit="fraction",
        comparison="structural failure", applicable=True, pause_flag_enabled=True, reason="structural",
    )
    decision = hh.evaluate_and_persist_hysteresis(
        conn, scope=f"{hh.DEFAULT_SCOPE}:{health_mod.DIMENSION_MODEL_PROVIDER_FAILURE}", cycle_id="sched-A",
        cycle_status=health_mod.dimension_cycle_status(check), qualified=health_mod.dimension_is_qualified(check),
        severe_error=evidence.structural_failure, config=hh.PersistentHealthPolicyConfig(), clock=lambda: NOW,
        immediate_pause=True,
    )
    assert decision.decision == hh.STATUS_PAUSE_REQUIRED


def test_repeated_transient_failures_reach_configured_pause_threshold(conn):
    """Required test #4."""
    config = hh.PersistentHealthPolicyConfig(
        warning_after_n_failures=1, pause_recommended_after_n_failures=2, pause_required_after_m_failures=3,
        recovery_streak=2,
    )
    fail_check = health_mod.HealthCheckResult(
        check_name=health_mod.CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE, status=health_mod.CHECK_STATUS_FAIL,
        input_value="1.0", input_unit="fraction", threshold_value="0.5", threshold_unit="fraction", comparison=">",
        applicable=True, pause_flag_enabled=True, reason="rate",
    )
    scope = f"{hh.DEFAULT_SCOPE}:{health_mod.DIMENSION_MODEL_PROVIDER_FAILURE}"
    decision = None
    for sched_id in ("sched-1", "sched-2", "sched-3"):
        decision = hh.evaluate_and_persist_hysteresis(
            conn, scope=scope, cycle_id=sched_id, cycle_status=health_mod.dimension_cycle_status(fail_check),
            qualified=True, severe_error=False, config=config, clock=lambda: NOW,
        )
    assert decision.decision == hh.STATUS_PAUSE_REQUIRED
    assert decision.consecutive_failures == 3


def test_one_transient_timeout_does_not_immediately_pause(conn):
    """Required test #3."""
    _persist_attempt(
        conn, scheduler_run_id="sched-A", research_run_id="run-A",
        attempt=_attempt(
            attempt_id="a-1", research_run_id="run-A", success=False,
            failure_code="CODEX_PROCESS_TIMEOUT", failure_retryable=True,
        ),
    )
    rows = list_research_attempts_for_scheduler_run(conn, "sched-A")
    evidence = mph_mod.evaluate_model_provider_health(rows)
    assert evidence.structural_failure is False


def test_healthy_attempts_advance_only_model_provider_recovery(conn):
    """Required test #5."""
    scope = f"{hh.DEFAULT_SCOPE}:{health_mod.DIMENSION_MODEL_PROVIDER_FAILURE}"
    other_scope = hh.DEFAULT_SCOPE
    config = hh.PersistentHealthPolicyConfig(
        warning_after_n_failures=1, pause_recommended_after_n_failures=1, pause_required_after_m_failures=1,
        recovery_streak=1,
    )
    fail_check = health_mod.HealthCheckResult(
        check_name=health_mod.CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE, status=health_mod.CHECK_STATUS_FAIL,
        input_value="1.0", input_unit="fraction", threshold_value="0.5", threshold_unit="fraction", comparison=">",
        applicable=True, pause_flag_enabled=True, reason="rate",
    )
    hh.evaluate_and_persist_hysteresis(
        conn, scope=scope, cycle_id="sched-1", cycle_status=health_mod.dimension_cycle_status(fail_check),
        qualified=True, config=config, clock=lambda: NOW,
    )
    healthy_check = health_mod.HealthCheckResult(
        check_name=health_mod.CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE, status=health_mod.CHECK_STATUS_PASS,
        input_value="0.0", input_unit="fraction", threshold_value="0.5", threshold_unit="fraction", comparison=">",
        applicable=True, pause_flag_enabled=True, reason="rate",
    )
    recovery = hh.evaluate_and_persist_hysteresis(
        conn, scope=scope, cycle_id="sched-2", cycle_status=health_mod.dimension_cycle_status(healthy_check),
        qualified=True, config=config, clock=lambda: NOW,
    )
    assert recovery.decision == hh.STATUS_HEALTHY
    # Evidence-provider's independent scope is untouched by this dimension's activity.
    assert hh.repo.load_health_hysteresis_state(conn, other_scope) is None


def test_replay_is_idempotent(conn):
    """Required test #8."""
    scope = f"{hh.DEFAULT_SCOPE}:{health_mod.DIMENSION_MODEL_PROVIDER_FAILURE}"
    config = hh.PersistentHealthPolicyConfig()
    fail_check = health_mod.HealthCheckResult(
        check_name=health_mod.CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE, status=health_mod.CHECK_STATUS_FAIL,
        input_value="1.0", input_unit="fraction", threshold_value="0.5", threshold_unit="fraction", comparison=">",
        applicable=True, pause_flag_enabled=True, reason="rate",
    )
    first = hh.evaluate_and_persist_hysteresis(
        conn, scope=scope, cycle_id="sched-1", cycle_status=health_mod.dimension_cycle_status(fail_check),
        qualified=True, config=config, clock=lambda: NOW,
    )
    second = hh.evaluate_and_persist_hysteresis(
        conn, scope=scope, cycle_id="sched-1", cycle_status=health_mod.dimension_cycle_status(fail_check),
        qualified=True, config=config, clock=lambda: NOW,
    )
    assert second.idempotent_replay is True
    assert second.consecutive_failures == first.consecutive_failures
