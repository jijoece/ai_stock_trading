"""SQLite schema for operational alerts and per-run health/readiness summaries
(Milestone 7, docs/milestone-7.md Step 13/21/22). Separate module from
`shadow_operations_schema.py` per this task's explicit boundary (that module's
own docstring names these three tables as "owned by later, separate work").

Applied idempotently (`CREATE TABLE IF NOT EXISTS`) from
`storage/database.py::connect`, exactly like every other schema module.

Never persisted here: API keys, authorization headers, account identifiers,
raw Claude prompts/responses. `shadow_alerts.context_json` is only ever
written from `shadow/alerts.py::_sanitize_context`, which strips
secret-shaped keys before this module ever sees them — this module itself
does not re-validate that (single responsibility: schema + raw persistence),
matching how `research_attempt_failures` trusts
`failure_taxonomy.py::_validate_metadata` to have already cleaned metadata.
"""
from __future__ import annotations

SHADOW_ALERTS_SCHEMA_VERSION = 1

SHADOW_ALERTS_DDL = """
CREATE TABLE IF NOT EXISTS shadow_alerts (
    alert_id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL,
    severity TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    message TEXT NOT NULL,
    context_json TEXT NOT NULL,
    suppressed_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_alert_deliveries (
    delivery_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL REFERENCES shadow_alerts(alert_id),
    sink_name TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    success INTEGER NOT NULL,
    response_text TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_run_summaries (
    scheduler_run_id TEXT PRIMARY KEY,
    intended_schedule_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    health_status TEXT NOT NULL,
    health_reasons_json TEXT NOT NULL,
    provider_success_rate REAL,
    evidence_completeness_rate REAL,
    claude_role_success_rate REAL,
    retry_rate REAL,
    retry_exhaustion_rate REAL,
    unsupported_claim_rate REAL,
    output_truncation_rate REAL,
    latency_seconds REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd TEXT,
    paper_reconciliation_mismatch INTEGER NOT NULL DEFAULT 0,
    duplicate_prevention_violation INTEGER NOT NULL DEFAULT 0,
    cycle_duration_seconds REAL,
    created_at TEXT NOT NULL
);

-- Milestone 7.2 (docs/milestone-7.2.md Part 3): one row per field-level
-- health-check dimension (shadow/health.py::HealthCheckResult), so the
-- summary verdict on `shadow_run_summaries` is fully explainable after the
-- fact without re-deriving it from raw telemetry. `check_id` is
-- deterministic (sha256 of scheduler_run_id|check_name|policy_version) and
-- inserted via `INSERT OR IGNORE`, so re-evaluating/resuming the same
-- scheduler run never creates duplicate rows. Additive only — no change to
-- `shadow_run_summaries` above.
CREATE TABLE IF NOT EXISTS shadow_run_health_checks (
    check_id TEXT PRIMARY KEY,
    scheduler_run_id TEXT NOT NULL,
    cycle_id TEXT,
    check_name TEXT NOT NULL,
    check_status TEXT NOT NULL,
    input_value TEXT,
    input_unit TEXT,
    threshold_value TEXT,
    threshold_unit TEXT,
    comparison TEXT NOT NULL,
    applicable INTEGER NOT NULL,
    pause_flag_enabled INTEGER NOT NULL,
    reason TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);

-- Milestone 11.3.1 Item 8 Part C: persistent, multi-cycle provider-health
-- hysteresis state. One row per `scope` (a singleton "default" scope today;
-- the column exists so a future multi-book/multi-provider-set deployment
-- can key additional rows without a schema change). Reading this row is how
-- `evaluate_and_persist_hysteresis` survives a process restart -- the
-- decision is reconstructed from durable state, never from in-memory
-- counters.
CREATE TABLE IF NOT EXISTS shadow_health_hysteresis_state (
    scope TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    decision TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    consecutive_recoveries INTEGER NOT NULL DEFAULT 0,
    qualified_cycle_count INTEGER NOT NULL DEFAULT 0,
    failing_cycle_count INTEGER NOT NULL DEFAULT 0,
    window_start TEXT,
    window_end TEXT,
    last_cycle_id TEXT,
    last_evaluated_at TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    per_provider_metrics_json TEXT NOT NULL DEFAULT '{}'
);
"""

SHADOW_ALERTS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_shadow_alerts_dedup ON shadow_alerts(dedup_key);
CREATE INDEX IF NOT EXISTS idx_shadow_alerts_severity ON shadow_alerts(severity);
CREATE INDEX IF NOT EXISTS idx_shadow_alerts_type ON shadow_alerts(alert_type);
CREATE INDEX IF NOT EXISTS idx_shadow_alerts_created ON shadow_alerts(created_at);
CREATE INDEX IF NOT EXISTS idx_shadow_alert_deliveries_alert ON shadow_alert_deliveries(alert_id);
CREATE INDEX IF NOT EXISTS idx_shadow_alert_deliveries_sink ON shadow_alert_deliveries(sink_name);
CREATE INDEX IF NOT EXISTS idx_shadow_run_summaries_intended ON shadow_run_summaries(intended_schedule_id);
CREATE INDEX IF NOT EXISTS idx_shadow_run_summaries_status ON shadow_run_summaries(health_status);
CREATE INDEX IF NOT EXISTS idx_shadow_run_summaries_created ON shadow_run_summaries(created_at);
CREATE INDEX IF NOT EXISTS idx_shadow_run_health_checks_run ON shadow_run_health_checks(scheduler_run_id);
CREATE INDEX IF NOT EXISTS idx_shadow_run_health_checks_cycle ON shadow_run_health_checks(cycle_id);
CREATE INDEX IF NOT EXISTS idx_shadow_run_health_checks_name ON shadow_run_health_checks(check_name);
"""

# Milestone 9.1 (docs/milestone9-1-controlled-soak-readiness.md): additive,
# nullable resolution columns on the pre-existing `shadow_alerts` table.
# Milestone 7 never persisted any resolved/acknowledged concept for an
# alert — every alert was permanently "open" with no way to distinguish a
# stale historical CRITICAL alert from one still requiring attention. Added
# via `ALTER TABLE` (mirrors `storage/trading_schema.py::_ensure_columns`)
# rather than a destructive rebuild, so every pre-existing alert row simply
# reads back as unresolved (`resolved_at IS NULL`) — never fabricated as
# resolved.
_SHADOW_ALERTS_COLUMN_UPGRADES = {
    "shadow_alerts": {
        "resolved_at": "TEXT",
        "resolved_by": "TEXT",
        "resolved_reason": "TEXT",
    },
}


def _ensure_columns(conn) -> None:
    for table, columns in _SHADOW_ALERTS_COLUMN_UPGRADES.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            continue  # table doesn't exist yet; CREATE handles it in full
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def apply_shadow_alerts_schema(conn) -> None:
    conn.executescript(SHADOW_ALERTS_DDL)
    _ensure_columns(conn)
    conn.executescript(SHADOW_ALERTS_INDEXES)
    conn.commit()
