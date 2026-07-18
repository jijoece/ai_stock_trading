"""Provider and pipeline health metrics (docs/milestone-6.md Step 19).

Computed from persisted `evidence_provider_requests` rows rather than tracked
in a separate live/mutable counter — this keeps health reconstructible and
auditable exactly like every other derived value in this repository
(`evaluation/metrics.py` does the same over persisted evaluations). Explicit
statuses instead of a misleading zero when there is no data yet.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..hashing import hash_config
from .http_client import (
    TRANSPORT_AUTHENTICATION_FAILURE,
    TRANSPORT_CONFIGURATION_ERROR,
    TRANSPORT_CONNECTION_REFUSED,
    TRANSPORT_CONNECTION_RESET,
    TRANSPORT_DNS_FAILURE,
    TRANSPORT_HTTP_CLIENT_ERROR,
    TRANSPORT_HTTP_SERVER_ERROR,
    TRANSPORT_NONE,
    TRANSPORT_PROTOCOL_ERROR,
    TRANSPORT_RATE_LIMITED,
    TRANSPORT_TIMEOUT,
    TRANSPORT_TLS_FAILURE,
    TRANSPORT_UNKNOWN_ERROR,
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
    # Milestone 12.1 Item 8: computed from the exact, already-classified
    # `transport_failure_category` enum (`http_client.py`) — never inferred
    # from a generic exception class name, a free-text message, or a missing
    # HTTP status. `timeout_rate` above is now ALSO computed this way
    # (previously it counted every generic `ProviderRequestError`, which
    # could include 5xx/connection failures/other non-timeout errors).
    dns_failure_rate: float | None = None
    connection_refused_rate: float | None = None
    connection_reset_rate: float | None = None
    tls_failure_rate: float | None = None
    authentication_failure_rate: float | None = None
    rate_limit_rate: float | None = None
    http_client_error_rate: float | None = None
    http_server_error_rate: float | None = None
    protocol_error_rate: float | None = None
    configuration_error_rate: float | None = None
    unknown_transport_error_rate: float | None = None


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(round(pct * (len(sorted_values) - 1))))
    return sorted_values[idx]


# Milestone 12.1 Item 8: exact enum -> rate-field mapping. Deliberately a
# lookup table, not a chain of `if transport_category == ...: elif ...` —
# every category maps to exactly one rate field, and `TRANSPORT_NONE` is
# absent on purpose (it means "no transport failure", never itself a rate).
_TRANSPORT_CATEGORY_TO_RATE_FIELD = {
    TRANSPORT_TIMEOUT: "timeout_rate",
    TRANSPORT_DNS_FAILURE: "dns_failure_rate",
    TRANSPORT_CONNECTION_REFUSED: "connection_refused_rate",
    TRANSPORT_CONNECTION_RESET: "connection_reset_rate",
    TRANSPORT_TLS_FAILURE: "tls_failure_rate",
    TRANSPORT_AUTHENTICATION_FAILURE: "authentication_failure_rate",
    TRANSPORT_RATE_LIMITED: "rate_limit_rate",
    TRANSPORT_HTTP_CLIENT_ERROR: "http_client_error_rate",
    TRANSPORT_HTTP_SERVER_ERROR: "http_server_error_rate",
    TRANSPORT_PROTOCOL_ERROR: "protocol_error_rate",
    TRANSPORT_CONFIGURATION_ERROR: "configuration_error_rate",
    TRANSPORT_UNKNOWN_ERROR: "unknown_transport_error_rate",
}


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
    rate_limited = sum(1 for r in provider_rows if r["rate_limited"])
    invalid = sum(1 for r in provider_rows if r.get("error_code") == "MalformedProviderResponseError")
    cache_hits = sum(1 for r in provider_rows if r["cache_status"] == "HIT")
    latencies = sorted(r["latency_ms"] for r in provider_rows if r.get("latency_ms") is not None)

    # Milestone 12.1 Item 8: typed rates use EXACT `transport_failure_category`
    # matching — never inferred from a generic exception class name, a
    # missing HTTP status, or a free-text message. A row whose category is
    # `TRANSPORT_NONE` (a legacy pre-migration-5 row, or a genuinely
    # successful/non-transport failure) contributes to none of these rates —
    # it is not silently folded into "unknown" either, since "unknown" is
    # itself a real, classified category (`TRANSPORT_UNKNOWN_ERROR`).
    category_counts: dict[str, int] = {field_name: 0 for field_name in _TRANSPORT_CATEGORY_TO_RATE_FIELD.values()}
    for row in provider_rows:
        category = row.get("transport_failure_category") or TRANSPORT_NONE
        field_name = _TRANSPORT_CATEGORY_TO_RATE_FIELD.get(category)
        if field_name is not None:
            category_counts[field_name] += 1
    typed_rates = {field_name: count / total for field_name, count in category_counts.items()}

    success_rate = successes / total
    if success_rate < UNAVAILABLE_SUCCESS_RATE_THRESHOLD:
        status = STATUS_UNAVAILABLE
    elif success_rate < DEGRADED_SUCCESS_RATE_THRESHOLD:
        status = STATUS_DEGRADED
    else:
        status = STATUS_HEALTHY

    return ProviderHealthSummary(
        provider=provider, status=status, total_requests=total, success_rate=success_rate,
        rate_limited_rate=rate_limited / total,
        invalid_response_rate=invalid / total, cache_hit_rate=cache_hits / total,
        average_latency_ms=(sum(latencies) / len(latencies)) if latencies else None,
        p95_latency_ms=_percentile(latencies, 0.95) if latencies else None,
        **typed_rates,
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


# --- Milestone 12.1 Item 6: required-provider health evaluated per category,
# independently of every other category's/provider's success. An aggregate
# success rate across all providers can hide a required provider's total
# failure behind an unrelated provider's success (e.g. SEC EDGAR 9/9 plus
# Alpaca 0/1 reads as a healthy 90% in aggregate) — this section computes one
# health verdict per required category, using only that category's own
# acceptable providers' requests, never diluted by any other provider.

REQUIRED_CATEGORY_STATUS_PASS = "PASS"
REQUIRED_CATEGORY_STATUS_WARNING = "WARNING"
REQUIRED_CATEGORY_STATUS_FAIL = "FAIL"
REQUIRED_CATEGORY_STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
REQUIRED_CATEGORY_STATUS_MISSING = "MISSING"
REQUIRED_CATEGORY_STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

REQUIRED_CATEGORY_STATUSES = (
    REQUIRED_CATEGORY_STATUS_PASS, REQUIRED_CATEGORY_STATUS_WARNING, REQUIRED_CATEGORY_STATUS_FAIL,
    REQUIRED_CATEGORY_STATUS_INSUFFICIENT_DATA, REQUIRED_CATEGORY_STATUS_MISSING,
    REQUIRED_CATEGORY_STATUS_NOT_APPLICABLE,
)

# A category unhealthy for pause purposes — FAIL (below the configured
# success-rate floor) or MISSING (zero requests to any acceptable provider
# this cycle). INSUFFICIENT_DATA (below the request-count floor, but no
# outright failure) is deliberately NOT treated as unhealthy — a category
# that naturally makes fewer requests than its floor is not itself a
# failure signal (docs: "choose whether a low-sample category is
# INSUFFICIENT_DATA or acceptable" — this repository treats it as
# observationally distinct from FAIL, never silently passing OR failing).
_UNHEALTHY_REQUIRED_CATEGORY_STATUSES = (REQUIRED_CATEGORY_STATUS_FAIL, REQUIRED_CATEGORY_STATUS_MISSING)

# Milestone 12.1.1 Item 5: a required category that is itself
# INSUFFICIENT_DATA must never let the overall provider-health dimension
# read as a computed PASS just because an unrelated required/optional
# provider's requests happened to succeed in aggregate (e.g. SEC EDGAR 9/9
# plus a single Alpaca request below its own 3-request floor must not read
# as a healthy 100% aggregate) — distinct from `_UNHEALTHY_REQUIRED_CATEGORY_STATUSES`,
# which drives an outright FAIL.
_INSUFFICIENT_REQUIRED_CATEGORY_STATUSES = (REQUIRED_CATEGORY_STATUS_INSUFFICIENT_DATA,)

# Category-specific sample floor defaults (docs' suggested
# `provider_health.required_categories.*` shape) — most required categories
# in this repository make exactly one request per cycle, so the default
# floor is 1 request / 100% success, not one global minimum blindly applied
# to every category regardless of its expected request volume.
DEFAULT_CATEGORY_MINIMUM_REQUESTS = 1
DEFAULT_CATEGORY_MINIMUM_SUCCESS_RATE = 1.0


@dataclass(frozen=True)
class RequiredCategoryHealth:
    category: str
    acceptable_providers: tuple[str, ...]
    observed_provider: str | None
    request_count: int
    success_count: int
    failure_count: int
    success_rate: float | None
    sample_floor: int
    minimum_success_rate: float
    status: str
    reasons: tuple[str, ...]


def evaluate_required_category_health(
    rows: list[dict], policy: "ProviderCoveragePolicy",
) -> tuple[RequiredCategoryHealth, ...]:
    """One independent health verdict per required category — computed only
    from THAT category's own acceptable providers' rows, never from an
    aggregate across every provider. `rows` are raw, un-normalized
    `evidence_provider_requests` rows (provider names are normalized here,
    matching `compute_cycle_provider_telemetry`)."""
    normalized_rows = [{**row, "provider": normalize_provider_name(row["provider"])} for row in rows]
    results: list[RequiredCategoryHealth] = []
    for category, acceptable_providers in policy.required_categories:
        acceptable = tuple(normalize_provider_name(p) for p in acceptable_providers)
        category_rows = [r for r in normalized_rows if r["provider"] in acceptable]
        request_count = len(category_rows)
        minimum_requests = policy.category_minimum_requests.get(category, DEFAULT_CATEGORY_MINIMUM_REQUESTS)
        minimum_success_rate = policy.category_minimum_success_rate.get(
            category, DEFAULT_CATEGORY_MINIMUM_SUCCESS_RATE
        )
        observed_provider = category_rows[0]["provider"] if category_rows else None
        if request_count == 0:
            results.append(RequiredCategoryHealth(
                category=category, acceptable_providers=acceptable, observed_provider=None,
                request_count=0, success_count=0, failure_count=0, success_rate=None,
                sample_floor=minimum_requests, minimum_success_rate=minimum_success_rate,
                status=REQUIRED_CATEGORY_STATUS_MISSING,
                reasons=(f"no request to any acceptable provider {acceptable} this cycle",),
            ))
            continue
        success_count = sum(1 for r in category_rows if r["success"])
        failure_count = request_count - success_count
        success_rate = success_count / request_count
        if request_count < minimum_requests:
            status = REQUIRED_CATEGORY_STATUS_INSUFFICIENT_DATA
            reasons = (
                f"request_count {request_count} < minimum_requests {minimum_requests} for category {category!r} — "
                "not treated as pass or fail",
            )
        elif success_rate < minimum_success_rate:
            status = REQUIRED_CATEGORY_STATUS_FAIL
            reasons = (
                f"success_rate {success_rate:.3f} < minimum_success_rate {minimum_success_rate:.3f} for "
                f"required category {category!r} (provider {observed_provider!r})",
            )
        else:
            status = REQUIRED_CATEGORY_STATUS_PASS
            reasons = (f"success_rate {success_rate:.3f} meets minimum_success_rate {minimum_success_rate:.3f}",)
        results.append(RequiredCategoryHealth(
            category=category, acceptable_providers=acceptable, observed_provider=observed_provider,
            request_count=request_count, success_count=success_count, failure_count=failure_count,
            success_rate=success_rate, sample_floor=minimum_requests, minimum_success_rate=minimum_success_rate,
            status=status, reasons=reasons,
        ))
    for category in policy.unavailable_required_categories:
        results.append(RequiredCategoryHealth(
            category=category, acceptable_providers=(), observed_provider=None, request_count=0, success_count=0,
            failure_count=0, success_rate=None, sample_floor=0, minimum_success_rate=0.0,
            status=REQUIRED_CATEGORY_STATUS_NOT_APPLICABLE,
            reasons=(f"category {category!r} has no provider configured/enabled — excluded from coverage entirely",),
        ))
    return tuple(sorted(results, key=lambda r: r.category))


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
    # Milestone 12.1 Item 6: per-category sample floor/success-rate policy —
    # `evaluate_required_category_health` falls back to
    # `DEFAULT_CATEGORY_MINIMUM_REQUESTS`/`DEFAULT_CATEGORY_MINIMUM_SUCCESS_RATE`
    # for any category not named here, so an existing caller that never sets
    # these keeps the pre-existing "one request, 100% success" expectation.
    category_minimum_requests: "dict[str, int]" = field(default_factory=dict)
    category_minimum_success_rate: "dict[str, float]" = field(default_factory=dict)

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
            "category_minimum_requests": tuple(sorted(self.category_minimum_requests.items())),
            "category_minimum_success_rate": tuple(sorted(self.category_minimum_success_rate.items())),
        })


class ProviderHealthPolicyOverrideError(ValueError):
    """A `shadow/config.py::ProviderHealthSection` override could not be
    applied to an already-resolved `ProviderCoveragePolicy` — fails closed
    rather than silently ignoring an inconsistent/unknown configuration."""


def apply_provider_health_policy_overrides(policy: "ProviderCoveragePolicy", section) -> "ProviderCoveragePolicy":
    """Milestone 12.1.1 Item 5: overlay strict, frozen `provider_health.
    required_categories.*.minimum_requests`/`minimum_success_rate` config
    (`shadow/config.py::ProviderHealthSection`) onto a policy whose
    category->acceptable-provider mapping is already resolved from
    evidence-provider enablement (`coverage_policy_from_configuration`).
    `section` is `None` for "no override configured" — that keeps the
    pre-existing `DEFAULT_CATEGORY_MINIMUM_REQUESTS`/
    `DEFAULT_CATEGORY_MINIMUM_SUCCESS_RATE` behavior verbatim, never a
    silent behavior change for a config file written before this section
    existed. Every named category must already be one of `policy`'s
    required categories, and its configured `providers` must exactly match
    the already-resolved acceptable-provider set for that category —
    an unknown category name or a mismatched provider list both fail
    closed rather than silently accepting a stale/wrong override."""
    if section is None:
        return policy
    import dataclasses

    known_categories = {category: providers for category, providers in policy.required_categories}
    minimum_requests = dict(policy.category_minimum_requests)
    minimum_success_rate = dict(policy.category_minimum_success_rate)
    for category, category_section in section.required_categories.items():
        if category not in known_categories:
            raise ProviderHealthPolicyOverrideError(
                f"provider_health.required_categories.{category} is not a configured required category "
                f"— known categories are {sorted(known_categories)}"
            )
        configured_providers = tuple(sorted(normalize_provider_name(p) for p in category_section.providers))
        resolved_providers = tuple(sorted(known_categories[category]))
        if configured_providers != resolved_providers:
            raise ProviderHealthPolicyOverrideError(
                f"provider_health.required_categories.{category}.providers {configured_providers} does not match "
                f"the resolved acceptable-provider set {resolved_providers}"
            )
        minimum_requests[category] = category_section.minimum_requests
        minimum_success_rate[category] = category_section.minimum_success_rate
    return dataclasses.replace(
        policy, policy_version=section.policy_version, category_minimum_requests=minimum_requests,
        category_minimum_success_rate=minimum_success_rate,
    )


def coverage_policy_from_configuration(
    configuration, *, telemetry_expected: bool = True, provider_health_section=None,
) -> ProviderCoveragePolicy:
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
    policy = ProviderCoveragePolicy(
        required_categories=tuple(required), optional_providers=tuple(optional),
        unavailable_required_categories=tuple(sorted(unavailable)),
        configuration_hash=configuration.config_hash, telemetry_expected=telemetry_expected,
    )
    return apply_provider_health_policy_overrides(policy, provider_health_section)


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
    # Milestone 12.1 Item 6: independent per-required-category verdicts —
    # `unhealthy_required_categories` names every category whose OWN
    # acceptable-provider requests failed its OWN success-rate floor
    # (FAIL) or made zero requests (MISSING/`missing_required_categories`
    # already covers the latter) — never derived from the aggregate rate.
    required_category_health: "tuple[RequiredCategoryHealth, ...]" = ()
    unhealthy_required_categories: tuple[str, ...] = ()
    # Milestone 12.1.1 Item 5: required categories whose OWN request count is
    # below their OWN sample floor (INSUFFICIENT_DATA) — disjoint from
    # `unhealthy_required_categories` (FAIL/MISSING). Non-empty here, with
    # `unhealthy_required_categories` empty, must produce an overall
    # INSUFFICIENT_DATA provider-health dimension, never a fabricated PASS.
    insufficient_required_categories: tuple[str, ...] = ()


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
    required_category_health = evaluate_required_category_health(rows, policy)
    unhealthy_required_categories = tuple(sorted(
        h.category for h in required_category_health if h.status in _UNHEALTHY_REQUIRED_CATEGORY_STATUSES
    ))
    insufficient_required_categories = tuple(sorted(
        h.category for h in required_category_health if h.status in _INSUFFICIENT_REQUIRED_CATEGORY_STATUSES
    ))
    return CycleProviderTelemetry(
        total_requests=total, successful_requests=successes, aggregate_success_rate=aggregate_rate,
        per_provider=per_provider, required_providers_missing=missing,
        severe_error=bool(severe_categories), severe_error_categories=tuple(severe_categories),
        required_categories=policy.required_category_names,
        resolved_required_providers=resolved_required, observed_providers=tuple(providers_seen),
        missing_required_categories=tuple(sorted(missing_categories)), policy_version=policy.policy_version,
        policy_hash=policy.policy_hash, configuration_hash=policy.configuration_hash,
        telemetry_expected=policy.telemetry_expected,
        required_category_health=required_category_health,
        unhealthy_required_categories=unhealthy_required_categories,
        insufficient_required_categories=insufficient_required_categories,
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
