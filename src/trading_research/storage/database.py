"""SQLite connection management."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .corporate_status_schema import apply_corporate_status_schema
from .evaluation_schema import apply_evaluation_schema
from .evidence_provider_schema import apply_evidence_provider_schema
from .execution_schema import apply_execution_schema
from .migrations import apply_schema
from .paper_books_schema import apply_paper_books_schema
from .research_cycle_schema import apply_research_cycle_schema
from .research_schema import apply_research_schema
from .schema_version import apply_pending_schema_migrations, check_schema_not_forward_versioned
from .shadow_alerts_schema import apply_shadow_alerts_schema
from .shadow_operations_schema import apply_shadow_operations_schema
from .trading_schema import apply_trading_schema
from .transactions import TransactionAlreadyActiveError, begin_immediate, transaction

__all__ = [
    "SQLITE_BUSY_TIMEOUT_MS", "TransactionAlreadyActiveError", "begin_immediate", "transaction",
    "connect", "session",
]

SQLITE_BUSY_TIMEOUT_MS = 5_000


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    # Milestone 11.3.1 Item 2: explicit transaction control only. True SQLite
    # autocommit — no statement outside an explicit BEGIN/BEGIN IMMEDIATE
    # ever silently opens an implicit transaction — so `begin_immediate`
    # (storage/transactions.py) can trust `conn.in_transaction` as "a
    # caller-owned transaction is genuinely still open" and never has to
    # guess whether it is safe to discard.
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    # busy_timeout must be set before any pragma/statement that can contend
    # for the database's write lock (journal_mode included) -- another
    # connection can legitimately be mid-BEGIN-IMMEDIATE against this same
    # file while this one is still connecting, and without a timeout in
    # place yet that contention raises "database is locked" immediately
    # instead of waiting.
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL permits readers during a campaign date write. NORMAL is the
    # conservative SQLite-recommended WAL tradeoff: atomic/consistent after
    # application or OS crashes, without FULL's fsync cost on every commit.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    check_schema_not_forward_versioned(conn)
    apply_schema(conn)
    apply_trading_schema(conn)
    apply_execution_schema(conn)
    apply_evaluation_schema(conn)
    apply_research_schema(conn)
    apply_evidence_provider_schema(conn)
    apply_research_cycle_schema(conn)
    apply_corporate_status_schema(conn)
    apply_shadow_operations_schema(conn)
    apply_shadow_alerts_schema(conn)
    apply_paper_books_schema(conn)
    apply_pending_schema_migrations(conn)
    return conn


@contextmanager
def session(db_path: str | Path):
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
