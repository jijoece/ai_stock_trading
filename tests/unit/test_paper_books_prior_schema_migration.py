"""Milestone 11.3 Part 2: full prior-schema migration fixture matrix.

Milestone 11.2 only proved the upgrade path for the one Milestone-11.1
lookup-trigger fixture (`test_paper_external_lookup_trigger_migration.py`).
This extends that same "build an on-disk database with the *exact* prior
version's literal DDL/index/trigger SQL, then open it through the real
`connect()`" pattern to the three schema generations the spec names:

* pre-Milestone-11 (no `paper_external_*` tables at all yet)
* Milestone-11 (external tables exist, but not the 11.1 share-reservation/
  lease tables or lookup retry columns)
* Milestone-11.1 (everything above exists, but `paper_external_order_leases`
  has no `generation` column yet, and the lookup trigger is still the
  interim "blocks re-consumption only" body)

The prior-version SQL is not paraphrased here: each fixture module under
`tests/fixtures/schema_history/` is a verbatim `git show` snapshot of
`storage/paper_books_schema.py` at the commit that introduced the next
milestone, loaded at test time via `runpy.run_path` so the literal
`PAPER_BOOKS_DDL`/`PAPER_BOOKS_INDEXES`/`PAPER_BOOKS_TRIGGERS` strings are
used unmodified to build the on-disk fixture.
"""
from __future__ import annotations

import runpy
import sqlite3
from pathlib import Path

import pytest

from trading_research.storage.database import connect

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "schema_history"


def _load_historical_schema(filename: str) -> dict:
    return runpy.run_path(str(_FIXTURES_DIR / filename))


def _build_prior_db(tmp_path: Path, schema_module: dict, *, name: str) -> Path:
    db_path = tmp_path / name
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema_module["PAPER_BOOKS_DDL"])
    conn.executescript(schema_module["PAPER_BOOKS_INDEXES"])
    conn.executescript(schema_module["PAPER_BOOKS_TRIGGERS"])
    conn.execute(
        "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
        "VALUES ('BASELINE', 'BASELINE', '100000', '2026-01-01T00:00:00Z', 'cfg')"
    )
    conn.execute(
        "INSERT INTO paper_book_cash_ledger "
        "(book_id, ledger_entry_id, event_type, amount_usd, event_timestamp, idempotency_key, created_at) "
        "VALUES ('BASELINE', 'ledger-1', 'INITIAL_CAPITAL', '100000', '2026-01-01T00:00:00Z', 'idem-1', "
        "'2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Fixture 1: pre-Milestone-11 (no paper_external_* tables exist at all).
# ---------------------------------------------------------------------------

@pytest.fixture
def pre_milestone_11_db(tmp_path):
    schema = _load_historical_schema("pre_milestone_11_schema.py")
    assert "paper_external_order_lookups" not in schema["PAPER_BOOKS_DDL"], (
        "fixture sanity: pre-Milestone-11 DDL must not already contain Milestone-11 tables"
    )
    return _build_prior_db(tmp_path, schema, name="pre_m11.sqlite3")


def test_pre_milestone_11_upgrade_creates_external_tables_and_preserves_data(pre_milestone_11_db):
    conn = connect(pre_milestone_11_db)

    # Pre-existing data untouched.
    book = conn.execute("SELECT * FROM paper_books WHERE book_id = 'BASELINE'").fetchone()
    assert book["starting_cash_usd"] == "100000"
    ledger_row = conn.execute(
        "SELECT * FROM paper_book_cash_ledger WHERE ledger_entry_id = 'ledger-1'"
    ).fetchone()
    assert ledger_row["amount_usd"] == "100000"

    # New Milestone-11+ tables now exist and are fully usable, including the
    # 11.2 lease-generation column and the current strict lookup trigger.
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    for expected in (
        "paper_external_order_previews", "paper_external_order_events",
        "paper_external_broker_fills", "paper_external_order_lookups",
        "paper_external_reconciliations", "paper_external_submission_queue",
        "paper_external_position_reservation_events", "paper_external_order_leases",
        "paper_books_trigger_versions",
    ):
        assert expected in tables, f"missing table after upgrade: {expected}"

    lease_columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_external_order_leases)").fetchall()}
    assert "generation" in lease_columns

    conn.execute(
        "INSERT INTO paper_external_order_leases "
        "(lease_key, book_id, client_order_id, owner_id, operation, acquired_at, heartbeat_at, expires_at, status) "
        "VALUES ('lk', 'BASELINE', 'co-1', 'owner-1', 'SUBMIT', '2026-01-01T00:00:00Z', "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z', 'ACTIVE')"
    )
    conn.commit()
    row = conn.execute("SELECT generation FROM paper_external_order_leases WHERE lease_key = 'lk'").fetchone()
    assert row["generation"] == 1, "generation must default to 1 for a freshly created lease"

    conn.close()


def test_pre_milestone_11_upgrade_append_only_triggers_active_on_legacy_tables(pre_milestone_11_db):
    conn = connect(pre_milestone_11_db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE paper_book_cash_ledger SET amount_usd = '999' WHERE ledger_entry_id = 'ledger-1'"
        )
    conn.rollback()
    conn.close()


# ---------------------------------------------------------------------------
# Fixture 2: Milestone-11 (external tables exist; no 11.1 reservation/lease
# tables, no retry columns on lookups, no scope_sequence on events).
# ---------------------------------------------------------------------------

@pytest.fixture
def milestone_11_db(tmp_path):
    schema = _load_historical_schema("milestone_11_schema.py")
    assert "paper_external_order_leases" not in schema["PAPER_BOOKS_DDL"], (
        "fixture sanity: Milestone-11 DDL must predate the 11.1 lease table"
    )
    db_path = _build_prior_db(tmp_path, schema, name="m11.sqlite3")
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO paper_external_order_lookups "
        "(lookup_id, book_id, paper_order_intent_id, client_order_id, account_fingerprint, "
        " result, authoritative, created_at) "
        "VALUES ('lk-1', 'BASELINE', 'intent-1', 'client-1', 'fp-1', 'NOT_FOUND', 1, '2026-01-02T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO paper_external_order_events "
        "(external_order_event_id, external_order_scope_id, book_id, paper_order_intent_id, client_order_id, "
        " account_fingerprint, previous_state, new_state, payload_hash, quantity, limit_price, operator, reason, "
        " created_at, policy_version, config_hash) "
        "VALUES ('evt-1', 'scope-1', 'BASELINE', 'intent-1', 'client-1', 'fp-1', 'NONE', 'PREVIEWED', "
        " 'hash-1', '10', '5.00', 'op', 'preview', '2026-01-02T00:00:00Z', 'v1', 'cfg')"
    )
    conn.commit()
    conn.close()
    return db_path


def test_milestone_11_upgrade_adds_reservation_and_lease_tables_preserving_prior_external_rows(milestone_11_db):
    conn = connect(milestone_11_db)

    lookup = conn.execute("SELECT * FROM paper_external_order_lookups WHERE lookup_id = 'lk-1'").fetchone()
    assert lookup["result"] == "NOT_FOUND"
    lookup_columns = set(lookup.keys())
    for col in (
        "attempt_number", "ambiguous_event_id", "payload_hash",
        "lookup_started_at", "lookup_completed_at", "consumed_by_retry_event_id",
    ):
        assert col in lookup_columns
        assert lookup[col] is None, f"pre-existing row must read back {col} as NULL, not fabricated"

    event = conn.execute("SELECT * FROM paper_external_order_events WHERE external_order_event_id = 'evt-1'").fetchone()
    assert event["new_state"] == "PREVIEWED"
    assert "scope_sequence" in event.keys()
    assert event["scope_sequence"] is None

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert "paper_external_position_reservation_events" in tables
    assert "paper_external_order_leases" in tables

    # scope_sequence uniqueness index applies to new rows (NULLs from legacy
    # rows are not compared for uniqueness by SQLite, so this is safe).
    conn.execute(
        "INSERT INTO paper_external_order_events "
        "(external_order_event_id, external_order_scope_id, book_id, paper_order_intent_id, client_order_id, "
        " account_fingerprint, previous_state, new_state, payload_hash, quantity, limit_price, operator, reason, "
        " created_at, policy_version, config_hash, scope_sequence) "
        "VALUES ('evt-2', 'scope-1', 'BASELINE', 'intent-1', 'client-1', 'fp-1', 'PREVIEWED', 'SUBMITTED', "
        " 'hash-2', '10', '5.00', 'op', 'submit', '2026-01-02T00:01:00Z', 'v1', 'cfg', 1)"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO paper_external_order_events "
            "(external_order_event_id, external_order_scope_id, book_id, paper_order_intent_id, client_order_id, "
            " account_fingerprint, previous_state, new_state, payload_hash, quantity, limit_price, operator, reason, "
            " created_at, policy_version, config_hash, scope_sequence) "
            "VALUES ('evt-3', 'scope-1', 'BASELINE', 'intent-1', 'client-1', 'fp-1', 'SUBMITTED', 'FILLED', "
            " 'hash-3', '10', '5.00', 'op', 'dup', '2026-01-02T00:02:00Z', 'v1', 'cfg', 1)"
        )
    conn.rollback()
    conn.close()


# ---------------------------------------------------------------------------
# Fixture 3: Milestone-11.1 (has leases table with no `generation` column,
# and the interim lookup trigger — the same body Milestone 11.2 Part 3/18
# already exercises for the trigger alone; this fixture additionally checks
# the lease-generation column upgrade and reservation-event preservation).
# ---------------------------------------------------------------------------

@pytest.fixture
def milestone_11_1_db(tmp_path):
    schema = _load_historical_schema("milestone_11_1_schema.py")
    assert "generation" not in schema["PAPER_BOOKS_DDL"].split("paper_external_order_leases")[1].split(");")[0], (
        "fixture sanity: Milestone-11.1 lease table must predate the 11.2 generation column"
    )
    db_path = _build_prior_db(tmp_path, schema, name="m11_1.sqlite3")
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO paper_external_order_leases "
        "(lease_key, book_id, client_order_id, owner_id, operation, acquired_at, heartbeat_at, expires_at, status) "
        "VALUES ('lk-1', 'BASELINE', 'client-1', 'owner-1', 'SUBMIT', '2026-01-02T00:00:00Z', "
        "'2026-01-02T00:00:00Z', '2026-01-02T00:00:30Z', 'ACTIVE')"
    )
    conn.execute(
        "INSERT INTO paper_external_position_reservation_events "
        "(reservation_event_id, book_id, symbol, paper_order_intent_id, client_order_id, quantity, event_type, "
        " created_at) "
        "VALUES ('res-1', 'BASELINE', 'AAPL', 'intent-1', 'client-1', '10', 'RESERVE', '2026-01-02T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO paper_external_order_lookups "
        "(lookup_id, book_id, paper_order_intent_id, client_order_id, account_fingerprint, result, authoritative, "
        " created_at) "
        "VALUES ('lk-2', 'BASELINE', 'intent-1', 'client-1', 'fp-1', 'NOT_FOUND', 1, '2026-01-02T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    return db_path


def test_milestone_11_1_upgrade_adds_lease_generation_and_preserves_reservation_evidence(milestone_11_1_db):
    conn = connect(milestone_11_1_db)

    lease = conn.execute("SELECT * FROM paper_external_order_leases WHERE lease_key = 'lk-1'").fetchone()
    assert lease["owner_id"] == "owner-1"
    assert lease["generation"] == 1, "pre-11.2 lease row must backfill generation to the DEFAULT 1, not NULL"

    reservation = conn.execute(
        "SELECT * FROM paper_external_position_reservation_events WHERE reservation_event_id = 'res-1'"
    ).fetchone()
    assert reservation["quantity"] == "10"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE paper_external_position_reservation_events SET quantity = '999' WHERE reservation_event_id = 'res-1'"
        )
    conn.rollback()

    # The upgraded (v2) lookup trigger contract now applies even though this
    # row was inserted under the old (v1, interim) trigger body.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE paper_external_order_lookups SET result = 'FOUND' WHERE lookup_id = 'lk-2'")
    conn.rollback()
    conn.execute(
        "UPDATE paper_external_order_lookups SET consumed_by_retry_event_id = 'evt-x' WHERE lookup_id = 'lk-2'"
    )
    conn.commit()

    version = conn.execute(
        "SELECT version FROM paper_books_trigger_versions WHERE trigger_name = 'trg_paper_external_lookups_no_update'"
    ).fetchone()[0]
    assert version == 2
    conn.close()


def test_milestone_11_1_upgrade_activation_review_attempt_reference_preserved(milestone_11_1_db):
    """`paper_soak_activation_reviews.campaign_attempt_id` already existed in
    the Milestone-11.1 schema (added in Milestone 9.3.1); confirm an
    activation-review row referencing an attempt survives the upgrade
    unchanged and the column upgrade path is a no-op for a column that
    already existed."""
    conn = sqlite3.connect(str(milestone_11_1_db))
    conn.execute(
        "INSERT INTO paper_soak_activation_reviews "
        "(activation_review_id, campaign_id, campaign_attempt_id, campaign_manifest_hash, completed_market_days, "
        " completed_cycles, provider_provenance_counts_json, provider_success_counts_json, "
        " cross_book_verification_history_json, reconciliation_history_json, valuation_history_json, "
        " alert_summary_json, pause_and_kill_summary_json, performance_metrics_json, promotion_evidence_status, "
        " controlled_readiness_history_json, final_recommendation, policy_version, created_at) "
        "VALUES ('rev-1', 'camp-1', 'attempt-1', 'manifest-1', 5, 5, '{}', '{}', '[]', '[]', '[]', '{}', '{}', '{}', "
        " 'PENDING', '[]', 'HOLD', 'v1', '2026-01-02T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    conn = connect(milestone_11_1_db)
    row = conn.execute(
        "SELECT campaign_attempt_id FROM paper_soak_activation_reviews WHERE activation_review_id = 'rev-1'"
    ).fetchone()
    assert row["campaign_attempt_id"] == "attempt-1"
    conn.close()
