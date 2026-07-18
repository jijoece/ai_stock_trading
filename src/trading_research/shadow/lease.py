"""Database-backed run lease (docs/milestone-7.md Step 14, ADR 0005 Decision 3).

A lease prevents two *concurrent* processes from starting the same intended
cycle at the same time — a mutex with a bounded TTL, owner identity, and
stale-recovery. This is a separate, complementary mechanism from cycle-level
idempotency (`research/scheduled_cycle.py::derive_cycle_id`): a lease alone
does not survive a crash between "lease released" and "cycle marked
complete"; idempotency alone does not prevent two processes racing to
acquire before either persists anything.

Atomicity is achieved with a real SQL constraint (`UNIQUE(lease_key)` on
`shadow_run_leases`) plus a conditional `UPDATE ... WHERE expires_at < ?`
for stale reclaim, both wrapped in a `BEGIN IMMEDIATE` transaction — this is
safe across separate `sqlite3.Connection` objects opened against the same
on-disk database file (see `tests/unit/test_shadow_lease.py`'s
two-real-connections test), not just safe within one process.

`clock: Callable[[], datetime]` is injected everywhere for testability, per
repository convention. No permanent lock after crash: a lease is always
TTL-bounded and reclaimable once expired. No force-unlock function exists
that doesn't require an explicit reason string recorded as an operator
action (`force_release`).
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from ..storage.database import begin_immediate
from ..storage.shadow_operations_repositories import insert_operator_action, load_lease

LEASE_STATUS_HELD = "HELD"
LEASE_STATUS_RELEASED = "RELEASED"

Clock = Callable[[], datetime]


class LeaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaseHandle:
    lease_key: str
    owner: str
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class LeaseConflict:
    lease_key: str
    held_by: str
    expires_at: datetime


def _new_owner_token() -> str:
    return f"owner-{uuid.uuid4().hex}"


def acquire(
    conn: sqlite3.Connection, lease_key: str, owner: str, ttl_seconds: int, clock: Clock
) -> LeaseHandle | LeaseConflict:
    """Attempt to acquire `lease_key` for `owner`. Returns a `LeaseHandle` on
    success or a `LeaseConflict` describing the current holder on failure.

    Implementation: `BEGIN IMMEDIATE` takes a write lock on the sqlite file
    for the duration of this transaction, so a second connection attempting
    the same statement blocks (and ultimately observes the first
    transaction's committed result) rather than racing it in memory."""
    now = clock()
    expires_at = now + timedelta(seconds=ttl_seconds)
    try:
        begin_immediate(conn)
        existing = conn.execute(
            "SELECT * FROM shadow_run_leases WHERE lease_key = ?", (lease_key,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO shadow_run_leases (lease_key, owner, acquired_at, expires_at, renewed_at, "
                "released_at, status) VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                (lease_key, owner, now.isoformat(), expires_at.isoformat(), LEASE_STATUS_HELD),
            )
            conn.commit()
            return LeaseHandle(lease_key=lease_key, owner=owner, acquired_at=now, expires_at=expires_at)

        existing_status = existing["status"]
        existing_expires_at = datetime.fromisoformat(existing["expires_at"])
        is_stale = existing_status == LEASE_STATUS_HELD and existing_expires_at < now
        is_free = existing_status == LEASE_STATUS_RELEASED

        if is_free:
            conn.execute(
                "UPDATE shadow_run_leases SET owner = ?, acquired_at = ?, expires_at = ?, renewed_at = NULL, "
                "released_at = NULL, status = ? WHERE lease_key = ?",
                (owner, now.isoformat(), expires_at.isoformat(), LEASE_STATUS_HELD, lease_key),
            )
            conn.commit()
            return LeaseHandle(lease_key=lease_key, owner=owner, acquired_at=now, expires_at=expires_at)

        if is_stale:
            previous_owner = existing["owner"]
            cursor = conn.execute(
                "UPDATE shadow_run_leases SET owner = ?, acquired_at = ?, expires_at = ?, renewed_at = NULL, "
                "released_at = NULL, status = ? WHERE lease_key = ? AND expires_at < ?",
                (owner, now.isoformat(), expires_at.isoformat(), LEASE_STATUS_HELD, lease_key, now.isoformat()),
            )
            if cursor.rowcount == 0:
                # Another connection reclaimed or renewed it first within
                # this transaction window — conflict, not a silent takeover.
                conn.rollback()
                refreshed = conn.execute(
                    "SELECT * FROM shadow_run_leases WHERE lease_key = ?", (lease_key,)
                ).fetchone()
                return LeaseConflict(
                    lease_key=lease_key, held_by=refreshed["owner"],
                    expires_at=datetime.fromisoformat(refreshed["expires_at"]),
                )
            conn.commit()
            _record_stale_recovery(conn, lease_key, previous_owner, owner, now)
            return LeaseHandle(lease_key=lease_key, owner=owner, acquired_at=now, expires_at=expires_at)

        # Held and not stale -> conflict.
        conn.rollback()
        return LeaseConflict(lease_key=lease_key, held_by=existing["owner"], expires_at=existing_expires_at)
    except Exception:
        conn.rollback()
        raise


def _record_stale_recovery(
    conn: sqlite3.Connection, lease_key: str, previous_owner: str, new_owner: str, now: datetime
) -> None:
    """Stale reclaim writes an audit record noting the recovery — never a
    silent takeover (docs/milestone-7.md Step 14)."""
    insert_operator_action(
        conn,
        {
            "action_id": f"stale-recovery-{uuid.uuid4().hex}",
            "action_type": "LEASE_STALE_RECOVERED",
            "target": lease_key,
            "reason": f"lease expired while held by {previous_owner!r}; reclaimed by {new_owner!r}",
            "operator": None,
            "details": None,
            "created_at": now.isoformat(),
        },
    )


def renew(conn: sqlite3.Connection, lease_key: str, owner: str, ttl_seconds: int, clock: Clock) -> LeaseHandle:
    now = clock()
    new_expires_at = now + timedelta(seconds=ttl_seconds)
    try:
        begin_immediate(conn)
        row = conn.execute("SELECT * FROM shadow_run_leases WHERE lease_key = ?", (lease_key,)).fetchone()
        if row is None or row["status"] != LEASE_STATUS_HELD or row["owner"] != owner:
            conn.rollback()
            raise LeaseError(f"cannot renew lease {lease_key!r}: not held by {owner!r}")
        conn.execute(
            "UPDATE shadow_run_leases SET expires_at = ?, renewed_at = ? WHERE lease_key = ? AND owner = ?",
            (new_expires_at.isoformat(), now.isoformat(), lease_key, owner),
        )
        conn.commit()
        return LeaseHandle(lease_key=lease_key, owner=owner, acquired_at=now, expires_at=new_expires_at)
    except Exception:
        conn.rollback()
        raise


def release(conn: sqlite3.Connection, lease_key: str, owner: str, clock: Clock) -> None:
    now = clock()
    try:
        begin_immediate(conn)
        row = conn.execute("SELECT * FROM shadow_run_leases WHERE lease_key = ?", (lease_key,)).fetchone()
        if row is None or row["status"] != LEASE_STATUS_HELD or row["owner"] != owner:
            conn.rollback()
            raise LeaseError(f"cannot release lease {lease_key!r}: not held by {owner!r}")
        conn.execute(
            "UPDATE shadow_run_leases SET status = ?, released_at = ? WHERE lease_key = ? AND owner = ?",
            (LEASE_STATUS_RELEASED, now.isoformat(), lease_key, owner),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def force_release(conn: sqlite3.Connection, lease_key: str, reason: str, operator: str, clock: Clock) -> None:
    """The only way to release a lease without presenting the owner token.
    Requires an explicit reason and operator identity, always recorded as an
    operator action (docs/milestone-7.md Step 14: "no force unlock without a
    recorded operator action")."""
    if not reason or not reason.strip():
        raise LeaseError("force_release requires a non-empty reason")
    if not operator or not operator.strip():
        raise LeaseError("force_release requires a non-empty operator identity")
    now = clock()
    try:
        begin_immediate(conn)
        row = conn.execute("SELECT * FROM shadow_run_leases WHERE lease_key = ?", (lease_key,)).fetchone()
        if row is None:
            conn.rollback()
            raise LeaseError(f"cannot force-release lease {lease_key!r}: no such lease")
        conn.execute(
            "UPDATE shadow_run_leases SET status = ?, released_at = ? WHERE lease_key = ?",
            (LEASE_STATUS_RELEASED, now.isoformat(), lease_key),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    insert_operator_action(
        conn,
        {
            "action_id": f"force-release-{uuid.uuid4().hex}",
            "action_type": "LEASE_FORCE_RELEASED",
            "target": lease_key,
            "reason": reason,
            "operator": operator,
            "details": None,
            "created_at": now.isoformat(),
        },
    )


def current_lease(conn: sqlite3.Connection, lease_key: str) -> dict | None:
    return load_lease(conn, lease_key)


def generate_owner_token() -> str:
    """uuid4-based owner token, per repository convention (uuid4 for
    lease-owner tokens, content-hash IDs reserved for anything that must be
    idempotent from its inputs)."""
    return _new_owner_token()
