"""Milestone 11.3 Part 28: loading/validating configuration must not create
directories. Directory creation belongs to the first operation that
actually needs the directory (e.g. `storage/database.py::connect()`)."""
from __future__ import annotations

import os

from trading_research.config import load_config


def test_load_config_does_not_create_research_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "would-be-data-dir"
    db_path = data_dir / "nested" / "research.sqlite3"
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("RESEARCH_DATABASE_PATH", str(db_path))

    cfg = load_config(env_file=tmp_path / "does-not-exist.env")

    assert cfg.research_data_dir == data_dir
    assert not data_dir.exists(), "load_config must not create the research data directory"
    assert not db_path.parent.exists(), "load_config must not create the database directory"


def test_load_config_under_read_only_parent_still_succeeds(tmp_path, monkeypatch):
    """If loading config *did* try to create a directory, this would fail
    with PermissionError under a read-only parent — the fact it succeeds is
    the proof there's no filesystem mutation on the load path."""
    readonly_parent = tmp_path / "readonly"
    readonly_parent.mkdir()
    data_dir = readonly_parent / "data"
    db_path = data_dir / "research.sqlite3"
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("RESEARCH_DATABASE_PATH", str(db_path))

    original_mode = readonly_parent.stat().st_mode
    os.chmod(readonly_parent, 0o500)
    try:
        cfg = load_config(env_file=tmp_path / "does-not-exist.env")
        assert cfg.research_data_dir == data_dir
    finally:
        os.chmod(readonly_parent, original_mode)


def test_invalid_config_raises_without_creating_directories(tmp_path, monkeypatch):
    data_dir = tmp_path / "invalid-run-data-dir"
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("REDDIT_MCP_MODE", "http")
    monkeypatch.delenv("REDDIT_MCP_URL", raising=False)

    import pytest
    from trading_research.config import ConfigError
    with pytest.raises(ConfigError):
        load_config(env_file=tmp_path / "does-not-exist.env")

    assert not data_dir.exists()


def test_dry_run_load_leaves_filesystem_untouched(tmp_path, monkeypatch):
    data_dir = tmp_path / "dry-run-dir"
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(data_dir))
    before = set(tmp_path.iterdir())
    load_config(env_file=tmp_path / "does-not-exist.env")
    after = set(tmp_path.iterdir())
    assert before == after
