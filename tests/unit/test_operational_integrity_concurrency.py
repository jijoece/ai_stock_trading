from __future__ import annotations

import dataclasses
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.paper_books import cash_ledger, valuation
from trading_research.paper_books.external_broker import OrderLeaseHandle, OrderLeaseLostError
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _seed_book(path) -> None:
    conn = connect(path)
    cash_ledger.open_book(
        conn, book_id="BASELINE", starting_cash_usd=Decimal("100000"),
        config_hash="cfg", clock=lambda: NOW,
    )
    conn.close()


def test_concurrent_identical_snapshot_persistence_is_one_insert_one_replay(tmp_path):
    path = tmp_path / "snapshots.sqlite3"
    _seed_book(path)
    seed = connect(path)
    snapshot = valuation.build_portfolio_snapshot(
        seed, "BASELINE", NOW, maximum_price_age_seconds=900, persist=False,
    )
    seed.close()
    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[BaseException] = []

    def writer() -> None:
        conn = connect(path)
        try:
            barrier.wait(timeout=2)
            results.append(repo.save_snapshot(conn, snapshot, []))
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=writer), threading.Thread(target=writer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(results) == [False, True]
    check = connect(path)
    assert check.execute("SELECT COUNT(*) FROM paper_book_snapshots").fetchone()[0] == 1
    check.close()


def test_concurrent_same_snapshot_id_different_hash_fails_closed(tmp_path):
    path = tmp_path / "snapshot-conflict.sqlite3"
    _seed_book(path)
    seed = connect(path)
    snapshot = valuation.build_portfolio_snapshot(
        seed, "BASELINE", NOW, maximum_price_age_seconds=900, persist=False,
    )
    seed.close()
    tampered = dataclasses.replace(snapshot, source_hash="different-source-hash")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def writer(value) -> None:
        conn = connect(path)
        try:
            barrier.wait(timeout=2)
            repo.save_snapshot(conn, value, [])
            outcomes.append("inserted")
        except repo.SnapshotIdentityConflictError:
            outcomes.append("conflict")
        finally:
            conn.close()

    threads = [threading.Thread(target=writer, args=(snapshot,)), threading.Thread(target=writer, args=(tampered,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sorted(outcomes) == ["conflict", "inserted"]


def test_snapshot_header_rolls_back_when_position_insert_fails(tmp_path):
    path = tmp_path / "snapshot-rollback.sqlite3"
    _seed_book(path)
    conn = connect(path)
    snapshot = valuation.build_portfolio_snapshot(
        conn, "BASELINE", NOW, maximum_price_age_seconds=900, persist=False,
    )
    with pytest.raises(KeyError):
        repo.save_snapshot(conn, snapshot, [{"symbol": "AAPL"}])
    assert repo.load_snapshot(conn, "BASELINE", snapshot.snapshot_id) is None
    assert repo.save_snapshot(conn, snapshot, []) is True
    conn.close()


def test_fenced_write_blocks_takeover_then_rejects_stale_generation(tmp_path):
    path = tmp_path / "lease.sqlite3"
    _seed_book(path)
    conn_a = connect(path)
    conn_a.execute("CREATE TABLE fenced_probe (value TEXT PRIMARY KEY)")
    generation_a = repo.acquire_external_order_lease(
        conn_a, lease_key="BASELINE:client", book_id="BASELINE", client_order_id="client",
        owner_id="owner-a", operation="TEST", now=NOW.isoformat(),
        expires_at=(NOW + timedelta(seconds=5)).isoformat(),
    )
    clock_a = MutableClock(NOW + timedelta(seconds=4))
    handle_a = OrderLeaseHandle(conn_a, "BASELINE:client", "owner-a", generation_a, 5, clock_a)
    started = threading.Event()
    finished = threading.Event()
    generation_b: list[int | None] = []

    def takeover() -> None:
        conn_b = connect(path)
        started.set()
        try:
            generation_b.append(repo.acquire_external_order_lease(
                conn_b, lease_key="BASELINE:client", book_id="BASELINE", client_order_id="client",
                owner_id="owner-b", operation="TEST", now=(NOW + timedelta(seconds=6)).isoformat(),
                expires_at=(NOW + timedelta(seconds=30)).isoformat(),
            ))
        finally:
            conn_b.close()
            finished.set()

    with handle_a.fenced_write():
        thread = threading.Thread(target=takeover)
        thread.start()
        assert started.wait(timeout=1)
        time.sleep(0.05)
        assert not finished.is_set(), "takeover must wait for the protected write transaction"
        conn_a.execute("INSERT INTO fenced_probe(value) VALUES ('owner-a-write')")
    thread.join(timeout=5)
    assert generation_b == [generation_a + 1]

    # Represents a runtime call returning before owner B's takeover. No DB
    # transaction spans that call; the following stale protected write fails.
    clock_a.value = NOW + timedelta(seconds=7)
    with pytest.raises(OrderLeaseLostError):
        with handle_a.fenced_write():
            conn_a.execute("INSERT INTO fenced_probe(value) VALUES ('stale-write')")
    assert conn_a.execute("SELECT value FROM fenced_probe ORDER BY value").fetchall()[0][0] == "owner-a-write"
    assert conn_a.execute("SELECT COUNT(*) FROM fenced_probe").fetchone()[0] == 1
    conn_a.close()
