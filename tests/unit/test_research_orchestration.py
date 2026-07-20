"""Category F: orchestrator tests (docs/milestone-5.md Step 20.F)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_research.research.configuration import ResearchConfiguration
from trading_research.research.deterministic_provider import ScriptedResearchProvider, ScriptedStep
from trading_research.research.errors import ManagerNotConfiguredError
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.orchestration import (
    RUN_STATUS_ANALYSIS_INCOMPLETE,
    RUN_STATUS_ANALYST_REPORTS_COMPLETE_NO_MANAGER,
    RUN_STATUS_COMPLETED,
    analyze_with_research_committee,
    compute_research_run_id,
)
from trading_research.research.prompt_registry import PromptRegistry
from trading_research.research.models import (
    TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT,
    TOKEN_ACCOUNTING_REASONING_SEPARATE,
)
from trading_research.storage.database import connect
from trading_research.storage.shadow_operations_repositories import list_budget_reservations
from trading_research.research.token_budget import PersistentResearchTokenBudgetController

from tests.support.research_fixtures import FakeResearchRepository

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _config(roles=("fundamental", "technical", "manager"), max_attempts_per_role=2) -> ResearchConfiguration:
    return ResearchConfiguration(
        version=1, enabled=True, provider="scripted", model="test-model", max_attempts_per_role=max_attempts_per_role,
        request_timeout_seconds=30, max_input_characters=100_000, max_evidence_items=100,
        max_items_per_source_category=25, max_claims_per_role=20, max_output_tokens=2000,
        require_point_in_time_safe=True, require_evidence_for_material_claims=True,
        fail_on_stale_required_evidence=True, allow_parallel_roles=False, roles=roles,
        overlay_policy_version="test.v1", overlay_allow_score_increase=False,
        overlay_allow_position_size_increase=False, overlay_incomplete_action="ANALYSIS_INCOMPLETE",
        overlay_critical_risk_action="FORCE_NO_ACTION", config_hash="c" * 64, raw={},
    )


def _snapshot():
    return build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)


ANALYST_REPORT_PAYLOAD = {
    "stance": "BULLISH", "summary": "growth", "claims": [], "catalysts": [], "risks": ["some risk"],
    "uncertainties": [], "missing_data_reasons": [],
}
MANAGER_PAYLOAD = {
    "rating": "OVERWEIGHT", "confidence": 0.6, "thesis": "t", "bull_case": "bull", "bear_case": "bear",
    "catalysts": [], "risks": ["some risk"], "invalidation_conditions": [], "claims": [], "evidence_ids": [],
    "missing_data_reasons": [],
}


def _happy_provider() -> ScriptedResearchProvider:
    return ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
        ("technical", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
        ("manager", 1): ScriptedStep(kind="response", payload=MANAGER_PAYLOAD),
    })


def test_deterministic_role_order():
    provider = _happy_provider()
    repo = FakeResearchRepository()
    analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )
    assert [c.role for c in provider.calls] == ["fundamental", "technical", "manager"]


def test_run_persisted_before_provider_invoked():
    provider = _happy_provider()
    repo = FakeResearchRepository()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )
    assert result.research_run_id in repo.runs
    assert repo.runs[result.research_run_id]["status"] == RUN_STATUS_COMPLETED


def test_reuse_of_completed_run_makes_no_new_provider_calls():
    provider = _happy_provider()
    repo = FakeResearchRepository()
    result1 = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )
    call_count_after_first = len(provider.calls)

    result2 = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )
    assert result2.reused_existing_run is True
    assert result2.research_run_id == result1.research_run_id
    assert len(provider.calls) == call_count_after_first  # no new provider calls


def test_duplicate_invocation_idempotency_same_run_id():
    run_id_1 = compute_research_run_id(
        snapshot_id=_snapshot().snapshot_id, provider_name="scripted", model_name="test-model",
        roles=("fundamental", "technical", "manager"), prompt_registry=PromptRegistry(), run_mode="scripted",
        config_hash="c" * 64,
    )
    run_id_2 = compute_research_run_id(
        snapshot_id=_snapshot().snapshot_id, provider_name="scripted", model_name="test-model",
        roles=("fundamental", "technical", "manager"), prompt_registry=PromptRegistry(), run_mode="scripted",
        config_hash="c" * 64,
    )
    assert run_id_1 == run_id_2


def test_manager_never_invoked_when_a_required_analyst_role_exhausts_retries():
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="malformed", raw_text="not json"),
        ("fundamental", 2): ScriptedStep(kind="malformed", raw_text="still not json"),
        ("technical", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
    })
    repo = FakeResearchRepository()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )
    assert result.status == RUN_STATUS_ANALYSIS_INCOMPLETE
    assert result.decision is None
    assert "manager" not in [c.role for c in provider.calls]
    assert any("fundamental" in reason for reason in result.incomplete_reasons)


def test_failed_role_excluded_from_valid_reports():
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="malformed", raw_text="bad"),
        ("fundamental", 2): ScriptedStep(kind="malformed", raw_text="still bad"),
        ("technical", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
    })
    repo = FakeResearchRepository()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )
    assert [r.role for r in result.role_reports] == ["technical"]


def test_resumption_after_interruption_skips_already_persisted_roles():
    repo = FakeResearchRepository()
    snapshot = _snapshot()
    run_id = compute_research_run_id(
        snapshot_id=snapshot.snapshot_id, provider_name="scripted", model_name="test-model",
        roles=("fundamental", "technical", "manager"), prompt_registry=PromptRegistry(), run_mode="scripted",
        config_hash="c" * 64,
    )
    # Simulate a prior interrupted run: fundamental already has a persisted report, run is RUNNING.
    repo.runs[run_id] = {"status": "RUNNING", "snapshot_id": snapshot.snapshot_id}
    from trading_research.research.output_validation import build_role_report

    existing_report = build_role_report(
        ANALYST_REPORT_PAYLOAD, report_id=f"{run_id}-fundamental-1", research_run_id=run_id, role="fundamental",
        symbol=snapshot.symbol, snapshot_id=snapshot.snapshot_id, model_name="test-model", prompt_version="v1",
    )
    repo.role_reports[(run_id, "fundamental")] = existing_report

    # Provider is only scripted for technical + manager — if the orchestrator
    # tried to re-invoke fundamental it would hit an AssertionError.
    provider = ScriptedResearchProvider({
        ("technical", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
        ("manager", 1): ScriptedStep(kind="response", payload=MANAGER_PAYLOAD),
    })
    result = analyze_with_research_committee(
        snapshot, provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )
    assert result.status == RUN_STATUS_COMPLETED
    assert "fundamental" not in [c.role for c in provider.calls]


def test_prompt_version_change_creates_a_new_run_id(tmp_path: Path):
    root_v1 = tmp_path / "prompts_v1"
    (root_v1 / "fundamental").mkdir(parents=True)
    (root_v1 / "fundamental" / "v1.txt").write_text("Original fundamental prompt.")

    root_v1_edited = tmp_path / "prompts_v1_edited"
    (root_v1_edited / "fundamental").mkdir(parents=True)
    (root_v1_edited / "fundamental" / "v1.txt").write_text("Edited fundamental prompt — different content, same version string.")

    registry_original = PromptRegistry(root_v1)
    registry_edited = PromptRegistry(root_v1_edited)

    run_id_original = compute_research_run_id(
        snapshot_id="snap-x", provider_name="scripted", model_name="m", roles=("fundamental",),
        prompt_registry=registry_original, run_mode="scripted", config_hash="c" * 64,
    )
    run_id_edited = compute_research_run_id(
        snapshot_id="snap-x", provider_name="scripted", model_name="m", roles=("fundamental",),
        prompt_registry=registry_edited, run_mode="scripted", config_hash="c" * 64,
    )
    assert run_id_original != run_id_edited


def test_manager_decision_with_empty_bear_case_is_retried_not_crashed():
    """Milestone 6.1 application-bug regression: an empty `bear_case` passes JSON Schema
    (no `minLength`) but fails `ResearchDecision.__post_init__`'s own invariant, raising
    `EvidenceValidationError`. Before the Step 14 fix this propagated uncaught out of
    `_run_role_with_retries`, crashing the whole committee run instead of being retried."""
    bad_manager_payload = dict(MANAGER_PAYLOAD, bear_case="")
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
        ("technical", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
        ("manager", 1): ScriptedStep(kind="response", payload=bad_manager_payload),
        ("manager", 2): ScriptedStep(kind="response", payload=MANAGER_PAYLOAD),
    })
    repo = FakeResearchRepository()

    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )

    assert result.status == RUN_STATUS_COMPLETED
    assert result.decision is not None
    assert [c.role for c in provider.calls if c.role == "manager"] == ["manager", "manager"]
    from trading_research.research.failure_taxonomy import CODE_MISSING_BEAR_CASE, STAGE_STRUCTURED_SCHEMA

    bear_case_failures = [f for f in repo.failures if f.code == CODE_MISSING_BEAR_CASE]
    assert len(bear_case_failures) == 1
    assert bear_case_failures[0].stage == STAGE_STRUCTURED_SCHEMA
    assert bear_case_failures[0].role == "manager"
    assert bear_case_failures[0].attempt_number == 1


def test_preflight_missing_evidence_blocks_before_any_provider_call():
    thin_snapshot = build_fixture_snapshot("XXXX", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)
    provider = ScriptedResearchProvider({})  # no steps scripted at all — any call is a test failure
    repo = FakeResearchRepository()
    result = analyze_with_research_committee(
        thin_snapshot, provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )
    assert result.status == RUN_STATUS_ANALYSIS_INCOMPLETE
    assert provider.calls == []


# --- Manager-invocation fix: manager only called when configured (require_decision) ---


def test_manager_configured_is_invoked_exactly_once():
    """Unchanged production behavior: with 'manager' in roles, it is called exactly once
    after every analyst role succeeds."""
    provider = _happy_provider()
    repo = FakeResearchRepository()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )
    assert result.status == RUN_STATUS_COMPLETED
    assert result.decision is not None
    assert [c.role for c in provider.calls].count("manager") == 1


def test_manager_omitted_is_never_invoked():
    """The core fix: 'manager' absent from roles + require_decision=False must never
    result in a manager provider call, even though every analyst role succeeded."""
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
        ("technical", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
        # Deliberately no ("manager", 1) step scripted — a manager call would raise
        # AssertionError from ScriptedResearchProvider, which pytest would report as an
        # error, proving the manager was never invoked (not merely "invoked but happened
        # to succeed anyway").
    })
    repo = FakeResearchRepository()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo,
        configuration=_config(roles=("fundamental", "technical")), clock=lambda: NOW,
        run_mode="scripted", require_decision=False,
    )
    assert "manager" not in [c.role for c in provider.calls]
    assert result.status == RUN_STATUS_ANALYST_REPORTS_COMPLETE_NO_MANAGER
    assert result.decision is None
    assert [r.role for r in result.role_reports] == ["fundamental", "technical"]


def test_bear_only_diagnostic_run_succeeds_without_extra_provider_call():
    provider = ScriptedResearchProvider({
        ("bear", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
    })
    repo = FakeResearchRepository()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo,
        configuration=_config(roles=("bear",)), clock=lambda: NOW,
        run_mode="scripted", require_decision=False,
    )
    assert [c.role for c in provider.calls] == ["bear"]  # exactly one call, bear only
    assert result.status == RUN_STATUS_ANALYST_REPORTS_COMPLETE_NO_MANAGER
    assert [r.role for r in result.role_reports] == ["bear"]


def test_analyst_only_failure_still_persists_structured_failures():
    """Even in analyst-only/no-manager mode, a rejected claim or retry exhaustion must
    still produce persisted structured failures — the manager-invocation fix must not
    weaken failure observability."""
    bad_payload = {
        "stance": "BEARISH", "summary": "s", "catalysts": [], "risks": ["r"], "uncertainties": [],
        "missing_data_reasons": [],
        "claims": [{
            "claim_id": "c1", "claim_type": "downside_estimate", "statement": "invented",
            "evidence_ids": ["ev-does-not-exist"], "numeric_value": None, "unit": None, "importance": "high",
        }],
    }
    provider = ScriptedResearchProvider({
        ("bear", 1): ScriptedStep(kind="response", payload=bad_payload),
        ("bear", 2): ScriptedStep(kind="response", payload=bad_payload),
    })
    repo = FakeResearchRepository()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo,
        configuration=_config(roles=("bear",)), clock=lambda: NOW,
        run_mode="scripted", require_decision=False,
    )
    assert result.status == RUN_STATUS_ANALYSIS_INCOMPLETE
    assert result.decision is None
    assert any(f.code == "UNKNOWN_EVIDENCE_ID" for f in repo.failures)
    assert any(f.stage == "RETRY_EXHAUSTED" for f in repo.failures)
    assert any(f.stage == "REQUIRED_ROLE_FAILED" for f in repo.failures)
    # No manager-skip failure — there was never a manager configured to skip.
    assert not any(f.stage == "MANAGER_SKIPPED" for f in repo.failures)
    assert "manager" not in [c.role for c in provider.calls]


def test_no_final_decision_fabricated_from_analyst_reports():
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
    })
    repo = FakeResearchRepository()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo,
        configuration=_config(roles=("fundamental",)), clock=lambda: NOW,
        run_mode="scripted", require_decision=False,
    )
    assert result.decision is None
    assert result.status != RUN_STATUS_COMPLETED
    assert repo.decisions == {}  # nothing was ever persisted as a decision


def test_production_mode_requiring_decision_fails_closed_without_manager():
    provider = ScriptedResearchProvider({})  # any call would be a test failure
    repo = FakeResearchRepository()
    with pytest.raises(ManagerNotConfiguredError):
        analyze_with_research_committee(
            _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
            prompt_registry=PromptRegistry(), research_repository=repo,
            configuration=_config(roles=("fundamental", "bear")), clock=lambda: NOW,
            run_mode="scripted",  # require_decision defaults to True
        )
    assert provider.calls == []  # fails before any provider call, not after wasted work
    assert repo.attempts == []
    assert repo.runs == {}  # not even a run_started row was persisted


def test_full_committee_behavior_unchanged_when_manager_configured():
    """Regression guard: a full fundamental+technical+manager run behaves identically to
    before this fix — same status, same decision, same role order."""
    provider = _happy_provider()
    repo = FakeResearchRepository()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )
    assert [c.role for c in provider.calls] == ["fundamental", "technical", "manager"]
    assert result.status == RUN_STATUS_COMPLETED
    assert result.decision.rating == "OVERWEIGHT"
    assert repo.runs[result.research_run_id]["status"] == RUN_STATUS_COMPLETED


def _token_controller(tmp_path, *, policy=TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT, cap=100_000):
    conn = connect(tmp_path / "orchestration-token-budget.sqlite3")
    return conn, PersistentResearchTokenBudgetController(
        conn=conn, daily_token_cap=cap, maximum_reasoning_tokens=100,
        token_accounting_policy=policy, clock=lambda: NOW,
    )


def test_fresh_provider_call_cannot_run_when_reservation_is_rejected(tmp_path):
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
    })
    conn, controller = _token_controller(tmp_path, cap=1)
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=FakeResearchRepository(),
        configuration=_config(roles=("fundamental",), max_attempts_per_role=1), clock=lambda: NOW,
        run_mode="scripted", require_decision=False, token_budget_controller=controller,
    )
    assert result.status == RUN_STATUS_ANALYSIS_INCOMPLETE
    assert provider.calls == []
    assert list_budget_reservations(conn) == []


def test_successful_provider_call_settles_input_output_and_reasoning(tmp_path):
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(
            kind="response", payload=ANALYST_REPORT_PAYLOAD,
            usage_overrides={
                "input_tokens": 100, "output_tokens": 50, "reasoning_output_tokens": 25,
                "token_accounting_policy": TOKEN_ACCOUNTING_REASONING_SEPARATE,
            },
        ),
    })
    conn, controller = _token_controller(tmp_path, policy=TOKEN_ACCOUNTING_REASONING_SEPARATE)
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=FakeResearchRepository(),
        configuration=_config(roles=("fundamental",), max_attempts_per_role=1), clock=lambda: NOW,
        run_mode="scripted", require_decision=False, token_budget_controller=controller,
    )
    assert result.status == RUN_STATUS_ANALYST_REPORTS_COMPLETE_NO_MANAGER
    [reservation] = list_budget_reservations(conn)
    assert reservation["status"] == "SETTLED"
    assert reservation["consumed_input_tokens"] == 100
    assert reservation["consumed_output_tokens"] == 50
    assert reservation["consumed_reasoning_tokens"] == 25


def test_reused_research_creates_no_new_token_reservation(tmp_path):
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(
            kind="response", payload=ANALYST_REPORT_PAYLOAD,
            usage_overrides={
                "reasoning_output_tokens": 10,
                "token_accounting_policy": TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT,
            },
        ),
    })
    repo = FakeResearchRepository()
    conn, controller = _token_controller(tmp_path)
    first = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo,
        configuration=_config(roles=("fundamental",), max_attempts_per_role=1), clock=lambda: NOW,
        run_mode="scripted", require_decision=False, token_budget_controller=controller,
    )
    reservation_count = len(list_budget_reservations(conn))
    second = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo,
        configuration=_config(roles=("fundamental",), max_attempts_per_role=1), clock=lambda: NOW,
        run_mode="scripted", require_decision=False, token_budget_controller=controller,
    )
    assert first.reused_existing_run is False
    assert second.reused_existing_run is True
    assert len(provider.calls) == 1
    assert len(list_budget_reservations(conn)) == reservation_count


def test_provider_unavailable_releases_reservation(tmp_path):
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="unavailable"),
    })
    conn, controller = _token_controller(tmp_path)
    analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=FakeResearchRepository(),
        configuration=_config(roles=("fundamental",), max_attempts_per_role=1), clock=lambda: NOW,
        run_mode="scripted", require_decision=False, token_budget_controller=controller,
    )
    [reservation] = list_budget_reservations(conn)
    assert reservation["status"] == "RELEASED"


def test_timeout_becomes_ambiguous_and_blocks_automatic_retry(tmp_path):
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="timeout"),
        ("fundamental", 2): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
    })
    conn, controller = _token_controller(tmp_path)
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=FakeResearchRepository(),
        configuration=_config(roles=("fundamental",), max_attempts_per_role=2), clock=lambda: NOW,
        run_mode="scripted", require_decision=False, token_budget_controller=controller,
    )
    assert result.status == RUN_STATUS_ANALYSIS_INCOMPLETE
    assert len(provider.calls) == 1
    [reservation] = list_budget_reservations(conn)
    assert reservation["status"] == "AMBIGUOUS"
