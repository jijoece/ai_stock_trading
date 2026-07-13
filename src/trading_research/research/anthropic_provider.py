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
from .failure_taxonomy import (
    CODE_EXPECTED_TOOL_USE_MISSING,
    CODE_MALFORMED_TOOL_INPUT,
    CODE_MULTIPLE_TOOL_BLOCKS,
    CODE_OUTPUT_TRUNCATED,
    CODE_PROVIDER_CLIENT_ERROR,
    CODE_PROVIDER_RATE_LIMITED,
    CODE_PROVIDER_SERVER_ERROR,
    CODE_PROVIDER_TIMEOUT,
    CODE_PROVIDER_UNAVAILABLE,
    CODE_UNEXPECTED_TOOL_NAME,
    STAGE_PROVIDER_REQUEST,
    STAGE_PROVIDER_RESPONSE,
    STAGE_TOOL_USE_EXTRACTION,
)
from .models import ResearchModelRequest, ResearchModelResponse
from .usage import PricingEntry, build_usage_record

# Anthropic truncates a response mid-generation on this stop_reason — a truncated
# tool_use.input is the single most likely real cause of "no valid tool_use block" for a
# verbose analyst role (Step 7: "For output truncation, record ... whether retrying with
# the same limit would be pointless").
_TRUNCATION_STOP_REASON = "max_tokens"

TOOL_NAME = "submit_structured_research_output"

_TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 529}

# Anthropic strict-tool JSON Schema only supports a subset of Draft-07 —
# numeric bounds, array-length bounds, and string-length bounds are all
# rejected with a 400 (confirmed against the real API during Milestone 5
# validation: "For 'array' type, property 'maxItems' is not supported").
# These bounds remain fully enforced locally: `output_validation.py`
# validates the returned tool_use.input against the *original*, unstripped
# schema, so nothing sent back by Claude escapes them — this only relaxes
# what we ask the API's strict-mode schema compiler to accept up front.
_UNSUPPORTED_STRICT_KEYWORDS = frozenset({
    "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "pattern",
})


def _strict_compatible_schema(schema):
    if isinstance(schema, dict):
        return {
            key: _strict_compatible_schema(value)
            for key, value in schema.items()
            if key not in _UNSUPPORTED_STRICT_KEYWORDS
        }
    if isinstance(schema, list):
        return [_strict_compatible_schema(item) for item in schema]
    return schema


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
        # strict=True (top-level on the tool, not on tool_choice) makes the
        # API guarantee tool_use.input validates exactly against
        # input_schema — including every entry in "required" — rather than
        # letting the model omit a required array field it judged empty.
        # Confirmed necessary against the real API during Milestone 5
        # validation: without it, Claude omitted uncertainties/
        # missing_data_reasons when it had nothing to put in them.
        tool = {
            "name": TOOL_NAME,
            "description": "Submit your complete structured research output exactly once.",
            "input_schema": _strict_compatible_schema(dict(request.json_schema)),
            "strict": True,
        }

        start = time.monotonic()
        try:
            # No `temperature` here: current-generation models (e.g.
            # claude-sonnet-5, Opus 4.7/4.8, Fable 5) reject non-default
            # sampling parameters outright (400 invalid_request_error) —
            # confirmed against the real API during Milestone 5 validation.
            # request.temperature is recorded for audit but intentionally
            # not sent on the wire.
            response = self._client.messages.create(
                model=request.model_name,
                max_tokens=request.max_output_tokens,
                system=request.system_prompt,
                messages=[{"role": "user", "content": request.user_prompt}],
                tools=[tool],
                tool_choice={"type": "tool", "name": TOOL_NAME},
            )
        except anthropic.APITimeoutError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            raise ProviderTimeoutError(
                f"Anthropic request timed out: {exc}", stage=STAGE_PROVIDER_REQUEST, code=CODE_PROVIDER_TIMEOUT,
                retryable=True, metadata={"latency_ms": latency_ms},
            ) from exc
        except anthropic.RateLimitError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            raise ProviderRateLimitError(
                f"Anthropic rate limit: {exc}", stage=STAGE_PROVIDER_RESPONSE, code=CODE_PROVIDER_RATE_LIMITED,
                retryable=True, metadata={"latency_ms": latency_ms},
            ) from exc
        except anthropic.APIConnectionError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            raise ProviderTransientError(
                f"Anthropic connection error: {exc}", stage=STAGE_PROVIDER_REQUEST, code=CODE_PROVIDER_UNAVAILABLE,
                retryable=True, metadata={"latency_ms": latency_ms},
            ) from exc
        except anthropic.APIStatusError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            status = getattr(exc, "status_code", None)
            status_metadata = {"latency_ms": latency_ms, "provider_status_code": status or 0}
            if status in _TRANSIENT_STATUS_CODES:
                raise ProviderTransientError(
                    f"Anthropic transient error (status {status}): {exc}", stage=STAGE_PROVIDER_RESPONSE,
                    code=CODE_PROVIDER_SERVER_ERROR, retryable=True, metadata=status_metadata,
                ) from exc
            # Any other client error (401/403/404/400 invalid-request/billing,
            # etc.) is not something a bounded retry can fix — treat it as
            # provider-unavailable rather than silently burning retry budget.
            raise ProviderUnavailableError(
                f"Anthropic API unavailable (status {status}): {exc}", stage=STAGE_PROVIDER_RESPONSE,
                code=CODE_PROVIDER_CLIENT_ERROR, retryable=False, metadata=status_metadata,
            ) from exc

        latency_ms = int((time.monotonic() - start) * 1000)

        # Extracted before tool-use validation (Milestone 6.1 fix — see
        # docs/milestone-6.1.md Step 7 / the M6.1 scratchpad's "Historical persistence
        # findings" #3): a malformed/truncated response used to raise MalformedOutputError
        # before any usage/stop_reason was ever read, making a real truncation
        # structurally undiagnosable after the fact. Every failure path below now carries
        # this metadata.
        usage_obj = getattr(response, "usage", None)
        input_tokens = getattr(usage_obj, "input_tokens", None)
        output_tokens = getattr(usage_obj, "output_tokens", None)
        cache_read_tokens = getattr(usage_obj, "cache_read_input_tokens", None)
        cache_write_tokens = getattr(usage_obj, "cache_creation_input_tokens", None)
        provider_request_id = getattr(response, "id", None)
        stop_reason = getattr(response, "stop_reason", None)

        tool_use_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        matching_blocks = [b for b in tool_use_blocks if b.name == TOOL_NAME]
        extraction_metadata = {
            "stop_reason": str(stop_reason) if stop_reason is not None else "unknown",
            "input_tokens": input_tokens if input_tokens is not None else 0,
            "output_tokens": output_tokens if output_tokens is not None else 0,
            "max_output_tokens": request.max_output_tokens,
            "tool_use_block_count": len(tool_use_blocks),
        }

        if not tool_use_blocks:
            truncated = stop_reason == _TRUNCATION_STOP_REASON
            raise MalformedOutputError(
                "Claude response contained no tool_use block for the forced structured-output tool"
                + (f" (stop_reason={stop_reason!r} — output was truncated before any tool call)" if truncated else ""),
                stage=STAGE_TOOL_USE_EXTRACTION,
                code=CODE_OUTPUT_TRUNCATED if truncated else CODE_EXPECTED_TOOL_USE_MISSING,
                retryable=True, metadata=extraction_metadata,
            )
        if not matching_blocks:
            raise MalformedOutputError(
                f"Claude response's tool_use block(s) did not use the forced tool name {TOOL_NAME!r}",
                stage=STAGE_TOOL_USE_EXTRACTION, code=CODE_UNEXPECTED_TOOL_NAME, retryable=True,
                metadata={**extraction_metadata, "tool_name": tool_use_blocks[0].name},
            )
        if len(matching_blocks) > 1:
            raise MalformedOutputError(
                "Claude response contained more than one tool_use block for the forced structured-output tool",
                stage=STAGE_TOOL_USE_EXTRACTION, code=CODE_MULTIPLE_TOOL_BLOCKS, retryable=True,
                metadata=extraction_metadata,
            )
        tool_use_block = matching_blocks[0]
        parsed = tool_use_block.input
        if not isinstance(parsed, dict):
            raise MalformedOutputError(
                "Claude tool_use input was not a JSON object",
                stage=STAGE_TOOL_USE_EXTRACTION, code=CODE_MALFORMED_TOOL_INPUT, retryable=True,
                metadata={**extraction_metadata, "actual_type": type(parsed).__name__},
            )

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
