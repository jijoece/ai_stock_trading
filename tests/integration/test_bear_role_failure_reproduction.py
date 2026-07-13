"""Milestone 6.1 Step 12: deterministic reproduction of the bear-role incident category.

The real `run-e4544adb0ac3e1faf405846132bdcf3d` bear-role failure could not be
reconstructed from persisted data (see `.claude/scratchpads/milestone6-progress.md`'s
"Bear-role incident investigation" — OBSERVABILITY GAP, historical attempt data
insufficient). This test reproduces the most evidence-backed *representative* failure
category instead of fabricating the exact historical one:

* Milestone 5's own real-Claude validation already demonstrated the claim-to-evidence
  validator correctly rejecting a live model's arithmetic-derived numeric claim (an
  unsupported number not present in any cited evidence's `normalized_values`) — see
  `docs/milestone5-evidence-backed-claude-research.md`, "Real Claude API validation".
* The bear role, by design (`prompts/research/bear/v1.txt`), argues quantified downside —
  exactly the kind of role most likely to have a live model invent a specific downside
  percentage not actually present in the evidence, reproducing the same failure category
  M5 already proved is real, applied to the role most prone to it.

This is reported honestly as a *representative reproduction*, not a claim that it is
provably the exact historical cause — see the Milestone 6.1 scratchpad's root-cause
classification for the full VALID EXPECTED REJECTION + PROMPT DEFECT classification.
"""
from __future__ import annotations

from datetime import datetime, timezone

from trading_research.research.configuration import ResearchConfiguration
from trading_research.research.deterministic_provider import ScriptedResearchProvider, ScriptedStep
from trading_research.research.failure_taxonomy import (
    CODE_MANAGER_NOT_INVOKED,
    CODE_MISSING_REQUIRED_ROLE,
    CODE_NUMERIC_VALUE_MISMATCH,
    CODE_RETRY_EXHAUSTED,
    STAGE_CLAIM_EVIDENCE_VALIDATION,
    STAGE_MANAGER_SKIPPED,
    STAGE_REQUIRED_ROLE_FAILED,
    STAGE_RETRY_EXHAUSTED,
)
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.orchestration import RUN_STATUS_ANALYSIS_INCOMPLETE, analyze_with_research_committee
from trading_research.research.prompt_registry import PromptRegistry

from tests.support.research_fixtures import FakeResearchRepository

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)

ANALYST_REPORT_PAYLOAD = {
    "stance": "BULLISH", "summary": "growth", "claims": [], "catalysts": [], "risks": ["some risk"],
    "uncertainties": [], "missing_data_reasons": [],
}

# A fabricated -35% downside claim citing a real evidence_id whose actual normalized
# value is 0.08 (8% revenue growth) — an invented number, not one derived from the
# evidence, exactly the category the strengthened bear prompt (Step 14) now forbids.
BEAR_REPORT_WITH_INVENTED_DOWNSIDE = {
    "stance": "BEARISH", "summary": "material downside risk", "catalysts": [],
    "risks": ["revenue deceleration"], "uncertainties": [],
    "missing_data_reasons": [],
    "claims": [
        {
            "claim_id": "bear-claim-1", "claim_type": "downside_estimate",
            "statement": "Expected downside of -35% based on decelerating growth.",
            "evidence_ids": ["fixture-fundamentals-AAPL-revenue_growth_yoy"],
            "numeric_value": -0.35, "unit": "percent", "importance": "high",
        }
    ],
}


def _config() -> ResearchConfiguration:
    return ResearchConfiguration(
        version=1, enabled=True, provider="scripted", model="test-model", max_attempts_per_role=2,
        request_timeout_seconds=30, max_input_characters=100_000, max_evidence_items=100,
        max_items_per_source_category=25, max_claims_per_role=20, max_output_tokens=2000,
        require_point_in_time_safe=True, require_evidence_for_material_claims=True,
        fail_on_stale_required_evidence=True, allow_parallel_roles=False,
        roles=("fundamental", "technical", "bull", "bear", "manager"),
        overlay_policy_version="test.v1", overlay_allow_score_increase=False,
        overlay_allow_position_size_increase=False, overlay_incomplete_action="ANALYSIS_INCOMPLETE",
        overlay_critical_risk_action="FORCE_NO_ACTION", config_hash="c" * 64, raw={},
    )


def _snapshot():
    return build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)


def test_bear_role_invented_downside_exhausts_retries_and_skips_manager():
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
        ("technical", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
        ("bull", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
        ("bear", 1): ScriptedStep(kind="response", payload=BEAR_REPORT_WITH_INVENTED_DOWNSIDE),
        ("bear", 2): ScriptedStep(kind="response", payload=BEAR_REPORT_WITH_INVENTED_DOWNSIDE),
    })
    repo = FakeResearchRepository()

    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )

    # bear attempt 1 -> failure persisted -> bounded retry feedback -> bear attempt 2 ->
    # failure persisted -> retry exhausted -> required role failed -> manager skip
    # persisted -> final result ANALYSIS_INCOMPLETE.
    assert result.status == RUN_STATUS_ANALYSIS_INCOMPLETE
    assert result.decision is None
    assert "manager" not in [c.role for c in provider.calls]

    bear_calls = [c for c in provider.calls if c.role == "bear"]
    assert len(bear_calls) == 2
    # Attempt 2 received bounded, code-grouped retry feedback naming the actual code and
    # the allowed evidence_id set — not the raw prior response, not an unbounded blob.
    feedback_text = " ".join(bear_calls[1].validation_feedback)
    assert CODE_NUMERIC_VALUE_MISMATCH in feedback_text
    assert "fixture-fundamentals-AAPL-revenue_growth_yoy" in feedback_text
    assert "complete replacement report" in feedback_text

    numeric_failures = [f for f in repo.failures if f.code == CODE_NUMERIC_VALUE_MISMATCH]
    assert len(numeric_failures) == 2  # one per bear attempt
    assert all(f.role == "bear" for f in numeric_failures)
    assert all(f.stage == STAGE_CLAIM_EVIDENCE_VALIDATION for f in numeric_failures)
    assert all(f.claim_id == "bear-claim-1" for f in numeric_failures)
    assert {f.attempt_number for f in numeric_failures} == {1, 2}
    # Prompt version/hash are visible in diagnostics (not just in-memory) — the hardened
    # bear/v2.txt prompt is the default `PromptRegistry().get("bear")` resolves to, and
    # every persisted failure for the bear role carries that version.
    assert all(f.prompt_version == "v2" for f in numeric_failures)
    from trading_research.research.prompt_registry import PromptRegistry as _PR
    assert all(f.schema_version == "role-report.v1" for f in numeric_failures)
    bear_prompt_hash = _PR().get("bear").text_hash
    bear_calls_prompt_hashes = {c.prompt_hash for c in bear_calls}
    assert bear_calls_prompt_hashes == {bear_prompt_hash}

    retry_exhausted = [f for f in repo.failures if f.stage == STAGE_RETRY_EXHAUSTED]
    assert len(retry_exhausted) == 1
    assert retry_exhausted[0].role == "bear"
    assert retry_exhausted[0].code == CODE_RETRY_EXHAUSTED

    required_role_failed = [f for f in repo.failures if f.stage == STAGE_REQUIRED_ROLE_FAILED]
    assert len(required_role_failed) == 1
    assert required_role_failed[0].role == "bear"
    assert required_role_failed[0].code == CODE_MISSING_REQUIRED_ROLE

    manager_skipped = [f for f in repo.failures if f.stage == STAGE_MANAGER_SKIPPED]
    assert len(manager_skipped) == 1
    assert manager_skipped[0].role == "manager"
    assert manager_skipped[0].code == CODE_MANAGER_NOT_INVOKED
    assert "bear" in manager_skipped[0].message

    # OrchestrationResult.failures mirrors what was persisted (usable without a DB round
    # trip — e.g. by the CLI diagnostics command, Step 15).
    assert set(f.failure_id for f in result.failures) == set(f.failure_id for f in repo.failures)

    # No decision, no enhanced recommendation input, no paper submission call ever
    # happens inside analyze_with_research_committee — it has no import of
    # execution/paper/recommendation_overlay at all (verified by this module's own
    # import list), so there is structurally no path from this failure to an executed
    # order. Baseline-side non-promotion on ANALYSIS_INCOMPLETE is already covered by
    # the existing Milestone 5 overlay suite (tests/unit/test_research_overlay.py).
