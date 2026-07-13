"""Tests for shadow/lease.py (docs/milestone-7.md Step 14, Step 27 section G).

Uses real on-disk sqlite files (not `:memory:`) for the concurrency tests so
that two separate `sqlite3.Connection` objects genuinely exercise SQLite's
file-level locking, not just Python-level state.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_research.shadow.lease import (
    LeaseConflict,
    LeaseError,
    LeaseHandle,
    acquire,
    current_lease,
    force_release,
    release,
    renew,
)
from trading_research.storage.database import connect
from trading_research.storage.shadow_operations_repositories import list_operator_actions

BASE_TIME = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)


def _clock_at(t: datetime):
    return lambda: t


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "shadow_test.db"


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


# --- acquisition --------------------------------------------------------------


def test_acquire_fresh_lease_succeeds(conn):
    result = acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    assert isinstance(result, LeaseHandle)
    assert result.owner == "owner-1"
    assert result.expires_at == BASE_TIME + timedelta(seconds=3600)


def test_acquire_persists_lease_row(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    row = current_lease(conn, "shadow-scheduler")
    assert row is not None
    assert row["owner"] == "owner-1"
    assert row["status"] == "HELD"


# --- conflict ------------------------------------------------------------------


def test_acquire_conflict_when_already_held(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    result = acquire(conn, "shadow-scheduler", "owner-2", 3600, _clock_at(BASE_TIME + timedelta(seconds=10)))
    assert isinstance(result, LeaseConflict)
    assert result.held_by == "owner-1"


def test_conflict_does_not_change_lease_owner(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    acquire(conn, "shadow-scheduler", "owner-2", 3600, _clock_at(BASE_TIME + timedelta(seconds=10)))
    row = current_lease(conn, "shadow-scheduler")
    assert row["owner"] == "owner-1"


# --- renewal -------------------------------------------------------------------


def test_renew_extends_expiration(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    renewed_time = BASE_TIME + timedelta(seconds=1800)
    result = renew(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(renewed_time))
    assert result.expires_at == renewed_time + timedelta(seconds=3600)


def test_renew_by_non_owner_raises(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    with pytest.raises(LeaseError):
        renew(conn, "shadow-scheduler", "owner-2", 3600, _clock_at(BASE_TIME + timedelta(seconds=10)))


def test_renew_nonexistent_lease_raises(conn):
    with pytest.raises(LeaseError):
        renew(conn, "no-such-lease", "owner-1", 3600, _clock_at(BASE_TIME))


# --- release -------------------------------------------------------------------


def test_release_frees_the_lease(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    release(conn, "shadow-scheduler", "owner-1", _clock_at(BASE_TIME + timedelta(seconds=10)))
    row = current_lease(conn, "shadow-scheduler")
    assert row["status"] == "RELEASED"


def test_release_by_non_owner_raises(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    with pytest.raises(LeaseError):
        release(conn, "shadow-scheduler", "owner-2", _clock_at(BASE_TIME + timedelta(seconds=10)))


def test_acquire_after_release_succeeds_for_new_owner(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    release(conn, "shadow-scheduler", "owner-1", _clock_at(BASE_TIME + timedelta(seconds=10)))
    result = acquire(conn, "shadow-scheduler", "owner-2", 3600, _clock_at(BASE_TIME + timedelta(seconds=20)))
    assert isinstance(result, LeaseHandle)
    assert result.owner == "owner-2"


# --- expiration and stale recovery ---------------------------------------------


def test_expired_lease_can_be_reclaimed_by_new_owner(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    after_expiry = BASE_TIME + timedelta(seconds=3601)
    result = acquire(conn, "shadow-scheduler", "owner-2", 3600, _clock_at(after_expiry))
    assert isinstance(result, LeaseHandle)
    assert result.owner == "owner-2"


def test_stale_reclaim_writes_operator_action_audit(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    after_expiry = BASE_TIME + timedelta(seconds=3601)
    acquire(conn, "shadow-scheduler", "owner-2", 3600, _clock_at(after_expiry))
    actions = list_operator_actions(conn, action_type="LEASE_STALE_RECOVERED")
    assert len(actions) == 1
    assert "owner-1" in actions[0]["reason"]
    assert "owner-2" in actions[0]["reason"]


def test_not_yet_expired_lease_cannot_be_reclaimed(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    almost_expired = BASE_TIME + timedelta(seconds=3599)
    result = acquire(conn, "shadow-scheduler", "owner-2", 3600, _clock_at(almost_expired))
    assert isinstance(result, LeaseConflict)


# --- concurrent connections (real file, two sqlite3.connect() calls) --------


def test_concurrent_connections_only_one_acquires(db_path):
    conn_a = connect(db_path)
    conn_b = connect(db_path)
    try:
        result_a = acquire(conn_a, "shadow-scheduler", "owner-a", 3600, _clock_at(BASE_TIME))
        result_b = acquire(conn_b, "shadow-scheduler", "owner-b", 3600, _clock_at(BASE_TIME))
        results = [result_a, result_b]
        handles = [r for r in results if isinstance(r, LeaseHandle)]
        conflicts = [r for r in results if isinstance(r, LeaseConflict)]
        assert len(handles) == 1
        assert len(conflicts) == 1
        assert conflicts[0].held_by == handles[0].owner
    finally:
        conn_a.close()
        conn_b.close()


def test_concurrent_connections_no_duplicate_cycle_while_held(db_path):
    """Simulates two scheduler invocations racing for the same intended
    cycle: only one may proceed."""
    conn_a = connect(db_path)
    conn_b = connect(db_path)
    try:
        acquire(conn_a, "cycle-2026-07-13", "invocation-a", 3600, _clock_at(BASE_TIME))
        second = acquire(conn_b, "cycle-2026-07-13", "invocation-b", 3600, _clock_at(BASE_TIME))
        assert isinstance(second, LeaseConflict)
        # The lease row, read from either connection, agrees on a single owner.
        row_from_b = current_lease(conn_b, "cycle-2026-07-13")
        assert row_from_b["owner"] == "invocation-a"
    finally:
        conn_a.close()
        conn_b.close()


def test_concurrent_stale_reclaim_only_one_new_owner_wins(db_path):
    conn_a = connect(db_path)
    conn_b = connect(db_path)
    try:
        acquire(conn_a, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
        after_expiry = BASE_TIME + timedelta(seconds=3601)
        result_a = acquire(conn_a, "shadow-scheduler", "owner-2", 3600, _clock_at(after_expiry))
        result_b = acquire(conn_b, "shadow-scheduler", "owner-3", 3600, _clock_at(after_expiry))
        results = [result_a, result_b]
        handles = [r for r in results if isinstance(r, LeaseHandle)]
        assert len(handles) == 1
    finally:
        conn_a.close()
        conn_b.close()


# --- operator force-release audit ----------------------------------------------


def test_force_release_frees_lease_and_records_audit(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    force_release(conn, "shadow-scheduler", "operator maintenance override", "jijo", _clock_at(BASE_TIME + timedelta(seconds=5)))
    row = current_lease(conn, "shadow-scheduler")
    assert row["status"] == "RELEASED"
    actions = list_operator_actions(conn, action_type="LEASE_FORCE_RELEASED")
    assert len(actions) == 1
    assert actions[0]["reason"] == "operator maintenance override"
    assert actions[0]["operator"] == "jijo"


def test_force_release_requires_non_empty_reason(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    with pytest.raises(LeaseError):
        force_release(conn, "shadow-scheduler", "", "jijo", _clock_at(BASE_TIME))


def test_force_release_requires_non_empty_operator(conn):
    acquire(conn, "shadow-scheduler", "owner-1", 3600, _clock_at(BASE_TIME))
    with pytest.raises(LeaseError):
        force_release(conn, "shadow-scheduler", "reason", "", _clock_at(BASE_TIME))


def test_force_release_nonexistent_lease_raises(conn):
    with pytest.raises(LeaseError):
        force_release(conn, "no-such-lease", "reason", "jijo", _clock_at(BASE_TIME))
