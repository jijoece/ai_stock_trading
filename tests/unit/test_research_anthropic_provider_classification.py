"""Milestone 6.1 Step 7/19: provider-boundary and tool-use-extraction classification
tests for `research/anthropic_provider.py`.

No test file exercised `AnthropicResearchProvider.generate_structured`'s error/extraction
classification logic before this session (the only other references to
`AnthropicResearchProvider` are `test_research_configuration.py`, unrelated to this
behavior, and the opt-in real-API smoke test, which never runs offline). The Anthropic
SDK client is never constructed for real here — `provider._client` is replaced with a
minimal fake object whose `.messages.create()` either raises a real (but locally
constructed, no network) `anthropic.*Error` or returns a fake response object, so this
stays fully offline and credential-free like the rest of the default suite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import anthropic
import httpx
import pytest

from trading_research.research.anthropic_provider import TOOL_NAME, AnthropicProviderConfig, AnthropicResearchProvider
from trading_research.research.errors import MalformedOutputError, ProviderRateLimitError, ProviderTimeoutError, ProviderTransientError, ProviderUnavailableError
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.models import ResearchModelRequest

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _snapshot():
    return build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)


def _request() -> ResearchModelRequest:
    return ResearchModelRequest(
        role="bear", research_run_id="run-1", snapshot=_snapshot(), system_prompt="sys", user_prompt="user",
        json_schema={"type": "object"}, model_name="claude-sonnet-5", max_output_tokens=1000, temperature=0.0,
        prompt_name="research/bear", prompt_version="v1", prompt_hash="h1", system_prompt_hash="sh1",
        schema_version="role-report.v1", attempt_number=1,
    )


def _provider() -> AnthropicResearchProvider:
    return AnthropicResearchProvider(AnthropicProviderConfig(api_key="test-key", request_timeout_seconds=30))


class _FakeMessages:
    def __init__(self, *, raiser: Exception | None = None, response: Any = None):
        self._raiser = raiser
        self._response = response

    def create(self, **kwargs):
        if self._raiser is not None:
            raise self._raiser
        return self._response


def _install_fake(provider: AnthropicResearchProvider, *, raiser: Exception | None = None, response: Any = None) -> None:
    provider._client = type("FakeClient", (), {"messages": _FakeMessages(raiser=raiser, response=response)})()


@dataclass
class _FakeBlock:
    type: str
    name: str = ""
    input: Any = None


@dataclass
class _FakeUsage:
    input_tokens: int | None = 10
    output_tokens: int | None = 20
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


@dataclass
class _FakeResponse:
    content: list
    stop_reason: str = "end_turn"
    usage: Any = field(default_factory=_FakeUsage)
    id: str = "msg_123"


def _httpx_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _httpx_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code=status_code, request=_httpx_request())


def test_timeout_classified_retryable():
    provider = _provider()
    _install_fake(provider, raiser=anthropic.APITimeoutError(request=_httpx_request()))
    with pytest.raises(ProviderTimeoutError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.stage == "PROVIDER_REQUEST"
    assert exc_info.value.code == "PROVIDER_TIMEOUT"
    assert exc_info.value.retryable is True


def test_rate_limit_classified_retryable():
    provider = _provider()
    err = anthropic.RateLimitError("rate limited", response=_httpx_response(429), body=None)
    _install_fake(provider, raiser=err)
    with pytest.raises(ProviderRateLimitError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "PROVIDER_RATE_LIMITED"
    assert exc_info.value.retryable is True


def test_connection_error_classified_retryable():
    provider = _provider()
    _install_fake(provider, raiser=anthropic.APIConnectionError(request=_httpx_request()))
    with pytest.raises(ProviderTransientError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "PROVIDER_UNAVAILABLE"
    assert exc_info.value.retryable is True


def test_transient_server_error_5xx_classified_retryable():
    provider = _provider()
    err = anthropic.APIStatusError("server error", response=_httpx_response(500), body=None)
    _install_fake(provider, raiser=err)
    with pytest.raises(ProviderTransientError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "PROVIDER_SERVER_ERROR"
    assert exc_info.value.metadata["provider_status_code"] == 500
    assert exc_info.value.retryable is True


def test_transient_529_overloaded_classified_retryable():
    provider = _provider()
    err = anthropic.APIStatusError("overloaded", response=_httpx_response(529), body=None)
    _install_fake(provider, raiser=err)
    with pytest.raises(ProviderTransientError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "PROVIDER_SERVER_ERROR"
    assert exc_info.value.metadata["provider_status_code"] == 529


def test_non_retryable_400_classified_client_error_not_retried():
    provider = _provider()
    err = anthropic.APIStatusError("invalid_request_error", response=_httpx_response(400), body=None)
    _install_fake(provider, raiser=err)
    with pytest.raises(ProviderUnavailableError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "PROVIDER_CLIENT_ERROR"
    assert exc_info.value.retryable is False


def test_billing_auth_401_classified_client_error_not_retried():
    provider = _provider()
    err = anthropic.APIStatusError("credit balance too low", response=_httpx_response(401), body=None)
    _install_fake(provider, raiser=err)
    with pytest.raises(ProviderUnavailableError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "PROVIDER_CLIENT_ERROR"
    assert exc_info.value.retryable is False


def test_missing_tool_use_block_classified():
    provider = _provider()
    _install_fake(provider, response=_FakeResponse(content=[], stop_reason="end_turn"))
    with pytest.raises(MalformedOutputError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.stage == "TOOL_USE_EXTRACTION"
    assert exc_info.value.code == "EXPECTED_TOOL_USE_MISSING"
    assert exc_info.value.retryable is True


def test_output_truncation_classified_when_stop_reason_is_max_tokens():
    """The exact application-bug fix from the M6.1 scratchpad's root-cause
    classification #3: a truncated response used to lose stop_reason/tokens entirely
    before this fix — now both are captured and the failure is specifically classified
    as OUTPUT_TRUNCATED rather than the generic EXPECTED_TOOL_USE_MISSING."""
    provider = _provider()
    _install_fake(provider, response=_FakeResponse(content=[], stop_reason="max_tokens"))
    with pytest.raises(MalformedOutputError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "OUTPUT_TRUNCATED"
    assert exc_info.value.metadata["stop_reason"] == "max_tokens"
    assert exc_info.value.metadata["output_tokens"] == 20
    assert exc_info.value.metadata["input_tokens"] == 10


def test_unexpected_tool_name_classified():
    provider = _provider()
    response = _FakeResponse(content=[_FakeBlock(type="tool_use", name="some_other_tool", input={})])
    _install_fake(provider, response=response)
    with pytest.raises(MalformedOutputError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "UNEXPECTED_TOOL_NAME"


def test_multiple_matching_tool_blocks_classified():
    provider = _provider()
    response = _FakeResponse(content=[
        _FakeBlock(type="tool_use", name=TOOL_NAME, input={"a": 1}),
        _FakeBlock(type="tool_use", name=TOOL_NAME, input={"a": 2}),
    ])
    _install_fake(provider, response=response)
    with pytest.raises(MalformedOutputError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "MULTIPLE_TOOL_BLOCKS"


def test_malformed_tool_input_not_a_dict_classified():
    provider = _provider()
    response = _FakeResponse(content=[_FakeBlock(type="tool_use", name=TOOL_NAME, input="not-a-dict")])
    _install_fake(provider, response=response)
    with pytest.raises(MalformedOutputError) as exc_info:
        provider.generate_structured(_request())
    assert exc_info.value.code == "MALFORMED_TOOL_INPUT"
    assert exc_info.value.metadata["actual_type"] == "str"


def test_successful_response_returns_parsed_json_and_usage():
    provider = _provider()
    response = _FakeResponse(content=[_FakeBlock(type="tool_use", name=TOOL_NAME, input={"stance": "BULLISH"})])
    _install_fake(provider, response=response)
    result = provider.generate_structured(_request())
    assert result.parsed_json == {"stance": "BULLISH"}
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 20
