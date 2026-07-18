"""Milestone 12.1 Item 7: scheduled provider telemetry is scoped by BOTH
`research_cycle_id` and `scheduler_run_id` for operational health decisions.

Scheduler resumption identity policy (documented here per the milestone's
"define what happens when a scheduler run resumes after a process crash"):
this repository generates a brand-new `scheduler_run_id`
(`f"shadow-run-{uuid.uuid4().hex}"`) at the start of every
`run_scheduled_cycle()` invocation — there is no persisted "resume the same
scheduler_run_id" path. A process restart after a crash therefore always
produces a NEW scheduler_run_id / a NEW operational attempt, even if it
revisits the same deterministic `research_cycle_id`. Telemetry follows this
model exactly: `list_provider_requests_for_scheduled_run` scopes strictly by
the (cycle, run) pair, so run A's health decision never sees run B's
requests even when both target the same cycle.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.evidence_providers.persistence import (
    CORRELATION_MANUAL,
    CORRELATION_RESEARCH_CYCLE,
    CORRELATION_SCHEDULED,
    ProviderRequestRecord,
    list_provider_requests_for_cycle,
    list_provider_requests_for_scheduled_run,
    save_provider_request,
)
from trading_research.storage.database import connect
from trading_research.research.deterministic_provider import ScriptedResearchProvider, ScriptedStep
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.failure_taxonomy import (
    CODE_UNSUPPORTED_MATERIAL_CLAIM,
    STAGE_CLAIM_EVIDENCE_VALIDATION,
    new_failure,
)
from trading_research.research.orchestration import AttemptControlDecision, analyze_with_research_committee
from trading_research.research.prompt_registry import PromptRegistry
from trading_research.research.scheduled_cycle import ResearchCycleResult, SymbolCycleResult
from trading_research.shadow.scheduler import _build_health_inputs_from_cycle_result
from trading_research.storage.research_repositories import (
    SQLiteResearchRepository,
    compute_cycle_telemetry,
    compute_scheduled_run_telemetry,
    save_evidence_snapshot,
)
from tests.unit.test_attempt_control_hooks import ANALYST_PAYLOAD, _config

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def _record(**overrides) -> ProviderRequestRecord:
    defaults = dict(
        provider="alpaca-data", operation="get_bars", symbol="AAPL", requested_as_of=NOW, retrieved_at=NOW,
        provider_response_timestamp=None, http_status=200, content_hash=None, normalized_record_hash=None,
        cache_status="MISS", rate_limited=False, retry_count=0, latency_ms=50, success=True, error_code=None,
        retryable=None, licensing_classification="ACCOUNT_LINKED",
    )
    defaults.update(overrides)
    return ProviderRequestRecord(**defaults)


def test_scheduler_run_a_sees_only_run_a(conn):
    save_provider_request(conn, _record(
        correlation_mode=CORRELATION_SCHEDULED, research_cycle_id="cycle-1", scheduler_run_id="run-A",
    ))
    save_provider_request(conn, _record(
        correlation_mode=CORRELATION_SCHEDULED, research_cycle_id="cycle-1", scheduler_run_id="run-B",
    ))
    rows_a = list_provider_requests_for_scheduled_run(conn, research_cycle_id="cycle-1", scheduler_run_id="run-A")
    assert len(rows_a) == 1
    assert rows_a[0]["scheduler_run_id"] == "run-A"


def test_scheduler_run_b_sees_only_run_b(conn):
    save_provider_request(conn, _record(
        correlation_mode=CORRELATION_SCHEDULED, research_cycle_id="cycle-1", scheduler_run_id="run-A",
    ))
    save_provider_request(conn, _record(
        correlation_mode=CORRELATION_SCHEDULED, research_cycle_id="cycle-1", scheduler_run_id="run-B",
    ))
    rows_b = list_provider_requests_for_scheduled_run(conn, research_cycle_id="cycle-1", scheduler_run_id="run-B")
    assert len(rows_b) == 1
    assert rows_b[0]["scheduler_run_id"] == "run-B"


def test_same_cycle_id_across_two_runs_remains_separated(conn):
    """Required tests #1-3: overlapping/repeated runs against the SAME
    deterministic cycle_id stay fully isolated from each other."""
    for run_id in ("run-A", "run-B"):
        save_provider_request(conn, _record(
            correlation_mode=CORRELATION_SCHEDULED, research_cycle_id="cycle-shared", scheduler_run_id=run_id,
            provider="sec-edgar",
        ))
    rows_a = list_provider_requests_for_scheduled_run(conn, research_cycle_id="cycle-shared", scheduler_run_id="run-A")
    rows_b = list_provider_requests_for_scheduled_run(conn, research_cycle_id="cycle-shared", scheduler_run_id="run-B")
    assert {r["request_id"] for r in rows_a}.isdisjoint({r["request_id"] for r in rows_b})
    assert len(rows_a) == 1 and len(rows_b) == 1


def test_manual_cycle_requests_do_not_enter_scheduled_health(conn):
    """Required test #4."""
    save_provider_request(conn, _record(correlation_mode=CORRELATION_MANUAL))
    save_provider_request(conn, _record(correlation_mode=CORRELATION_RESEARCH_CYCLE, research_cycle_id="cycle-1"))
    rows = list_provider_requests_for_scheduled_run(conn, research_cycle_id="cycle-1", scheduler_run_id="run-A")
    assert rows == []


def test_catch_up_invocation_remains_isolated(conn):
    """Required test #5: a later scheduler invocation (simulating a
    resumed/catch-up run with a brand-new scheduler_run_id per this
    repository's resumption policy) never sees the earlier run's requests."""
    save_provider_request(conn, _record(
        correlation_mode=CORRELATION_SCHEDULED, research_cycle_id="cycle-1", scheduler_run_id="original-run",
        retrieved_at=NOW,
    ))
    save_provider_request(conn, _record(
        correlation_mode=CORRELATION_SCHEDULED, research_cycle_id="cycle-1", scheduler_run_id="catch-up-run",
        retrieved_at=NOW + timedelta(minutes=30),
    ))
    catch_up_rows = list_provider_requests_for_scheduled_run(
        conn, research_cycle_id="cycle-1", scheduler_run_id="catch-up-run",
    )
    assert len(catch_up_rows) == 1
    assert catch_up_rows[0]["scheduler_run_id"] == "catch-up-run"


def test_missing_scheduler_id_fails_closed_in_scheduled_mode(conn):
    """Required test #7."""
    with pytest.raises(ValueError, match="scheduled provider requests require"):
        save_provider_request(conn, _record(correlation_mode=CORRELATION_SCHEDULED, research_cycle_id="cycle-1"))


def test_cycle_wide_report_can_still_aggregate_both_runs_intentionally(conn):
    """Required test #8: `list_provider_requests_for_cycle` remains the
    correct query for historical/aggregate reporting across every run."""
    for run_id in ("run-A", "run-B"):
        save_provider_request(conn, _record(
            correlation_mode=CORRELATION_SCHEDULED, research_cycle_id="cycle-shared", scheduler_run_id=run_id,
        ))
    aggregate_rows = list_provider_requests_for_cycle(conn, "cycle-shared")
    assert len(aggregate_rows) == 2


class _ScheduledAttemptController:
    def __init__(self, scheduler_run_id: str):
        self.scheduler_run_id = scheduler_run_id

    def before_attempt(self, request):
        return AttemptControlDecision(
            allowed=True, code="PROCEED", correlation_mode="SCHEDULED",
            scheduler_run_id=self.scheduler_run_id, research_cycle_id="cycle-X",
            attempt_control_check_id=f"{self.scheduler_run_id}-{request.role}-{request.attempt_number}",
        )

    def after_attempt(self, request, attempt):
        pass


def _run_research_attempts(scheduler_run_id: str, steps, *, max_attempts: int = 1):
    provider = ScriptedResearchProvider(steps)
    result = analyze_with_research_committee(
        build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW),
        provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=None,
        configuration=_config(roles=("fundamental",), max_attempts_per_role=max_attempts),
        clock=lambda: NOW, run_mode="scripted",
        attempt_controller=_ScheduledAttemptController(scheduler_run_id), require_decision=False,
    )
    return result


def test_scheduled_research_telemetry_isolated_by_scheduler_run(conn):
    """Reusable research IDs cannot mix scheduled attempts, failures, or usage."""
    run_a = _run_research_attempts("sched-A", {
        ("fundamental", 1): ScriptedStep(
            kind="response", payload=ANALYST_PAYLOAD,
            usage_overrides={
                "input_tokens": 101, "output_tokens": 51, "latency_ms": 11,
                "pricing_version": "test.v1", "estimated_cost": Decimal("1.25"),
                "cost_status": "CALCULATED", "cost_estimate_basis": "DIRECT_API_ESTIMATE",
            },
        ),
    })
    unsupported_payload = {
        **ANALYST_PAYLOAD,
        "claims": [{
            "claim_id": "unsupported", "claim_type": "downside_estimate", "statement": "unsupported",
            "evidence_ids": ["missing-evidence"], "numeric_value": None, "unit": None, "importance": "high",
        }],
    }
    run_b = _run_research_attempts("sched-B", {
        ("fundamental", 1): ScriptedStep(
            kind="response", payload=unsupported_payload,
            usage_overrides={
                "input_tokens": 202, "output_tokens": 52, "latency_ms": 22,
                "pricing_version": "test.v1", "estimated_cost": Decimal("2.50"),
                "cost_status": "CALCULATED", "cost_estimate_basis": "DIRECT_API_ESTIMATE",
            },
        ),
    })
    run_b = dataclasses.replace(
        run_b,
        failures=run_b.failures + (new_failure(
            research_run_id=run_b.research_run_id, attempt_id=run_b.attempts[0].attempt_id,
            role="fundamental", attempt_number=1, stage=STAGE_CLAIM_EVIDENCE_VALIDATION,
            code=CODE_UNSUPPORTED_MATERIAL_CLAIM, message="unsupported material claim",
            retryable=True, model_name="test-model", prompt_version=run_b.attempts[0].prompt_version,
            schema_version=run_b.attempts[0].schema_version, occurred_at=NOW,
        ),),
    )
    run_c = _run_research_attempts("sched-C", {
        ("fundamental", 1): ScriptedStep(
            kind="malformed", raw_text="bad", retryable=False, code="CODEX_USAGE_METADATA_MISSING",
        ),
    }, max_attempts=3)

    assert run_a.research_run_id == run_b.research_run_id == run_c.research_run_id
    snapshot = build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)
    save_evidence_snapshot(conn, snapshot)
    repo = SQLiteResearchRepository(conn)
    repo.save_run_started(
        run_a.research_run_id, snapshot.snapshot_id, "scripted", "test-model", ("fundamental",),
        "scripted", "c" * 64, NOW,
    )
    for result in (run_a, run_b, run_c):
        for attempt in result.attempts:
            if attempt.scheduler_run_id == "sched-A":
                attempt = dataclasses.replace(
                    attempt,
                    usage=dataclasses.replace(
                        attempt.usage, pricing_version="test.v1", estimated_cost=Decimal("1.25"),
                        cost_status="CALCULATED", cost_estimate_basis="DIRECT_API_ESTIMATE",
                    ),
                )
            elif attempt.scheduler_run_id == "sched-B":
                attempt = dataclasses.replace(
                    attempt,
                    usage=dataclasses.replace(
                        attempt.usage, pricing_version="test.v1", estimated_cost=Decimal("2.50"),
                        cost_status="CALCULATED", cost_estimate_basis="DIRECT_API_ESTIMATE",
                    ),
                )
            repo.save_attempt(attempt)
        repo.save_attempt_failures(result.failures)

    # Same research ID, but non-scheduled correlation modes must never enter
    # either scheduled run's ownership boundary.
    manual_attempt = dataclasses.replace(
        run_a.attempts[0], attempt_id="manual-attempt", correlation_mode="MANUAL",
        scheduler_run_id=None, research_cycle_id=None, attempt_control_check_id=None,
    )
    legacy_attempt = dataclasses.replace(
        run_a.attempts[0], attempt_id="legacy-attempt", correlation_mode="LEGACY_UNKNOWN",
        scheduler_run_id=None, research_cycle_id=None, attempt_control_check_id=None,
    )
    repo.save_attempt(manual_attempt)
    repo.save_attempt(legacy_attempt)

    telemetry_a = compute_scheduled_run_telemetry(
        conn, scheduler_run_id="sched-A", research_run_ids=(run_a.research_run_id,),
    )
    telemetry_b = compute_scheduled_run_telemetry(
        conn, scheduler_run_id="sched-B", research_run_ids=(run_b.research_run_id,),
    )
    telemetry_c = compute_scheduled_run_telemetry(
        conn, scheduler_run_id="sched-C", research_run_ids=(run_c.research_run_id,),
    )

    assert telemetry_a.attempt_count == 1
    assert telemetry_a.successful_attempt_count == 1
    assert telemetry_a.failed_attempt_count == 0
    assert telemetry_a.retry_exhaustion_count == 0
    assert telemetry_a.unsupported_claim_count == 0
    assert (telemetry_a.input_tokens, telemetry_a.output_tokens, telemetry_a.latency_ms) == (101, 51, 11)
    assert telemetry_a.priced_usage_cost_usd == Decimal("1.25")

    assert telemetry_b.attempt_count == 1
    assert telemetry_b.successful_attempt_count == 0
    assert telemetry_b.failed_attempt_count == 1
    assert telemetry_b.retry_exhaustion_count == 1
    assert telemetry_b.required_role_failure_count == 1
    assert telemetry_b.unsupported_claim_count == 1
    assert (telemetry_b.input_tokens, telemetry_b.output_tokens, telemetry_b.latency_ms) == (202, 52, 22)
    assert telemetry_b.priced_usage_cost_usd == Decimal("2.50")

    assert telemetry_c.attempt_count == 1
    assert telemetry_c.failed_attempt_count == 1
    assert telemetry_c.retry_exhaustion_count == 0
    assert telemetry_c.required_role_failure_count == 1

    # The historical/cycle-wide function intentionally retains its original
    # aggregate behavior, including manual and legacy attempts.
    aggregate = compute_cycle_telemetry(conn, (run_a.research_run_id,))
    assert aggregate.attempt_count == 5

    cycle_result = ResearchCycleResult(
        cycle_id="cycle-X", universe_id="test-universe", as_of=NOW, status="COMPLETED",
        symbol_results=(SymbolCycleResult(
            symbol="AAPL", status="COMPLETED", evidence_outcome="COMPLETE",
            research_run_id=run_c.research_run_id,
        ),),
        reused_existing_cycle=False,
    )
    health_inputs = _build_health_inputs_from_cycle_result(
        conn, cycle_result, symbols_attempted=1, cycle_duration_seconds=1.0,
        scheduler_run_id="sched-C",
    )
    assert health_inputs.claude_role_success_rate == 0.0
    assert health_inputs.retry_exhaustion_rate == 0.0
