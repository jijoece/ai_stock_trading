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

import hashlib
import re
from dataclasses import dataclass

from ..research.model_provider_health_policy import (
    MODEL_PROVIDER_FAILURE_STRUCTURAL,
    UNCLASSIFIED_STRUCTURAL_FAILURE,
    classify_model_provider_failure,
)
from .budget import PRICING_EXEMPT_PROVIDERS, REAL_CLAUDE_PROVIDERS

PRODUCTION_MODEL_PROVIDERS = frozenset(REAL_CLAUDE_PROVIDERS)
FIXTURE_MODEL_PROVIDERS = frozenset(PRICING_EXEMPT_PROVIDERS)

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
    expected_provider: str = ""
    expected_model: str = ""
    provider_configuration_hash: str = ""
    applicable: bool = True
    excluded_attempt_count: int = 0
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
    structural_failure_count: int = 0
    transient_failure_count: int = 0
    unclassified_structural_failure_count: int = 0
    structural_failure: bool = False
    structural_failure_codes: tuple[str, ...] = ()

    @property
    def success_rate(self) -> float | None:
        return (self.success_count / self.attempt_count) if self.attempt_count > 0 else None

    def bounded_metrics(self) -> dict:
        """Safe persisted hysteresis evidence; never includes messages/output."""
        return {
            "provider": self.expected_provider,
            "model": self.expected_model,
            "attempt_count": self.attempt_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "structural_failure_count": self.structural_failure_count,
            "transient_failure_count": self.transient_failure_count,
            "unclassified_structural_failure_count": self.unclassified_structural_failure_count,
            "structural_failure_codes": list(self.structural_failure_codes),
        }


def _scope_component(value: str) -> str:
    normalized = value.strip().lower()
    normalized = re.sub(r"[^a-z0-9._-]+", "-", normalized).strip("-._")
    return (normalized or "unknown")[:80]


def _configuration_hash_component(value: str) -> str:
    normalized = value.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", normalized):
        return normalized
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def model_provider_health_scope(
    *, expected_provider: str, expected_model: str, provider_configuration_hash: str,
) -> str:
    """Provider/model/config-specific persistent health policy boundary."""
    prefix = "MODEL_PROVIDER_FAILURE" if expected_provider in PRODUCTION_MODEL_PROVIDERS else "MODEL_PROVIDER_FIXTURE"
    return ":".join((
        prefix,
        _scope_component(expected_provider),
        _scope_component(expected_model),
        _configuration_hash_component(provider_configuration_hash),
    ))


def evaluate_model_provider_health(
    rows: "list[dict]", *, expected_provider: str, expected_model: str,
    provider_configuration_hash: str,
) -> ModelProviderHealthEvidence:
    """Pure function over already-fetched `research_attempts` rows (each a
    dict with `success`/`failure_code`/`failure_retryable`) for one
    scheduler run. Never queries the database itself — the caller supplies
    `rows` (typically via `list_research_attempts_for_scheduler_run`), so
    this stays a pure, independently testable function exactly like
    `evidence_providers/health.py::compute_cycle_provider_telemetry`."""
    matching_rows = [
        row for row in rows
        if row.get("provider") == expected_provider and row.get("model_name") == expected_model
    ]
    identity = {
        "expected_provider": expected_provider,
        "expected_model": expected_model,
        "provider_configuration_hash": provider_configuration_hash,
        "applicable": expected_provider in PRODUCTION_MODEL_PROVIDERS,
        "excluded_attempt_count": len(rows) - len(matching_rows),
    }
    if expected_provider in FIXTURE_MODEL_PROVIDERS or expected_provider not in PRODUCTION_MODEL_PROVIDERS:
        return ModelProviderHealthEvidence(**identity)

    attempt_count = len(matching_rows)
    success_count = sum(1 for r in matching_rows if r["success"])
    failure_rows = [r for r in matching_rows if not r["success"]]

    def _retryable(r: dict) -> bool | None:
        # sqlite3 has no native boolean type — `failure_retryable` round-trips
        # as `0`/`1`/`None`, never a real Python `True`/`False`.
        value = r.get("failure_retryable")
        return None if value is None else bool(value)

    retryable_count = sum(1 for r in failure_rows if _retryable(r) is True)
    non_retryable_count = len(failure_rows) - retryable_count
    structural_codes: list[str] = []
    structural_count = 0
    transient_count = 0
    unclassified_structural_count = 0
    for r in failure_rows:
        code = r.get("failure_code")
        classification = classify_model_provider_failure(code, _retryable(r))
        if classification == MODEL_PROVIDER_FAILURE_STRUCTURAL:
            structural_count += 1
            if code:
                structural_codes.append(code)
            else:
                unclassified_structural_count += 1
                structural_codes.append(UNCLASSIFIED_STRUCTURAL_FAILURE)
        else:
            transient_count += 1
    return ModelProviderHealthEvidence(
        **identity,
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
        structural_failure_count=structural_count, transient_failure_count=transient_count,
        unclassified_structural_failure_count=unclassified_structural_count,
        structural_failure=structural_count > 0,
        structural_failure_codes=tuple(sorted(set(structural_codes))),
    )


__all__ = [
    "FIXTURE_MODEL_PROVIDERS", "PRODUCTION_MODEL_PROVIDERS", "ModelProviderHealthEvidence",
    "evaluate_model_provider_health", "model_provider_health_scope",
]
