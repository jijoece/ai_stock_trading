"""Deterministic, test-clock-controlled rate limiting (docs/milestone-6.md
Step 13). A minimum-interval limiter, not a busy-loop: `acquire()` sleeps for
exactly the remaining wait (via an injectable `sleep_fn`), so tests can
assert on the computed wait without a real sleep and production code gets a
real, bounded pace against a provider's documented limit.
"""
from __future__ import annotations

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
    _last_acquired: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")

    def acquire(self) -> float:
        """Blocks (via `sleep_fn`) until the minimum interval has elapsed
        since the previous acquisition. Returns the actual wait applied, in
        seconds — 0.0 if no wait was needed."""
        now = self.clock()
        if self._last_acquired is None:
            self._last_acquired = now
            return 0.0
        elapsed = now - self._last_acquired
        wait = max(0.0, self.min_interval_seconds - elapsed)
        if wait > 0:
            self.sleep_fn(wait)
        self._last_acquired = self.clock()
        return wait
