"""Milestone 11.2 Part 10/37 regression: order-scope leases must be
renewable (heartbeat) and fenced (a stale owner's generation can never
write again after another owner reclaims the lease)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)


def _conn():
    c = connect(":memory:")
    c.execute(
        "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
        "VALUES ('BASELINE', 'BASELINE', '100000', '2026-01-01T00:00:00Z', 'cfg')"
    )
    c.commit()
    return c


def _acquire(conn, owner_id, now, ttl=30):
    return repo.acquire_external_order_lease(
        conn, lease_key="BASELINE:client-1", book_id="BASELINE", client_order_id="client-1",
        owner_id=owner_id, operation="SUBMIT", now=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl)).isoformat(),
    )


def test_fresh_acquire_returns_generation_one():
    conn = _conn()
    generation = _acquire(conn, "owner-a", NOW)
    assert generation == 1


def test_second_acquire_while_active_and_unexpired_fails():
    conn = _conn()
    _acquire(conn, "owner-a", NOW)
    generation = _acquire(conn, "owner-b", NOW + timedelta(seconds=5))
    assert generation is None


def test_owner_heartbeats_beyond_original_ttl_and_second_owner_still_cannot_acquire():
    """owner A acquires -> operation runs beyond original TTL while
    heartbeating -> owner B cannot acquire."""
    conn = _conn()
    generation = _acquire(conn, "owner-a", NOW, ttl=30)
    assert generation == 1

    # Heartbeat at t+25s (before the original 30s TTL would expire), extending it.
    t1 = NOW + timedelta(seconds=25)
    ok = repo.heartbeat_external_order_lease(
        conn, lease_key="BASELINE:client-1", owner_id="owner-a", generation=1,
        now=t1.isoformat(), expires_at=(t1 + timedelta(seconds=30)).isoformat(),
    )
    assert ok is True

    # At t+40s (past the *original* 30s TTL, but within the heartbeat-extended one).
    t2 = NOW + timedelta(seconds=40)
    second = _acquire(conn, "owner-b", t2)
    assert second is None  # owner-a's heartbeat kept it alive


def test_owner_stops_heartbeating_lease_expires_new_owner_gets_new_generation():
    """owner A stops heartbeating -> lease expires -> owner B acquires new
    generation -> owner A's later write (heartbeat/release) is rejected."""
    conn = _conn()
    generation_a = _acquire(conn, "owner-a", NOW, ttl=30)
    assert generation_a == 1

    # No heartbeat. At t+31s the lease is expired; owner-b reclaims it.
    t_expired = NOW + timedelta(seconds=31)
    generation_b = _acquire(conn, "owner-b", t_expired, ttl=30)
    assert generation_b == 2  # generation advanced on reclaim

    # owner-a's later write attempts (heartbeat or release) using its stale
    # generation must be rejected now that owner-b holds generation 2.
    stale_heartbeat_ok = repo.heartbeat_external_order_lease(
        conn, lease_key="BASELINE:client-1", owner_id="owner-a", generation=generation_a,
        now=(t_expired + timedelta(seconds=1)).isoformat(),
        expires_at=(t_expired + timedelta(seconds=31)).isoformat(),
    )
    assert stale_heartbeat_ok is False

    stale_release_ok = repo.release_external_order_lease(
        conn, lease_key="BASELINE:client-1", owner_id="owner-a", generation=generation_a,
        now=(t_expired + timedelta(seconds=2)).isoformat(),
    )
    assert stale_release_ok is False

    # owner-b, the true current holder, can still verify/release normally.
    assert repo.verify_external_order_lease(
        conn, lease_key="BASELINE:client-1", owner_id="owner-b", generation=generation_b,
        now=(t_expired + timedelta(seconds=2)).isoformat(),
    ) is True
    assert repo.release_external_order_lease(
        conn, lease_key="BASELINE:client-1", owner_id="owner-b", generation=generation_b,
        now=(t_expired + timedelta(seconds=3)).isoformat(),
    ) is True


def test_verify_fails_once_lease_expires_even_without_a_new_owner():
    conn = _conn()
    generation = _acquire(conn, "owner-a", NOW, ttl=10)
    assert repo.verify_external_order_lease(
        conn, lease_key="BASELINE:client-1", owner_id="owner-a", generation=generation,
        now=(NOW + timedelta(seconds=5)).isoformat(),
    ) is True
    assert repo.verify_external_order_lease(
        conn, lease_key="BASELINE:client-1", owner_id="owner-a", generation=generation,
        now=(NOW + timedelta(seconds=11)).isoformat(),
    ) is False
