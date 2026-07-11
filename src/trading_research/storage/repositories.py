"""Thin repository functions over the SQLite schema in migrations.py.

Kept as plain functions (not a full Repository/UnitOfWork abstraction) —
the schema is small and every caller already holds a sqlite3.Connection.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_run(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO research_runs (run_id, created_at, status) VALUES (?, ?, ?)",
        (run_id, _now(), "created"),
    )
    conn.commit()


def update_run_status(conn: sqlite3.Connection, run_id: str, status: str) -> None:
    conn.execute("UPDATE research_runs SET status = ? WHERE run_id = ?", (status, run_id))
    conn.commit()


def save_capability_inventory(conn: sqlite3.Connection, server_name: str, timestamp: str, inventory_json: dict) -> None:
    conn.execute(
        "INSERT INTO capability_inventories (server_name, inventory_timestamp, inventory_json) VALUES (?, ?, ?)",
        (server_name, timestamp, json.dumps(inventory_json)),
    )
    conn.commit()


def upsert_source_record(conn: sqlite3.Connection, dedup_key: str, source_type: str, record: dict, injection_risk: str) -> bool:
    """Returns True if this was a new record, False if it already existed."""
    cur = conn.execute("SELECT 1 FROM source_records WHERE dedup_key = ?", (dedup_key,))
    exists = cur.fetchone() is not None
    conn.execute(
        "INSERT OR REPLACE INTO source_records (dedup_key, source_type, record_json, retrieved_at, injection_risk) "
        "VALUES (?, ?, ?, ?, ?)",
        (dedup_key, source_type, json.dumps(record), _now(), injection_risk),
    )
    conn.commit()
    return not exists


def save_batch_job(conn: sqlite3.Connection, batch_id: str, run_id: str, model: str, status: str, request_count: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO batch_jobs (batch_id, run_id, model, status, created_at, request_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (batch_id, run_id, model, status, _now(), request_count),
    )
    conn.commit()


def save_batch_request(conn: sqlite3.Connection, **fields) -> None:
    columns = [
        "custom_id", "batch_id", "run_id", "workstream_id", "submission_time",
        "status", "model", "input_hash", "source_set_hash", "prompt_hash",
    ]
    values = [fields.get(c) for c in columns]
    if "submission_time" not in fields:
        values[columns.index("submission_time")] = _now()
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT OR REPLACE INTO batch_requests ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()


def mark_batch_request_result(
    conn: sqlite3.Connection,
    custom_id: str,
    status: str,
    validation_status: str,
    output_hash: str | None = None,
    error_details: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    conn.execute(
        "UPDATE batch_requests SET status = ?, validation_status = ?, output_hash = ?, "
        "error_details = ?, completion_time = ?, input_tokens = ?, output_tokens = ? "
        "WHERE custom_id = ?",
        (status, validation_status, output_hash, error_details, _now(), input_tokens, output_tokens, custom_id),
    )
    conn.commit()


def increment_retry_count(conn: sqlite3.Connection, custom_id: str) -> None:
    conn.execute(
        "UPDATE batch_requests SET retry_count = retry_count + 1 WHERE custom_id = ?", (custom_id,)
    )
    conn.commit()


def save_batch_result(conn: sqlite3.Connection, custom_id: str, raw_json: str | None, validated_json: str | None, validation_status: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO batch_results (custom_id, raw_json, validated_json, validation_status, downloaded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (custom_id, raw_json, validated_json, validation_status, _now()),
    )
    conn.commit()


def log_error(conn: sqlite3.Connection, run_id: str | None, custom_id: str | None, error_type: str, message: str) -> None:
    conn.execute(
        "INSERT INTO errors (run_id, custom_id, occurred_at, error_type, message) VALUES (?, ?, ?, ?, ?)",
        (run_id, custom_id, _now(), error_type, message),
    )
    conn.commit()


def record_artifact(conn: sqlite3.Connection, run_id: str, path: str, artifact_type: str) -> None:
    conn.execute(
        "INSERT INTO generated_artifacts (run_id, artifact_path, artifact_type, created_at) VALUES (?, ?, ?, ?)",
        (run_id, path, artifact_type, _now()),
    )
    conn.commit()


def requests_needing_retry(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM batch_requests WHERE run_id = ? AND "
        "(status IN ('errored', 'expired', 'canceled', 'missing') OR validation_status IN ('invalid_json', 'schema_failed', 'truncated'))",
        (run_id,),
    )
    return cur.fetchall()
