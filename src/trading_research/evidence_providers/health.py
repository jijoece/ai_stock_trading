"""Provider and pipeline health metrics (docs/milestone-6.md Step 19).

Computed from persisted `evidence_provider_requests` rows rather than tracked
in a separate live/mutable counter — this keeps health reconstructible and
auditable exactly like every other derived value in this repository
(`evaluation/metrics.py` does the same over persisted evaluations). Explicit
statuses instead of a misleading zero when there is no data yet.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..hashing import hash_config
from .http_client import (
    TRANSPORT_AUTHENTICATION_FAILURE,
    TRANSPORT_CONFIGURATION_ERROR,
    TRANSPORT_PROTOCOL_ERROR,
    TRANSPORT_TLS_FAILURE,
)

STATUS_HEALTHY = "HEALTHY"
STATUS_DEGRADED = "DEGRADED"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

MIN_SAMPLE_SIZE = 3
DEGRADED_SUCCESS_RATE_THRESHOLD = 0.8
UNAVAILABLE_SUCCESS_RATE_THRESHOLD = 0.5


@dataclass(frozen=True)
class ProviderHealthSummary:
    provider: str
    status: str
    total_requests: int
    success_rate: float | None
    timeout_rate: float | None
    rate_limited_rate: float | None
    invalid_response_rate: float | None
    cache_hit_rate: float | None
    average_latency_ms: float | None
    p95_latency_ms: float | None


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(round(pct * (len(sorted_values) - 1))))
    return sorted_values[idx]


def compute_provider_health(rows: list[dict], provider: str) -> ProviderHealthSummary:
    provider_rows = [r for r in rows if r["provider"] == provider]
    total = len(provider_rows)
    if total < MIN_SAMPLE_SIZE:
        return ProviderHealthSummary(
            provider=provider, status=STATUS_INSUFFICIENT_DATA, total_requests=total,
            success_rate=None, timeout_rate=None, rate_limited_rate=None, invalid_response_rate=None,
            cache_hit_rate=None, average_latency_ms=None, p95_latency_ms=None,
        )

    successes = sum(1 for r in provider_rows if r["success"])
    timeouts = sum(1 for r in provider_rows if r.get("error_code") == "ProviderRequestError" and not r["success"])
    rate_limited = sum(1 for r in provider_rows if r["rate_limited"])
    invalid = sum(1 for r in provider_rows if r.get("error_code") == "MalformedProviderResponseError")
    cache_hits = sum(1 for r in provider_rows if r["cache_status"] == "HIT")
    latencies = sorted(r["latency_ms"] for r in provider_rows if r.get("latency_ms") is not None)

    success_rate = successes / total
    if success_rate < UNAVAILABLE_SUCCESS_RATE_THRESHOLD:
        status = STATUS_UNAVAILABLE
    elif success_rate < DEGRADED_SUCCESS_RATE_THRESHOLD:
        status = STATUS_DEGRADED
    else:
        status = STATUS_HEALTHY

    return ProviderHealthSummary(
        provider=provider, status=status, total_requests=total, success_rate=success_rate,
        timeout_rate=timeouts / total, rate_limited_rate=rate_limited / total,
        invalid_response_rate=invalid / total, cache_hit_rate=cache_hits / total,
        average_latency_ms=(sum(latencies) / len(latencies)) if latencies else None,
        p95_latency_ms=_percentile(latencies, 0.95) if latencies else None,
    )


def compute_all_provider_health(rows: list[dict]) -> tuple[ProviderHealthSummary, ...]:
    providers = sorted({r["provider"] for r in rows})
    return tuple(compute_provider_health(rows, p) for p in providers)


# -- Milestone 11.3.1 Item 8: bounded severe-error taxonomy, wired from ------
# -- real persisted request telemetry (never inferred from raw exception ----
# -- text). `error_code` on `evidence_provider_requests` is always one of a --
# -- fixed set of exception class names an adapter actually raised ----------
# -- (`evidence_providers/errors.py`), so this mapping is a lookup over a ---
# -- bounded, real vocabulary, not a text-pattern guess. ---------------------

SEVERE_AUTHENTICATION_FAILED = TRANSPORT_AUTHENTICATION_FAILURE
SEVERE_DNS_OR_CONNECTION_FAILURE = "DNS_OR_CONNECTION_FAILURE"  # legacy display alias; no longer inferred
SEVERE_TLS_FAILURE = TRANSPORT_TLS_FAILURE
SEVERE_PROVIDER_CONFIGURATION_INVALID = TRANSPORT_CONFIGURATION_ERROR
SEVERE_REPEATED_RATE_LIMIT_EXHAUSTION = "REPEATED_RATE_LIMIT_EXHAUSTION"
SEVERE_PROTOCOL_OR_SCHEMA_BREAK = TRANSPORT_PROTOCOL_ERROR

SEVERE_ERROR_CATEGORIES = (
    SEVERE_AUTHENTICATION_FAILED, SEVERE_DNS_OR_CONNECTION_FAILURE, SEVERE_TLS_FAILURE,
    SEVERE_PROVIDER_CONFIGURATION_INVALID, SEVERE_REPEATED_RATE_LIMIT_EXHAUSTION,
    SEVERE_PROTOCOL_OR_SCHEMA_BREAK,
)

# Minimum exhausted-retry count on a still-rate-limited failed request before
# it counts as "repeated" rather than one ordinary rate-limit backoff.
_REPEATED_RATE_LIMIT_RETRY_THRESHOLD = 2


def classify_severe_error(row: dict) -> str | None:
    """Returns one of `SEVERE_ERROR_CATEGORIES`, or `None` if this request
    row is either successful or an ordinary (non-severe) failure. Reads only
    the bounded, already-classified `error_code`/`http_status`/
    `rate_limited`/`retry_count` fields persisted on the row — never raw
    exception text."""
    if row.get("success"):
        return None
    http_status = row.get("http_status")
    error_code = row.get("error_code")
    transport_category = row.get("transport_failure_category")
    if transport_category == TRANSPORT_AUTHENTICATION_FAILURE or http_status in (401, 403):
        return SEVERE_AUTHENTICATION_FAILED
    if transport_category == TRANSPORT_CONFIGURATION_ERROR or error_code == "ProviderConfigurationError":
        return SEVERE_PROVIDER_CONFIGURATION_INVALID
    if transport_category == TRANSPORT_PROTOCOL_ERROR or error_code == "MalformedProviderResponseError":
        return SEVERE_PROTOCOL_OR_SCHEMA_BREAK
    if transport_category == TRANSPORT_TLS_FAILURE:
        return SEVERE_TLS_FAILURE
    # Rate limits, timeouts, resets, DNS failures, refusals, and temporary
    # server failures remain hysteresis inputs. They are not structural
    # immediate-pause categories merely because no response arrived.
    return None


PROVIDER_COVERAGE_POLICY_VERSION = "provider-coverage/v1"


def normalize_provider_name(provider: str) -> str:
    normalized = provider.strip().lower().replace("_", "-")
    aliases = {
        "alpaca": "alpaca-data", "alpaca-market-data": "alpaca-data", "alpacadata": "alpaca-data",
        "sec": "sec-edgar", "edgar": "sec-edgar", "sec-edgar-api": "sec-edgar",
        "alpaca-news-api": "alpaca-news", "alpacanews": "alpaca-news",
    }
    return aliases.get(normalized, normalized)


@dataclass(frozen=True)
class ProviderCoveragePolicy:
    policy_version: str = PROVIDER_COVERAGE_POLICY_VERSION
    required_categories: tuple[tuple[str, tuple[str, ...]], ...] = ()
    optional_providers: tuple[str, ...] = ()
    unavailable_required_categories: tuple[str, ...] = ()
    configuration_hash: str = ""
    telemetry_expected: bool = True

    @property
    def required_providers(self) -> tuple[str, ...]:
        return tuple(sorted({normalize_provider_name(p) for _, providers in self.required_categories for p in providers}))

    @property
    def required_category_names(self) -> tuple[str, ...]:
        return tuple(category for category, _ in self.required_categories)

    @property
    def policy_hash(self) -> str:
        return hash_config({
            "policy_version": self.policy_version,
            "required_categories": self.required_categories,
            "optional_providers": tuple(sorted(normalize_provider_name(p) for p in self.optional_providers)),
            "unavailable_required_categories": self.unavailable_required_categories,
            "configuration_hash": self.configuration_hash,
            "telemetry_expected": self.telemetry_expected,
        })


def coverage_policy_from_configuration(configuration, *, telemetry_expected: bool = True) -> ProviderCoveragePolicy:
    """Resolve the versioned required-category policy from the frozen provider config."""
    required: list[tuple[str, tuple[str, ...]]] = []
    unavailable: list[str] = []
    if configuration.sec.enabled:
        required.append(("corporate_filings", ("sec-edgar",)))
    else:
        unavailable.append("corporate_filings")
    if configuration.market_data.enabled:
        required.append(("market_data", ("alpaca-data",)))
    else:
        unavailable.append("market_data")
    optional: list[str] = []
    if configuration.news.enabled:
        optional.append("alpaca-news")
    if configuration.sentiment.enabled:
        optional.append("reddit-mcp")
    if configuration.reddit_free.enabled:
        optional.append("reddit-free")
    return ProviderCoveragePolicy(
        required_categories=tuple(required), optional_providers=tuple(optional),
        unavailable_required_categories=tuple(sorted(unavailable)),
        configuration_hash=configuration.config_hash, telemetry_expected=telemetry_expected,
    )


@dataclass(frozen=True)
class CycleProviderTelemetry:
    """Authoritative per-cycle provider-request telemetry (Milestone 11.3.1
    Item 8 Part A) — the real request-attempt count and outcome, not a
    symbols-attempted proxy. `per_provider` preserves per-provider rates
    (never collapsed into a single aggregate that could hide one required
    provider's complete outage behind another provider's success).
    `required_providers_missing` names any provider in the caller's
    required set that produced zero requests this cycle — an absent
    required provider is never treated as a passing/successful result."""

    total_requests: int
    successful_requests: int
    aggregate_success_rate: float | None
    per_provider: dict[str, ProviderHealthSummary]
    required_providers_missing: tuple[str, ...]
    severe_error: bool
    severe_error_categories: tuple[str, ...]
    required_categories: tuple[str, ...] = ()
    resolved_required_providers: tuple[str, ...] = ()
    observed_providers: tuple[str, ...] = ()
    missing_required_categories: tuple[str, ...] = ()
    policy_version: str = PROVIDER_COVERAGE_POLICY_VERSION
    policy_hash: str = ""
    configuration_hash: str = ""
    telemetry_expected: bool = True


def compute_cycle_provider_telemetry(
    rows: list[dict], *, required_providers: tuple[str, ...] = (),
    coverage_policy: ProviderCoveragePolicy | None = None,
) -> CycleProviderTelemetry:
    normalized_rows = [{**row, "provider": normalize_provider_name(row["provider"])} for row in rows]
    total = len(normalized_rows)
    successes = sum(1 for r in normalized_rows if r["success"])
    aggregate_rate = (successes / total) if total > 0 else None
    providers_seen = sorted({r["provider"] for r in normalized_rows})
    per_provider = {provider: compute_provider_health(normalized_rows, provider) for provider in providers_seen}
    policy = coverage_policy or ProviderCoveragePolicy(
        required_categories=(("legacy_required", tuple(required_providers)),) if required_providers else (),
    )
    resolved_required = policy.required_providers
    missing = tuple(p for p in resolved_required if p not in providers_seen)
    missing_categories = set(policy.unavailable_required_categories)
    for category, providers in policy.required_categories:
        if not any(normalize_provider_name(provider) in providers_seen for provider in providers):
            missing_categories.add(category)
    severe_categories = sorted({
        category for row in normalized_rows for category in (classify_severe_error(row),) if category is not None
    })
    return CycleProviderTelemetry(
        total_requests=total, successful_requests=successes, aggregate_success_rate=aggregate_rate,
        per_provider=per_provider, required_providers_missing=missing,
        severe_error=bool(severe_categories), severe_error_categories=tuple(severe_categories),
        required_categories=policy.required_category_names,
        resolved_required_providers=resolved_required, observed_providers=tuple(providers_seen),
        missing_required_categories=tuple(sorted(missing_categories)), policy_version=policy.policy_version,
        policy_hash=policy.policy_hash, configuration_hash=policy.configuration_hash,
        telemetry_expected=policy.telemetry_expected,
    )


REDUNDANCY_SINGLE_PROVIDER_PER_CATEGORY = "SINGLE_PROVIDER_PER_CATEGORY"


def compute_provider_concentration() -> dict:
    """Milestone 6.1 (docs/milestone-6.1.md Step 18, "Single-provider concentration"): a
    static architectural fact about *this milestone's fixed, documented provider set*
    (ADR 0004), not a metric derived from request volume — adding redundant providers is
    explicitly out of scope ("Do not add providers"). SEC EDGAR backs both
    `filing_provider_count` and `fundamentals_provider_count` (one raw client, two
    evidence categories — see ADR 0004 Decision 1); Alpaca backs
    `market_data_provider_count`; news/sentiment remain `0` because
    `RealNewsEvidenceProvider`/`RealSentimentEvidenceProvider` are ENVIRONMENTALLY_PENDING
    (no API key / Reddit credentials in this environment), not silently omitted."""
    return {
        "market_data_provider_count": 1,
        "filing_provider_count": 1,
        "fundamentals_provider_count": 1,
        "news_provider_count": 0,
        "sentiment_provider_count": 0,
        "redundancy_status": REDUNDANCY_SINGLE_PROVIDER_PER_CATEGORY,
    }
