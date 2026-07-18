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

from datetime import datetime, timedelta, timezone

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
