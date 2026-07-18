"""Centralized Codex terminal-failure classifier (Milestone 12.1 Item 3).

Before this module existed, a nonzero process exit was classified by
scanning stdout/stderr text (`codex_provider.py::_classify_nonzero_exit`),
while a zero-exit `turn.failed` JSONL event was collapsed into one generic
`CODEX_PROCESS_EXITED` code regardless of its actual message — an
authentication failure surfaced through `turn.failed` was indistinguishable
from a transient one. This module is the single place both paths route
through, so the same logical failure always produces the same typed code
no matter which of the three surfaces it arrived on (nonzero exit, stderr
text, or a `turn.failed` event).

Input is always a short, already-bounded diagnostic string (bounded upstream
by `bounded_subprocess.py`'s byte limits for stdout/stderr, and by
`codex_jsonl_adapter.py`'s `maximum_jsonl_line_bytes` for a `turn.failed`
message) — this module additionally caps what it inspects so a
pathologically large or adversarial diagnostic can never make
classification itself expensive or unbounded. The raw diagnostic text is
NEVER included in the classification result or in any exception message
this module's caller raises — only the fixed, safe, per-category message
strings below are ever surfaced.
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import (
    ProviderRateLimitError,
    ProviderTransientError,
    ProviderUnavailableError,
    ResearchError,
)

# Bounds how much of a diagnostic string this module ever inspects —
# defense in depth on top of the byte limits already enforced upstream.
_MAX_DIAGNOSTIC_CHARS = 4096

_AUTH_MARKERS = ("not authenticated", "not logged in", "authentication required", "authentication failed")
_QUOTA_MARKERS = ("quota", "usage limit", "plan limit", "insufficient_quota", "credits are unavailable")
_RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "too many requests", "429")
_NETWORK_MARKERS = ("dns", "connection refused", "connection reset", "network is unreachable", "no route to host")
_TRANSIENT_MARKERS = ("temporarily unavailable", "service unavailable", "timed out", "internal server error", "502", "503", "504")
_UNSUPPORTED_MODEL_MARKERS = ("unsupported model", "unknown model", "model not found", "model_not_found")
_INVALID_CONFIG_MARKERS = ("invalid configuration", "invalid config", "unrecognized option", "invalid argument")
_SCHEMA_MARKERS = ("json schema", "json-schema", "schema invalid", "invalid schema")


@dataclass(frozen=True)
class CodexFailureClassification:
    code: str
    error_type: type[ResearchError]
    retryable: bool
    health_category: str
    message: str


# Deliberately reuses the pre-existing `CODEX_PROCESS_EXITED` code as the
# fallback for BOTH an unrecognized nonzero exit and an unrecognized
# `turn.failed` message — the same "we don't know why, but the process/turn
# failed" logical outcome must produce the same typed code regardless of
# which of the two surfaces it arrived through (Milestone 12.1 Item 3).
_UNKNOWN_TERMINAL = CodexFailureClassification(
    code="CODEX_PROCESS_EXITED", error_type=ProviderUnavailableError, retryable=False,
    health_category="UNKNOWN", message="Codex terminated with an unrecognized failure",
)


def classify_codex_diagnostic(diagnostic: str) -> CodexFailureClassification:
    """`diagnostic` is stdout+stderr text (nonzero exit) or a `turn.failed`
    message (zero exit) — either way, this is the one place that turns free
    text into a stable typed code. Fails closed to `_UNKNOWN_TERMINAL` for
    anything it does not recognize, rather than guessing."""
    bounded = diagnostic[:_MAX_DIAGNOSTIC_CHARS].lower()
    if any(marker in bounded for marker in _AUTH_MARKERS):
        return CodexFailureClassification(
            code="CODEX_NOT_AUTHENTICATED", error_type=ProviderUnavailableError, retryable=False,
            health_category="AUTHENTICATION", message="Codex authentication failed",
        )
    if any(marker in bounded for marker in _QUOTA_MARKERS):
        return CodexFailureClassification(
            code="CODEX_QUOTA_EXHAUSTED", error_type=ProviderUnavailableError, retryable=False,
            health_category="QUOTA", message="Codex quota is exhausted",
        )
    if any(marker in bounded for marker in _RATE_LIMIT_MARKERS):
        return CodexFailureClassification(
            code="CODEX_RATE_LIMITED", error_type=ProviderRateLimitError, retryable=True,
            health_category="RATE_LIMIT", message="Codex rate limited the request",
        )
    if any(marker in bounded for marker in _NETWORK_MARKERS):
        return CodexFailureClassification(
            code="CODEX_NETWORK_FAILURE", error_type=ProviderTransientError, retryable=True,
            health_category="NETWORK", message="Codex encountered a network failure",
        )
    if any(marker in bounded for marker in _TRANSIENT_MARKERS):
        return CodexFailureClassification(
            code="CODEX_TRANSIENT_FAILURE", error_type=ProviderTransientError, retryable=True,
            health_category="TRANSIENT", message="Codex encountered a transient service failure",
        )
    if any(marker in bounded for marker in _UNSUPPORTED_MODEL_MARKERS):
        return CodexFailureClassification(
            code="CODEX_UNSUPPORTED_MODEL", error_type=ProviderUnavailableError, retryable=False,
            health_category="UNSUPPORTED_MODEL", message="Codex rejected the configured model",
        )
    if any(marker in bounded for marker in _INVALID_CONFIG_MARKERS):
        return CodexFailureClassification(
            code="CODEX_INVALID_CONFIGURATION", error_type=ProviderUnavailableError, retryable=False,
            health_category="INVALID_CONFIGURATION", message="Codex rejected the request configuration",
        )
    if any(marker in bounded for marker in _SCHEMA_MARKERS):
        return CodexFailureClassification(
            code="CODEX_SCHEMA_REJECTED", error_type=ProviderUnavailableError, retryable=False,
            health_category="SCHEMA_REJECTION", message="Codex rejected the JSON Schema",
        )
    return _UNKNOWN_TERMINAL


__all__ = ["CodexFailureClassification", "classify_codex_diagnostic"]
