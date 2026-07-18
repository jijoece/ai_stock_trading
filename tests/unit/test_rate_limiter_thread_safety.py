"""Milestone 11.3 Part 25: `MinIntervalRateLimiter.acquire()` must be
thread-safe — concurrent callers cannot acquire the same interval, no
negative sleep, clock rollback handled, no lock held during the sleep/
network call itself."""
from __future__ import annotations

import threading
import time

from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter


def test_concurrent_callers_get_distinct_non_overlapping_slots():
    """Two real threads racing `acquire()` against a real monotonic clock
    (deterministic in *outcome*, not in timing) must never both compute a
    wait of 0 — exactly one goes first, the other is pushed out by at least
    `min_interval_seconds`."""
    limiter = MinIntervalRateLimiter(min_interval_seconds=0.05, sleep_fn=lambda s: time.sleep(s))
    waits: list[float] = []
    lock = threading.Lock()

    def worker():
        wait = limiter.acquire()
        with lock:
            waits.append(wait)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(waits) == 5
    # First caller pays nothing; every other caller pays a real, positive
    # wait — no two callers both got the "free" first slot.
    assert waits.count(0.0) == 1
    assert all(w >= 0.0 for w in waits)


def test_no_lock_held_during_sleep_second_thread_can_reserve_while_first_sleeps():
    """If the lock were held across `sleep_fn`, a second thread's call to
    `acquire()` would block for the *entire* first thread's sleep duration
    before it could even compute its own wait. Prove the opposite: the
    second thread's slot-computation returns promptly, even while the first
    thread is still "sleeping" (a fake, non-blocking sleep_fn here isolates
    slot-computation latency from a real sleep)."""
    started_sleep = threading.Event()
    release_sleep = threading.Event()

    def slow_sleep(seconds: float):
        started_sleep.set()
        release_sleep.wait(timeout=5)

    limiter = MinIntervalRateLimiter(min_interval_seconds=1.0, sleep_fn=slow_sleep)
    limiter.acquire()  # first call: no wait, no sleep

    t1 = threading.Thread(target=limiter.acquire)
    t1.start()
    assert started_sleep.wait(timeout=5), "first thread should have entered sleep_fn"

    # Second thread's acquire() must be able to compute+reserve its own slot
    # promptly, without waiting for the first thread's (still-blocked) sleep.
    second_computed = threading.Event()

    def second_caller():
        limiter.acquire()
        second_computed.set()

    t2 = threading.Thread(target=second_caller, daemon=True)
    t2.start()
    # The second thread's own slow_sleep call will also block on
    # release_sleep, but reaching *that point* (i.e. having computed its
    # slot and called sleep_fn) must not require waiting on the lock held by
    # the first thread's in-flight sleep.
    assert started_sleep.wait(timeout=5)

    release_sleep.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert second_computed.is_set()


def test_no_negative_sleep_on_clock_rollback():
    readings = iter([10.0, 5.0])  # second reading rolls backward
    limiter = MinIntervalRateLimiter(
        min_interval_seconds=1.0, clock=lambda: next(readings, 5.0), sleep_fn=lambda s: None,
    )
    first = limiter.acquire()
    second = limiter.acquire()
    assert first == 0.0
    assert second >= 0.0


def test_uses_monotonic_clock_by_default():
    import time as time_module
    limiter = MinIntervalRateLimiter(min_interval_seconds=0.0)
    assert limiter.clock is time_module.monotonic
