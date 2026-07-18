"""Migration tests for Milestone 12.1 Item 1: `research_attempts` gains
`failure_code`/`failure_stage`/`failure_retryable`/`failure_metadata_json`.
Required tests #9 ("legacy attempt rows load safely with null structured
fields") and #10 ("migration preserves existing attempts")."""
from __future__ import annotations

import sqlite3

from trading_research.storage.database import connect
from trading_research.storage.research_schema import RESEARCH_DDL
from trading_research.storage.schema_version import CURRENT_SCHEMA_VERSION


def _pre_migration_6_db(db_path) -> None:
    """Builds a database shaped like it was written by pre-Item-1 code: the
    full research schema DDL applies (it always defines the four new columns
    at `CREATE TABLE IF NOT EXISTS` time now), but we simulate an *older*
    on-disk table — created without those columns — by hand, then insert a
    legacy attempt row exactly as pre-Item-1 code would have."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE research_committee_runs (
            research_run_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, provider TEXT NOT NULL,
            model_name TEXT NOT NULL, roles_json TEXT NOT NULL, run_mode TEXT NOT NULL,
            status TEXT NOT NULL, config_hash TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT
        );
        CREATE TABLE research_attempts (
            attempt_id TEXT PRIMARY KEY, research_run_id TEXT NOT NULL, role TEXT NOT NULL,
            attempt_number INTEGER NOT NULL, prompt_name TEXT NOT NULL, prompt_version TEXT NOT NULL,
            prompt_hash TEXT NOT NULL, system_prompt_hash TEXT NOT NULL, schema_version TEXT NOT NULL,
            provider TEXT NOT NULL, model_name TEXT NOT NULL, success INTEGER NOT NULL,
            failure_reason TEXT, raw_response_json TEXT, validated_payload_json TEXT,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            latency_ms INTEGER, provider_request_id TEXT, retry_count INTEGER NOT NULL,
            pricing_version TEXT, estimated_cost TEXT, cost_status TEXT NOT NULL,
            cost_estimate_basis TEXT NOT NULL DEFAULT 'NOT_APPLICABLE', configured_model_alias TEXT,
            resolved_model_name TEXT, claude_code_version TEXT, provider_cli_version TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO research_committee_runs "
        "(research_run_id, snapshot_id, provider, model_name, roles_json, run_mode, status, config_hash, created_at) "
        "VALUES ('run-legacy', 'snap-1', 'codex', 'gpt-5.1-codex', '[]', 'MANUAL', 'COMPLETED', 'hash', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO research_attempts "
        "(attempt_id, research_run_id, role, attempt_number, prompt_name, prompt_version, prompt_hash, "
        "system_prompt_hash, schema_version, provider, model_name, success, failure_reason, retry_count, cost_status, created_at) "
        "VALUES ('run-legacy-fundamental-1', 'run-legacy', 'fundamental', 1, 'p', 'v1', 'h1', 'sph1', 's1', "
        "'codex', 'gpt-5.1-codex', 0, 'Codex is not authenticated with ChatGPT', 0, 'NOT_APPLICABLE', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def test_legacy_attempt_rows_load_safely_with_null_structured_fields(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    _pre_migration_6_db(db_path)

    conn = connect(db_path)  # runs the full additive DDL + pending migrations
    row = conn.execute(
        "SELECT failure_code, failure_stage, failure_retryable, failure_metadata_json, failure_reason "
        "FROM research_attempts WHERE attempt_id = 'run-legacy-fundamental-1'"
    ).fetchone()
    assert row is not None
    assert row["failure_code"] is None
    assert row["failure_stage"] is None
    assert row["failure_retryable"] is None
    assert row["failure_metadata_json"] is None
    # The original free-text reason is preserved verbatim — migration never
    # invents a structured code by re-parsing it.
    assert row["failure_reason"] == "Codex is not authenticated with ChatGPT"
    conn.close()


def test_migration_preserves_existing_attempt_row_count(tmp_path):
    db_path = tmp_path / "legacy_count.sqlite3"
    _pre_migration_6_db(db_path)

    conn = connect(db_path)
    count = conn.execute("SELECT COUNT(*) AS c FROM research_attempts").fetchone()["c"]
    assert count == 1
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == CURRENT_SCHEMA_VERSION
    conn.close()


def test_fresh_database_has_structured_failure_columns_from_ddl(tmp_path):
    conn = connect(tmp_path / "fresh.sqlite3")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(research_attempts)").fetchall()}
    assert {"failure_code", "failure_stage", "failure_retryable", "failure_metadata_json"} <= columns
    conn.close()


def test_ddl_defines_structured_failure_columns():
    assert "failure_code" in RESEARCH_DDL
    assert "failure_metadata_json" in RESEARCH_DDL


def test_legacy_database_gains_reasoning_token_columns(tmp_path):
    """Milestone 12.1 Item 4, required test #8: legacy usage rows load
    safely — a pre-existing row gets NULL reasoning_output_tokens and the
    NOT_APPLICABLE default policy, never a fabricated value."""
    db_path = tmp_path / "legacy_reasoning.sqlite3"
    _pre_migration_6_db(db_path)

    conn = connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(research_attempts)").fetchall()}
    assert {"reasoning_output_tokens", "token_accounting_policy"} <= columns
    row = conn.execute(
        "SELECT reasoning_output_tokens, token_accounting_policy FROM research_attempts "
        "WHERE attempt_id = 'run-legacy-fundamental-1'"
    ).fetchone()
    assert row["reasoning_output_tokens"] is None
    assert row["token_accounting_policy"] == "NOT_APPLICABLE"
    conn.close()


def test_legacy_database_gains_provider_adapter_version_column(tmp_path):
    """Milestone 12.1 Item 2: migration 7 adds `provider_adapter_version`
    to a pre-existing `research_attempts` table without touching row data."""
    db_path = tmp_path / "legacy_adapter.sqlite3"
    _pre_migration_6_db(db_path)

    conn = connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(research_attempts)").fetchall()}
    assert "provider_adapter_version" in columns
    row = conn.execute(
        "SELECT provider_adapter_version FROM research_attempts WHERE attempt_id = 'run-legacy-fundamental-1'"
    ).fetchone()
    assert row["provider_adapter_version"] is None
    conn.close()
