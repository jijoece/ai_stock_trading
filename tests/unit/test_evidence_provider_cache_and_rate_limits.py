"""Unit tests for evidence_providers/cache.py and rate_limits.py — Milestone
6 docs/milestone-6.md Step 22 category F (provider caching tests)."""
from __future__ import annotations

from trading_research.evidence_providers.cache import CacheKey, ProviderCache
from trading_research.evidence_providers.errors import CacheCorruptionError
from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter


def test_cache_key_identity_includes_all_dimensions():
    k1 = CacheKey.build(provider="sec", operation="bars", symbol="aapl", start="2026-01-01")
    k2 = CacheKey.build(provider="sec", operation="bars", symbol="AAPL", start="2026-01-01")
    k3 = CacheKey.build(provider="sec", operation="bars", symbol="AAPL", start="2026-01-02")
    assert k1 == k2  # symbol case-normalized
    assert k1 != k3  # different params -> different identity


def test_cache_hit_and_miss_counted():
    clock = iter([0.0, 0.0, 0.0]).__next__
    cache = ProviderCache(clock=lambda: 0.0)
    key = CacheKey.build(provider="p", operation="op", symbol="AAPL")
    assert cache.get(key) is None
    cache.set(key, "value", ttl_seconds=None)
    assert cache.get(key) == "value"
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_cache_ttl_expiry_is_test_clock_controlled():
    t = {"now": 0.0}
    cache = ProviderCache(clock=lambda: t["now"])
    key = CacheKey.build(provider="p", operation="quote", symbol="AAPL")
    cache.set(key, "fresh", ttl_seconds=10.0)
    assert cache.get(key) == "fresh"
    t["now"] = 11.0
    assert cache.get(key) is None  # expired


def test_cache_immutable_entries_never_expire():
    t = {"now": 0.0}
    cache = ProviderCache(clock=lambda: t["now"])
    key = CacheKey.build(provider="p", operation="bars", symbol="AAPL")
    cache.set(key, "bars", ttl_seconds=None)
    t["now"] = 10_000_000.0
    assert cache.get(key) == "bars"


def test_rate_limiter_enforces_minimum_interval():
    clock_time = {"t": 0.0}
    sleeps: list[float] = []

    def clock():
        return clock_time["t"]

    def sleep_fn(seconds: float):
        sleeps.append(seconds)
        clock_time["t"] += seconds

    limiter = MinIntervalRateLimiter(min_interval_seconds=1.0, clock=clock, sleep_fn=sleep_fn)
    assert limiter.acquire() == 0.0  # first call never waits
    clock_time["t"] += 0.2  # only 0.2s elapsed
    wait = limiter.acquire()
    assert wait == 0.8
    assert sleeps == [0.8]


def test_rate_limiter_no_wait_when_interval_already_elapsed():
    clock_time = {"t": 0.0}
    limiter = MinIntervalRateLimiter(min_interval_seconds=1.0, clock=lambda: clock_time["t"], sleep_fn=lambda s: None)
    limiter.acquire()
    clock_time["t"] += 5.0
    assert limiter.acquire() == 0.0


def test_cache_hit_notifies_on_response_with_hit_status():
    """Milestone 6.1 Step 18 hardening: a cache hit is now observable through the same
    `on_response` callback `HttpJsonClient` uses, closing the documented Milestone 6
    known limitation ("evidence_provider_requests doesn't log cache hits")."""
    records: list[dict] = []
    cache = ProviderCache(clock=lambda: 0.0, on_response=records.append)
    key = CacheKey.build(provider="sec-edgar", operation="companyfacts", symbol="AAPL")
    cache.set(key, "value", ttl_seconds=None)

    assert cache.get(key) == "value"

    assert len(records) == 1
    assert records[0]["provider"] == "sec-edgar"
    assert records[0]["operation"] == "companyfacts"
    assert records[0]["symbol"] == "AAPL"
    assert records[0]["cache_status"] == "HIT"
    assert records[0]["success"] is True


def test_cache_miss_does_not_notify():
    """A plain miss must never notify — the real caller always falls through to a genuine
    `HttpJsonClient.get_json` call that persists its own row; notifying here too would
    double-count every real network request."""
    records: list[dict] = []
    cache = ProviderCache(clock=lambda: 0.0, on_response=records.append)
    key = CacheKey.build(provider="sec-edgar", operation="companyfacts", symbol="AAPL")

    assert cache.get(key) is None
    assert records == []


def test_cache_expired_entry_does_not_notify():
    t = {"now": 0.0}
    records: list[dict] = []
    cache = ProviderCache(clock=lambda: t["now"], on_response=records.append)
    key = CacheKey.build(provider="sec-edgar", operation="quote", symbol="AAPL")
    cache.set(key, "stale", ttl_seconds=10.0)
    t["now"] = 11.0

    assert cache.get(key) is None
    assert records == []


def test_cache_no_on_response_configured_is_a_safe_no_op():
    cache = ProviderCache(clock=lambda: 0.0)
    key = CacheKey.build(provider="sec-edgar", operation="companyfacts", symbol="AAPL")
    cache.set(key, "value", ttl_seconds=None)
    assert cache.get(key) == "value"  # no exception even with on_response unset
