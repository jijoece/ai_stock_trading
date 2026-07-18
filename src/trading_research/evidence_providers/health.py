"""Provider and pipeline health metrics (docs/milestone-6.md Step 19).

Computed from persisted `evidence_provider_requests` rows rather than tracked
in a separate live/mutable counter — this keeps health reconstructible and
auditable exactly like every other derived value in this repository
(`evaluation/metrics.py` does the same over persisted evaluations). Explicit
statuses instead of a misleading zero when there is no data yet.
"""
from __future__ import annotations

from dataclasses import dataclass

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

SEVERE_AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
SEVERE_DNS_OR_CONNECTION_FAILURE = "DNS_OR_CONNECTION_FAILURE"
SEVERE_TLS_FAILURE = "TLS_FAILURE"
SEVERE_PROVIDER_CONFIGURATION_INVALID = "PROVIDER_CONFIGURATION_INVALID"
SEVERE_REPEATED_RATE_LIMIT_EXHAUSTION = "REPEATED_RATE_LIMIT_EXHAUSTION"
SEVERE_PROTOCOL_OR_SCHEMA_BREAK = "PROTOCOL_OR_SCHEMA_BREAK"

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
    if http_status in (401, 403):
        return SEVERE_AUTHENTICATION_FAILED
    if error_code == "ProviderConfigurationError":
        return SEVERE_PROVIDER_CONFIGURATION_INVALID
    if error_code == "MalformedProviderResponseError":
        return SEVERE_PROTOCOL_OR_SCHEMA_BREAK
    if row.get("rate_limited") and (row.get("retry_count") or 0) >= _REPEATED_RATE_LIMIT_RETRY_THRESHOLD:
        return SEVERE_REPEATED_RATE_LIMIT_EXHAUSTION
    if error_code == "ProviderRequestError" and http_status is None:
        # No HTTP response at all reached this adapter -- connection-level
        # failure. TLS failures are not currently distinguishable from a
        # generic connection failure at this persisted-field granularity,
        # so both fold into this bucket (a documented limitation, not a
        # guess from exception text).
        return SEVERE_DNS_OR_CONNECTION_FAILURE
    return None


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


def compute_cycle_provider_telemetry(
    rows: list[dict], *, required_providers: tuple[str, ...] = (),
) -> CycleProviderTelemetry:
    total = len(rows)
    successes = sum(1 for r in rows if r["success"])
    aggregate_rate = (successes / total) if total > 0 else None
    providers_seen = sorted({r["provider"] for r in rows})
    per_provider = {provider: compute_provider_health(rows, provider) for provider in providers_seen}
    missing = tuple(p for p in required_providers if p not in providers_seen)
    severe_categories = sorted({
        category for row in rows for category in (classify_severe_error(row),) if category is not None
    })
    return CycleProviderTelemetry(
        total_requests=total, successful_requests=successes, aggregate_success_rate=aggregate_rate,
        per_provider=per_provider, required_providers_missing=missing,
        severe_error=bool(severe_categories), severe_error_categories=tuple(severe_categories),
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
