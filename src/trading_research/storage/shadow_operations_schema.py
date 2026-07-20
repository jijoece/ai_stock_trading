"""SQLite schema for production shadow-operations persistence (Milestone 7,
docs/milestone-7.md Step 13). Separate concern from `research_cycle_schema.py`
(which persists the research-cycle coordination unit) — these tables persist
the *operational control layer* wrapped around a cycle: scheduler runs,
leases, budget reservations/usage, pause state, and operator actions.

Applied idempotently (`CREATE TABLE IF NOT EXISTS`) from
`storage/database.py::connect`, exactly like every other schema module.

Deliberately NOT created here (owned by later, separate work per
docs/milestone-7.md Step 13's full table list and this task's explicit
boundary): `shadow_alerts`, `shadow_alert_deliveries`, `shadow_run_summaries`.

Never persisted here: API keys, authorization headers, account identifiers,
raw Claude prompts/responses. `shadow_operator_actions` is pure append-only —
no UPDATE/DELETE statement anywhere in this repository targets that table.
"""
from __future__ import annotations

SHADOW_OPERATIONS_SCHEMA_VERSION = 1

SHADOW_OPERATIONS_DDL = """
CREATE TABLE IF NOT EXISTS shadow_scheduler_runs (
    scheduler_run_id TEXT PRIMARY KEY,
    intended_schedule_id TEXT NOT NULL,
    scheduled_time TEXT NOT NULL,
    actual_start_at TEXT,
    actual_finish_at TEXT,
    cycle_id TEXT,
    configuration_hash TEXT NOT NULL,
    mode TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    status TEXT NOT NULL,
    pause_state TEXT,
    budget_reservation_id TEXT,
    budget_reserved_usd TEXT,
    budget_consumed_usd TEXT,
    symbols_attempted INTEGER NOT NULL DEFAULT 0,
    symbols_completed INTEGER NOT NULL DEFAULT 0,
    symbols_skipped INTEGER NOT NULL DEFAULT 0,
    provider_failures INTEGER NOT NULL DEFAULT 0,
    research_failures INTEGER NOT NULL DEFAULT 0,
    paper_submissions INTEGER NOT NULL DEFAULT 0,
    alert_count INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    operator_action TEXT,
    deployment_source TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_run_leases (
    lease_key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    renewed_at TEXT,
    released_at TEXT,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    cycle_intent TEXT NOT NULL,
    reserved_estimated_cost_usd TEXT NOT NULL,
    reserved_input_tokens INTEGER NOT NULL,
    reserved_output_tokens INTEGER NOT NULL,
    reserved_latency_seconds INTEGER NOT NULL,
    status TEXT NOT NULL,
    consumed_cost_usd TEXT NOT NULL DEFAULT '0',
    consumed_input_tokens INTEGER NOT NULL DEFAULT 0,
    consumed_output_tokens INTEGER NOT NULL DEFAULT 0,
    consumed_latency_seconds INTEGER NOT NULL DEFAULT 0,
    emergency_margin_breached INTEGER NOT NULL DEFAULT 0,
    reservation_kind TEXT NOT NULL DEFAULT 'RESEARCH_COST',
    budget_date TEXT,
    reserved_reasoning_tokens INTEGER,
    consumed_reasoning_tokens INTEGER,
    token_accounting_policy TEXT,
    provider_request_id TEXT,
    created_at TEXT NOT NULL,
    settled_at TEXT,
    UNIQUE (idempotency_key)
);

CREATE TABLE IF NOT EXISTS shadow_token_budget_reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL REFERENCES shadow_budget_reservations(reservation_id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    reconciliation_source TEXT NOT NULL,
    provider_request_id TEXT,
    actual_input_tokens INTEGER,
    actual_output_tokens INTEGER,
    actual_reasoning_tokens INTEGER,
    token_accounting_policy TEXT,
    reconciled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_budget_usage (
    usage_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL REFERENCES shadow_budget_reservations(reservation_id),
    usage_date TEXT NOT NULL,
    actual_cost_usd TEXT NOT NULL,
    actual_input_tokens INTEGER NOT NULL,
    actual_output_tokens INTEGER NOT NULL,
    actual_latency_seconds INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model_name TEXT,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_pause_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state TEXT NOT NULL,
    previous_state TEXT,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    operator TEXT,
    is_current INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_operator_actions (
    action_id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    target TEXT NOT NULL,
    reason TEXT NOT NULL,
    operator TEXT,
    details TEXT,
    created_at TEXT NOT NULL
);

-- Milestone 7.1 (docs/milestone-7.1.md Steps 13-14): append-only audit trail
-- of every pre-attempt role-budget check `shadow/attempt_controller.py`
-- performs before a Claude call. Deterministic/idempotent check identity
-- (`check_id`) so a resumed/retried attempt never creates a duplicate row.
CREATE TABLE IF NOT EXISTS shadow_role_budget_checks (
    check_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    scheduler_run_id TEXT,
    cycle_id TEXT,
    research_run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    role TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model_name TEXT,
    decision TEXT NOT NULL,
    reason TEXT,
    remaining_input_tokens INTEGER,
    remaining_output_tokens INTEGER,
    remaining_latency_ms INTEGER,
    remaining_cost_usd TEXT,
    maximum_attempt_input_tokens INTEGER,
    maximum_attempt_output_tokens INTEGER,
    maximum_attempt_latency_ms INTEGER,
    maximum_attempt_cost_usd TEXT,
    checked_at TEXT NOT NULL
);

-- Milestone 7.1 (docs/milestone-7.1.md Step 15): idempotency companion for
-- `shadow_budget_usage` — additive rather than an ALTER TABLE on the
-- existing append-only usage table. One row per attempt_id ever charged;
-- `record_actual_usage_for_attempt` checks this table before inserting a
-- new `shadow_budget_usage` row, so a resumed cycle never double-charges
-- the same attempt.
CREATE TABLE IF NOT EXISTS shadow_budget_usage_attempts (
    attempt_id TEXT PRIMARY KEY,
    usage_id TEXT NOT NULL,
    reservation_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""

SHADOW_OPERATIONS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_shadow_scheduler_runs_status ON shadow_scheduler_runs(status);
CREATE INDEX IF NOT EXISTS idx_shadow_scheduler_runs_scheduled ON shadow_scheduler_runs(scheduled_time);
CREATE INDEX IF NOT EXISTS idx_shadow_scheduler_runs_intended ON shadow_scheduler_runs(intended_schedule_id);
CREATE INDEX IF NOT EXISTS idx_shadow_run_leases_status ON shadow_run_leases(status);
CREATE INDEX IF NOT EXISTS idx_shadow_budget_reservations_status ON shadow_budget_reservations(status);
CREATE INDEX IF NOT EXISTS idx_shadow_budget_usage_date ON shadow_budget_usage(usage_date);
CREATE INDEX IF NOT EXISTS idx_shadow_budget_usage_reservation ON shadow_budget_usage(reservation_id);
CREATE INDEX IF NOT EXISTS idx_shadow_pause_state_current ON shadow_pause_state(is_current);
CREATE INDEX IF NOT EXISTS idx_shadow_operator_actions_type ON shadow_operator_actions(action_type);
CREATE INDEX IF NOT EXISTS idx_shadow_operator_actions_created ON shadow_operator_actions(created_at);
CREATE INDEX IF NOT EXISTS idx_shadow_role_budget_checks_run ON shadow_role_budget_checks(scheduler_run_id);
CREATE INDEX IF NOT EXISTS idx_shadow_role_budget_checks_role ON shadow_role_budget_checks(role);
CREATE INDEX IF NOT EXISTS idx_shadow_role_budget_checks_symbol ON shadow_role_budget_checks(symbol);
CREATE INDEX IF NOT EXISTS idx_shadow_role_budget_checks_decision ON shadow_role_budget_checks(decision);
CREATE INDEX IF NOT EXISTS idx_shadow_budget_usage_attempts_reservation ON shadow_budget_usage_attempts(reservation_id);
CREATE INDEX IF NOT EXISTS idx_shadow_token_budget_reconciliations_reservation
    ON shadow_token_budget_reconciliations(reservation_id);
"""


def apply_shadow_operations_schema(conn) -> None:
    conn.executescript(SHADOW_OPERATIONS_DDL)
    # CREATE TABLE IF NOT EXISTS does not add columns to databases created by
    # older releases.  Keep this additive migration local and idempotent so
    # existing operational databases gain the token-accounting audit fields
    # without a destructive table rebuild.
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(shadow_budget_reservations)").fetchall()
    }
    additive_columns = {
        "reservation_kind": "TEXT NOT NULL DEFAULT 'RESEARCH_COST'",
        "budget_date": "TEXT",
        "reserved_reasoning_tokens": "INTEGER",
        "consumed_reasoning_tokens": "INTEGER",
        "token_accounting_policy": "TEXT",
        "provider_request_id": "TEXT",
    }
    for column_name, declaration in additive_columns.items():
        if column_name not in existing_columns:
            conn.execute(
                f"ALTER TABLE shadow_budget_reservations ADD COLUMN {column_name} {declaration}"
            )
    conn.executescript(SHADOW_OPERATIONS_INDEXES)
    conn.commit()
