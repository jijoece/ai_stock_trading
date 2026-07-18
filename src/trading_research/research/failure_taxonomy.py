"""Structured research-failure taxonomy and immutable failure model (Milestone 6.1
docs/milestone-6.1.md Steps 4-5).

Before this module, a failed research attempt's diagnostic detail lived entirely in
`ResearchAttemptRecord.failure_reason`, a single free-text string built by joining
exception messages or claim-rejection reasons. That is enough for a human reading one
row, but it cannot be queried by stage, by code, or by claim, and multiple independent
claim rejections on the same attempt collapse into one opaque string. This module gives
every failure — provider-boundary, structured-output, claim-evidence, retry-exhaustion,
required-role, or manager-skip — a validated stage, a validated code, and a bounded,
allowlisted set of safe metadata, so it can be persisted once per failure (not once per
attempt) and queried precisely (`storage/research_repositories.py::list_run_failures`
etc.) without ever storing a secret, a raw prompt, or hidden chain-of-thought.

Reuses concepts already present in `research/errors.py` (typed exceptions) and
`research/claim_validation.py` (independent claim re-derivation) rather than duplicating
a parallel taxonomy — this module only adds the missing structured/queryable layer on
top of what already exists.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .configuration import KNOWN_ROLES

STAGE_PROVIDER_REQUEST = "PROVIDER_REQUEST"
STAGE_PROVIDER_RESPONSE = "PROVIDER_RESPONSE"
STAGE_TOOL_USE_EXTRACTION = "TOOL_USE_EXTRACTION"
STAGE_JSON_DECODING = "JSON_DECODING"
STAGE_STRUCTURED_SCHEMA = "STRUCTURED_SCHEMA"
STAGE_ROLE_REPORT_VALIDATION = "ROLE_REPORT_VALIDATION"
STAGE_CLAIM_EVIDENCE_VALIDATION = "CLAIM_EVIDENCE_VALIDATION"
STAGE_NUMERIC_CLAIM_VALIDATION = "NUMERIC_CLAIM_VALIDATION"
STAGE_PROMPT_INJECTION_VALIDATION = "PROMPT_INJECTION_VALIDATION"
STAGE_RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
STAGE_REQUIRED_ROLE_FAILED = "REQUIRED_ROLE_FAILED"
STAGE_MANAGER_SKIPPED = "MANAGER_SKIPPED"
STAGE_PERSISTENCE = "PERSISTENCE"
STAGE_BUDGET_GATED = "BUDGET_GATED"
STAGE_UNKNOWN = "UNKNOWN"

STAGES = (
    STAGE_PROVIDER_REQUEST,
    STAGE_PROVIDER_RESPONSE,
    STAGE_TOOL_USE_EXTRACTION,
    STAGE_JSON_DECODING,
    STAGE_STRUCTURED_SCHEMA,
    STAGE_ROLE_REPORT_VALIDATION,
    STAGE_CLAIM_EVIDENCE_VALIDATION,
    STAGE_NUMERIC_CLAIM_VALIDATION,
    STAGE_PROMPT_INJECTION_VALIDATION,
    STAGE_RETRY_EXHAUSTED,
    STAGE_REQUIRED_ROLE_FAILED,
    STAGE_MANAGER_SKIPPED,
    STAGE_PERSISTENCE,
    STAGE_BUDGET_GATED,
    STAGE_UNKNOWN,
)

CODE_PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
CODE_PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
CODE_PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
CODE_PROVIDER_CLIENT_ERROR = "PROVIDER_CLIENT_ERROR"
CODE_PROVIDER_SERVER_ERROR = "PROVIDER_SERVER_ERROR"
CODE_OUTPUT_TRUNCATED = "OUTPUT_TRUNCATED"
CODE_EXPECTED_TOOL_USE_MISSING = "EXPECTED_TOOL_USE_MISSING"
CODE_UNEXPECTED_TOOL_NAME = "UNEXPECTED_TOOL_NAME"
CODE_MULTIPLE_TOOL_BLOCKS = "MULTIPLE_TOOL_BLOCKS"
CODE_MALFORMED_TOOL_INPUT = "MALFORMED_TOOL_INPUT"
CODE_INVALID_JSON = "INVALID_JSON"
CODE_SCHEMA_REQUIRED_FIELD_MISSING = "SCHEMA_REQUIRED_FIELD_MISSING"
CODE_SCHEMA_TYPE_MISMATCH = "SCHEMA_TYPE_MISMATCH"
CODE_SCHEMA_ENUM_INVALID = "SCHEMA_ENUM_INVALID"
CODE_SCHEMA_EXTRA_FIELD = "SCHEMA_EXTRA_FIELD"
CODE_SCHEMA_LIST_LIMIT_EXCEEDED = "SCHEMA_LIST_LIMIT_EXCEEDED"
CODE_UNKNOWN_EVIDENCE_ID = "UNKNOWN_EVIDENCE_ID"
CODE_CROSS_SNAPSHOT_EVIDENCE = "CROSS_SNAPSHOT_EVIDENCE"
CODE_CROSS_SYMBOL_EVIDENCE = "CROSS_SYMBOL_EVIDENCE"
CODE_STALE_EVIDENCE_REFERENCE = "STALE_EVIDENCE_REFERENCE"
CODE_POINT_IN_TIME_UNSAFE_EVIDENCE = "POINT_IN_TIME_UNSAFE_EVIDENCE"
CODE_UNSUPPORTED_NUMERIC_CLAIM = "UNSUPPORTED_NUMERIC_CLAIM"
CODE_NUMERIC_VALUE_MISMATCH = "NUMERIC_VALUE_MISMATCH"
CODE_UNIT_MISMATCH = "UNIT_MISMATCH"
CODE_UNSUPPORTED_MATERIAL_CLAIM = "UNSUPPORTED_MATERIAL_CLAIM"
CODE_MISSING_BEAR_CASE = "MISSING_BEAR_CASE"
CODE_MISSING_RISK = "MISSING_RISK"
CODE_MISSING_REQUIRED_ROLE = "MISSING_REQUIRED_ROLE"
CODE_RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
CODE_MANAGER_NOT_INVOKED = "MANAGER_NOT_INVOKED"
CODE_PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"
CODE_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
CODE_UNCLASSIFIED_VALIDATION_FAILURE = "UNCLASSIFIED_VALIDATION_FAILURE"

# Locked-down Claude Code subprocess/provider failures. These are intentionally
# stable, provider-specific categories; raw subprocess diagnostics are never
# persisted in their place.
CLAUDE_CODE_FAILURE_CODES = (
    "CLAUDE_CODE_BINARY_MISSING",
    "CLAUDE_CODE_BINARY_NOT_EXECUTABLE",
    "CLAUDE_CODE_VERSION_UNPARSABLE",
    "CLAUDE_CODE_VERSION_UNSUPPORTED",
    "CLAUDE_CODE_NOT_AUTHENTICATED",
    "CLAUDE_CODE_AUTH_STATUS_FAILED",
    "CLAUDE_CODE_UNEXPECTED_AUTH_METHOD",
    "CLAUDE_CODE_OAUTH_TOKEN_MISSING",
    "CLAUDE_CODE_PROCESS_TIMEOUT",
    "CLAUDE_CODE_PROCESS_EXITED",
    "CLAUDE_CODE_OUTPUT_OVERFLOW",
    "CLAUDE_CODE_STDERR_OVERFLOW",
    "CLAUDE_CODE_INVALID_ENVELOPE",
    "CLAUDE_CODE_STRUCTURED_OUTPUT_MISSING",
    "CLAUDE_CODE_USAGE_METADATA_MISSING",
    "CLAUDE_CODE_CREDIT_EXHAUSTED",
    "CLAUDE_CODE_RATE_LIMITED",
    "CLAUDE_CODE_TRANSIENT_FAILURE",
    "CLAUDE_CODE_SCHEMA_REJECTED",
    "CLAUDE_CODE_LOCAL_SCHEMA_FAILED",
    "CLAUDE_CODE_PROMPT_TOO_LARGE",
)

# Locked-down Codex subprocess/provider failures — mirrors
# CLAUDE_CODE_FAILURE_CODES's intent for the second locked-down CLI
# provider; raw CLI diagnostics are never persisted in their place.
CODEX_FAILURE_CODES = (
    "CODEX_BINARY_MISSING",
    "CODEX_BINARY_NOT_EXECUTABLE",
    "CODEX_VERSION_UNPARSABLE",
    "CODEX_VERSION_UNSUPPORTED",
    "CODEX_LOGIN_STATUS_FAILED",
    "CODEX_NOT_AUTHENTICATED",
    "CODEX_UNEXPECTED_AUTH_METHOD",
    "CODEX_PROCESS_TIMEOUT",
    "CODEX_PROCESS_EXITED",
    "CODEX_RATE_LIMITED",
    "CODEX_QUOTA_EXHAUSTED",
    "CODEX_TRANSIENT_FAILURE",
    "CODEX_SCHEMA_REJECTED",
    "CODEX_PROMPT_TOO_LARGE",
    "CODEX_OUTPUT_OVERFLOW",
    "CODEX_STDERR_OVERFLOW",
    "CODEX_INVALID_JSONL",
    "CODEX_TERMINAL_EVENT_MISSING",
    "CODEX_MULTIPLE_TERMINAL_EVENTS",
    "CODEX_FINAL_OUTPUT_MISSING",
    "CODEX_FINAL_OUTPUT_MALFORMED",
    "CODEX_USAGE_METADATA_MISSING",
    "CODEX_MODEL_PROVENANCE_MISSING",
    # Milestone 12.1 Item 3: centralized terminal-failure classification
    # categories, reachable identically from a nonzero process exit or a
    # zero-exit `turn.failed` JSONL event.
    "CODEX_NETWORK_FAILURE",
    "CODEX_UNSUPPORTED_MODEL",
    "CODEX_INVALID_CONFIGURATION",
    "CODEX_REASONING_TOKENS_INVALID",
)

CODES = (
    CODE_PROVIDER_TIMEOUT,
    CODE_PROVIDER_RATE_LIMITED,
    CODE_PROVIDER_UNAVAILABLE,
    CODE_PROVIDER_CLIENT_ERROR,
    CODE_PROVIDER_SERVER_ERROR,
    CODE_OUTPUT_TRUNCATED,
    CODE_EXPECTED_TOOL_USE_MISSING,
    CODE_UNEXPECTED_TOOL_NAME,
    CODE_MULTIPLE_TOOL_BLOCKS,
    CODE_MALFORMED_TOOL_INPUT,
    CODE_INVALID_JSON,
    CODE_SCHEMA_REQUIRED_FIELD_MISSING,
    CODE_SCHEMA_TYPE_MISMATCH,
    CODE_SCHEMA_ENUM_INVALID,
    CODE_SCHEMA_EXTRA_FIELD,
    CODE_SCHEMA_LIST_LIMIT_EXCEEDED,
    CODE_UNKNOWN_EVIDENCE_ID,
    CODE_CROSS_SNAPSHOT_EVIDENCE,
    CODE_CROSS_SYMBOL_EVIDENCE,
    CODE_STALE_EVIDENCE_REFERENCE,
    CODE_POINT_IN_TIME_UNSAFE_EVIDENCE,
    CODE_UNSUPPORTED_NUMERIC_CLAIM,
    CODE_NUMERIC_VALUE_MISMATCH,
    CODE_UNIT_MISMATCH,
    CODE_UNSUPPORTED_MATERIAL_CLAIM,
    CODE_MISSING_BEAR_CASE,
    CODE_MISSING_RISK,
    CODE_MISSING_REQUIRED_ROLE,
    CODE_RETRY_EXHAUSTED,
    CODE_MANAGER_NOT_INVOKED,
    CODE_PERSISTENCE_FAILURE,
    CODE_BUDGET_EXHAUSTED,
    CODE_UNCLASSIFIED_VALIDATION_FAILURE,
) + CLAUDE_CODE_FAILURE_CODES + CODEX_FAILURE_CODES

# Roles this taxonomy accepts beyond the analyst/manager roles — "system" is used for
# run-level meta-failures (retry exhaustion / manager skip) that are not attributable to
# a single analyst role's own output.
_META_ROLES = ("system",)
VALID_ROLES = tuple(KNOWN_ROLES) + _META_ROLES

# Metadata is allowlisted, not denylisted: only these keys may ever be stored, so a
# secret/credential/raw-payload key can never reach persistence even by accident (Step 5:
# "Do not store arbitrary metadata mappings without validation").
ALLOWED_METADATA_KEYS = frozenset(
    {
        "stop_reason",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "provider_status_code",
        "expected_type",
        "actual_type",
        "allowed_evidence_count",
        "numeric_tolerance",
        "max_output_tokens",
        "tool_name",
        "tool_use_block_count",
        "blocking_role_count",
        "attempts_made",
        "exit_code",
        "stdout_bytes",
        "stderr_bytes",
        "claude_code_version",
        "provider_cli_version",
        "usage_present",
        "event_count",
        "cli_version",
        # Milestone 12.1.1 Item 6: safe version-preflight provenance — never a
        # secret, always one of a bounded set of already-validated semver
        # strings the operator or `codex_version_policy.py` itself supplied.
        "configured_minimum_version",
        "adapter_version",
    }
)

# Defense-in-depth on metadata *values* (allowlisted keys still can't smuggle a secret
# string in): matched case-insensitively against every string value.
_SECRET_LIKE_VALUE_PATTERNS = (
    "sk-ant-",
    "bearer ",
    "authorization",
    "api_key",
    "apikey",
    "password",
    "secret",
    "x-api-key",
)

MAX_MESSAGE_CHARS = 2000
MAX_FIELD_PATH_CHARS = 300
MAX_CLAIM_ID_CHARS = 200
MAX_EVIDENCE_ID_CHARS = 200
MAX_EVIDENCE_IDS = 50


class FailureValidationError(ValueError):
    """A `ResearchValidationFailure` was constructed with invalid or unsafe data."""


def sanitize_message(text: str, *, max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """Bounds free text before it reaches `ResearchValidationFailure` — callers should
    prefer this over relying on the dataclass's own bound check, since truncating here
    keeps the most useful (leading) portion of an oversized validator/exception message."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _compute_failure_id(
    *,
    attempt_id: str,
    stage: str,
    code: str,
    field_path: str | None,
    claim_id: str | None,
    evidence_ids: tuple[str, ...],
    message: str,
) -> str:
    """Deterministic: the same failure content always produces the same failure_id, which
    is what makes `save_attempt_failures` idempotent on retry/replay (Step 6: "idempotent
    insertion")."""
    payload = "|".join(
        [
            attempt_id,
            stage,
            code,
            field_path or "",
            claim_id or "",
            ",".join(evidence_ids),
            message,
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"failure-{digest[:32]}"


def _validate_metadata(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        if key not in ALLOWED_METADATA_KEYS:
            raise FailureValidationError(f"metadata key {key!r} is not in the allowlisted metadata key set")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise FailureValidationError(f"metadata key {key!r} must be a JSON-safe scalar, got {type(value)!r}")
        if isinstance(value, str):
            lowered = value.lower()
            if any(pattern in lowered for pattern in _SECRET_LIKE_VALUE_PATTERNS):
                raise FailureValidationError(f"metadata key {key!r} value looks secret-like — refusing to persist")
            if len(value) > MAX_MESSAGE_CHARS:
                raise FailureValidationError(f"metadata key {key!r} value exceeds {MAX_MESSAGE_CHARS} chars")
        cleaned[key] = value
    return cleaned


@dataclass(frozen=True)
class ResearchValidationFailure:
    """One structured, immutable failure record (Step 5). An attempt can produce several
    of these — e.g. three independently rejected claims on the same attempt — which is
    exactly why persistence (Step 6) is a one-row-per-failure child table, not a column on
    `research_attempts`.
    """

    failure_id: str
    research_run_id: str
    attempt_id: str
    role: str
    attempt_number: int
    stage: str
    code: str
    message: str
    field_path: str | None
    claim_id: str | None
    evidence_ids: tuple[str, ...]
    retryable: bool
    model_name: str
    prompt_version: str
    schema_version: str
    occurred_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise FailureValidationError("ResearchValidationFailure.occurred_at must be timezone-aware")
        if self.role not in VALID_ROLES:
            raise FailureValidationError(f"role {self.role!r} is not one of {VALID_ROLES}")
        if self.stage not in STAGES:
            raise FailureValidationError(f"stage {self.stage!r} is not one of {STAGES}")
        if self.code not in CODES:
            raise FailureValidationError(f"code {self.code!r} is not one of {CODES}")
        if self.attempt_number < 1:
            raise FailureValidationError("attempt_number must be a positive integer")
        if not self.research_run_id:
            raise FailureValidationError("research_run_id must be non-empty")
        if not self.attempt_id:
            raise FailureValidationError("attempt_id must be non-empty")
        if not self.message.strip():
            raise FailureValidationError("message must be non-empty")
        if len(self.message) > MAX_MESSAGE_CHARS:
            raise FailureValidationError(f"message exceeds {MAX_MESSAGE_CHARS} chars — sanitize before constructing")
        if self.field_path is not None and len(self.field_path) > MAX_FIELD_PATH_CHARS:
            raise FailureValidationError(f"field_path exceeds {MAX_FIELD_PATH_CHARS} chars")
        if self.claim_id is not None and len(self.claim_id) > MAX_CLAIM_ID_CHARS:
            raise FailureValidationError(f"claim_id exceeds {MAX_CLAIM_ID_CHARS} chars")
        if len(self.evidence_ids) > MAX_EVIDENCE_IDS:
            raise FailureValidationError(f"evidence_ids exceeds {MAX_EVIDENCE_IDS} entries")
        for eid in self.evidence_ids:
            if not eid or len(eid) > MAX_EVIDENCE_ID_CHARS:
                raise FailureValidationError(f"evidence_id {eid!r} is empty or exceeds {MAX_EVIDENCE_ID_CHARS} chars")
        object.__setattr__(self, "metadata", _validate_metadata(self.metadata))


def new_failure(
    *,
    research_run_id: str,
    attempt_id: str,
    role: str,
    attempt_number: int,
    stage: str,
    code: str,
    message: str,
    model_name: str,
    prompt_version: str,
    schema_version: str,
    occurred_at: datetime,
    field_path: str | None = None,
    claim_id: str | None = None,
    evidence_ids: tuple[str, ...] = (),
    retryable: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> ResearchValidationFailure:
    """Convenience constructor: sanitizes the message, computes the deterministic
    `failure_id`, and defaults `occurred_at` to UTC-aware if a naive value somehow slips
    through (the dataclass itself still rejects a naive timestamp — this only guards the
    common `datetime.now()` mistake at the call site)."""
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    clean_message = sanitize_message(message)
    failure_id = _compute_failure_id(
        attempt_id=attempt_id, stage=stage, code=code, field_path=field_path,
        claim_id=claim_id, evidence_ids=tuple(evidence_ids), message=clean_message,
    )
    return ResearchValidationFailure(
        failure_id=failure_id, research_run_id=research_run_id, attempt_id=attempt_id, role=role,
        attempt_number=attempt_number, stage=stage, code=code, message=clean_message,
        field_path=field_path, claim_id=claim_id, evidence_ids=tuple(evidence_ids), retryable=retryable,
        model_name=model_name, prompt_version=prompt_version, schema_version=schema_version,
        occurred_at=occurred_at, metadata=dict(metadata or {}),
    )


# Milestone 12.1.1 Item 2: deterministic primary-failure selection. An attempt
# can carry several independent `ResearchValidationFailure` rows (e.g. three
# rejected claims, or a schema failure alongside a claim failure) — the
# `ResearchAttemptRecord.failure_code`/`failure_stage`/`failure_retryable`
# summary columns must pick exactly one of them the same way regardless of
# the order the failures happened to be appended in. Never uses `message`
# (free text) to rank or break ties — only the already-validated, allowlisted
# `stage`/`code`/`retryable` fields.
PRIMARY_FAILURE_POLICY_VERSION = "primary-failure-selection/v2"

_PROVIDER_STAGES = (STAGE_PROVIDER_REQUEST, STAGE_PROVIDER_RESPONSE)
_CONTRACT_STAGES = (STAGE_TOOL_USE_EXTRACTION, STAGE_JSON_DECODING, STAGE_STRUCTURED_SCHEMA, STAGE_ROLE_REPORT_VALIDATION)
_CLAIM_STAGES = (STAGE_CLAIM_EVIDENCE_VALIDATION, STAGE_NUMERIC_CLAIM_VALIDATION, STAGE_PROMPT_INJECTION_VALIDATION)


def _failure_tier(failure: ResearchValidationFailure) -> int:
    """Lower number = higher priority. Every branch is derived only from the
    already-typed/validated `stage` and `retryable` fields — never from
    `code` free-form matching or `message` text. A stage this policy does not
    otherwise recognize is conservative when non-retryable (or has unknown
    retryability): it ranks immediately after a known structural provider
    failure and ahead of retryable claim/schema failures. Unknown retryable
    diagnostics remain in the lowest-priority tier.
    """
    if failure.stage in _PROVIDER_STAGES and failure.retryable is not True:
        return 0  # structural provider failure
    if (
        failure.retryable is not True
        and failure.stage not in _CONTRACT_STAGES
        and failure.stage != STAGE_BUDGET_GATED
    ):
        return 1  # unknown non-retryable failure
    if failure.stage in _CONTRACT_STAGES and failure.retryable is not True:
        return 2  # non-retryable provider contract failure
    if failure.stage == STAGE_BUDGET_GATED:
        return 3  # budget or kill failure
    if failure.stage in _PROVIDER_STAGES and failure.retryable is True:
        return 4  # retryable provider failure
    if failure.stage in _CONTRACT_STAGES and failure.retryable is True:
        return 5  # schema validation
    if failure.stage in _CLAIM_STAGES:
        return 6  # claim validation
    return 7  # diagnostic failure (RETRY_EXHAUSTED, REQUIRED_ROLE_FAILED, MANAGER_SKIPPED, PERSISTENCE, UNKNOWN, ...)


def select_primary_failure(
    failures: "list[ResearchValidationFailure] | tuple[ResearchValidationFailure, ...]",
) -> ResearchValidationFailure | None:
    """Deterministically picks the one failure that should drive
    attempt-level operational behavior out of possibly several failures on
    the same attempt. Stable under any reordering of `failures`: the
    selection key is `(tier, stage, code, field_path, claim_id, failure_id)`
    — every component already stage/code-validated or a deterministic hash
    (`failure_id`), never the human-readable `message`."""
    if not failures:
        return None
    return min(
        failures,
        key=lambda f: (
            _failure_tier(f), f.stage, f.code, f.field_path or "", f.claim_id or "", f.failure_id,
        ),
    )


MAX_RETRY_FEEDBACK_ITEMS = 5


def build_retry_feedback(
    failures: tuple[ResearchValidationFailure, ...],
    *,
    allowed_evidence_ids: tuple[str, ...] = (),
    max_items: int = MAX_RETRY_FEEDBACK_ITEMS,
) -> tuple[str, ...]:
    """Bounded, code-grouped remediation feedback for the next retry attempt (Step 11).

    Groups multiple failures sharing the same `code` into one line (with an occurrence
    count) rather than repeating near-duplicate text, caps the number of distinct lines at
    `max_items`, and — when the failures include evidence-citation problems — appends the
    exact allowed `evidence_id` set so a retry cannot claim it didn't know which IDs were
    valid. Never includes the complete prior response or any secret; always ends with an
    explicit "return a complete replacement report" instruction, matching the rest of this
    codebase's retry contract (`prompts.py::build_user_prompt`: "do not introduce new
    evidence or claims beyond what is provided above").
    """
    if not failures:
        return ()
    grouped: dict[str, list[ResearchValidationFailure]] = {}
    for f in failures:
        grouped.setdefault(f.code, []).append(f)

    lines: list[str] = []
    for code, group in list(grouped.items())[:max_items]:
        sample = group[0]
        field = f" field={sample.field_path}" if sample.field_path else ""
        claim = f" claim_id={sample.claim_id}" if sample.claim_id else ""
        count = f" ({len(group)} occurrences)" if len(group) > 1 else ""
        lines.append(f"code={code}{field}{claim}{count}: {sample.message}")

    if allowed_evidence_ids:
        lines.append(
            "Allowed evidence_id values for this snapshot (use only these; do not invent one or "
            "reuse a rejected one): " + ", ".join(sorted(allowed_evidence_ids))
        )

    lines.append(
        "Return a complete replacement report. Use only the supplied evidence_ids, do not introduce "
        "unsupported numeric values or invented downside/upside estimates, and do not provide order "
        "instructions, allocation, or position size."
    )
    return tuple(lines)
