"""SQLite connection management."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .evaluation_schema import apply_evaluation_schema
from .execution_schema import apply_execution_schema
from .migrations import apply_schema
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
    return conn


@contextmanager
def session(db_path: str | Path):
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
