"""Persistence for `shadow_operations_schema.py`'s tables (Milestone 7,
docs/milestone-7.md Step 13). Raw sqlite3, no ORM — mirrors
`research_cycle_repositories.py`/`corporate_status_repositories.py`'s
save/load/list function-per-table convention. Consumed by
`shadow/lease.py`, `shadow/pause.py`, and `shadow/budget.py`.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

# --- shadow_scheduler_runs ---------------------------------------------------


def save_scheduler_run(conn: sqlite3.Connection, run: dict) -> None:
    conn.execute(
        "INSERT INTO shadow_scheduler_runs "
        "(scheduler_run_id, intended_schedule_id, scheduled_time, actual_start_at, actual_finish_at, "
        "cycle_id, configuration_hash, mode, lease_owner, lease_expires_at, status, pause_state, "
        "budget_reservation_id, budget_reserved_usd, budget_consumed_usd, symbols_attempted, "
        "symbols_completed, symbols_skipped, provider_failures, research_failures, paper_submissions, "
        "alert_count, failure_reason, operator_action, deployment_source, created_at) "
        "VALUES (:scheduler_run_id, :intended_schedule_id, :scheduled_time, :actual_start_at, :actual_finish_at, "
        ":cycle_id, :configuration_hash, :mode, :lease_owner, :lease_expires_at, :status, :pause_state, "
        ":budget_reservation_id, :budget_reserved_usd, :budget_consumed_usd, :symbols_attempted, "
        ":symbols_completed, :symbols_skipped, :provider_failures, :research_failures, :paper_submissions, "
        ":alert_count, :failure_reason, :operator_action, :deployment_source, :created_at)",
        run,
    )
    conn.commit()


def update_scheduler_run(conn: sqlite3.Connection, scheduler_run_id: str, updates: dict) -> None:
    if not updates:
        return
    columns = ", ".join(f"{key} = :{key}" for key in updates)
    params = dict(updates)
    params["scheduler_run_id"] = scheduler_run_id
    conn.execute(f"UPDATE shadow_scheduler_runs SET {columns} WHERE scheduler_run_id = :scheduler_run_id", params)
    conn.commit()


def load_scheduler_run(conn: sqlite3.Connection, scheduler_run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM shadow_scheduler_runs WHERE scheduler_run_id = ?", (scheduler_run_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def find_scheduler_run_by_intended_schedule(conn: sqlite3.Connection, intended_schedule_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM shadow_scheduler_runs WHERE intended_schedule_id = ? ORDER BY created_at DESC LIMIT 1",
        (intended_schedule_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def find_scheduler_run_by_cycle_id(conn: sqlite3.Connection, cycle_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM shadow_scheduler_runs WHERE cycle_id = ? ORDER BY created_at DESC LIMIT 1", (cycle_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def list_scheduler_runs(conn: sqlite3.Connection, *, status: str | None = None) -> list[dict]:
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM shadow_scheduler_runs WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM shadow_scheduler_runs ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


# --- shadow_run_leases --------------------------------------------------------


def load_lease(conn: sqlite3.Connection, lease_key: str) -> dict | None:
    row = conn.execute("SELECT * FROM shadow_run_leases WHERE lease_key = ?", (lease_key,)).fetchone()
    return dict(row) if row is not None else None


def list_leases(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM shadow_run_leases ORDER BY lease_key").fetchall()
    return [dict(r) for r in rows]


# --- shadow_budget_reservations ------------------------------------------------


def load_budget_reservation(conn: sqlite3.Connection, reservation_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM shadow_budget_reservations WHERE reservation_id = ?", (reservation_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def load_budget_reservation_by_idempotency_key(conn: sqlite3.Connection, idempotency_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM shadow_budget_reservations WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    return dict(row) if row is not None else None


def list_budget_reservations(conn: sqlite3.Connection, *, status: str | None = None) -> list[dict]:
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM shadow_budget_reservations WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM shadow_budget_reservations ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


# --- shadow_budget_usage --------------------------------------------------------


def list_budget_usage(
    conn: sqlite3.Connection, *, usage_date_prefix: str | None = None, reservation_id: str | None = None
) -> list[dict]:
    clauses = []
    params: list[object] = []
    if usage_date_prefix is not None:
        clauses.append("usage_date LIKE ?")
        params.append(f"{usage_date_prefix}%")
    if reservation_id is not None:
        clauses.append("reservation_id = ?")
        params.append(reservation_id)
    query = "SELECT * FROM shadow_budget_usage"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY recorded_at"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# --- shadow_pause_state ---------------------------------------------------------


def load_current_pause_state(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM shadow_pause_state WHERE is_current = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row is not None else None


def list_pause_state_history(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM shadow_pause_state ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


def insert_pause_state(conn: sqlite3.Connection, entry: dict) -> None:
    """Append a new pause-state row and demote any previously-current row.
    Both statements run in the same implicit transaction so the "single
    current row" invariant (ADR 0005 Decision 4) is never briefly violated
    or briefly absent for a concurrent reader."""
    conn.execute("UPDATE shadow_pause_state SET is_current = 0 WHERE is_current = 1")
    conn.execute(
        "INSERT INTO shadow_pause_state "
        "(state, previous_state, reason, source, operator, is_current, expires_at, created_at) "
        "VALUES (:state, :previous_state, :reason, :source, :operator, 1, :expires_at, :created_at)",
        entry,
    )
    conn.commit()


# --- shadow_operator_actions (append-only) --------------------------------------


def insert_operator_action(conn: sqlite3.Connection, action: dict) -> None:
    conn.execute(
        "INSERT INTO shadow_operator_actions "
        "(action_id, action_type, target, reason, operator, details, created_at) "
        "VALUES (:action_id, :action_type, :target, :reason, :operator, :details, :created_at)",
        action,
    )
    conn.commit()


def list_operator_actions(conn: sqlite3.Connection, *, action_type: str | None = None) -> list[dict]:
    if action_type is not None:
        rows = conn.execute(
            "SELECT * FROM shadow_operator_actions WHERE action_type = ? ORDER BY created_at ASC", (action_type,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM shadow_operator_actions ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]


# --- shadow_role_budget_checks (Milestone 7.1, Steps 13-14) -------------------


def save_role_budget_check(conn: sqlite3.Connection, check: dict) -> None:
    """Idempotent on `check_id` (deterministic — see
    `shadow/attempt_controller.py::_compute_check_id`): `INSERT OR IGNORE`
    so a resumed cycle's identical pre-attempt check never creates a
    duplicate audit row."""
    conn.execute(
        "INSERT OR IGNORE INTO shadow_role_budget_checks "
        "(check_id, reservation_id, scheduler_run_id, cycle_id, research_run_id, symbol, role, attempt_number, "
        "provider, model_name, decision, reason, remaining_input_tokens, remaining_output_tokens, "
        "remaining_latency_ms, remaining_cost_usd, maximum_attempt_input_tokens, maximum_attempt_output_tokens, "
        "maximum_attempt_latency_ms, maximum_attempt_cost_usd, checked_at) "
        "VALUES (:check_id, :reservation_id, :scheduler_run_id, :cycle_id, :research_run_id, :symbol, :role, "
        ":attempt_number, :provider, :model_name, :decision, :reason, :remaining_input_tokens, "
        ":remaining_output_tokens, :remaining_latency_ms, :remaining_cost_usd, :maximum_attempt_input_tokens, "
        ":maximum_attempt_output_tokens, :maximum_attempt_latency_ms, :maximum_attempt_cost_usd, :checked_at)",
        check,
    )
    conn.commit()


def list_role_budget_checks(
    conn: sqlite3.Connection, *, scheduler_run_id: str | None = None, symbol: str | None = None,
    role: str | None = None, decision: str | None = None,
) -> list[dict]:
    clauses = []
    params: list[object] = []
    if scheduler_run_id is not None:
        clauses.append("scheduler_run_id = ?")
        params.append(scheduler_run_id)
    if symbol is not None:
        clauses.append("symbol = ?")
        params.append(symbol)
    if role is not None:
        clauses.append("role = ?")
        params.append(role)
    if decision is not None:
        clauses.append("decision = ?")
        params.append(decision)
    query = "SELECT * FROM shadow_role_budget_checks"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY checked_at"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# --- shadow_budget_usage_attempts (Milestone 7.1, Step 15) --------------------


def load_budget_usage_attempt(conn: sqlite3.Connection, attempt_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM shadow_budget_usage_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def save_budget_usage_attempt(conn: sqlite3.Connection, entry: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO shadow_budget_usage_attempts (attempt_id, usage_id, reservation_id, recorded_at) "
        "VALUES (:attempt_id, :usage_id, :reservation_id, :recorded_at)",
        entry,
    )
    conn.commit()


# --- Milestone 12.1.1 Item 7: model-provider health attempt lookup -----------


def list_research_attempts_for_scheduler_run(conn: sqlite3.Connection, scheduler_run_id: str) -> list[dict]:
    """Every `research_attempts` row that was actually gated/checked for
    this scheduler run — joined via `shadow_role_budget_checks`, which is
    the only table that records the (scheduler_run_id, research_run_id,
    role, attempt_number) linkage (`research_attempts` itself has no
    `scheduler_run_id` column; Milestone 12.1.1's persistence-minimality
    requirement prefers this join over widening that core research table).
    Excludes `failure_stage = 'BUDGET_GATED'` rows — a budget/pause/kill
    gate denial never reached the provider at all, so it is not a
    model-provider outcome (success or failure) by definition."""
    rows = conn.execute(
        "SELECT DISTINCT ra.attempt_id, ra.role, ra.attempt_number, ra.provider, ra.success, "
        "ra.failure_code, ra.failure_stage, ra.failure_retryable "
        "FROM research_attempts ra "
        "JOIN shadow_role_budget_checks c "
        "ON c.research_run_id = ra.research_run_id AND c.role = ra.role AND c.attempt_number = ra.attempt_number "
        "WHERE c.scheduler_run_id = ? AND (ra.failure_stage IS NULL OR ra.failure_stage != 'BUDGET_GATED')",
        (scheduler_run_id,),
    ).fetchall()
    return [dict(r) for r in rows]
