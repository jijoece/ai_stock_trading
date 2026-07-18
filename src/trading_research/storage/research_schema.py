"""SQLite schema for the Milestone 5 research layer (evidence snapshots,
research runs/attempts/reports/decisions, overlay decisions, experiment
assignments). Separate from `trading_schema.py` / `execution_schema.py` for
the same reason those are separate from each other: an independently
evolving concern, applied idempotently from `storage/database.py::connect`.

Immutability mirrors `trading_schema.py`'s `recommendations` table: evidence
snapshots, role reports, and decisions are append-once and DB-trigger
protected against UPDATE/DELETE. `research_attempts` is strictly
append-only (retries add new rows, never rewrite a prior attempt).
`research_committee_runs` is the one table that legitimately transitions status
(RUNNING -> COMPLETED/ANALYSIS_INCOMPLETE/FAILED) and is therefore not
trigger-protected.
"""
from __future__ import annotations

RESEARCH_SCHEMA_VERSION = 1

RESEARCH_DDL = """
CREATE TABLE IF NOT EXISTS research_evidence_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_records_json TEXT NOT NULL,
    evidence_items_json TEXT NOT NULL,
    deterministic_factors_json TEXT NOT NULL,
    sentiment_metrics_json TEXT NOT NULL,
    portfolio_context_json TEXT,
    missing_data_reasons_json TEXT NOT NULL,
    conflict_reasons_json TEXT NOT NULL,
    point_in_time_safe INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    git_sha TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_committee_runs (
    research_run_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES research_evidence_snapshots(snapshot_id),
    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    roles_json TEXT NOT NULL,
    run_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

-- Append-only: every attempt (including failed/retried ones) gets its own row.
CREATE TABLE IF NOT EXISTS research_attempts (
    attempt_id TEXT PRIMARY KEY,
    research_run_id TEXT NOT NULL REFERENCES research_committee_runs(research_run_id),
    role TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    prompt_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    system_prompt_hash TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    success INTEGER NOT NULL,
    failure_reason TEXT,
    raw_response_json TEXT,
    validated_payload_json TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    latency_ms INTEGER,
    provider_request_id TEXT,
    retry_count INTEGER NOT NULL,
    pricing_version TEXT,
    estimated_cost TEXT,
    cost_status TEXT NOT NULL,
    cost_estimate_basis TEXT NOT NULL DEFAULT 'NOT_APPLICABLE',
    configured_model_alias TEXT,
    resolved_model_name TEXT,
    claude_code_version TEXT,
    provider_cli_version TEXT,
    provider_adapter_version TEXT,
    reasoning_output_tokens INTEGER,
    token_accounting_policy TEXT NOT NULL DEFAULT 'NOT_APPLICABLE',
    failure_code TEXT,
    failure_stage TEXT,
    failure_retryable INTEGER,
    failure_metadata_json TEXT,
    created_at TEXT NOT NULL
);

-- One validated report per (run, role) — retries append to research_attempts,
-- only the attempt that finally validated is copied here.
CREATE TABLE IF NOT EXISTS research_role_reports (
    report_id TEXT PRIMARY KEY,
    research_run_id TEXT NOT NULL REFERENCES research_committee_runs(research_run_id),
    role TEXT NOT NULL,
    symbol TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    stance TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (research_run_id, role)
);

CREATE TABLE IF NOT EXISTS research_decisions (
    decision_id TEXT PRIMARY KEY,
    research_run_id TEXT NOT NULL UNIQUE REFERENCES research_committee_runs(research_run_id),
    symbol TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    rating TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_overlay_decisions (
    overlay_id TEXT PRIMARY KEY,
    research_decision_id TEXT NOT NULL,
    baseline_score TEXT,
    action TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_experiment_assignments (
    experiment_id TEXT NOT NULL,
    candidate_run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    arm TEXT NOT NULL,
    baseline_recommendation_id TEXT,
    enhanced_recommendation_id TEXT,
    assignment_policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (experiment_id, symbol, arm)
);

CREATE TABLE IF NOT EXISTS research_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    research_run_id TEXT,
    role TEXT,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

-- Milestone 6.1 (docs/milestone-6.1.md Step 6): one row per structured failure, not one
-- row per attempt — a single failed attempt can produce several independent claim
-- rejections. Append-only, idempotent on the deterministic failure_id (see
-- research/failure_taxonomy.py::new_failure). `attempt_id` is a real
-- `research_attempts.attempt_id` for attempt-scoped failures (provider/schema/claim); for
-- run-level meta-failures (retry exhaustion, manager skip) it is a synthetic identifier
-- (`{research_run_id}-{role}-retry-exhausted`, `{research_run_id}-manager-skipped`) since
-- no real attempt exists to reference — deliberately not a foreign key for that reason.
CREATE TABLE IF NOT EXISTS research_attempt_failures (
    failure_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    research_run_id TEXT NOT NULL REFERENCES research_committee_runs(research_run_id),
    role TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    stage TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    field_path TEXT,
    claim_id TEXT,
    evidence_ids_json TEXT NOT NULL,
    retryable INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
"""

RESEARCH_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_research_committee_runs_snapshot ON research_committee_runs(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_research_attempts_run ON research_attempts(research_run_id);
CREATE INDEX IF NOT EXISTS idx_research_attempts_role ON research_attempts(research_run_id, role);
CREATE INDEX IF NOT EXISTS idx_research_role_reports_run ON research_role_reports(research_run_id);
CREATE INDEX IF NOT EXISTS idx_research_decisions_snapshot ON research_decisions(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_research_overlay_decision ON research_overlay_decisions(research_decision_id);
CREATE INDEX IF NOT EXISTS idx_research_experiment_symbol ON research_experiment_assignments(experiment_id, symbol);
CREATE INDEX IF NOT EXISTS idx_research_attempt_failures_run ON research_attempt_failures(research_run_id);
CREATE INDEX IF NOT EXISTS idx_research_attempt_failures_attempt ON research_attempt_failures(attempt_id);
CREATE INDEX IF NOT EXISTS idx_research_attempt_failures_role ON research_attempt_failures(research_run_id, role);
CREATE INDEX IF NOT EXISTS idx_research_attempt_failures_stage ON research_attempt_failures(stage);
CREATE INDEX IF NOT EXISTS idx_research_attempt_failures_code ON research_attempt_failures(code);
"""

RESEARCH_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_research_snapshots_no_update
BEFORE UPDATE ON research_evidence_snapshots
BEGIN SELECT RAISE(ABORT, 'evidence snapshots are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_research_snapshots_no_delete
BEFORE DELETE ON research_evidence_snapshots
BEGIN SELECT RAISE(ABORT, 'evidence snapshots are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_research_decisions_no_update
BEFORE UPDATE ON research_decisions
BEGIN SELECT RAISE(ABORT, 'research decisions are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_research_decisions_no_delete
BEFORE DELETE ON research_decisions
BEGIN SELECT RAISE(ABORT, 'research decisions are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_research_role_reports_no_update
BEFORE UPDATE ON research_role_reports
BEGIN SELECT RAISE(ABORT, 'research role reports are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_research_role_reports_no_delete
BEFORE DELETE ON research_role_reports
BEGIN SELECT RAISE(ABORT, 'research role reports are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_research_attempts_no_update
BEFORE UPDATE ON research_attempts
BEGIN SELECT RAISE(ABORT, 'research attempts are append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_research_attempts_no_delete
BEFORE DELETE ON research_attempts
BEGIN SELECT RAISE(ABORT, 'research attempts are append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_research_attempt_failures_no_update
BEFORE UPDATE ON research_attempt_failures
BEGIN SELECT RAISE(ABORT, 'research attempt failures are append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_research_attempt_failures_no_delete
BEFORE DELETE ON research_attempt_failures
BEGIN SELECT RAISE(ABORT, 'research attempt failures are append-only'); END;
"""


def apply_research_schema(conn) -> None:
    conn.executescript(RESEARCH_DDL)
    conn.executescript(RESEARCH_INDEXES)
    conn.executescript(RESEARCH_TRIGGERS)
    conn.commit()
