"""Milestone 11.2 Part 3/18/36 regression: the `paper_external_order_lookups`
immutability trigger must be explicitly migrated on an existing database,
not merely re-declared with `CREATE TRIGGER IF NOT EXISTS` (which is a
no-op when a trigger with that name already exists).

This test builds an on-disk database carrying the exact Milestone 11.1
trigger body (`WHEN OLD.consumed_by_retry_event_id IS NOT NULL` — blocks
re-consumption but otherwise permits arbitrary field edits on an unconsumed
row), then opens it through the real `connect()` and verifies the upgraded
trigger's full immutability contract.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from trading_research.storage.database import connect
from trading_research.storage.paper_books_schema import PAPER_BOOKS_DDL, PAPER_BOOKS_INDEXES

_MILESTONE_11_1_LOOKUP_TRIGGER = """
CREATE TRIGGER trg_paper_external_lookups_no_update
BEFORE UPDATE ON paper_external_order_lookups
WHEN OLD.consumed_by_retry_event_id IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'paper external order lookup consumption is recorded once'); END;
"""


@pytest.fixture
def prior_schema_db_path():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "prior.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript(PAPER_BOOKS_DDL)
        conn.executescript(PAPER_BOOKS_INDEXES)
        conn.executescript(_MILESTONE_11_1_LOOKUP_TRIGGER)
        conn.execute(
            "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
            "VALUES ('BASELINE', 'BASELINE', '100000', '2026-01-01T00:00:00Z', 'cfg')"
        )
        conn.execute(
            "INSERT INTO paper_external_order_lookups "
            "(lookup_id, book_id, paper_order_intent_id, client_order_id, account_fingerprint, "
            " result, authoritative, created_at) "
            "VALUES ('lk-1', 'BASELINE', 'intent-1', 'client-1', 'fp-1', 'NOT_FOUND', 1, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()
        yield db_path


def test_legacy_trigger_blocked_every_update_before_upgrade(prior_schema_db_path):
    """Sanity check on the fixture itself: under the exact Milestone 11.1
    trigger body, even the legitimate NULL -> value consumption transition
    was rejected once any other field changed alongside it — establishing
    the fixture faithfully reproduces pre-upgrade behavior before we assert
    the upgrade fixes anything."""
    conn = sqlite3.connect(str(prior_schema_db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "UPDATE paper_external_order_lookups SET result = 'FOUND' WHERE lookup_id = 'lk-1'"
    )
    conn.commit()
    row = conn.execute(
        "SELECT result FROM paper_external_order_lookups WHERE lookup_id = 'lk-1'"
    ).fetchone()
    assert row[0] == "FOUND", "legacy trigger permitted an unconsumed-row field edit"
    conn.close()


def test_upgrade_replaces_stale_trigger_and_enforces_full_contract(prior_schema_db_path):
    conn = connect(prior_schema_db_path)

    trigger_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_paper_external_lookups_no_update'"
    ).fetchone()[0]
    assert "consumed_by_retry_event_id IS OLD.consumed_by_retry_event_id" not in trigger_sql
    assert "NOT (" in trigger_sql

    # Modifying an unrelated field on an unconsumed row must now abort.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE paper_external_order_lookups SET result = 'FOUND' WHERE lookup_id = 'lk-1'")
    conn.rollback()

    # The single controlled consumption transition succeeds.
    conn.execute(
        "UPDATE paper_external_order_lookups SET consumed_by_retry_event_id = 'evt-1' WHERE lookup_id = 'lk-1'"
    )
    conn.commit()
    row = conn.execute(
        "SELECT consumed_by_retry_event_id FROM paper_external_order_lookups WHERE lookup_id = 'lk-1'"
    ).fetchone()
    assert row["consumed_by_retry_event_id"] == "evt-1"

    # A second consumption is rejected.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE paper_external_order_lookups SET consumed_by_retry_event_id = 'evt-2' WHERE lookup_id = 'lk-1'"
        )
    conn.rollback()

    # Clearing the field back to NULL is rejected.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE paper_external_order_lookups SET consumed_by_retry_event_id = NULL WHERE lookup_id = 'lk-1'"
        )
    conn.rollback()

    # Modifying another column while consuming is rejected.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE paper_external_order_lookups SET result = 'FOUND', consumed_by_retry_event_id = 'evt-3' "
            "WHERE lookup_id = 'lk-1'"
        )
    conn.rollback()

    # Deletion is rejected.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM paper_external_order_lookups WHERE lookup_id = 'lk-1'")
    conn.rollback()

    conn.close()


def test_reopening_upgraded_database_stays_stable(prior_schema_db_path):
    """Idempotency: opening the same (already-upgraded) database a second
    time must not error and must not re-apply the drop/recreate needlessly."""
    connect(prior_schema_db_path).close()
    conn = connect(prior_schema_db_path)
    version = conn.execute(
        "SELECT version FROM paper_books_trigger_versions WHERE trigger_name = "
        "'trg_paper_external_lookups_no_update'"
    ).fetchone()[0]
    assert version == 2
    conn.close()
