"""Unit tests for the framework-neutral attempt-control hooks in
`research/orchestration.py` (docs/milestone-7.1.md Step 12) — default no-op
behavior, before/after invocation counts, denial semantics, and the
structural "no shadow import" guard.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

from trading_research.research.configuration import ResearchConfiguration
from trading_research.research.deterministic_provider import ScriptedResearchProvider, ScriptedStep
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.orchestration import (
    AttemptControlDecision,
    AttemptControlRequest,
    analyze_with_research_committee,
)

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)

ANALYST_PAYLOAD = {
    "stance": "BULLISH", "summary": "growth", "claims": [], "catalysts": [], "risks": ["some risk"],
    "uncertainties": [], "missing_data_reasons": [],
}
MANAGER_PAYLOAD = {
    "rating": "OVERWEIGHT", "confidence": 0.6, "thesis": "t", "bull_case": "bull", "bear_case": "bear",
    "catalysts": [], "risks": ["some risk"], "invalidation_conditions": [], "claims": [], "evidence_ids": [],
    "missing_data_reasons": [],
}


def _config(roles=("fundamental", "manager"), max_attempts_per_role=2) -> ResearchConfiguration:
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


class _RecordingController:
    def __init__(self, *, deny_roles: frozenset[str] = frozenset()):
        self.before_calls: list[AttemptControlRequest] = []
        self.after_calls: list[tuple[AttemptControlRequest, object]] = []
        self._deny_roles = deny_roles

    def before_attempt(self, request: AttemptControlRequest) -> AttemptControlDecision:
        self.before_calls.append(request)
        if request.role in self._deny_roles:
            return AttemptControlDecision(allowed=False, code="TEST_DENIED", reason="test denial")
        return AttemptControlDecision(allowed=True, code="PROCEED")

    def after_attempt(self, request: AttemptControlRequest, attempt) -> None:
        self.after_calls.append((request, attempt))


class _ScheduledRecordingController(_RecordingController):
    def before_attempt(self, request: AttemptControlRequest) -> AttemptControlDecision:
        self.before_calls.append(request)
        return AttemptControlDecision(
            allowed=True, code="PROCEED", scheduler_run_id="sched-A",
            research_cycle_id="cycle-X", attempt_control_check_id=f"check-{request.role}-{request.attempt_number}",
            correlation_mode="SCHEDULED",
        )


def test_default_no_controller_is_a_pure_no_op():
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
        ("manager", 1): ScriptedStep(kind="response", payload=MANAGER_PAYLOAD),
    })
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=__import__("trading_research.research.prompt_registry", fromlist=["PromptRegistry"]).PromptRegistry(),
        research_repository=None, configuration=_config(), clock=lambda: NOW, run_mode="scripted",
    )
    assert result.status == "COMPLETED"


def test_before_hook_called_once_per_attempt_and_after_called_on_success():
    from trading_research.research.prompt_registry import PromptRegistry

    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
        ("manager", 1): ScriptedStep(kind="response", payload=MANAGER_PAYLOAD),
    })
    controller = _RecordingController()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=None, configuration=_config(),
        clock=lambda: NOW, run_mode="scripted", attempt_controller=controller,
    )
    assert result.status == "COMPLETED"
    assert len(controller.before_calls) == 2  # fundamental + manager
    assert len(controller.after_calls) == 2
    assert {r.role for r in controller.before_calls} == {"fundamental", "manager"}


def test_retries_separately_checked_before_each_attempt():
    from trading_research.research.prompt_registry import PromptRegistry

    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="malformed", raw_text="not json"),
        ("fundamental", 2): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
        ("manager", 1): ScriptedStep(kind="response", payload=MANAGER_PAYLOAD),
    })
    controller = _RecordingController()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=None, configuration=_config(),
        clock=lambda: NOW, run_mode="scripted", attempt_controller=controller,
    )
    assert result.status == "COMPLETED"
    fundamental_attempts = [r.attempt_number for r in controller.before_calls if r.role == "fundamental"]
    assert fundamental_attempts == [1, 2]
    # after_attempt called for both the malformed (rejected) attempt and the successful retry.
    fundamental_after = [r for r, _a in controller.after_calls if r.role == "fundamental"]
    assert len(fundamental_after) == 2


def test_scheduled_retry_attempts_retain_exact_ownership():
    from trading_research.research.prompt_registry import PromptRegistry

    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="malformed", raw_text="not json"),
        ("fundamental", 2): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
    })
    controller = _ScheduledRecordingController()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=None,
        configuration=_config(roles=("fundamental",), max_attempts_per_role=2),
        clock=lambda: NOW, run_mode="scripted", attempt_controller=controller,
        require_decision=False,
    )
    assert result.status == "ANALYST_REPORTS_COMPLETE_NO_MANAGER"
    assert len(result.attempts) == 2
    assert all(attempt.scheduler_run_id == "sched-A" for attempt in result.attempts)
    assert all(attempt.research_cycle_id == "cycle-X" for attempt in result.attempts)
    assert all(attempt.correlation_mode == "SCHEDULED" for attempt in result.attempts)
    assert len({attempt.attempt_id for attempt in result.attempts}) == 2


def test_denied_attempt_never_calls_provider():
    from trading_research.research.prompt_registry import PromptRegistry

    provider = ScriptedResearchProvider({})  # any call is a test failure
    controller = _RecordingController(deny_roles=frozenset({"fundamental"}))
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=None, configuration=_config(),
        clock=lambda: NOW, run_mode="scripted", attempt_controller=controller,
    )
    assert result.status == "ANALYSIS_INCOMPLETE"
    assert len(provider.calls) == 0
    # after_attempt is not called for a denied (never-attempted) request.
    assert controller.after_calls == []
    failure_codes = {f.code for f in result.failures}
    assert "BUDGET_EXHAUSTED" in failure_codes
    # Denial is distinct from a provider-failure code.
    assert "PROVIDER_UNAVAILABLE" not in failure_codes


def test_manager_separately_checked_after_analysts_succeed():
    from trading_research.research.prompt_registry import PromptRegistry

    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
    })
    controller = _RecordingController(deny_roles=frozenset({"manager"}))
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=None, configuration=_config(),
        clock=lambda: NOW, run_mode="scripted", attempt_controller=controller,
    )
    assert result.status == "ANALYSIS_INCOMPLETE"
    assert len(provider.calls) == 1  # only fundamental was actually called
    assert provider.calls[0].role == "fundamental"
    manager_checks = [r for r in controller.before_calls if r.role == "manager"]
    assert len(manager_checks) == 1


def test_after_hook_called_after_schema_rejection():
    from trading_research.research.prompt_registry import PromptRegistry

    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="malformed", raw_text="not json"),
        ("fundamental", 2): ScriptedStep(kind="malformed", raw_text="still not json"),
        ("manager", 1): ScriptedStep(kind="response", payload=MANAGER_PAYLOAD),
    })
    controller = _RecordingController()
    analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=None, configuration=_config(max_attempts_per_role=2),
        clock=lambda: NOW, run_mode="scripted", attempt_controller=controller,
    )
    fundamental_after = [a for r, a in controller.after_calls if r.role == "fundamental"]
    assert len(fundamental_after) == 2
    assert all(not a.success for a in fundamental_after)


def test_orchestration_module_never_imports_shadow():
    """Structural guard (docs/milestone-7.1.md Step 12: "no direct SQLite
    dependency in research orchestration", "research/orchestration.py never
    imports shadow modules directly")."""
    source_path = Path(__file__).resolve().parents[2] / "src" / "trading_research" / "research" / "orchestration.py"
    tree = ast.parse(source_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("trading_research.shadow")
                assert alias.name != "sqlite3"
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "shadow" not in node.module


def test_non_retryable_malformed_output_produces_exactly_one_provider_call():
    """Milestone 12.1.1 Item 1: `MalformedOutputError` is usually retryable
    (`default_retryable = True`), but a scripted instance with
    `retryable=False` — e.g. the real CODEX_USAGE_METADATA_MISSING /
    CODEX_REASONING_TOKENS_INVALID contract failures — must stop the retry
    loop after exactly one attempt instead of continuing to attempt 2."""
    from trading_research.research.deterministic_provider import ScriptedStep
    from trading_research.research.prompt_registry import PromptRegistry

    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(
            kind="malformed", raw_text="bad", retryable=False, code="CODEX_USAGE_METADATA_MISSING",
        ),
    })
    controller = _RecordingController()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=None,
        configuration=_config(max_attempts_per_role=3), clock=lambda: NOW, run_mode="scripted",
        attempt_controller=controller,
    )
    assert result.status == "ANALYSIS_INCOMPLETE"
    assert len(provider.calls) == 1
    fundamental_before = [r for r in controller.before_calls if r.role == "fundamental"]
    assert len(fundamental_before) == 1
    failure_codes = {f.code for f in result.failures}
    assert "CODEX_USAGE_METADATA_MISSING" in failure_codes


def test_retryable_timeout_can_retry():
    from trading_research.research.deterministic_provider import ScriptedStep
    from trading_research.research.prompt_registry import PromptRegistry

    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="timeout"),
        ("fundamental", 2): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
        ("manager", 1): ScriptedStep(kind="response", payload=MANAGER_PAYLOAD),
    })
    controller = _RecordingController()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=None,
        configuration=_config(max_attempts_per_role=3), clock=lambda: NOW, run_mode="scripted",
        attempt_controller=controller,
    )
    assert result.status == "COMPLETED"
    assert len([c for c in provider.calls if c.role == "fundamental"]) == 2


def test_authentication_failure_never_retries():
    """Milestone 12.1.1 Item 1, required test #5: `ProviderUnavailableError`
    (authentication/quota-class failures) already stops after exactly one
    call — regression guard against that behavior silently changing."""
    from trading_research.research.prompt_registry import PromptRegistry

    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="unavailable"),
    })
    controller = _RecordingController()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=None,
        configuration=_config(max_attempts_per_role=3), clock=lambda: NOW, run_mode="scripted",
        attempt_controller=controller,
    )
    assert result.status == "ANALYSIS_INCOMPLETE"
    assert len(provider.calls) == 1


def test_persisted_failure_retryable_matches_actual_behavior():
    """Milestone 12.1.1 Item 1, required test #8."""
    from trading_research.research.deterministic_provider import ScriptedStep
    from trading_research.research.prompt_registry import PromptRegistry

    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="malformed", raw_text="bad", retryable=False),
    })
    controller = _RecordingController()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=None,
        configuration=_config(max_attempts_per_role=3), clock=lambda: NOW, run_mode="scripted",
        attempt_controller=controller,
    )
    non_retried_attempts = [a for _r, a in controller.after_calls if a.attempt_number == 1]
    assert len(non_retried_attempts) == 1
    assert non_retried_attempts[0].failure_retryable is False
    assert len(provider.calls) == 1


def test_mixed_failures_use_all_failures_for_retry_authorization(monkeypatch):
    from trading_research.research import orchestration as orchestration_mod
    from trading_research.research.failure_taxonomy import (
        STAGE_UNKNOWN,
        new_failure,
    )
    from trading_research.research.prompt_registry import PromptRegistry

    bad_payload = {
        **ANALYST_PAYLOAD,
        "claims": [{
            "claim_id": "c1", "claim_type": "downside_estimate", "statement": "unsupported",
            "evidence_ids": ["missing-evidence"], "numeric_value": None, "unit": None,
            "importance": "high",
        }],
    }
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=bad_payload),
        ("fundamental", 2): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
    })
    original = orchestration_mod._claim_validation_failures

    def _mixed_failures(validation, **kwargs):
        failures = original(validation, **kwargs)
        failures.append(new_failure(
            research_run_id=kwargs["research_run_id"], attempt_id=kwargs["attempt_id"],
            role=kwargs["role"], attempt_number=kwargs["attempt_number"], stage=STAGE_UNKNOWN,
            code="UNCLASSIFIED_VALIDATION_FAILURE", message="bounded diagnostic",
            retryable=False, model_name=kwargs["model_name"], prompt_version=kwargs["prompt_version"],
            schema_version=kwargs["schema_version"], occurred_at=kwargs["occurred_at"],
        ))
        return failures

    monkeypatch.setattr(orchestration_mod, "_claim_validation_failures", _mixed_failures)
    controller = _RecordingController()
    result = analyze_with_research_committee(
        _snapshot(), provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=None,
        configuration=_config(roles=("fundamental",), max_attempts_per_role=2),
        clock=lambda: NOW, run_mode="scripted", attempt_controller=controller,
        require_decision=False,
    )
    assert result.status == "ANALYSIS_INCOMPLETE"
    assert len(provider.calls) == 1
    attempt = controller.after_calls[0][1]
    assert attempt.failure_code == "UNCLASSIFIED_VALIDATION_FAILURE"
    assert attempt.failure_retryable is False
