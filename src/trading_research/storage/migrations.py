"""SQLite schema for research-run/batch/result state.

Applied idempotently via `CREATE TABLE IF NOT EXISTS` — no migration
framework needed at this scale (single-file local SQLite DB).
"""
from __future__ import annotations

SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS research_runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created'
);

CREATE TABLE IF NOT EXISTS capability_inventories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    inventory_timestamp TEXT NOT NULL,
    inventory_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_records (
    dedup_key TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    record_json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    injection_risk TEXT NOT NULL DEFAULT 'none'
);

CREATE TABLE IF NOT EXISTS batch_jobs (
    batch_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES research_runs(run_id),
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    request_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS batch_requests (
    custom_id TEXT PRIMARY KEY,
    batch_id TEXT REFERENCES batch_jobs(batch_id),
    run_id TEXT NOT NULL,
    workstream_id TEXT NOT NULL,
    submission_time TEXT NOT NULL,
    completion_time TEXT,
    status TEXT NOT NULL DEFAULT 'submitted',
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    input_hash TEXT NOT NULL,
    output_hash TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    validation_status TEXT NOT NULL DEFAULT 'pending',
    error_details TEXT,
    source_set_hash TEXT NOT NULL,
    prompt_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_results (
    custom_id TEXT PRIMARY KEY REFERENCES batch_requests(custom_id),
    raw_json TEXT,
    validated_json TEXT,
    validation_status TEXT NOT NULL DEFAULT 'pending',
    downloaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workstream_id TEXT NOT NULL,
    claim TEXT NOT NULL,
    confidence TEXT NOT NULL,
    source_ids_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workstream_id TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    confidence TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    position_a TEXT NOT NULL,
    position_b TEXT NOT NULL,
    workstream_a TEXT,
    workstream_b TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    custom_id TEXT,
    occurred_at TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generated_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _table_columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def rename_legacy_research_recommendations(conn) -> None:
    """One-time rename: `recommendations` used to hold research-run rows.

    That name now belongs to the trading schema (rec_id PK, frozen rows —
    architecture §17); the research-run table is `research_recommendations`.
    Databases created before the rename get migrated here. Distinguished by
    shape: the legacy research table has `workstream_id`, never `rec_id`.
    """
    cols = _table_columns(conn, "recommendations")
    if not cols or "rec_id" in cols or "workstream_id" not in cols:
        return  # absent, or already the trading-schema table
    if _table_columns(conn, "research_recommendations"):
        raise RuntimeError(
            "both `recommendations` (legacy research shape) and "
            "`research_recommendations` exist — manual reconciliation required"
        )
    conn.execute("ALTER TABLE recommendations RENAME TO research_recommendations")
    conn.commit()


def apply_schema(conn) -> None:
    rename_legacy_research_recommendations(conn)
    conn.executescript(DDL)
    conn.commit()
