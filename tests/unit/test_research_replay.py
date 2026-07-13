"""Category J: replay tests (docs/milestone-5.md Step 20.J)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_research.research.configuration import ResearchConfiguration
from trading_research.research.deterministic_provider import ScriptedResearchProvider, ScriptedStep
from trading_research.research.evidence import EvidenceItem, EvidenceSnapshot
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.orchestration import analyze_with_research_committee, compute_research_run_id
from trading_research.research.prompt_registry import PromptRegistry
from trading_research.research.replay import replay_research_run

from tests.support.research_fixtures import FakeResearchRepository

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)

ANALYST_REPORT_PAYLOAD = {
    "stance": "BULLISH", "summary": "growth", "claims": [], "catalysts": [], "risks": ["some risk"],
    "uncertainties": [], "missing_data_reasons": [],
}
MANAGER_PAYLOAD = {
    "rating": "OVERWEIGHT", "confidence": 0.6, "thesis": "t", "bull_case": "bull", "bear_case": "bear",
    "catalysts": [], "risks": ["some risk"], "invalidation_conditions": [], "claims": [], "evidence_ids": [],
    "missing_data_reasons": [],
}


def _config() -> ResearchConfiguration:
    return ResearchConfiguration(
        version=1, enabled=True, provider="scripted", model="test-model", max_attempts_per_role=2,
        request_timeout_seconds=30, max_input_characters=100_000, max_evidence_items=100,
        max_items_per_source_category=25, max_claims_per_role=20, max_output_tokens=2000,
        require_point_in_time_safe=True, require_evidence_for_material_claims=True,
        fail_on_stale_required_evidence=True, allow_parallel_roles=False, roles=("fundamental", "manager"),
        overlay_policy_version="test.v1", overlay_allow_score_increase=False,
        overlay_allow_position_size_increase=False, overlay_incomplete_action="ANALYSIS_INCOMPLETE",
        overlay_critical_risk_action="FORCE_NO_ACTION", config_hash="c" * 64, raw={},
    )


def _completed_run():
    snapshot = build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
        ("manager", 1): ScriptedStep(kind="response", payload=MANAGER_PAYLOAD),
    })
    repo = FakeResearchRepository()
    result = analyze_with_research_committee(
        snapshot, provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )
    return snapshot, repo, result


def test_exact_reconstruction_matches():
    snapshot, repo, result = _completed_run()
    replay = replay_research_run(
        result.research_run_id, research_repository=repo, snapshot=snapshot, provider_name="scripted",
        model_name="test-model", prompt_registry=PromptRegistry(), configuration=_config(), run_mode="scripted",
    )
    assert replay.matches is True
    assert replay.mismatches == ()
    assert replay.reconstructed_decision.rating == "OVERWEIGHT"
    assert replay.reconstructed_overlay.action == "ALLOW_BASELINE"


def test_snapshot_hash_mismatch_detected():
    snapshot, repo, result = _completed_run()
    tampered = EvidenceSnapshot(
        snapshot_id=snapshot.snapshot_id,  # claims to be the same snapshot_id...
        symbol=snapshot.symbol, as_of=snapshot.as_of, created_at=snapshot.created_at,
        source_records=snapshot.source_records, evidence_items=snapshot.evidence_items,
        deterministic_factors={"tampered": 999.0},  # ...but content was altered
        sentiment_metrics=snapshot.sentiment_metrics, portfolio_context=snapshot.portfolio_context,
        missing_data_reasons=snapshot.missing_data_reasons, conflict_reasons=snapshot.conflict_reasons,
        point_in_time_safe=snapshot.point_in_time_safe, config_hash=snapshot.config_hash, git_sha=snapshot.git_sha,
    )
    replay = replay_research_run(
        result.research_run_id, research_repository=repo, snapshot=tampered, provider_name="scripted",
        model_name="test-model", prompt_registry=PromptRegistry(), configuration=_config(), run_mode="scripted",
    )
    assert replay.matches is False
    assert any("snapshot content hash mismatch" in m for m in replay.mismatches)


def test_model_config_mismatch_detected():
    snapshot, repo, result = _completed_run()
    replay = replay_research_run(
        result.research_run_id, research_repository=repo, snapshot=snapshot, provider_name="scripted",
        model_name="a-different-model", prompt_registry=PromptRegistry(), configuration=_config(), run_mode="scripted",
    )
    assert replay.matches is False
    assert any("research_run_id mismatch" in m for m in replay.mismatches)


def test_prompt_hash_mismatch_detected(tmp_path):
    snapshot, repo, result = _completed_run()
    edited_root = tmp_path / "prompts"
    (edited_root / "fundamental").mkdir(parents=True)
    (edited_root / "fundamental" / "v1.txt").write_text("A materially different fundamental prompt.")
    (edited_root / "manager").mkdir(parents=True)
    (edited_root / "manager" / "v1.txt").write_text("A materially different manager prompt.")

    replay = replay_research_run(
        result.research_run_id, research_repository=repo, snapshot=snapshot, provider_name="scripted",
        model_name="test-model", prompt_registry=PromptRegistry(edited_root), configuration=_config(), run_mode="scripted",
    )
    assert replay.matches is False
    assert any("research_run_id mismatch" in m for m in replay.mismatches)


def test_missing_role_response_reported():
    snapshot = build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)
    repo = FakeResearchRepository()
    fake_run_id = compute_research_run_id(
        snapshot_id=snapshot.snapshot_id, provider_name="scripted", model_name="test-model",
        roles=("fundamental", "manager"), prompt_registry=PromptRegistry(), run_mode="scripted", config_hash="c" * 64,
    )
    replay = replay_research_run(
        fake_run_id, research_repository=repo, snapshot=snapshot, provider_name="scripted",
        model_name="test-model", prompt_registry=PromptRegistry(), configuration=_config(), run_mode="scripted",
    )
    assert replay.matches is False
    assert any("no persisted decision found" in m for m in replay.mismatches)


def test_replay_reconstructs_no_claim_failures_for_a_clean_run():
    """Milestone 6.1 Step 16: a run with no rejected claims reconstructs an empty
    failure-comparison set — matched/missing/unexpected/not_reconstructible all empty."""
    snapshot, repo, result = _completed_run()
    replay = replay_research_run(
        result.research_run_id, research_repository=repo, snapshot=snapshot, provider_name="scripted",
        model_name="test-model", prompt_registry=PromptRegistry(), configuration=_config(), run_mode="scripted",
    )
    assert replay.failure_comparison["matched"] == []
    assert replay.failure_comparison["missing_persisted"] == []
    assert replay.failure_comparison["unexpected_persisted"] == []
    assert replay.persisted_failures == ()


def test_replay_reports_not_reconstructible_for_a_role_with_no_persisted_report():
    """A role whose every attempt was rejected has no `RoleResearchReport` persisted at
    all — replay cannot re-validate it (there is nothing to re-run the validators
    against), and this must be labeled `not_reconstructible`, never conflated with
    "the validator no longer agrees" (`unexpected_persisted`)."""
    from trading_research.research.failure_taxonomy import new_failure

    snapshot, repo, result = _completed_run()
    bogus_claim_failure = new_failure(
        research_run_id=result.research_run_id, attempt_id=f"{result.research_run_id}-bear-1", role="bear",
        attempt_number=1, stage="CLAIM_EVIDENCE_VALIDATION", code="UNKNOWN_EVIDENCE_ID",
        message="claim cites unknown evidence_id 'ev-x'", claim_id="bear-claim-1", retryable=True,
        model_name="test-model", prompt_version="v1", schema_version="role-report.v1", occurred_at=NOW,
    )
    repo.save_attempt_failures((bogus_claim_failure,))

    replay = replay_research_run(
        result.research_run_id, research_repository=repo, snapshot=snapshot, provider_name="scripted",
        model_name="test-model", prompt_registry=PromptRegistry(), configuration=_config(), run_mode="scripted",
    )
    assert ("bear", "UNKNOWN_EVIDENCE_ID", "bear-claim-1") in replay.failure_comparison["not_reconstructible"]
    assert replay.failure_comparison["unexpected_persisted"] == []


def test_replay_never_calls_a_provider_even_with_persisted_failures():
    """Structural guarantee unchanged by the Step 16 failure-comparison addition:
    `replay_research_run` still has no `provider` parameter, so failure reconstruction can
    only re-run local validators, never a real API call."""
    import inspect

    params = inspect.signature(replay_research_run).parameters
    assert "provider" not in params


def test_replay_signature_has_no_provider_parameter():
    """Structural guarantee: replay_research_run cannot call a provider
    because it has no provider argument at all."""
    import inspect

    params = inspect.signature(replay_research_run).parameters
    assert "provider" not in params
