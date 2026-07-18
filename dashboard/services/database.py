"""Strictly read-only SQLite access for the dashboard."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
from typing import Iterator


DATABASE_PATH_ENV = "AI_STOCK_TRADING_DB_PATH"


class DashboardDatabaseError(RuntimeError):
    """A sanitized dashboard database failure."""


class DatabaseConfigurationError(DashboardDatabaseError):
    """The dashboard database path was not configured."""


class DatabaseUnavailableError(DashboardDatabaseError):
    """The configured dashboard database cannot be opened read-only."""


def configured_database_path() -> Path:
    value = os.environ.get(DATABASE_PATH_ENV, "").strip()
    if not value:
        raise DatabaseConfigurationError(
            f"Set {DATABASE_PATH_ENV} to an existing research database file."
        )
    return Path(value).expanduser()


@contextmanager
def connect_read_only(database_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a short-lived SQLite connection that cannot write or create files."""
    path = Path(database_path).expanduser() if database_path is not None else configured_database_path()
    if not path.is_file():
        raise DatabaseUnavailableError("The configured dashboard database is not available.")

    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseUnavailableError(
            "The configured dashboard database could not be opened read-only."
        ) from exc

    try:
        yield connection
    finally:
        connection.close()
