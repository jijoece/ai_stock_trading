"""Milestone 12.1.1 Item 7: scheduler-run-scoped model-provider health,
end to end through the real join query
(`storage/shadow_operations_repositories.py::list_research_attempts_for_scheduler_run`)
and the persistent hysteresis engine — proves attempts are correctly scoped
to their own scheduler run, structural failures pause immediately, transient
failures use ordinary hysteresis, and replay is idempotent.
"""
from __future__ import annotations

import tempfile
from dataclasses import replace
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
    attempt = replace(
        attempt, scheduler_run_id=scheduler_run_id, research_cycle_id=cycle_id,
        attempt_control_check_id=f"check-{attempt.attempt_id}", correlation_mode="SCHEDULED",
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


def _evaluate(rows, *, provider="codex", model="m", config_hash="c" * 64):
    return mph_mod.evaluate_model_provider_health(
        rows, expected_provider=provider, expected_model=model,
        provider_configuration_hash=config_hash,
    )


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


def test_same_research_identifiers_are_isolated_by_direct_scheduler_ownership(conn):
    """Critical regression: reusable research identifiers cannot cross runs."""
    shared_run = "cycle-X-AAPL"
    _persist_attempt(
        conn, scheduler_run_id="sched-A", research_run_id=shared_run, cycle_id="cycle-X",
        attempt=_attempt(attempt_id="attempt-A", research_run_id=shared_run, success=True),
    )
    _persist_attempt(
        conn, scheduler_run_id="sched-B", research_run_id=shared_run, cycle_id="cycle-X",
        attempt=_attempt(
            attempt_id="attempt-B", research_run_id=shared_run, success=False,
            failure_code="CODEX_QUOTA_EXHAUSTED", failure_retryable=False,
        ),
    )
    repo = SQLiteResearchRepository(conn)
    repo.save_attempt(_attempt(attempt_id="attempt-manual", research_run_id=shared_run, success=True))
    repo.save_attempt(replace(
        _attempt(attempt_id="attempt-legacy", research_run_id=shared_run, success=True),
        correlation_mode="LEGACY_UNKNOWN",
    ))

    rows_a = list_research_attempts_for_scheduler_run(conn, "sched-A")
    rows_b = list_research_attempts_for_scheduler_run(conn, "sched-B")
    assert [row["attempt_id"] for row in rows_a] == ["attempt-A"]
    assert [row["attempt_id"] for row in rows_b] == ["attempt-B"]
    assert _evaluate(rows_a).success_rate == 1.0
    assert _evaluate(rows_b).structural_failure is True


def test_retry_attempts_keep_ownership_and_order_deterministically(conn):
    for attempt_id, number in (("z-retry", 2), ("a-first", 1)):
        _persist_attempt(
            conn, scheduler_run_id="sched-A", research_run_id="shared-run", cycle_id="cycle-X",
            attempt=_attempt(
                attempt_id=attempt_id, research_run_id="shared-run", attempt_number=number,
                success=False, failure_code="CODEX_PROCESS_TIMEOUT", failure_retryable=True,
            ),
        )
    rows = list_research_attempts_for_scheduler_run(conn, "sched-A")
    assert [row["attempt_id"] for row in rows] == ["a-first", "z-retry"]
    assert all(row["scheduler_run_id"] == "sched-A" for row in rows)


def test_budget_gated_attempt_is_not_a_provider_invocation(conn):
    attempt = replace(
        _attempt(
            attempt_id="gated", research_run_id="run-gated", success=False,
            failure_code=None, failure_retryable=False,
        ),
        failure_stage="BUDGET_GATED",
    )
    _persist_attempt(
        conn, scheduler_run_id="sched-A", research_run_id="run-gated", attempt=attempt,
    )
    assert list_research_attempts_for_scheduler_run(conn, "sched-A") == []


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
    evidence = _evaluate(rows)
    assert evidence.structural_failure is True

    check = health_mod.HealthCheckResult(
        check_name=health_mod.CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE, status=health_mod.CHECK_STATUS_FAIL,
        input_value="1.0", input_unit="fraction", threshold_value="0.5", threshold_unit="fraction",
        comparison="structural failure", applicable=True, pause_flag_enabled=True, reason="structural",
    )
    decision = hh.evaluate_and_persist_hysteresis(
        conn, scope=mph_mod.model_provider_health_scope(
            expected_provider="codex", expected_model="m", provider_configuration_hash="c" * 64,
        ), cycle_id="sched-A",
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
    scope = mph_mod.model_provider_health_scope(
        expected_provider="codex", expected_model="m", provider_configuration_hash="c" * 64,
    )
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
    evidence = _evaluate(rows)
    assert evidence.structural_failure is False


def test_healthy_attempts_advance_only_model_provider_recovery(conn):
    """Required test #5."""
    scope = mph_mod.model_provider_health_scope(
        expected_provider="codex", expected_model="m", provider_configuration_hash="c" * 64,
    )
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
    scope = mph_mod.model_provider_health_scope(
        expected_provider="codex", expected_model="m", provider_configuration_hash="c" * 64,
    )
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


def test_provider_switch_success_cannot_recover_codex_failure_scope(conn):
    config = hh.PersistentHealthPolicyConfig(recovery_streak=1)
    codex_scope = mph_mod.model_provider_health_scope(
        expected_provider="codex", expected_model="gpt-test", provider_configuration_hash="a" * 64,
    )
    deterministic_scope = mph_mod.model_provider_health_scope(
        expected_provider="deterministic", expected_model="deterministic-v1",
        provider_configuration_hash="b" * 64,
    )
    anthropic_scope = mph_mod.model_provider_health_scope(
        expected_provider="anthropic", expected_model="claude-test", provider_configuration_hash="c" * 64,
    )
    hh.evaluate_and_persist_hysteresis(
        conn, scope=codex_scope, cycle_id="run-A", cycle_status=health_mod.STATUS_PAUSE_REQUIRED,
        qualified=True, config=config, clock=lambda: NOW,
    )
    hh.evaluate_and_persist_hysteresis(
        conn, scope=deterministic_scope, cycle_id="run-B", cycle_status=health_mod.STATUS_HEALTHY,
        qualified=False, config=config, clock=lambda: NOW,
    )
    hh.evaluate_and_persist_hysteresis(
        conn, scope=anthropic_scope, cycle_id="run-C", cycle_status=health_mod.STATUS_HEALTHY,
        qualified=True, config=config, clock=lambda: NOW,
    )
    codex_state = hh.repo.load_health_hysteresis_state(conn, codex_scope)
    assert codex_state["consecutive_failures"] == 1
    assert codex_state["consecutive_recoveries"] == 0
