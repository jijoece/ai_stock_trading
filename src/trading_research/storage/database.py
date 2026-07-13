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
from .research_cycle_schema import apply_research_cycle_schema
from .research_schema import apply_research_schema
from .shadow_alerts_schema import apply_shadow_alerts_schema
from .shadow_operations_schema import apply_shadow_operations_schema
from .trading_schema import apply_trading_schema


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
    return conn


@contextmanager
def session(db_path: str | Path):
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
