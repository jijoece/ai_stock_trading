"""Milestone 11.2 Part 4/5/36 regression: `database.begin_immediate` must
start a clean `BEGIN IMMEDIATE` transaction even when Python's legacy
sqlite3 isolation mode has already silently opened an implicit transaction
on this connection (a pending DML that neither committed nor rolled back),
and a failed manual transaction must never leave the connection wedged for
either itself or a second real connection against the same file.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from trading_research.storage.database import begin_immediate, connect


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "tx.sqlite3"


def test_pending_dml_before_begin_immediate_does_not_raise(db_path):
    """A prior unguarded write left an implicit transaction open (Python's
    default isolation_level=""). begin_immediate must clear it rather than
    raising 'cannot start a transaction within a transaction'."""
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
        "VALUES ('BASELINE', 'BASELINE', '100000', '2026-01-01T00:00:00Z', 'cfg')"
    )
    assert conn.in_transaction  # pending, uncommitted implicit transaction

    begin_immediate(conn)  # must not raise
    try:
        assert conn.in_transaction
        conn.commit()
    finally:
        conn.close()


def test_pending_dml_is_rolled_back_not_silently_committed(db_path):
    """The pending write that begin_immediate clears must actually be
    rolled back (not accidentally committed) — it was never explicitly
    committed by the caller, so its fate must remain a rollback."""
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
        "VALUES ('BASELINE', 'BASELINE', '100000', '2026-01-01T00:00:00Z', 'cfg')"
    )
    begin_immediate(conn)
    conn.rollback()
    row = conn.execute("SELECT COUNT(*) AS c FROM paper_books WHERE book_id = 'BASELINE'").fetchone()
    assert row["c"] == 0
    conn.close()


def test_manual_transaction_success_commits(db_path):
    conn = connect(db_path)
    begin_immediate(conn)
    conn.execute(
        "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
        "VALUES ('BASELINE', 'BASELINE', '100000', '2026-01-01T00:00:00Z', 'cfg')"
    )
    conn.commit()
    row = conn.execute("SELECT COUNT(*) AS c FROM paper_books").fetchone()
    assert row["c"] == 1
    conn.close()


def test_manual_transaction_rollback_leaves_connection_usable(db_path):
    conn = connect(db_path)
    begin_immediate(conn)
    conn.execute(
        "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
        "VALUES ('BASELINE', 'BASELINE', '100000', '2026-01-01T00:00:00Z', 'cfg')"
    )
    conn.rollback()
    assert not conn.in_transaction

    # The same connection object must remain fully usable afterward.
    begin_immediate(conn)
    conn.execute(
        "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
        "VALUES ('ENHANCED', 'ENHANCED', '100000', '2026-01-01T00:00:00Z', 'cfg')"
    )
    conn.commit()
    row = conn.execute("SELECT book_id FROM paper_books").fetchone()
    assert row["book_id"] == "ENHANCED"
    conn.close()


def test_failed_operation_does_not_block_a_second_real_connection(db_path):
    """A crash/abort inside a begin_immediate-protected block on connection
    A must release its write lock so connection B (a genuinely separate
    sqlite3.Connection against the same file) is never left blocked."""
    conn_a = connect(db_path)
    conn_b = connect(db_path)
    try:
        begin_immediate(conn_a)
        conn_a.execute(
            "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
            "VALUES ('BASELINE', 'BASELINE', '100000', '2026-01-01T00:00:00Z', 'cfg')"
        )
        conn_a.rollback()  # simulated crash/abort path

        # conn_b must be able to acquire its own BEGIN IMMEDIATE promptly —
        # busy_timeout is configured, but a leaked lock would still time out.
        begin_immediate(conn_b)
        conn_b.execute(
            "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
            "VALUES ('ENHANCED', 'ENHANCED', '100000', '2026-01-01T00:00:00Z', 'cfg')"
        )
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
