"""Milestone 11.3.1 Item 4: `OrderLeaseHandle` must use a fresh clock read
for every heartbeat/verify call (never the operation's original `now`), a
failed heartbeat must stop the operation immediately, and protected writes
must fence against a lease takeover -- not just hold the context-manager
scope. Complements the repo-level generation/fencing tests already in
`test_external_order_lease_fencing.py` (which exercise
`acquire/heartbeat/verify/release_external_order_lease` directly).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.unit.test_external_paper_broker import _config, _seed, FakeRuntime
from trading_research.paper_books.config import ExternalBrokerSection, PaperBooksConfigError
from trading_research.paper_books.external_broker import (
    ExternalPaperError, OrderLeaseHandle, preview_external_paper_order,
)
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)


class _MutableClock:
    """A deterministic, externally-advanceable clock -- distinct from a
    fixed `lambda: NOW`, so a call site that reuses a stale captured `now`
    (rather than re-invoking the clock) is observably different from one
    that always re-invokes it."""

    def __init__(self, start: datetime):
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


# --- 1. heartbeat extends expiry using fresh clock time ----------------------


def _conn():
    c = connect(":memory:")
    c.execute(
        "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
        "VALUES ('BASELINE', 'BASELINE', '100000', '2026-01-01T00:00:00Z', 'cfg')"
    )
    c.commit()
    return c


def test_heartbeat_uses_fresh_clock_time_not_acquisition_time():
    conn = _conn()
    clock = _MutableClock(NOW)
    generation = repo.acquire_external_order_lease(
        conn, lease_key="BASELINE:client-1", book_id="BASELINE", client_order_id="client-1",
        owner_id="owner-a", operation="TEST", now=NOW.isoformat(),
        expires_at=(NOW + timedelta(seconds=45)).isoformat(),
    )
    handle = OrderLeaseHandle(conn, "BASELINE:client-1", "owner-a", generation, 45, clock)

    clock.advance(40)  # still within the original 45s TTL
    assert handle.heartbeat() is True
    row = conn.execute(
        "SELECT expires_at FROM paper_external_order_leases WHERE lease_key = ?", ("BASELINE:client-1",),
    ).fetchone()
    expected_expires_at = (clock() + timedelta(seconds=45)).isoformat()
    assert row["expires_at"] == expected_expires_at
    # Proves the heartbeat used the *current* clock reading, not the stale
    # `NOW` captured when the handle/lease were first created.
    assert row["expires_at"] != (NOW + timedelta(seconds=45)).isoformat()


# --- 2. stale timestamps do not renew a lease --------------------------------


def test_repo_heartbeat_rejects_a_stale_now_after_expiry():
    conn = _conn()
    generation = repo.acquire_external_order_lease(
        conn, lease_key="BASELINE:client-1", book_id="BASELINE", client_order_id="client-1",
        owner_id="owner-a", operation="TEST", now=NOW.isoformat(),
        expires_at=(NOW + timedelta(seconds=10)).isoformat(),
    )
    # A heartbeat call whose own timestamp is already past its lease's
    # expiry (e.g. a stale `now` captured long before the actual heartbeat
    # attempt) must not renew it.
    stale_now = NOW + timedelta(seconds=11)
    ok = repo.heartbeat_external_order_lease(
        conn, lease_key="BASELINE:client-1", owner_id="owner-a", generation=generation,
        now=stale_now.isoformat(), expires_at=(stale_now + timedelta(seconds=45)).isoformat(),
    )
    assert ok is False


# --- 3. failed heartbeat aborts the operation --------------------------------


def test_failed_heartbeat_or_raise_stops_operation_immediately():
    conn = _conn()
    clock = _MutableClock(NOW)
    generation = repo.acquire_external_order_lease(
        conn, lease_key="BASELINE:client-1", book_id="BASELINE", client_order_id="client-1",
        owner_id="owner-a", operation="TEST", now=NOW.isoformat(),
        expires_at=(NOW + timedelta(seconds=5)).isoformat(),
    )
    handle = OrderLeaseHandle(conn, "BASELINE:client-1", "owner-a", generation, 5, clock)
    clock.advance(10)  # past the 5s TTL -- lease is now expired
    with pytest.raises(ExternalPaperError) as excinfo:
        handle.heartbeat_or_raise()
    assert excinfo.value.code == "ORDER_LEASE_LOST"


# --- 4/5/6/7. two-connection takeover: generation, fencing, no cross-release -


def test_takeover_generation_changes_and_stale_owner_cannot_write_or_release():
    conn = _conn()
    clock_a = _MutableClock(NOW)
    generation_a = repo.acquire_external_order_lease(
        conn, lease_key="BASELINE:client-1", book_id="BASELINE", client_order_id="client-1",
        owner_id="owner-a", operation="TEST", now=NOW.isoformat(),
        expires_at=(NOW + timedelta(seconds=5)).isoformat(),
    )
    handle_a = OrderLeaseHandle(conn, "BASELINE:client-1", "owner-a", generation_a, 5, clock_a)

    # owner-a's lease expires with no heartbeat; owner-b reclaims it.
    takeover_now = NOW + timedelta(seconds=6)
    generation_b = repo.acquire_external_order_lease(
        conn, lease_key="BASELINE:client-1", book_id="BASELINE", client_order_id="client-1",
        owner_id="owner-b", operation="TEST", now=takeover_now.isoformat(),
        expires_at=(takeover_now + timedelta(seconds=45)).isoformat(),
    )
    assert generation_b == generation_a + 1  # 4. generation changes after takeover

    # 7. owner-a's write conditional on the old generation affects zero rows
    # (heartbeat is itself a conditional UPDATE keyed on owner_id+generation).
    clock_a.advance(6)
    assert handle_a.heartbeat() is False
    assert handle_a.verify() is False

    # 5. owner-a cannot write (fence) past the takeover.
    with pytest.raises(ExternalPaperError):
        handle_a.verify_or_raise()

    # 6. owner-a cannot release owner-b's lease.
    released = repo.release_external_order_lease(
        conn, lease_key="BASELINE:client-1", owner_id="owner-a", now=clock_a().isoformat(), generation=generation_a,
    )
    assert released is False
    still_held = repo.verify_external_order_lease(
        conn, lease_key="BASELINE:client-1", owner_id="owner-b", generation=generation_b, now=clock_a().isoformat(),
    )
    assert still_held is True


# --- 8. long-running submit/reconcile paths heartbeat before expiry ---------


def test_preview_survives_a_slow_runtime_call_via_heartbeat(monkeypatch):
    """A runtime call slow enough to approach the *original* TTL must not
    fail the operation as long as the lease is heartbeated with a fresh
    clock read beforehand."""
    conn = connect(":memory:")
    _seed(conn)
    clock = _MutableClock(NOW)

    class _SlowRuntime(FakeRuntime):
        def preview_limit_order(self, payload):
            # Simulate a slow broker round-trip that would blow past a
            # short TTL if the lease were never heartbeated with fresh time.
            clock.advance(20)
            return super().preview_limit_order(payload)

    cfg = _config()
    short_ttl_broker = ExternalBrokerSection(
        cfg.external_broker.enabled, cfg.external_broker.provider, cfg.external_broker.allow_order_submission,
        cfg.external_broker.enabled_book_ids, cfg.external_broker.require_explicit_preview,
        cfg.external_broker.require_recent_preview_seconds, cfg.external_broker.maximum_order_notional_usd,
        cfg.external_broker.maximum_daily_notional_usd,
        cfg.external_broker.permitted_order_types, cfg.external_broker.permitted_time_in_force,
        cfg.external_broker.maximum_retry_attempts, order_lease_ttl_seconds=41, order_lease_heartbeat_seconds=10,
    )
    from dataclasses import replace

    short_ttl_cfg = replace(cfg, external_broker=short_ttl_broker)

    result = preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        runtime=_SlowRuntime(), config=short_ttl_cfg, clock=clock,
    )
    assert result["result"] == "APPROVED"


# --- 9. invalid TTL/timeout combinations fail configuration validation ------


def test_ttl_at_or_below_runtime_timeout_plus_margin_rejected():
    with pytest.raises(PaperBooksConfigError):
        ExternalBrokerSection(
            True, "alpaca_paper", True, ("BASELINE",), True, 300, Decimal("100"), Decimal("300"),
            ("limit",), ("day",), 1, order_lease_ttl_seconds=40, order_lease_heartbeat_seconds=10,
        )


def test_ttl_strictly_above_runtime_timeout_plus_margin_accepted():
    section = ExternalBrokerSection(
        True, "alpaca_paper", True, ("BASELINE",), True, 300, Decimal("100"), Decimal("300"),
        ("limit",), ("day",), 1, order_lease_ttl_seconds=41, order_lease_heartbeat_seconds=10,
    )
    assert section.order_lease_ttl_seconds == 41


def test_repository_default_config_satisfies_ttl_validation():
    from trading_research.paper_books.config import DEFAULT_PAPER_BOOKS_CONFIG_PATH, load_paper_books_config

    config = load_paper_books_config(DEFAULT_PAPER_BOOKS_CONFIG_PATH)
    assert config.external_broker.order_lease_ttl_seconds > 30 + config.external_broker.order_lease_heartbeat_seconds


# --- 10. lease loss leaves durable ambiguity evidence ------------------------


def test_lease_loss_before_protected_write_leaves_no_partial_state(monkeypatch):
    """If the lease is lost between the read-only checks and the protected
    preview-persistence write, the write must never happen -- no half
    written preview/event, and the caller sees a clear error rather than
    silent corruption."""
    conn = connect(":memory:")
    _seed(conn)
    clock = _MutableClock(NOW)

    import trading_research.paper_books.external_broker as eb

    real_verify_or_raise = eb.OrderLeaseHandle.verify_or_raise
    calls = {"n": 0}

    def _fail_second_verify(self):
        calls["n"] += 1
        if calls["n"] >= 1:
            raise eb.OrderLeaseLostError("simulated takeover before the protected write")
        return real_verify_or_raise(self)

    monkeypatch.setattr(eb.OrderLeaseHandle, "verify_or_raise", _fail_second_verify)
    with pytest.raises(ExternalPaperError) as excinfo:
        preview_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            runtime=FakeRuntime(), config=_config(), clock=clock,
        )
    assert excinfo.value.code == "ORDER_LEASE_LOST"
    monkeypatch.setattr(eb.OrderLeaseHandle, "verify_or_raise", real_verify_or_raise)

    # No preview and no event were persisted -- the write never happened.
    previews = conn.execute("SELECT COUNT(*) AS c FROM paper_external_order_previews").fetchone()
    events = conn.execute("SELECT COUNT(*) AS c FROM paper_external_order_events").fetchone()
    assert previews["c"] == 0
    assert events["c"] == 0
