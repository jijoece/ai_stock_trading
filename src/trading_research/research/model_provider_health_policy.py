"""Centralized model-provider failure-code health policy (Milestone 12.1.1
Item 7).

The prior state of the repository allowlisted "which failure codes must
immediately pause the provider" only inside
`shadow/attempt_controller.py::IMMEDIATE_PROVIDER_PAUSE_CODES` — anything
computing model-provider *health* (as opposed to acting on a single
attempt's `after_attempt` hook) would otherwise have had to duplicate that
same allowlist. This module is the single source of truth both call sites
import from.

Classification is driven primarily by the already-typed, already-validated
`ResearchAttemptRecord.failure_retryable` boolean (set once, correctly, at
each raise site in `codex_provider.py`/`claude_code_provider.py`/
`research/errors.py` — see Milestone 12.1.1 Item 1), not by re-deriving
anything from `failure_code` or free-text `failure_reason`. The explicit
`STRUCTURAL_MODEL_PROVIDER_FAILURE_CODES` set exists only for audit-visible
documentation of the "named" structural categories the milestone calls
out (authentication, quota, unsupported version/model, invalid
configuration, schema/CLI contract rejection, missing usage metadata,
invalid reasoning-token contract) — every one of those codes is already
raised with `retryable=False`, so the set does not change the outcome of
`classify_model_provider_failure`, it only names it. A `failure_code` this
module has never seen, with `retryable` `False` or `None`, still fails
closed to `MODEL_PROVIDER_FAILURE_STRUCTURAL` purely from the boolean —
never silently treated as transient/hysteresis-eligible.
"""
from __future__ import annotations

MODEL_PROVIDER_FAILURE_STRUCTURAL = "STRUCTURAL"
MODEL_PROVIDER_FAILURE_TRANSIENT = "TRANSIENT"
UNCLASSIFIED_STRUCTURAL_FAILURE = "UNCLASSIFIED_STRUCTURAL_FAILURE"

# Milestone 12.1 Item 1 (Codex/Claude Code) + Milestone 12.1.1 Item 7:
# explicit, code-reviewed named structural failures — authentication,
# quota/credit exhaustion, unsupported CLI version, unsupported model,
# invalid configuration, schema/CLI contract rejection, missing required
# usage metadata, and invalid reasoning-token contract. Every member is
# raised with `retryable=False` at its source; this set never overrides an
# attempt's own `retryable` value, it only documents which codes are
# expected to appear here.
STRUCTURAL_MODEL_PROVIDER_FAILURE_CODES = frozenset(
    {
        # Authentication
        "CODEX_NOT_AUTHENTICATED",
        "CODEX_UNEXPECTED_AUTH_METHOD",
        "CLAUDE_CODE_NOT_AUTHENTICATED",
        "CLAUDE_CODE_AUTH_STATUS_FAILED",
        "CLAUDE_CODE_UNEXPECTED_AUTH_METHOD",
        "CLAUDE_CODE_OAUTH_TOKEN_MISSING",
        # Quota / credit exhaustion
        "CODEX_QUOTA_EXHAUSTED",
        "CLAUDE_CODE_CREDIT_EXHAUSTED",
        # Unsupported CLI version
        "CODEX_VERSION_UNSUPPORTED",
        "CLAUDE_CODE_VERSION_UNSUPPORTED",
        # Unsupported model
        "CODEX_UNSUPPORTED_MODEL",
        # Invalid configuration
        "CODEX_INVALID_CONFIGURATION",
        # Schema / CLI contract rejection
        "CODEX_SCHEMA_REJECTED",
        "CLAUDE_CODE_SCHEMA_REJECTED",
        "CLAUDE_CODE_LOCAL_SCHEMA_FAILED",
        # Missing required usage metadata
        "CODEX_USAGE_METADATA_MISSING",
        "CLAUDE_CODE_USAGE_METADATA_MISSING",
        # Invalid reasoning-token contract
        "CODEX_REASONING_TOKENS_INVALID",
    }
)

# Milestone 12.1.1 Item 7: named transient categories — timeout, rate limit,
# network failure, temporary service failure, retryable malformed output.
# Documentation only, same as the structural set above: every member is
# raised with `retryable=True` at its source.
TRANSIENT_MODEL_PROVIDER_FAILURE_CODES = frozenset(
    {
        "PROVIDER_TIMEOUT",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_SERVER_ERROR",
        "CODEX_PROCESS_TIMEOUT",
        "CODEX_RATE_LIMITED",
        "CODEX_TRANSIENT_FAILURE",
        "CODEX_NETWORK_FAILURE",
        "CLAUDE_CODE_PROCESS_TIMEOUT",
        "CLAUDE_CODE_RATE_LIMITED",
        "CLAUDE_CODE_TRANSIENT_FAILURE",
    }
)


def classify_model_provider_failure(failure_code: str | None, retryable: bool | None) -> str:
    """Returns `MODEL_PROVIDER_FAILURE_STRUCTURAL` (immediate pause,
    bypasses hysteresis counting) or `MODEL_PROVIDER_FAILURE_TRANSIENT`
    (ordinary hysteresis-eligible failure).

    `retryable is not True` (i.e. `False` or `None` — never fabricated as
    `True`) always fails closed to STRUCTURAL, independent of whether
    `failure_code` happens to be a recognized name. This is the actual
    decision boundary; `STRUCTURAL_MODEL_PROVIDER_FAILURE_CODES` is checked
    first only so an explicitly-named structural code is classified
    correctly even in the hypothetical case its `retryable` flag was ever
    left unset.
    """
    if failure_code in STRUCTURAL_MODEL_PROVIDER_FAILURE_CODES:
        return MODEL_PROVIDER_FAILURE_STRUCTURAL
    if retryable is True:
        return MODEL_PROVIDER_FAILURE_TRANSIENT
    return MODEL_PROVIDER_FAILURE_STRUCTURAL


__all__ = [
    "MODEL_PROVIDER_FAILURE_STRUCTURAL",
    "MODEL_PROVIDER_FAILURE_TRANSIENT",
    "UNCLASSIFIED_STRUCTURAL_FAILURE",
    "STRUCTURAL_MODEL_PROVIDER_FAILURE_CODES",
    "TRANSIENT_MODEL_PROVIDER_FAILURE_CODES",
    "classify_model_provider_failure",
]
