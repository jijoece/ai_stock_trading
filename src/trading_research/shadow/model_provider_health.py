"""Model-provider health evidence (Milestone 12.1.1 Item 7).

Distinct from `evidence_providers/health.py` (SEC/Alpaca/Reddit request
rows): this module's input is `research_attempts` rows for the *model*
provider (Codex/Claude Code/Anthropic) itself, scoped to one scheduler run
via `storage/shadow_operations_repositories.py::list_research_attempts_for_scheduler_run`.
A healthy evidence-provider cycle says nothing about whether the model
provider itself is authenticating, within quota, or returning a usable
contract — this is the independent signal for that.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..research.model_provider_health_policy import (
    MODEL_PROVIDER_FAILURE_STRUCTURAL,
    classify_model_provider_failure,
)

TIMEOUT_CODES = frozenset({"PROVIDER_TIMEOUT", "CODEX_PROCESS_TIMEOUT", "CLAUDE_CODE_PROCESS_TIMEOUT"})
RATE_LIMIT_CODES = frozenset({"PROVIDER_RATE_LIMITED", "CODEX_RATE_LIMITED", "CLAUDE_CODE_RATE_LIMITED"})
AUTHENTICATION_FAILURE_CODES = frozenset(
    {
        "CODEX_NOT_AUTHENTICATED", "CODEX_UNEXPECTED_AUTH_METHOD",
        "CLAUDE_CODE_NOT_AUTHENTICATED", "CLAUDE_CODE_AUTH_STATUS_FAILED",
        "CLAUDE_CODE_UNEXPECTED_AUTH_METHOD", "CLAUDE_CODE_OAUTH_TOKEN_MISSING",
    }
)
QUOTA_FAILURE_CODES = frozenset({"CODEX_QUOTA_EXHAUSTED", "CLAUDE_CODE_CREDIT_EXHAUSTED"})
CONFIGURATION_FAILURE_CODES = frozenset(
    {"CODEX_INVALID_CONFIGURATION", "CODEX_VERSION_UNSUPPORTED", "CLAUDE_CODE_VERSION_UNSUPPORTED", "CODEX_UNSUPPORTED_MODEL"}
)
PROTOCOL_FAILURE_CODES = frozenset(
    {
        "CODEX_SCHEMA_REJECTED", "CLAUDE_CODE_SCHEMA_REJECTED", "CLAUDE_CODE_LOCAL_SCHEMA_FAILED",
        "CODEX_REASONING_TOKENS_INVALID",
    }
)
MISSING_USAGE_FAILURE_CODES = frozenset({"CODEX_USAGE_METADATA_MISSING", "CLAUDE_CODE_USAGE_METADATA_MISSING"})


@dataclass(frozen=True)
class ModelProviderHealthEvidence:
    attempt_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    retryable_failure_count: int = 0
    non_retryable_failure_count: int = 0
    timeout_count: int = 0
    rate_limit_count: int = 0
    authentication_failure_count: int = 0
    quota_failure_count: int = 0
    configuration_failure_count: int = 0
    protocol_failure_count: int = 0
    missing_usage_failure_count: int = 0
    structural_failure: bool = False
    structural_failure_codes: tuple[str, ...] = ()

    @property
    def success_rate(self) -> float | None:
        return (self.success_count / self.attempt_count) if self.attempt_count > 0 else None


def evaluate_model_provider_health(rows: "list[dict]") -> ModelProviderHealthEvidence:
    """Pure function over already-fetched `research_attempts` rows (each a
    dict with `success`/`failure_code`/`failure_retryable`) for one
    scheduler run. Never queries the database itself — the caller supplies
    `rows` (typically via `list_research_attempts_for_scheduler_run`), so
    this stays a pure, independently testable function exactly like
    `evidence_providers/health.py::compute_cycle_provider_telemetry`."""
    attempt_count = len(rows)
    success_count = sum(1 for r in rows if r["success"])
    failure_rows = [r for r in rows if not r["success"]]

    def _retryable(r: dict) -> bool | None:
        # sqlite3 has no native boolean type — `failure_retryable` round-trips
        # as `0`/`1`/`None`, never a real Python `True`/`False`.
        value = r.get("failure_retryable")
        return None if value is None else bool(value)

    retryable_count = sum(1 for r in failure_rows if _retryable(r) is True)
    non_retryable_count = len(failure_rows) - retryable_count
    structural_codes: list[str] = []
    for r in failure_rows:
        code = r.get("failure_code")
        classification = classify_model_provider_failure(code, _retryable(r))
        if classification == MODEL_PROVIDER_FAILURE_STRUCTURAL and code:
            structural_codes.append(code)
    return ModelProviderHealthEvidence(
        attempt_count=attempt_count, success_count=success_count, failure_count=len(failure_rows),
        retryable_failure_count=retryable_count, non_retryable_failure_count=non_retryable_count,
        timeout_count=sum(1 for r in failure_rows if r.get("failure_code") in TIMEOUT_CODES),
        rate_limit_count=sum(1 for r in failure_rows if r.get("failure_code") in RATE_LIMIT_CODES),
        authentication_failure_count=sum(
            1 for r in failure_rows if r.get("failure_code") in AUTHENTICATION_FAILURE_CODES
        ),
        quota_failure_count=sum(1 for r in failure_rows if r.get("failure_code") in QUOTA_FAILURE_CODES),
        configuration_failure_count=sum(
            1 for r in failure_rows if r.get("failure_code") in CONFIGURATION_FAILURE_CODES
        ),
        protocol_failure_count=sum(1 for r in failure_rows if r.get("failure_code") in PROTOCOL_FAILURE_CODES),
        missing_usage_failure_count=sum(
            1 for r in failure_rows if r.get("failure_code") in MISSING_USAGE_FAILURE_CODES
        ),
        structural_failure=bool(structural_codes),
        structural_failure_codes=tuple(sorted(set(structural_codes))),
    )


__all__ = ["ModelProviderHealthEvidence", "evaluate_model_provider_health"]
