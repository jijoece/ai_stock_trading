"""Category E: provider tests, using the scripted provider (docs/milestone-5.md Step 20.E)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_research.research.deterministic_provider import DeterministicResearchProvider, ScriptedResearchProvider, ScriptedStep
from trading_research.research.errors import (
    MalformedOutputError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUnavailableError,
)
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.models import ResearchModelRequest

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _snapshot(symbol="AAPL"):
    return build_fixture_snapshot(symbol, NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)


def _request(role="fundamental", attempt_number=1, snapshot=None) -> ResearchModelRequest:
    return ResearchModelRequest(
        role=role, research_run_id="run-1", snapshot=snapshot or _snapshot(), system_prompt="sys",
        user_prompt="user", json_schema={}, model_name="test-model", max_output_tokens=1000, temperature=0.0,
        prompt_name="research/fundamental", prompt_version="v1", prompt_hash="h1", system_prompt_hash="sh1",
        schema_version="role-report.v1", attempt_number=attempt_number,
    )


def test_deterministic_provider_never_fabricates_claims():
    provider = DeterministicResearchProvider()
    response = provider.generate_structured(_request())
    assert response.parsed_json["claims"] == []
    assert response.usage.cost_status == "NOT_APPLICABLE"


def test_deterministic_provider_manager_role_produces_valid_ratings():
    provider = DeterministicResearchProvider()
    response = provider.generate_structured(_request(role="manager"))
    assert response.parsed_json["rating"] in ("BUY", "OVERWEIGHT", "HOLD", "UNDERWEIGHT", "SELL", "ANALYSIS_INCOMPLETE")
    assert response.parsed_json["bull_case"] and response.parsed_json["bear_case"]


def test_scripted_provider_success():
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload={"stance": "BULLISH"}),
    })
    response = provider.generate_structured(_request())
    assert response.parsed_json == {"stance": "BULLISH"}
    assert response.usage.success is True


def test_scripted_provider_timeout():
    provider = ScriptedResearchProvider({("fundamental", 1): ScriptedStep(kind="timeout")})
    with pytest.raises(ProviderTimeoutError):
        provider.generate_structured(_request())


def test_scripted_provider_transient_failure():
    provider = ScriptedResearchProvider({("fundamental", 1): ScriptedStep(kind="transient")})
    with pytest.raises(ProviderTransientError):
        provider.generate_structured(_request())


def test_scripted_provider_rate_limit():
    provider = ScriptedResearchProvider({("fundamental", 1): ScriptedStep(kind="rate_limit")})
    with pytest.raises(ProviderRateLimitError):
        provider.generate_structured(_request())


def test_scripted_provider_malformed_output():
    provider = ScriptedResearchProvider({("fundamental", 1): ScriptedStep(kind="malformed", raw_text="not json")})
    with pytest.raises(MalformedOutputError):
        provider.generate_structured(_request())


def test_scripted_provider_retry_then_success():
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="transient"),
        ("fundamental", 2): ScriptedStep(kind="response", payload={"stance": "NEUTRAL"}),
    })
    with pytest.raises(ProviderTransientError):
        provider.generate_structured(_request(attempt_number=1))
    response = provider.generate_structured(_request(attempt_number=2))
    assert response.parsed_json == {"stance": "NEUTRAL"}


def test_scripted_provider_retry_exhaustion_is_caller_responsibility():
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="transient"),
        ("fundamental", 2): ScriptedStep(kind="transient"),
    })
    for attempt in (1, 2):
        with pytest.raises(ProviderTransientError):
            provider.generate_structured(_request(attempt_number=attempt))
    # a third, unscripted attempt is a test-authoring bug, not a silent pass
    with pytest.raises(AssertionError):
        provider.generate_structured(_request(attempt_number=3))


def test_scripted_provider_unavailable():
    provider = ScriptedResearchProvider({("fundamental", 1): ScriptedStep(kind="unavailable")})
    with pytest.raises(ProviderUnavailableError):
        provider.generate_structured(_request())


def test_scripted_provider_token_usage_absent_by_default_override():
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload={"stance": "BULLISH"}, usage_overrides={"input_tokens": None, "output_tokens": None}),
    })
    response = provider.generate_structured(_request())
    assert response.usage.input_tokens is None
    assert response.usage.cost_status == "USAGE_NOT_RETURNED"


def test_scripted_provider_records_every_call():
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload={"stance": "BULLISH"}),
        ("technical", 1): ScriptedStep(kind="response", payload={"stance": "NEUTRAL"}),
    })
    provider.generate_structured(_request(role="fundamental"))
    provider.generate_structured(_request(role="technical"))
    assert [c.role for c in provider.calls] == ["fundamental", "technical"]
