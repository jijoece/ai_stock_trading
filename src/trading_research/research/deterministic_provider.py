"""Offline research-model providers (docs/milestone-5.md Step 7).

Neither class here imports the Anthropic SDK or performs network I/O —
this is what keeps the default test suite (and the CLI's default
`--provider deterministic`) fully offline and credential-free.

`DeterministicResearchProvider` derives a structured response purely from
the evidence snapshot's own `deterministic_factors` using a fixed, documented
rule (no LLM, no randomness). `ScriptedResearchProvider` lets tests script an
exact sequence of responses/errors per (role, attempt_number) to exercise
retries, timeouts, and malformed output deterministically.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping

from .errors import (
    MalformedOutputError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUnavailableError,
)
from .models import ResearchModelRequest, ResearchModelResponse
from .usage import build_usage_record

_BULLISH_THRESHOLD = 0.15
_BEARISH_THRESHOLD = -0.15

_MANAGER_RATING_BY_STANCE = {
    "BULLISH": "OVERWEIGHT",
    "BEARISH": "UNDERWEIGHT",
    "NEUTRAL": "HOLD",
    "ANALYSIS_INCOMPLETE": "ANALYSIS_INCOMPLETE",
}


def _stance_from_factors(factors: Mapping[str, float]) -> str:
    if not factors:
        return "ANALYSIS_INCOMPLETE"
    average = sum(factors.values()) / len(factors)
    if average > _BULLISH_THRESHOLD:
        return "BULLISH"
    if average < _BEARISH_THRESHOLD:
        return "BEARISH"
    return "NEUTRAL"


class DeterministicResearchProvider:
    """The default, no-LLM "provider: deterministic" arm. Never fabricates a
    claim: it emits zero `claims` (nothing to cite beyond the factors
    already visible to the deterministic scorer) and always discloses in
    `risks`/`uncertainties` that no qualitative evidence was reasoned over.
    """

    def generate_structured(self, request: ResearchModelRequest) -> ResearchModelResponse:
        start = time.monotonic()
        snapshot = request.snapshot
        stance = _stance_from_factors(snapshot.deterministic_factors)
        evidence_ids = [item.evidence_id for item in snapshot.evidence_items]
        disclosure = "deterministic-only analysis: no qualitative evidence was reasoned over by a model"

        if request.role == "manager":
            rating = _MANAGER_RATING_BY_STANCE[stance]
            payload = {
                "rating": rating,
                "confidence": 0.5,
                "thesis": (
                    f"Deterministic synthesis for {snapshot.symbol}: stance={stance} from "
                    f"{len(snapshot.deterministic_factors)} deterministic factor(s); no LLM was used."
                ),
                "bull_case": (
                    "Deterministic factors are net non-negative." if stance != "BEARISH"
                    else "No credible bull case from deterministic factors alone."
                ),
                "bear_case": (
                    "Deterministic factors are net non-positive." if stance != "BULLISH"
                    else "No credible bear case from deterministic factors alone."
                ),
                "catalysts": [],
                "risks": [disclosure],
                "invalidation_conditions": [],
                "claims": [],
                "evidence_ids": evidence_ids,
                "missing_data_reasons": list(snapshot.missing_data_reasons),
            }
        else:
            payload = {
                "stance": stance,
                "summary": (
                    f"Deterministic {request.role} view for {snapshot.symbol}: stance={stance} "
                    f"from factors {dict(sorted(snapshot.deterministic_factors.items()))}."
                ),
                "claims": [],
                "catalysts": [],
                "risks": [disclosure],
                "uncertainties": ["no LLM reasoning was applied"],
                "missing_data_reasons": list(snapshot.missing_data_reasons),
            }

        latency_ms = int((time.monotonic() - start) * 1000)
        usage = build_usage_record(
            provider="deterministic", model_name=request.model_name, role=request.role,
            input_tokens=None, output_tokens=None, cache_read_tokens=None, cache_write_tokens=None,
            latency_ms=latency_ms, provider_request_id=None, retry_count=request.attempt_number - 1,
            success=True,
        )
        return ResearchModelResponse(
            role=request.role, provider="deterministic", model_name=request.model_name,
            parsed_json=payload, raw_text="", usage=usage, provider_request_id=None,
        )


@dataclass(frozen=True)
class ScriptedStep:
    """One scripted outcome for `ScriptedResearchProvider`.

    kind: "response" | "timeout" | "rate_limit" | "transient" | "malformed" | "unavailable"
    """

    kind: str
    payload: Mapping | None = None
    raw_text: str | None = None
    usage_overrides: Mapping | None = None
    retryable: bool | None = None
    code: str | None = None


_ERROR_KINDS = {
    "timeout": ProviderTimeoutError,
    "rate_limit": ProviderRateLimitError,
    "transient": ProviderTransientError,
    "unavailable": ProviderUnavailableError,
}


class ScriptedResearchProvider:
    """Deterministic test double. `steps[(role, attempt_number)]` (attempt
    numbers are 1-indexed, matching `ResearchModelRequest.attempt_number`)
    determines the outcome of that call. A missing key is a test-authoring
    bug, not a silent no-op — it raises `AssertionError` immediately."""

    def __init__(self, steps: Mapping[tuple[str, int], ScriptedStep]):
        self._steps = dict(steps)
        self.calls: list[ResearchModelRequest] = []

    def generate_structured(self, request: ResearchModelRequest) -> ResearchModelResponse:
        self.calls.append(request)
        key = (request.role, request.attempt_number)
        if key not in self._steps:
            raise AssertionError(f"ScriptedResearchProvider has no step scripted for {key}")
        step = self._steps[key]

        if step.kind in _ERROR_KINDS:
            kwargs: dict = {}
            if step.retryable is not None:
                kwargs["retryable"] = step.retryable
            if step.code is not None:
                kwargs["code"] = step.code
            raise _ERROR_KINDS[step.kind](
                f"scripted {step.kind} for role={request.role} attempt={request.attempt_number}", **kwargs
            )
        if step.kind == "malformed":
            kwargs = {}
            if step.retryable is not None:
                kwargs["retryable"] = step.retryable
            if step.code is not None:
                kwargs["code"] = step.code
            raise MalformedOutputError(step.raw_text or "scripted malformed output", **kwargs)
        if step.kind != "response":
            raise AssertionError(f"unknown ScriptedStep.kind {step.kind!r}")

        overrides = dict(step.usage_overrides or {})
        usage = build_usage_record(
            provider="scripted", model_name=request.model_name, role=request.role,
            input_tokens=overrides.get("input_tokens", 100), output_tokens=overrides.get("output_tokens", 50),
            cache_read_tokens=overrides.get("cache_read_tokens"), cache_write_tokens=overrides.get("cache_write_tokens"),
            latency_ms=overrides.get("latency_ms", 10),
            provider_request_id=overrides.get("provider_request_id", f"scripted-{request.role}-{request.attempt_number}"),
            retry_count=request.attempt_number - 1, success=True,
        )
        return ResearchModelResponse(
            role=request.role, provider="scripted", model_name=request.model_name,
            parsed_json=step.payload or {}, raw_text=step.raw_text or "", usage=usage,
            provider_request_id=usage.provider_request_id,
        )
