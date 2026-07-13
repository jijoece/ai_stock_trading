"""Framework-neutral research-model provider boundary (docs/milestone-5.md Step 2).

Domain code (`orchestration.py`, `overlay.py`, the CLI) only ever depends on
this Protocol — never on `anthropic_provider.py` directly and never on the
Anthropic SDK. This is what lets the default test suite run with no
Anthropic SDK usage, no credentials, and no network: tests inject
`deterministic_provider.DeterministicResearchProvider` or
`deterministic_provider.ScriptedResearchProvider` instead.
"""
from __future__ import annotations

from typing import Protocol

from .models import ResearchModelRequest, ResearchModelResponse


class ResearchModelProvider(Protocol):
    """A provider turns one structured request into one structured response.

    Implementations must raise a typed error from `research/errors.py`
    (`ProviderTimeoutError`, `ProviderRateLimitError`, `ProviderTransientError`,
    `MalformedOutputError`, `ProviderUnavailableError`) rather than letting an
    SDK-specific exception escape. No implementation may call an execution,
    broker, or order API — see docs/adr/0003-claude-research-boundary.md.
    """

    def generate_structured(self, request: ResearchModelRequest) -> ResearchModelResponse: ...
