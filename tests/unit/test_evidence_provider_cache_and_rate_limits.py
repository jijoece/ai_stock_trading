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
