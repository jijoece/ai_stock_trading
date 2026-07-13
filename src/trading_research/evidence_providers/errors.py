"""Typed, framework-neutral errors for real evidence providers (Milestone 6).

Mirrors `research/errors.py`'s convention: every provider raises one of these
instead of leaking an HTTP-library-specific exception into caller code, and
callers can distinguish retryable transport failures from a genuinely
unavailable/misconfigured provider without importing httpx.
"""
from __future__ import annotations


class ProviderError(RuntimeError):
    """Base class for every evidence-provider error."""


class ProviderConfigurationError(ProviderError):
    """The provider is not configured to be usable (missing credentials, an
    explicitly disabled provider, or an unknown provider name) — never
    silently swapped for a different provider or a fixture."""


class ProviderRequestError(ProviderError):
    """A single request could not be completed (network failure, timeout, or
    a non-2xx response). `retryable` distinguishes a transient failure
    (timeout, 429, 5xx) from a non-retryable one (4xx other than 429)."""

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class ProviderRateLimitedError(ProviderRequestError):
    """The provider signalled a rate limit (HTTP 429) — always retryable up
    to the bounded retry count."""

    def __init__(self, message: str, *, status_code: int | None = 429):
        super().__init__(message, retryable=True, status_code=status_code)


class RetryBoundExceededError(ProviderError):
    """Bounded retries were exhausted without a successful response — never
    retried again by the same call, no infinite retry."""


class MalformedProviderResponseError(ProviderError):
    """The provider returned a 2xx response whose body could not be parsed
    into the expected shape — never silently coerced into empty/zero data."""


class LookAheadViolationError(ProviderError):
    """A provider response would introduce data that was not actually
    available as of the requested `as_of` timestamp — fails closed rather
    than silently dropping or backdating the offending record."""


class CacheCorruptionError(ProviderError):
    """A cached entry could not be deserialized — fails closed (treated as a
    cache miss, never as a poisoned/garbage result)."""
