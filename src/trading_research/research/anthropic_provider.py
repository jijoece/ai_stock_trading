"""Real Anthropic Claude structured-output provider (docs/milestone-5.md Step 19).

This is the *only* file in the research layer that imports the `anthropic`
SDK. Everything else depends solely on `provider_protocol.ResearchModelProvider`,
so the default (offline) test suite, and every other module in this
package, never needs the SDK to be configured with real credentials.

Structured output uses forced tool use: a single tool whose `input_schema`
is exactly our own JSON Schema (the same schema `output_validation.py`
validates against), with `tool_choice` forced to that tool's name. This is
Anthropic's documented, stable mechanism for reliable structured JSON output
— see the Claude API tool-use documentation — and it avoids depending on the
model to emit clean, un-prefixed JSON in a plain-text response.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import anthropic

from .errors import (
    MalformedOutputError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUnavailableError,
)
from .models import ResearchModelRequest, ResearchModelResponse
from .usage import PricingEntry, build_usage_record

TOOL_NAME = "submit_structured_research_output"

_TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 529}


@dataclass(frozen=True)
class AnthropicProviderConfig:
    api_key: str
    request_timeout_seconds: int
    pricing_entries: tuple[PricingEntry, ...] = field(default_factory=tuple)


class AnthropicResearchProvider:
    """Real Claude API research provider. Never called by the default test
    suite — only by the opt-in `@pytest.mark.claude_api` smoke test and by
    the CLI when `--provider anthropic` is passed explicitly."""

    def __init__(self, config: AnthropicProviderConfig):
        if not config.api_key:
            raise ProviderUnavailableError("ANTHROPIC_API_KEY is not configured — cannot use provider=anthropic")
        self._config = config
        self._client = anthropic.Anthropic(api_key=config.api_key, timeout=config.request_timeout_seconds)

    def generate_structured(self, request: ResearchModelRequest) -> ResearchModelResponse:
        tool = {
            "name": TOOL_NAME,
            "description": "Submit your complete structured research output exactly once.",
            "input_schema": dict(request.json_schema),
        }

        start = time.monotonic()
        try:
            response = self._client.messages.create(
                model=request.model_name,
                max_tokens=request.max_output_tokens,
                temperature=request.temperature,
                system=request.system_prompt,
                messages=[{"role": "user", "content": request.user_prompt}],
                tools=[tool],
                tool_choice={"type": "tool", "name": TOOL_NAME},
            )
        except anthropic.APITimeoutError as exc:
            raise ProviderTimeoutError(f"Anthropic request timed out: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderRateLimitError(f"Anthropic rate limit: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderTransientError(f"Anthropic connection error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            status = getattr(exc, "status_code", None)
            if status in _TRANSIENT_STATUS_CODES:
                raise ProviderTransientError(f"Anthropic transient error (status {status}): {exc}") from exc
            # Any other client error (401/403/404/400 invalid-request/billing,
            # etc.) is not something a bounded retry can fix — treat it as
            # provider-unavailable rather than silently burning retry budget.
            raise ProviderUnavailableError(f"Anthropic API unavailable (status {status}): {exc}") from exc

        latency_ms = int((time.monotonic() - start) * 1000)

        tool_use_block = next(
            (block for block in response.content if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME),
            None,
        )
        if tool_use_block is None:
            raise MalformedOutputError(
                "Claude response contained no tool_use block for the forced structured-output tool"
            )
        parsed = tool_use_block.input
        if not isinstance(parsed, dict):
            raise MalformedOutputError("Claude tool_use input was not a JSON object")

        usage_obj = getattr(response, "usage", None)
        input_tokens = getattr(usage_obj, "input_tokens", None)
        output_tokens = getattr(usage_obj, "output_tokens", None)
        cache_read_tokens = getattr(usage_obj, "cache_read_input_tokens", None)
        cache_write_tokens = getattr(usage_obj, "cache_creation_input_tokens", None)
        provider_request_id = getattr(response, "id", None)

        usage = build_usage_record(
            provider="anthropic", model_name=request.model_name, role=request.role,
            input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens, latency_ms=latency_ms, provider_request_id=provider_request_id,
            retry_count=request.attempt_number - 1, success=True, pricing_entries=self._config.pricing_entries,
        )
        return ResearchModelResponse(
            role=request.role, provider="anthropic", model_name=request.model_name, parsed_json=parsed,
            raw_text=json.dumps(parsed, sort_keys=True), usage=usage, provider_request_id=provider_request_id,
        )
