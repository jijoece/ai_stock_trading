"""Persistence for `shadow_alerts_schema.py`'s tables (Milestone 7,
docs/milestone-7.md Step 13/21/22/23). Raw sqlite3, no ORM — mirrors
`shadow_operations_repositories.py`'s save/load/list function-per-table
convention. Consumed by `shadow/alerts.py`, `shadow/health.py`, and
`shadow/readiness.py`.
"""
from __future__ import annotations

import sqlite3

# --- shadow_alerts -------------------------------------------------------------


def insert_alert(conn: sqlite3.Connection, alert: dict) -> None:
    conn.execute(
        "INSERT INTO shadow_alerts "
        "(alert_id, dedup_key, severity, alert_type, message, context_json, suppressed_count, created_at) "
        "VALUES (:alert_id, :dedup_key, :severity, :alert_type, :message, :context_json, :suppressed_count, :created_at)",
        alert,
    )
    conn.commit()


def increment_alert_suppressed_count(conn: sqlite3.Connection, alert_id: str) -> None:
    conn.execute(
        "UPDATE shadow_alerts SET suppressed_count = suppressed_count + 1 WHERE alert_id = ?", (alert_id,)
    )
    conn.commit()


def load_alert(conn: sqlite3.Connection, alert_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM shadow_alerts WHERE alert_id = ?", (alert_id,)).fetchone()
    return dict(row) if row is not None else None


def find_recent_alerts_by_dedup_key(
    conn: sqlite3.Connection, dedup_key: str, *, since_iso: str
) -> list[dict]:
    """Feeds `shadow/alerts.py::raise_alert`'s dedup-window lookup — recent
    alerts sharing the same `dedup_key`, newest first."""
    rows = conn.execute(
        "SELECT * FROM shadow_alerts WHERE dedup_key = ? AND created_at >= ? ORDER BY created_at DESC",
        (dedup_key, since_iso),
    ).fetchall()
    return [dict(r) for r in rows]


def list_alerts(
    conn: sqlite3.Connection, *, severity: str | None = None, alert_type: str | None = None,
    since_iso: str | None = None,
) -> list[dict]:
    clauses = []
    params: list[object] = []
    if severity is not None:
        clauses.append("severity = ?")
        params.append(severity)
    if alert_type is not None:
        clauses.append("alert_type = ?")
        params.append(alert_type)
    if since_iso is not None:
        clauses.append("created_at >= ?")
        params.append(since_iso)
    query = "SELECT * FROM shadow_alerts"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# --- shadow_alert_deliveries ----------------------------------------------------


def insert_alert_delivery(conn: sqlite3.Connection, delivery: dict) -> None:
    conn.execute(
        "INSERT INTO shadow_alert_deliveries "
        "(delivery_id, alert_id, sink_name, attempt_number, success, response_text, created_at) "
        "VALUES (:delivery_id, :alert_id, :sink_name, :attempt_number, :success, :response_text, :created_at)",
        delivery,
    )
    conn.commit()


def list_alert_deliveries(conn: sqlite3.Connection, alert_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM shadow_alert_deliveries WHERE alert_id = ? ORDER BY created_at ASC", (alert_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_failed_deliveries_for_alert(conn: sqlite3.Connection, alert_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM shadow_alert_deliveries WHERE alert_id = ? AND success = 0 ORDER BY created_at ASC",
        (alert_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- shadow_run_summaries --------------------------------------------------------


def save_run_summary(conn: sqlite3.Connection, summary: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO shadow_run_summaries "
        "(scheduler_run_id, intended_schedule_id, policy_version, health_status, health_reasons_json, "
        "provider_success_rate, evidence_completeness_rate, claude_role_success_rate, retry_rate, "
        "retry_exhaustion_rate, unsupported_claim_rate, output_truncation_rate, latency_seconds, "
        "input_tokens, output_tokens, cost_usd, paper_reconciliation_mismatch, "
        "duplicate_prevention_violation, cycle_duration_seconds, created_at) "
        "VALUES (:scheduler_run_id, :intended_schedule_id, :policy_version, :health_status, :health_reasons_json, "
        ":provider_success_rate, :evidence_completeness_rate, :claude_role_success_rate, :retry_rate, "
        ":retry_exhaustion_rate, :unsupported_claim_rate, :output_truncation_rate, :latency_seconds, "
        ":input_tokens, :output_tokens, :cost_usd, :paper_reconciliation_mismatch, "
        ":duplicate_prevention_violation, :cycle_duration_seconds, :created_at)",
        summary,
    )
    conn.commit()


def load_run_summary(conn: sqlite3.Connection, scheduler_run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM shadow_run_summaries WHERE scheduler_run_id = ?", (scheduler_run_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def list_run_summaries(conn: sqlite3.Connection, *, since_iso: str | None = None) -> list[dict]:
    if since_iso is not None:
        rows = conn.execute(
            "SELECT * FROM shadow_run_summaries WHERE created_at >= ? ORDER BY created_at DESC", (since_iso,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM shadow_run_summaries ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


# --- shadow_run_health_checks (Milestone 7.2, Part 3) ---------------------------


def save_health_check(conn: sqlite3.Connection, check: dict) -> None:
    """Idempotent on `check_id` (deterministic — see
    `shadow/scheduler.py::_compute_health_check_id`): `INSERT OR IGNORE` so
    re-evaluating/resuming the same scheduler run never creates a duplicate
    diagnostic row."""
    conn.execute(
        "INSERT OR IGNORE INTO shadow_run_health_checks "
        "(check_id, scheduler_run_id, cycle_id, check_name, check_status, input_value, input_unit, "
        "threshold_value, threshold_unit, comparison, applicable, pause_flag_enabled, reason, policy_version, "
        "evaluated_at) "
        "VALUES (:check_id, :scheduler_run_id, :cycle_id, :check_name, :check_status, :input_value, :input_unit, "
        ":threshold_value, :threshold_unit, :comparison, :applicable, :pause_flag_enabled, :reason, "
        ":policy_version, :evaluated_at)",
        check,
    )
    conn.commit()


def list_health_checks(
    conn: sqlite3.Connection, *, scheduler_run_id: str | None = None, cycle_id: str | None = None,
    check_name: str | None = None,
) -> list[dict]:
    clauses = []
    params: list[object] = []
    if scheduler_run_id is not None:
        clauses.append("scheduler_run_id = ?")
        params.append(scheduler_run_id)
    if cycle_id is not None:
        clauses.append("cycle_id = ?")
        params.append(cycle_id)
    if check_name is not None:
        clauses.append("check_name = ?")
        params.append(check_name)
    query = "SELECT * FROM shadow_run_health_checks"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY check_name"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]
