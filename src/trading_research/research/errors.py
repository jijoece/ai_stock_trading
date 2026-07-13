"""Typed, framework-neutral errors for the research layer (Milestone 5).

Every provider (deterministic, scripted, anthropic) raises or returns these
instead of leaking SDK-specific exception types into domain code — nothing
outside `anthropic_provider.py` ever needs to import the Anthropic SDK to
handle a research failure.

Milestone 6.1 (docs/milestone-6.1.md Step 7): every error optionally carries
structured failure-taxonomy attributes (`stage`, `code`, `metadata`, ...) so
`orchestration.py` can build a `research/failure_taxonomy.py::ResearchValidationFailure`
directly from the caught exception instead of only having its string
representation. A raise site that does not pass these keeps sensible
per-class defaults — this is purely additive, no existing `raise XError("...")`
call site needs to change.
"""
from __future__ import annotations

from typing import Any, Mapping


class ResearchError(RuntimeError):
    """Base class for every research-layer error.

    `stage`/`code` default to the taxonomy's UNKNOWN/UNCLASSIFIED members so a
    raise site that predates Milestone 6.1 (or a future one that forgets to
    classify) never crashes failure persistence — it just produces a
    correctly-labeled "unclassified" record instead of a fabricated specific one.
    """

    default_stage = "UNKNOWN"
    default_code = "UNCLASSIFIED_VALIDATION_FAILURE"
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        code: str | None = None,
        field_path: str | None = None,
        claim_id: str | None = None,
        evidence_ids: tuple[str, ...] = (),
        retryable: bool | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage or self.default_stage
        self.code = code or self.default_code
        self.field_path = field_path
        self.claim_id = claim_id
        self.evidence_ids = tuple(evidence_ids)
        self.retryable = self.default_retryable if retryable is None else retryable
        self.metadata: Mapping[str, Any] = dict(metadata or {})


class EvidenceValidationError(ResearchError):
    """An evidence snapshot failed a structural or safety invariant."""


class ProviderUnavailableError(ResearchError):
    """No usable provider configuration exists (missing credentials, unknown
    provider/model, or the provider is not installed) — never silently
    swapped for a different provider."""

    default_stage = "PROVIDER_RESPONSE"
    default_code = "PROVIDER_UNAVAILABLE"
    default_retryable = False


class ProviderTimeoutError(ResearchError):
    """A single provider call exceeded its configured timeout."""

    default_stage = "PROVIDER_REQUEST"
    default_code = "PROVIDER_TIMEOUT"
    default_retryable = True


class ProviderRateLimitError(ResearchError):
    """The provider signalled a transient rate limit — eligible for bounded retry."""

    default_stage = "PROVIDER_RESPONSE"
    default_code = "PROVIDER_RATE_LIMITED"
    default_retryable = True


class ProviderTransientError(ResearchError):
    """Some other transient provider failure — eligible for bounded retry."""

    default_stage = "PROVIDER_RESPONSE"
    default_code = "PROVIDER_SERVER_ERROR"
    default_retryable = True


class MalformedOutputError(ResearchError):
    """The provider returned output that is not valid, schema-conforming JSON."""

    default_stage = "TOOL_USE_EXTRACTION"
    default_code = "EXPECTED_TOOL_USE_MISSING"
    default_retryable = True


class SchemaValidationError(ResearchError):
    """Structured output parsed as JSON but failed schema validation."""

    default_stage = "STRUCTURED_SCHEMA"
    default_code = "UNCLASSIFIED_VALIDATION_FAILURE"
    default_retryable = True

    def __init__(self, message: str, *, schema_errors: tuple[Mapping[str, Any], ...] = (), **kwargs) -> None:
        """`schema_errors` (Milestone 6.1 Step 8) is an optional tuple of one dict per
        underlying `jsonschema` error — `{"stage", "code", "field_path", "message"}` —
        letting `orchestration.py` persist one `ResearchValidationFailure` per violated
        schema rule instead of collapsing every violation into one joined string."""
        super().__init__(message, **kwargs)
        self.schema_errors = tuple(schema_errors)


class ClaimValidationError(ResearchError):
    """A research claim could not be validated against its cited evidence."""

    default_stage = "CLAIM_EVIDENCE_VALIDATION"
    default_code = "UNCLASSIFIED_VALIDATION_FAILURE"
    default_retryable = True


class RetryExhaustedError(ResearchError):
    """Bounded retries were exhausted without a valid structured response."""


class UnknownProviderError(ResearchError):
    """Configuration named a provider this repository does not implement."""


class UnknownRoleError(ResearchError):
    """Configuration named a research role this repository does not implement."""


class UnknownOverlayActionError(ResearchError):
    """A deterministic overlay policy produced/named an action outside the
    configured enum — fails closed rather than executing an unknown action."""


class ReplayMismatchError(ResearchError):
    """A replay's reconstructed hash did not match the persisted hash."""
