"""Unit tests for `research/cycle_telemetry.py` + `storage/
research_repositories.py::compute_cycle_telemetry` (docs/milestone-7.1.md
Step 16): derived entirely from persisted `research_attempts`/
`research_attempt_failures` rows, never a duplicated in-memory counter.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.research.cycle_telemetry import STATUS_COMPLETE, STATUS_PARTIAL, STATUS_UNAVAILABLE
from trading_research.research.deterministic_provider import ScriptedResearchProvider, ScriptedStep
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.orchestration import analyze_with_research_committee
from trading_research.research.prompt_registry import PromptRegistry
from trading_research.storage.database import connect
from trading_research.storage.research_repositories import SQLiteResearchRepository, compute_cycle_telemetry, save_evidence_snapshot
from tests.unit.test_attempt_control_hooks import ANALYST_PAYLOAD, MANAGER_PAYLOAD, _config

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "test.db")
        yield c
        c.close()


def _snapshot():
    return build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)


def test_unavailable_status_when_no_research_run_ids(conn):
    telemetry = compute_cycle_telemetry(conn, ())
    assert telemetry.status == STATUS_UNAVAILABLE
    assert telemetry.attempt_count == 0
    assert telemetry.input_tokens is None
    assert telemetry.output_tokens is None
    assert telemetry.priced_usage_cost_usd is None


def test_complete_status_for_a_successful_run(conn):
    snapshot = _snapshot()
    save_evidence_snapshot(conn, snapshot)
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD, usage_overrides={"input_tokens": 100, "output_tokens": 50}),
        ("manager", 1): ScriptedStep(kind="response", payload=MANAGER_PAYLOAD, usage_overrides={"input_tokens": 100, "output_tokens": 50}),
    })
    repo = SQLiteResearchRepository(conn)
    result = analyze_with_research_committee(
        snapshot, provider=provider, provider_name="scripted", model_name="test-model", prompt_registry=PromptRegistry(),
        research_repository=repo, configuration=_config(), clock=lambda: NOW, run_mode="scripted",
    )
    telemetry = compute_cycle_telemetry(conn, (result.research_run_id,))
    assert telemetry.status == STATUS_COMPLETE
    assert telemetry.attempt_count == 2
    assert telemetry.successful_attempt_count == 2
    assert telemetry.failed_attempt_count == 0
    assert telemetry.retry_count == 0
    assert telemetry.input_tokens == 200
    assert telemetry.output_tokens == 100
    assert telemetry.missing_usage_record_count == 0


def test_partial_status_with_retries_and_retry_exhaustion(conn):
    snapshot = _snapshot()
    save_evidence_snapshot(conn, snapshot)
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="malformed", raw_text="bad"),
        ("fundamental", 2): ScriptedStep(kind="malformed", raw_text="still bad"),
    })
    repo = SQLiteResearchRepository(conn)
    result = analyze_with_research_committee(
        snapshot, provider=provider, provider_name="scripted", model_name="test-model", prompt_registry=PromptRegistry(),
        research_repository=repo, configuration=_config(max_attempts_per_role=2), clock=lambda: NOW, run_mode="scripted",
    )
    telemetry = compute_cycle_telemetry(conn, (result.research_run_id,))
    assert telemetry.status == STATUS_PARTIAL
    assert telemetry.attempt_count == 2
    assert telemetry.retry_count == 1  # attempt_number=2
    assert telemetry.retry_exhaustion_count == 1
    assert telemetry.required_role_failure_count == 1


def test_unknown_cost_remains_unknown_not_fabricated_zero(conn):
    """`ScriptedResearchProvider` uses `provider="scripted"`, which has no
    pricing entry available in this test — cost stays unresolved, never a
    fabricated zero."""
    snapshot = _snapshot()
    save_evidence_snapshot(conn, snapshot)
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
        ("manager", 1): ScriptedStep(kind="response", payload=MANAGER_PAYLOAD),
    })
    repo = SQLiteResearchRepository(conn)
    result = analyze_with_research_committee(
        snapshot, provider=provider, provider_name="scripted", model_name="test-model", prompt_registry=PromptRegistry(),
        research_repository=repo, configuration=_config(), clock=lambda: NOW, run_mode="scripted",
    )
    telemetry = compute_cycle_telemetry(conn, (result.research_run_id,))
    assert telemetry.pricing_status == "PRICING_NOT_CONFIGURED"
    assert telemetry.priced_usage_cost_usd is None


def test_budget_skip_distinguished_from_provider_failure(conn):
    from trading_research.research.orchestration import AttemptControlDecision

    class _DenyManagerController:
        def before_attempt(self, request):
            if request.role == "manager":
                return AttemptControlDecision(allowed=False, code="SKIPPED_BUDGET_EXHAUSTED", reason="test")
            return AttemptControlDecision(allowed=True, code="PROCEED")

        def after_attempt(self, request, attempt):
            pass

    snapshot = _snapshot()
    save_evidence_snapshot(conn, snapshot)
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
    })
    repo = SQLiteResearchRepository(conn)
    result = analyze_with_research_committee(
        snapshot, provider=provider, provider_name="scripted", model_name="test-model", prompt_registry=PromptRegistry(),
        research_repository=repo, configuration=_config(), clock=lambda: NOW, run_mode="scripted",
        attempt_controller=_DenyManagerController(),
    )
    telemetry = compute_cycle_telemetry(conn, (result.research_run_id,))
    assert telemetry.budget_skipped_attempt_count == 1
    assert telemetry.provider_failure_count == 0
