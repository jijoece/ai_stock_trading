"""Milestone 11.3.1 Item 2 regression: `database.begin_immediate`/`transaction`
must never silently commit or roll back a transaction they did not start.

Supersedes the Milestone 11.2 Part 4/5/36 version of this file, which
asserted the *opposite* property — that `begin_immediate` silently rolled
back any already-open transaction so it could always proceed. That behavior
could discard real caller-owned reservation, fill, checkpoint, or lease work.
Every connection from `database.py::connect()` is now opened with
`isolation_level=None` (true SQLite autocommit), so a bare, unguarded write
outside an explicit `BEGIN`/`BEGIN IMMEDIATE` block commits immediately and
never leaves a stray implicit transaction behind — `conn.in_transaction`
being `True` when `begin_immediate` is entered can now only mean a real,
still-open, caller-owned transaction, which `begin_immediate` must never
guess is abandoned.
"""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import pytest

from trading_research.storage.database import connect
from trading_research.storage.transactions import (
    TransactionAlreadyActiveError,
    begin_immediate,
    transaction,
)


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "tx.sqlite3"


def _insert_book(conn, book_id: str) -> None:
    conn.execute(
        "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
        f"VALUES ('{book_id}', 'BASELINE', '100000', '2026-01-01T00:00:00Z', 'cfg')"
    )


def test_bare_unguarded_write_autocommits_and_leaves_no_open_transaction(db_path):
    """Under isolation_level=None, a bare write outside an explicit BEGIN
    commits immediately -- it is never left as a dangling implicit
    transaction for a later begin_immediate() to have to clean up."""
    conn = connect(db_path)
    _insert_book(conn, "BASELINE")
    assert conn.in_transaction is False
    row = conn.execute("SELECT COUNT(*) AS c FROM paper_books WHERE book_id = 'BASELINE'").fetchone()
    assert row["c"] == 1
    conn.close()


def test_begin_immediate_after_bare_write_starts_clean(db_path):
    conn = connect(db_path)
    _insert_book(conn, "BASELINE")
    begin_immediate(conn)  # must not raise -- no transaction was left open
    try:
        assert conn.in_transaction
        conn.commit()
    finally:
        conn.close()


def test_begin_immediate_raises_on_already_open_transaction_instead_of_discarding_it(db_path):
    """The core Item 2 fix: begin_immediate must never silently roll back a
    genuinely open, caller-owned transaction. It must fail closed instead,
    and the caller's pending, uncommitted work must survive."""
    conn = connect(db_path)
    begin_immediate(conn)
    _insert_book(conn, "BASELINE")  # caller-owned, uncommitted work

    with pytest.raises(TransactionAlreadyActiveError):
        begin_immediate(conn)

    # The caller's own pending work must still be intact and committable --
    # never silently discarded by the nested begin_immediate() attempt.
    assert conn.in_transaction
    conn.commit()
    row = conn.execute("SELECT COUNT(*) AS c FROM paper_books WHERE book_id = 'BASELINE'").fetchone()
    assert row["c"] == 1
    conn.close()


def test_transaction_context_manager_raises_on_already_open_transaction(db_path):
    conn = connect(db_path)
    begin_immediate(conn)
    _insert_book(conn, "BASELINE")
    with pytest.raises(TransactionAlreadyActiveError):
        with transaction(conn):
            pass
    # Outer caller-owned transaction is untouched by the failed nested attempt.
    assert conn.in_transaction
    conn.commit()
    conn.close()


def test_manual_transaction_success_commits(db_path):
    conn = connect(db_path)
    begin_immediate(conn)
    _insert_book(conn, "BASELINE")
    conn.commit()
    row = conn.execute("SELECT COUNT(*) AS c FROM paper_books").fetchone()
    assert row["c"] == 1
    conn.close()


def test_manual_transaction_rollback_leaves_connection_usable(db_path):
    conn = connect(db_path)
    begin_immediate(conn)
    _insert_book(conn, "BASELINE")
    conn.rollback()
    assert not conn.in_transaction

    begin_immediate(conn)
    _insert_book(conn, "ENHANCED")
    conn.commit()
    row = conn.execute("SELECT book_id FROM paper_books").fetchone()
    assert row["book_id"] == "ENHANCED"
    conn.close()


def test_failed_operation_does_not_block_a_second_real_connection(db_path):
    conn_a = connect(db_path)
    conn_b = connect(db_path)
    try:
        begin_immediate(conn_a)
        _insert_book(conn_a, "BASELINE")
        conn_a.rollback()  # simulated crash/abort path

        begin_immediate(conn_b)
        _insert_book(conn_b, "ENHANCED")
        conn_b.commit()
        row = conn_b.execute("SELECT COUNT(*) AS c FROM paper_books").fetchone()
        assert row["c"] == 1
    finally:
        conn_a.close()
        conn_b.close()


def test_busy_timeout_configured(db_path):
    conn = connect(db_path)
    value = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert value == 5_000
    conn.close()


# --- transaction() context manager -------------------------------------------


def test_transaction_context_manager_commits_on_success(db_path):
    conn = connect(db_path)
    with transaction(conn):
        _insert_book(conn, "BASELINE")
    assert not conn.in_transaction
    row = conn.execute("SELECT COUNT(*) AS c FROM paper_books").fetchone()
    assert row["c"] == 1
    conn.close()


def test_transaction_context_manager_rolls_back_on_exception(db_path):
    conn = connect(db_path)
    with pytest.raises(ValueError):
        with transaction(conn):
            _insert_book(conn, "BASELINE")
            raise ValueError("boom")
    assert not conn.in_transaction
    row = conn.execute("SELECT COUNT(*) AS c FROM paper_books").fetchone()
    assert row["c"] == 0
    conn.close()


def test_transaction_context_manager_rolls_back_on_base_exception(db_path):
    """A BaseException (not just Exception) mid-write must still roll back
    and never leave a dangling open transaction on the connection."""
    conn = connect(db_path)
    with pytest.raises(KeyboardInterrupt):
        with transaction(conn):
            _insert_book(conn, "BASELINE")
            raise KeyboardInterrupt()
    assert not conn.in_transaction
    row = conn.execute("SELECT COUNT(*) AS c FROM paper_books").fetchone()
    assert row["c"] == 0
    conn.close()


def test_exception_rolls_back_only_the_failing_operations_own_transaction(db_path):
    """A later, unrelated commit on a second connection must not be affected
    by (or accidentally resurrect) a first connection's rolled-back work."""
    conn_a = connect(db_path)
    conn_b = connect(db_path)
    try:
        with pytest.raises(ValueError):
            with transaction(conn_a):
                _insert_book(conn_a, "BASELINE")
                raise ValueError("boom")

        with transaction(conn_b):
            _insert_book(conn_b, "ENHANCED")

        rows = {r["book_id"] for r in conn_b.execute("SELECT book_id FROM paper_books")}
        assert rows == {"ENHANCED"}
    finally:
        conn_a.close()
        conn_b.close()


def test_two_connections_serialize_begin_immediate_workflows(db_path):
    """Two separate connections against the same file must still serialize
    at the BEGIN IMMEDIATE write-lock boundary -- one blocks until the other
    finishes, neither silently loses or duplicates work."""
    # Run schema setup (additive DDL + migrations) once, up front, on its own
    # connection -- connect() itself performs several small writes, and
    # racing that DDL setup on both connections is a separate, uninteresting
    # source of "database is locked" flakiness this test does not intend to
    # exercise. The property under test is BEGIN IMMEDIATE serialization on
    # an already-fully-migrated database.
    connect(db_path).close()

    started = threading.Event()
    released = threading.Event()

    def hold_transaction():
        # sqlite3 connections are single-thread-affine by default -- open
        # and use conn_a entirely inside this thread.
        conn_a = connect(db_path)
        try:
            with transaction(conn_a):
                _insert_book(conn_a, "BASELINE")
                started.set()
                released.wait(timeout=5)
                time.sleep(0.05)
        finally:
            conn_a.close()

    thread = threading.Thread(target=hold_transaction)
    thread.start()
    assert started.wait(timeout=5)
    conn_b = connect(db_path)
    try:
        released.set()
        # conn_b must be able to acquire its own BEGIN IMMEDIATE once conn_a
        # releases -- busy_timeout covers the wait.
        with transaction(conn_b):
            _insert_book(conn_b, "ENHANCED")
        thread.join(timeout=5)
        rows = {r["book_id"] for r in conn_b.execute("SELECT book_id FROM paper_books")}
        assert rows == {"BASELINE", "ENHANCED"}
    finally:
        thread.join(timeout=5)
        conn_b.close()


def test_schema_creation_and_migration_work_under_explicit_transaction_control(db_path):
    """connect() itself runs additive DDL and the schema_version migration
    ledger -- both must succeed cleanly under isolation_level=None."""
    conn = connect(db_path)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] is not None
    conn.close()

    # Idempotent: reopening the same database file must not fail or re-apply.
    conn2 = connect(db_path)
    row2 = conn2.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row2["v"] == row["v"]
    conn2.close()
