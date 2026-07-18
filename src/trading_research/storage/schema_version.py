"""General database schema-version tracking (Milestone 11.3 Part 34).

Distinct from `paper_books_schema.py::paper_books_trigger_versions`, which
is scoped narrowly to individual trigger *definitions*. This module tracks
one monotonically increasing version number for the database as a whole.

Every existing `apply_*_schema` function in `storage/` is already an
idempotent, additive `CREATE ... IF NOT EXISTS` script re-run on every
`connect()` — this module does not replace that pattern (the spec
explicitly says not to). It adds a persisted checkpoint on top of it so:

* a database written by a *newer* version of this code than the currently
  running code understands is refused rather than silently opened
  (`SchemaVersionError`, "forward version fails safely");
* any future migration that truly needs one-time, non-idempotent work
  (a data backfill, not just additive DDL) has an explicit, ordered,
  version-gated place to run it exactly once, inside its own transaction.

`CURRENT_SCHEMA_VERSION` is bumped whenever a new entry is added to
`_MIGRATIONS`. Migration 1 is a baseline marker: by the time this module
was introduced, every schema change through Milestone 11.2 had already
been folded into the various `apply_*_schema` scripts as additive DDL, so
there is nothing further for it to *do* beyond recording that baseline.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from .transactions import transaction

logger = logging.getLogger(__name__)


class SchemaVersionError(RuntimeError):
    """Raised when the database's persisted schema_version is newer than
    this running code's CURRENT_SCHEMA_VERSION — i.e. this connection would
    be a downgrade. Never auto-resolved; the caller must not proceed."""


_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def _migration_1_baseline(conn: sqlite3.Connection) -> None:
    """No-op: the schema this version number describes was already applied
    by the existing `apply_*_schema` calls in `database.py::connect()`
    before `ensure_schema_version` runs. This function exists so the
    ordered-migration mechanism has a real, idempotent callable to invoke
    (and test) rather than a bare version number with no associated code."""
    del conn


def _migration_2_backfill_execution_namespace_claims(conn: sqlite3.Connection) -> None:
    """Milestone 11.3.1 Item 6: `paper_order_execution_claims` did not exist
    before this migration, so every pre-existing `paper_book_orders` row has
    no claim row. Backfill one claim per legacy intent, inferring the
    namespace from the same evidence `has_external_execution_evidence` uses
    (any external preview/event/fill/queue row) -- otherwise LOCAL_SIMULATED,
    since every intent through Milestone 11.3 defaulted to the local
    simulator unless it had external evidence. Idempotent: `INSERT ... WHERE
    NOT EXISTS` skips any row a previous run (or the ordinary claim path)
    already created."""
    intent_ids = conn.execute("SELECT book_id, paper_order_intent_id FROM paper_book_orders").fetchall()
    now = datetime.now(timezone.utc).isoformat()
    for book_id, paper_order_intent_id in intent_ids:
        external_evidence = False
        for table in (
            "paper_external_order_previews", "paper_external_order_events",
            "paper_external_broker_fills", "paper_external_submission_queue",
        ):
            row = conn.execute(
                f"SELECT 1 FROM {table} WHERE book_id = ? AND paper_order_intent_id = ? LIMIT 1",
                (book_id, paper_order_intent_id),
            ).fetchone()
            if row is not None:
                external_evidence = True
                break
        namespace = "EXTERNAL_PAPER" if external_evidence else "LOCAL_SIMULATED"
        conn.execute(
            "INSERT INTO paper_order_execution_claims "
            "(book_id, paper_order_intent_id, execution_namespace, claim_generation, claimed_at, claimed_by) "
            "SELECT ?, ?, ?, 1, ?, 'schema_migration_2_backfill' "
            "WHERE NOT EXISTS (SELECT 1 FROM paper_order_execution_claims WHERE book_id = ? AND paper_order_intent_id = ?)",
            (book_id, paper_order_intent_id, namespace, now, book_id, paper_order_intent_id),
        )


def _migration_3_claude_code_usage_provenance(conn: sqlite3.Connection) -> None:
    """Add honest subscription API-equivalent estimate/provenance fields."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(research_attempts)").fetchall()}
    additions = (
        ("cost_estimate_basis", "TEXT NOT NULL DEFAULT 'NOT_APPLICABLE'"),
        ("configured_model_alias", "TEXT"),
        ("resolved_model_name", "TEXT"),
        ("claude_code_version", "TEXT"),
    )
    for column_name, column_type in additions:
        if column_name not in existing:
            conn.execute(f"ALTER TABLE research_attempts ADD COLUMN {column_name} {column_type}")


def _migration_4_codex_provider_cli_version(conn: sqlite3.Connection) -> None:
    """Add the generic `provider_cli_version` provenance column (Milestone
    12: Codex provider). Deliberately additive, not a rename of the existing
    `claude_code_version` column — every pre-existing Claude Code row stays
    readable exactly as written, and `claude_code_version` keeps meaning
    what it always meant. New Codex attempts populate `provider_cli_version`
    instead; Claude Code attempts continue to populate `claude_code_version`
    only."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(research_attempts)").fetchall()}
    if "provider_cli_version" not in existing:
        conn.execute("ALTER TABLE research_attempts ADD COLUMN provider_cli_version TEXT")


def _migration_6_attempt_structured_failure_fields(conn: sqlite3.Connection) -> None:
    """Milestone 12.1 Item 1: typed provider-failure taxonomy propagated onto
    `research_attempts` itself, so operational health decisions (Item 1's
    `IMMEDIATE_PROVIDER_PAUSE_CODES` allowlist in `shadow/attempt_controller.py`)
    can act on a stable code instead of scanning `failure_reason` free text.

    Every pre-existing attempt row becomes NULL/unknown for these four columns
    — never backfilled by guessing a code from historical `failure_reason`
    text, which would be exactly the free-text-classification anti-pattern
    this milestone removes."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(research_attempts)").fetchall()}
    additions = (
        ("failure_code", "TEXT"),
        ("failure_stage", "TEXT"),
        ("failure_retryable", "INTEGER"),
        ("failure_metadata_json", "TEXT"),
    )
    for column_name, column_type in additions:
        if column_name not in existing:
            conn.execute(f"ALTER TABLE research_attempts ADD COLUMN {column_name} {column_type}")


def _migration_7_codex_adapter_version(conn: sqlite3.Connection) -> None:
    """Milestone 12.1 Item 2: persist the tested JSONL-contract adapter
    version (`codex_version_policy.VersionRange.adapter_version`) a Codex
    attempt's installed CLI version was matched to, distinct from the raw
    `provider_cli_version` string. Pre-existing rows stay NULL."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(research_attempts)").fetchall()}
    if "provider_adapter_version" not in existing:
        conn.execute("ALTER TABLE research_attempts ADD COLUMN provider_adapter_version TEXT")


def _migration_8_reasoning_token_accounting(conn: sqlite3.Connection) -> None:
    """Milestone 12.1 Item 4: persist reasoning-token usage and the explicit
    accounting policy it was computed under. Pre-existing rows get
    `token_accounting_policy='NOT_APPLICABLE'` (they predate reasoning-token
    reporting entirely) and a NULL `reasoning_output_tokens` — never
    backfilled by guessing."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(research_attempts)").fetchall()}
    additions = (
        ("reasoning_output_tokens", "INTEGER"),
        ("token_accounting_policy", "TEXT NOT NULL DEFAULT 'NOT_APPLICABLE'"),
    )
    for column_name, column_type in additions:
        if column_name not in existing:
            conn.execute(f"ALTER TABLE research_attempts ADD COLUMN {column_name} {column_type}")


def _migration_9_scheduled_run_telemetry_index(conn: sqlite3.Connection) -> None:
    """Milestone 12.1 Item 7: supports the exact
    `evidence_providers/persistence.py::list_provider_requests_for_scheduled_run`
    query (`correlation_mode='SCHEDULED' AND research_cycle_id=? AND
    scheduler_run_id=?`) without a full-table scan."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_evidence_provider_requests_scheduled_run "
        "ON evidence_provider_requests(correlation_mode, research_cycle_id, scheduler_run_id, created_at, request_id)"
    )


def _migration_5_operational_integrity_telemetry(conn: sqlite3.Connection) -> None:
    """Milestone 11.3.2: exact request ownership and bounded transport taxonomy.

    Existing rows deliberately remain `LEGACY_MANUAL` with nullable ownership;
    they can be inspected but can never be attributed to a new cycle.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(evidence_provider_requests)").fetchall()}
    additions = (
        ("correlation_mode", "TEXT NOT NULL DEFAULT 'LEGACY_MANUAL'"),
        ("research_cycle_id", "TEXT"),
        ("scheduler_run_id", "TEXT"),
        ("research_run_id", "TEXT"),
        ("symbol_attempt_id", "TEXT"),
        ("provider_request_group_id", "TEXT"),
        ("transport_failure_category", "TEXT NOT NULL DEFAULT 'NONE'"),
    )
    for column_name, column_type in additions:
        if column_name not in existing:
            conn.execute(f"ALTER TABLE evidence_provider_requests ADD COLUMN {column_name} {column_type}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_evidence_provider_requests_cycle "
        "ON evidence_provider_requests(research_cycle_id, created_at, request_id)"
    )


# Ordered, idempotent migrations. Each callable must be safe to invoke more
# than once (defense in depth on top of the version gate, which normally
# prevents re-invocation) and must not raise on a database that already has
# the effect it describes.
_MIGRATIONS: dict[int, tuple[str, Callable[[sqlite3.Connection], None]]] = {
    1: ("baseline: schema_version tracking introduced (Milestone 11.3 Part 34)", _migration_1_baseline),
    2: (
        "backfill paper_order_execution_claims for pre-existing intents (Milestone 11.3.1 Item 6)",
        _migration_2_backfill_execution_namespace_claims,
    ),
    3: (
        "add Claude Code usage estimate basis and provider provenance",
        _migration_3_claude_code_usage_provenance,
    ),
    4: (
        "add generic provider_cli_version column for the Codex provider (Milestone 12)",
        _migration_4_codex_provider_cli_version,
    ),
    5: (
        "add provider-request cycle correlation and transport taxonomy (Milestone 11.3.2)",
        _migration_5_operational_integrity_telemetry,
    ),
    6: (
        "add structured typed failure fields to research_attempts (Milestone 12.1 Item 1)",
        _migration_6_attempt_structured_failure_fields,
    ),
    7: (
        "add Codex JSONL adapter version provenance column (Milestone 12.1 Item 2)",
        _migration_7_codex_adapter_version,
    ),
    8: (
        "add reasoning-token accounting columns to research_attempts (Milestone 12.1 Item 4)",
        _migration_8_reasoning_token_accounting,
    ),
    9: (
        "add scheduled-run-scoped provider telemetry index (Milestone 12.1 Item 7)",
        _migration_9_scheduled_run_telemetry_index,
    ),
}

CURRENT_SCHEMA_VERSION = max(_MIGRATIONS)


def check_schema_not_forward_versioned(conn: sqlite3.Connection) -> None:
    """Create the version table if absent, and fail closed if the database
    already carries a version newer than this code supports. Must be called
    before any other schema-mutating code touches the connection."""
    conn.execute(_SCHEMA_VERSION_DDL)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    stored = row[0] if row else None
    if stored is not None and stored > CURRENT_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema_version={stored} is newer than this code's "
            f"CURRENT_SCHEMA_VERSION={CURRENT_SCHEMA_VERSION}; refusing to open "
            "a database written by newer code to avoid silent data misinterpretation"
        )


def apply_pending_schema_migrations(conn: sqlite3.Connection) -> int:
    """Run any migrations between the stored version (exclusive) and
    CURRENT_SCHEMA_VERSION (inclusive), each in its own protected
    transaction, in ascending order. Returns the resulting version.

    Must be called after `check_schema_not_forward_versioned` and after the
    existing `apply_*_schema` additive DDL has run, so a fresh database
    always starts from the full current table/column/trigger set before any
    migration-specific logic executes."""
    # Determine pending work by the actual version ledger, not only MAX.
    # This remains correct if an interrupted/manual legacy database has a
    # gap (for example version 3 present but version 2 absent).
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_version").fetchall()}
    pending = sorted(v for v in _MIGRATIONS if v not in applied)
    for version in pending:
        description, migration_fn = _MIGRATIONS[version]
        # Milestone 11.3.1 Item 2: use the shared transaction-ownership
        # context manager instead of a bare `BEGIN IMMEDIATE` guarded by an
        # unconditional `conn.rollback()` — this connection is opened with
        # `isolation_level=None` (database.py::connect()), so an already-open
        # transaction here would be a real caller-owned one, never a stray
        # implicit one safe to discard.
        with transaction(conn):
            migration_fn(conn)
            conn.execute(
                "INSERT INTO schema_version (version, description, applied_at) VALUES (?, ?, ?)",
                (version, description, datetime.now(timezone.utc).isoformat()),
            )
        # Log only the version number and static migration description —
        # never row contents, credentials, or any other persisted data.
        logger.info("schema_version migration applied: version=%s description=%s", version, description)
    final_row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return final_row[0] if final_row and final_row[0] is not None else CURRENT_SCHEMA_VERSION
