"""Typed, framework-neutral errors for the research layer (Milestone 5).

Every provider (deterministic, scripted, anthropic) raises or returns these
instead of leaking SDK-specific exception types into domain code — nothing
outside `anthropic_provider.py` ever needs to import the Anthropic SDK to
handle a research failure.
"""
from __future__ import annotations


class ResearchError(RuntimeError):
    """Base class for every research-layer error."""


class EvidenceValidationError(ResearchError):
    """An evidence snapshot failed a structural or safety invariant."""


class ProviderUnavailableError(ResearchError):
    """No usable provider configuration exists (missing credentials, unknown
    provider/model, or the provider is not installed) — never silently
    swapped for a different provider."""


class ProviderTimeoutError(ResearchError):
    """A single provider call exceeded its configured timeout."""


class ProviderRateLimitError(ResearchError):
    """The provider signalled a transient rate limit — eligible for bounded retry."""


class ProviderTransientError(ResearchError):
    """Some other transient provider failure — eligible for bounded retry."""


class MalformedOutputError(ResearchError):
    """The provider returned output that is not valid, schema-conforming JSON."""


class SchemaValidationError(ResearchError):
    """Structured output parsed as JSON but failed schema validation."""


class ClaimValidationError(ResearchError):
    """A research claim could not be validated against its cited evidence."""


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
