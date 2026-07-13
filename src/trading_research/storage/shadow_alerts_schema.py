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
"""


def apply_shadow_alerts_schema(conn) -> None:
    conn.executescript(SHADOW_ALERTS_DDL)
    conn.executescript(SHADOW_ALERTS_INDEXES)
    conn.commit()
