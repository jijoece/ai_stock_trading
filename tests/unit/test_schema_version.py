"""Milestone 11.3 Part 34: general schema-version table, distinct from
`paper_books_schema.py::paper_books_trigger_versions` (which only tracks
individual trigger bodies)."""
from __future__ import annotations

import sqlite3

import pytest

from trading_research.storage.database import connect
from trading_research.storage.schema_version import (
    CURRENT_SCHEMA_VERSION,
    SchemaVersionError,
    apply_pending_schema_migrations,
    check_schema_not_forward_versioned,
)


def test_fresh_database_records_current_schema_version(tmp_path):
    conn = connect(tmp_path / "fresh.sqlite3")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    assert row[0] == CURRENT_SCHEMA_VERSION
    conn.close()


def test_reopening_database_does_not_duplicate_or_regress_version(tmp_path):
    db_path = tmp_path / "reopen.sqlite3"
    connect(db_path).close()
    conn = connect(db_path)
    rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    assert [r[0] for r in rows] == list(range(1, CURRENT_SCHEMA_VERSION + 1))
    conn.close()


def test_forward_versioned_database_is_refused(tmp_path):
    """A database written by hypothetical future code (a persisted version
    greater than this code's CURRENT_SCHEMA_VERSION) must not be silently
    opened — this is the concrete 'downgrade' scenario the version check
    protects against."""
    db_path = tmp_path / "future.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_version (version, description, applied_at) VALUES (?, 'from the future', 'x')",
        (CURRENT_SCHEMA_VERSION + 1,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionError):
        connect(db_path)


def test_migration_is_idempotent_when_invoked_twice(tmp_path):
    db_path = tmp_path / "idem.sqlite3"
    conn = connect(db_path)
    # A second explicit invocation (simulating a retried/duplicated call)
    # must not raise and must not create a duplicate version row.
    apply_pending_schema_migrations(conn)
    apply_pending_schema_migrations(conn)
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    assert sorted(r[0] for r in rows) == list(range(1, CURRENT_SCHEMA_VERSION + 1))
    conn.close()


def test_check_schema_not_forward_versioned_creates_table_on_absent_db(tmp_path):
    db_path = tmp_path / "absent.sqlite3"
    conn = sqlite3.connect(str(db_path))
    check_schema_not_forward_versioned(conn)  # must not raise; no prior version stored
    conn.commit()
    conn.close()


def test_prior_milestone_databases_upgrade_to_current_schema_version(tmp_path):
    """A database from before schema_version existed at all (no
    schema_version table on disk) upgrades cleanly to CURRENT_SCHEMA_VERSION
    on first open — the exact scenario every pre-11.3 database is in."""
    db_path = tmp_path / "no_version_table.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE paper_books (book_id TEXT PRIMARY KEY, experiment_arm TEXT NOT NULL, "
        "currency TEXT NOT NULL DEFAULT 'USD', starting_cash_usd TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL, config_hash TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    conn = connect(db_path)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    assert row[0] == CURRENT_SCHEMA_VERSION
    conn.close()
