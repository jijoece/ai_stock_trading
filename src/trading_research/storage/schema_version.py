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


# Ordered, idempotent migrations. Each callable must be safe to invoke more
# than once (defense in depth on top of the version gate, which normally
# prevents re-invocation) and must not raise on a database that already has
# the effect it describes.
_MIGRATIONS: dict[int, tuple[str, Callable[[sqlite3.Connection], None]]] = {
    1: ("baseline: schema_version tracking introduced (Milestone 11.3 Part 34)", _migration_1_baseline),
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
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    stored = row[0] if row else None
    pending = sorted(v for v in _MIGRATIONS if v > (stored or 0))
    for version in pending:
        description, migration_fn = _MIGRATIONS[version]
        if conn.in_transaction:
            conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        try:
            migration_fn(conn)
            conn.execute(
                "INSERT INTO schema_version (version, description, applied_at) VALUES (?, ?, ?)",
                (version, description, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        # Log only the version number and static migration description —
        # never row contents, credentials, or any other persisted data.
        logger.info("schema_version migration applied: version=%s description=%s", version, description)
    final_row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return final_row[0] if final_row and final_row[0] is not None else CURRENT_SCHEMA_VERSION
