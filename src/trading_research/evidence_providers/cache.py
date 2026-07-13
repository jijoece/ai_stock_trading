"""Deterministic provider-response caching (docs/milestone-6.md Step 13).

Cache identity is a tuple, not a free-form string, so identical requests
always collide and different requests never accidentally collide:
`(provider, operation, canonical_symbol, params)`. TTL policy differs per
category — immutable historical data caches forever within a process run
(the same historical bar never changes), current quotes use a short TTL,
everything else takes an explicit policy. The test clock is injectable so
TTL expiry is deterministic in tests, never wall-clock-dependent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import CacheCorruptionError

# Sentinel: this entry never expires within the cache's lifetime (matches
# "historical immutable data may use long-lived caching").
IMMUTABLE_TTL = None


@dataclass(frozen=True)
class CacheKey:
    provider: str
    operation: str
    symbol: str
    params: tuple[tuple[str, Any], ...]

    @classmethod
    def build(cls, *, provider: str, operation: str, symbol: str, **params: Any) -> "CacheKey":
        return cls(provider=provider, operation=operation, symbol=symbol.upper(), params=tuple(sorted(params.items())))


@dataclass
class _Entry:
    value: Any
    stored_at: float
    ttl_seconds: float | None


@dataclass
class ProviderCache:
    """In-process deterministic cache. Not shared across processes — each
    scheduled-cycle run gets a fresh cache, which is sufficient to satisfy
    "no duplicate provider calls within one cycle" without adding a second
    persistence layer redundant with `evidence_provider_responses`.

    Milestone 6.1 (docs/milestone-6.1.md Step 18, "Provider cache hits not
    persisted"): `on_response`, when set, mirrors `http_client.HttpJsonClient`'s own
    callback exactly — same dict shape, same call sites end up persisting it via the same
    `_make_persist_hook` — so a cache hit is now visible in `evidence_provider_requests`
    and in `provider-health`'s `cache_hit_rate` instead of only in this in-process
    `stats()` snapshot, which never survives the process. Never duplicates the cached raw
    payload — only request-level telemetry (`cache_status="HIT"`, zero latency, no retry)
    is emitted, matching the existing no-raw-payload-duplication policy.

    Deliberately does **not** notify on a plain MISS or a `STALE_CACHE_REJECTED` expiry:
    every real call site (`sec_provider.py`, `market_data_provider.py`) falls through to a
    genuine `HttpJsonClient.get_json` call immediately after either of those, and that
    call already persists its own row — notifying here too would double-count every real
    network request. `CACHE_CORRUPT` is safe to notify because it *raises*
    `CacheCorruptionError` instead of falling through to a follow-up HTTP call."""

    clock: Callable[[], float]
    _store: dict[CacheKey, _Entry] = field(default_factory=dict)
    hits: int = field(default=0, init=False)
    misses: int = field(default=0, init=False)
    corruptions: int = field(default=0, init=False)
    on_response: Callable[[dict], None] | None = None

    def get(self, key: CacheKey) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        try:
            if entry.ttl_seconds is not None:
                age = self.clock() - entry.stored_at
                if age > entry.ttl_seconds:
                    del self._store[key]
                    self.misses += 1
                    return None
            self.hits += 1
            self._notify(key, cache_status="HIT")
            return entry.value
        except Exception as exc:  # fail closed on any corrupted/unreadable entry
            self.corruptions += 1
            self._store.pop(key, None)
            self._notify(key, cache_status="CACHE_CORRUPT", success=False, error_code="CacheCorruptionError")
            raise CacheCorruptionError(f"corrupted cache entry for {key}: {exc}") from exc

    def _notify(self, key: CacheKey, *, cache_status: str, success: bool = True, error_code: str | None = None) -> None:
        if self.on_response is None:
            return
        self.on_response({
            "provider": key.provider, "operation": key.operation, "symbol": key.symbol,
            "http_status": None, "cache_status": cache_status, "rate_limited": False, "retry_count": 0,
            "latency_ms": 0, "success": success, "error_code": error_code, "retryable": None,
        })

    def set(self, key: CacheKey, value: Any, *, ttl_seconds: float | None) -> None:
        self._store[key] = _Entry(value=value, stored_at=self.clock(), ttl_seconds=ttl_seconds)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "corruptions": self.corruptions,
            "hit_rate": (self.hits / total) if total else None,
        }


# Documented TTL policies per evidence category (docs/milestone-6.md Step 13).
TTL_HISTORICAL_BARS = IMMUTABLE_TTL       # never changes once the session has closed
TTL_FILINGS = IMMUTABLE_TTL               # an accepted filing's metadata never changes
TTL_COMPANY_FACTS = 3600.0                # XBRL facts can be restated; re-check hourly within a run
TTL_CURRENT_QUOTE = 15.0                  # short TTL for near-real-time quotes
TTL_NEWS = 300.0                          # syndication/dedup can shift within minutes
TTL_PORTFOLIO_CONTEXT = 5.0               # local ledger read; effectively always fresh
