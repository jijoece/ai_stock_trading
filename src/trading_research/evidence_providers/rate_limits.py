"""Deterministic, test-clock-controlled rate limiting (docs/milestone-6.md
Step 13). A minimum-interval limiter, not a busy-loop: `acquire()` sleeps for
exactly the remaining wait (via an injectable `sleep_fn`), so tests can
assert on the computed wait without a real sleep and production code gets a
real, bounded pace against a provider's documented limit.

Thread-safety (Milestone 11.3 Part 25): one `MinIntervalRateLimiter`
instance is scoped to a single provider client (see each provider module's
own construction site — e.g. `evidence_providers/http_client.py`), not
shared globally across providers; if a future caller needs one limiter
shared across multiple provider instances, that scope decision belongs to
the caller, not this class. Within that scope, `acquire()` is now safe to
call concurrently from multiple threads: the interval-reservation slot is
computed and updated atomically under a lock, while the actual `sleep_fn`
call happens *outside* the lock so one thread's wait never blocks another
thread's ability to compute (and reserve) its own next slot.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class MinIntervalRateLimiter:
    """Enforces a minimum wall-clock interval between successive `acquire()`
    calls. No token bucket, no burst allowance — deliberately the simplest
    policy that satisfies "no thundering-herd retry" for a single-process,
    single-provider client making sequential requests."""

    min_interval_seconds: float
    clock: Callable[[], float] = time.monotonic
    sleep_fn: Callable[[float], None] = time.sleep
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _next_allowed: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")

    def acquire(self) -> float:
        """Blocks (via `sleep_fn`) until the minimum interval has elapsed
        since the previous acquisition. Returns the actual wait applied, in
        seconds — 0.0 if no wait was needed.

        Reserves this caller's slot atomically under `_lock` before
        releasing it, so two threads calling `acquire()` concurrently always
        compute distinct, correctly-spaced slots — never the same wait from
        the same stale state. `max(self._next_allowed, now)` also makes a
        clock rollback (`now` less than the previously recorded state) fail
        safe: `wait` is never negative, and a rolled-back clock can only
        make this limiter *more* conservative (wait relative to the last
        real forward-clock reading), never less.
        """
        with self._lock:
            now = self.clock()
            base = now if self._next_allowed is None else max(self._next_allowed, now)
            wait = base - now
            self._next_allowed = base + self.min_interval_seconds
        if wait > 0:
            self.sleep_fn(wait)
        return wait
