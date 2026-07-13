"""Category F: orchestrator tests (docs/milestone-5.md Step 20.F)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from trading_research.research.configuration import ResearchConfiguration
from trading_research.research.deterministic_provider import ScriptedResearchProvider, ScriptedStep
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.orchestration import RUN_STATUS_ANALYSIS_INCOMPLETE, RUN_STATUS_COMPLETED, analyze_with_research_committee, compute_research_run_id
from trading_research.research.prompt_registry import PromptRegistry

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
