"""Open-source / OpenAI-compatible structured-output research provider.

Mirrors `anthropic_provider.py`'s shape and error contract exactly, so
`orchestration.py`, budget tracking, and the retry/failure-taxonomy layers
need no changes to accept this provider. Only `research/configuration.py`'s
`KNOWN_PROVIDERS` and a factory branch in the CLI need to learn this
provider's name before `research.provider` can select it.

Uses OpenAI-compatible tool calling (function calling) with `tool_choice`
forced to a single function as the structured-output mechanism — the same
role Anthropic's forced `tool_use` plays in `anthropic_provider.py`. Any
host that speaks the OpenAI chat-completions wire format works here by
swapping `base_url` (DeepSeek, Together, Fireworks, DeepInfra, OpenRouter, a
self-hosted vLLM/Ollama server, ...).

Unlike Anthropic's `strict: true` tool use, most open-weight model backends
do *not* guarantee the returned arguments validate against the JSON Schema
— schema conformance here is best-effort, not enforced by the API. That is
why this provider is deliberately unchanged from `anthropic_provider.py` in
one respect: it still raises `MalformedOutputError`/lets `SchemaValidationError`
propagate on anything that doesn't parse or validate, so the existing
`max_attempts_per_role` retry loop in `orchestration.py` absorbs the extra
unreliability — no new retry logic needed here.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import openai

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

TOOL_NAME = "submit_structured_research_output"

# Server errors worth a bounded retry at the orchestration layer; everything
# else (4xx other than 429) is treated as non-retryable client error.
_TRANSIENT_STATUS_CODES = {500, 502, 503, 504}


@dataclass(frozen=True)
class OpenAICompatibleProviderConfig:
    """`provider_name` is the value `research.provider` must be set to, and
    must also match the `provider` field of any `research_pricing.yaml`
    entry used to price this host's calls — e.g. "deepseek", "qwen_together"."""

    provider_name: str
    api_key: str
    base_url: str
    request_timeout_seconds: int
    pricing_entries: tuple[PricingEntry, ...] = field(default_factory=tuple)


class OpenAICompatibleResearchProvider:
    """Structured-output research provider for any OpenAI-compatible chat
    completions endpoint. Never called by the default test suite — only
    when `research.provider` names this provider's `provider_name`
    explicitly and `--provider <provider_name>` is passed to the CLI."""

    def __init__(self, config: OpenAICompatibleProviderConfig):
        if not config.api_key:
            raise ProviderUnavailableError(
                f"no API key configured for provider={config.provider_name!r} — cannot use this provider"
            )
        self._config = config
        self._client = openai.OpenAI(
            api_key=config.api_key, base_url=config.base_url, timeout=config.request_timeout_seconds,
        )

    def generate_structured(self, request: ResearchModelRequest) -> ResearchModelResponse:
        tool = {
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": "Submit your complete structured research output exactly once.",
                "parameters": dict(request.json_schema),
            },
        }

        start = time.monotonic()
        try:
            response = self._client.chat.completions.create(
                model=request.model_name,
                max_tokens=request.max_output_tokens,
                messages=[
                    {"role": "system", "content": request.system_prompt},
                    {"role": "user", "content": request.user_prompt},
                ],
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            )
        except openai.APITimeoutError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            raise ProviderTimeoutError(
                f"{self._config.provider_name} request timed out: {exc}",
                stage=STAGE_PROVIDER_REQUEST, code=CODE_PROVIDER_TIMEOUT, retryable=True,
                metadata={"latency_ms": latency_ms},
            ) from exc
        except openai.RateLimitError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            raise ProviderRateLimitError(
                f"{self._config.provider_name} rate limited: {exc}",
                stage=STAGE_PROVIDER_RESPONSE, code=CODE_PROVIDER_RATE_LIMITED, retryable=True,
                metadata={"latency_ms": latency_ms},
            ) from exc
        except openai.APIConnectionError as exc:
            raise ProviderUnavailableError(
                f"{self._config.provider_name} unreachable: {exc}",
                stage=STAGE_PROVIDER_REQUEST, code=CODE_PROVIDER_UNAVAILABLE, retryable=True,
            ) from exc
        except openai.APIStatusError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            if exc.status_code in _TRANSIENT_STATUS_CODES:
                raise ProviderTransientError(
                    f"{self._config.provider_name} server error {exc.status_code}: {exc}",
                    stage=STAGE_PROVIDER_RESPONSE, code=CODE_PROVIDER_SERVER_ERROR, retryable=True,
                    metadata={"latency_ms": latency_ms, "provider_status_code": exc.status_code},
                ) from exc
            raise ProviderTransientError(
                f"{self._config.provider_name} client error {exc.status_code}: {exc}",
                stage=STAGE_PROVIDER_RESPONSE, code=CODE_PROVIDER_CLIENT_ERROR, retryable=False,
                metadata={"latency_ms": latency_ms, "provider_status_code": exc.status_code},
            ) from exc

        latency_ms = int((time.monotonic() - start) * 1000)
        choice = response.choices[0]

        if choice.finish_reason == "length":
            raise MalformedOutputError(
                f"{self._config.provider_name} truncated its response before completing the tool call",
                stage=STAGE_TOOL_USE_EXTRACTION, code=CODE_OUTPUT_TRUNCATED, retryable=True,
                metadata={"latency_ms": latency_ms, "max_output_tokens": request.max_output_tokens},
            )

        tool_calls = choice.message.tool_calls or []
        if not tool_calls:
            raise MalformedOutputError(
                f"{self._config.provider_name} returned no tool call — expected exactly one "
                f"{TOOL_NAME!r} call",
                stage=STAGE_TOOL_USE_EXTRACTION, code=CODE_EXPECTED_TOOL_USE_MISSING, retryable=True,
                metadata={"latency_ms": latency_ms},
            )
        if len(tool_calls) > 1:
            raise MalformedOutputError(
                f"{self._config.provider_name} returned {len(tool_calls)} tool calls — expected exactly one",
                stage=STAGE_TOOL_USE_EXTRACTION, code=CODE_MULTIPLE_TOOL_BLOCKS, retryable=True,
                metadata={"latency_ms": latency_ms, "tool_use_block_count": len(tool_calls)},
            )
        call = tool_calls[0]
        if call.function.name != TOOL_NAME:
            raise MalformedOutputError(
                f"{self._config.provider_name} called unexpected tool {call.function.name!r}",
                stage=STAGE_TOOL_USE_EXTRACTION, code=CODE_UNEXPECTED_TOOL_NAME, retryable=True,
                metadata={"latency_ms": latency_ms, "tool_name": call.function.name},
            )
        try:
            parsed = json.loads(call.function.arguments)
        except (json.JSONDecodeError, TypeError) as exc:
            raise MalformedOutputError(
                f"{self._config.provider_name} tool call arguments were not valid JSON: {exc}",
                stage=STAGE_TOOL_USE_EXTRACTION, code=CODE_MALFORMED_TOOL_INPUT, retryable=True,
                metadata={"latency_ms": latency_ms},
            ) from exc
        if not isinstance(parsed, dict):
            raise MalformedOutputError(
                f"{self._config.provider_name} tool call arguments decoded to "
                f"{type(parsed).__name__}, not an object",
                stage=STAGE_TOOL_USE_EXTRACTION, code=CODE_MALFORMED_TOOL_INPUT, retryable=True,
                metadata={"latency_ms": latency_ms},
            )

        usage = response.usage
        usage_record = build_usage_record(
            provider=self._config.provider_name, model_name=request.model_name, role=request.role,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            # Most OpenAI-compatible hosts don't report prompt-cache token
            # counts on the /chat/completions response — left unset rather
            # than guessed, same "never invented" rule as usage.py enforces.
            cache_read_tokens=None, cache_write_tokens=None,
            latency_ms=latency_ms, provider_request_id=response.id, retry_count=request.attempt_number,
            success=True, pricing_entries=self._config.pricing_entries,
        )
        return ResearchModelResponse(
            role=request.role, provider=self._config.provider_name, model_name=request.model_name,
            parsed_json=parsed, raw_text=call.function.arguments, usage=usage_record,
            provider_request_id=response.id,
        )
