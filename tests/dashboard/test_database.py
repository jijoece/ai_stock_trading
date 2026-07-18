from pathlib import Path
import sqlite3

import pytest

from dashboard.services.database import (
    DATABASE_PATH_ENV,
    DatabaseConfigurationError,
    DatabaseUnavailableError,
    connect_read_only,
)


def test_missing_configuration_is_friendly_and_sanitized(monkeypatch):
    monkeypatch.delenv(DATABASE_PATH_ENV, raising=False)

    with pytest.raises(DatabaseConfigurationError, match=DATABASE_PATH_ENV):
        with connect_read_only():
            pass


def test_missing_database_is_not_created(tmp_path: Path):
    missing = tmp_path / "secret" / "missing.sqlite3"

    with pytest.raises(DatabaseUnavailableError) as error:
        with connect_read_only(missing):
            pass

    assert str(missing) not in str(error.value)
    assert not missing.exists()


def test_connection_uses_rows_and_rejects_writes(dashboard_database: Path):
    with connect_read_only(dashboard_database) as connection:
        row = connection.execute("SELECT cycle_id FROM research_cycles LIMIT 1").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["cycle_id"] == "cycle-1"
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO research_cycles (cycle_id) VALUES ('forbidden')")
