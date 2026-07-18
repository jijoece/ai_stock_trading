"""SQLite schema for real evidence-provider request/response persistence and
health (Milestone 6, docs/milestone-6.md Step 5). Separate from
`research_schema.py` for the same reason every other schema module is
separate — an independently evolving concern, applied idempotently from
`storage/database.py::connect`.

`evidence_provider_requests` merges the milestone's suggested "requests" and
"responses" tables into one row per attempt: this repository's other
schema modules (e.g. `research_schema.py::research_attempts`) already use one
row per attempt as their append-only audit unit, so this follows the same
convention rather than introducing a second join-heavy table for no added
auditability at this scale.

Never persisted here: API keys, authorization header values, signed URLs,
account identifiers. `raw_payload_json` is capped at `MAX_RAW_PAYLOAD_BYTES`
and only stored when the provider's data is public domain or otherwise
explicitly safe to retain in full (see `evidence_providers/persistence.py`).
"""
from __future__ import annotations

EVIDENCE_PROVIDER_SCHEMA_VERSION = 2

EVIDENCE_PROVIDER_DDL = """
CREATE TABLE IF NOT EXISTS evidence_provider_requests (
    request_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    operation TEXT NOT NULL,
    symbol TEXT NOT NULL,
    requested_as_of TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    provider_response_timestamp TEXT,
    http_status INTEGER,
    content_hash TEXT,
    normalized_record_hash TEXT,
    cache_status TEXT NOT NULL,
    rate_limited INTEGER NOT NULL,
    retry_count INTEGER NOT NULL,
    latency_ms INTEGER,
    success INTEGER NOT NULL,
    error_code TEXT,
    retryable INTEGER,
    licensing_classification TEXT NOT NULL,
    raw_payload_stored INTEGER NOT NULL,
    raw_payload_json TEXT,
    correlation_mode TEXT NOT NULL DEFAULT 'LEGACY_MANUAL',
    research_cycle_id TEXT,
    scheduler_run_id TEXT,
    research_run_id TEXT,
    symbol_attempt_id TEXT,
    provider_request_group_id TEXT,
    transport_failure_category TEXT NOT NULL DEFAULT 'NONE',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_provider_health_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    total_requests INTEGER NOT NULL,
    success_count INTEGER NOT NULL,
    timeout_count INTEGER NOT NULL,
    rate_limited_count INTEGER NOT NULL,
    invalid_response_count INTEGER NOT NULL,
    cache_hit_count INTEGER NOT NULL,
    average_latency_ms REAL,
    p95_latency_ms REAL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

EVIDENCE_PROVIDER_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_evidence_provider_requests_provider ON evidence_provider_requests(provider, operation);
CREATE INDEX IF NOT EXISTS idx_evidence_provider_requests_symbol ON evidence_provider_requests(symbol);
CREATE INDEX IF NOT EXISTS idx_evidence_provider_requests_created ON evidence_provider_requests(created_at);
CREATE INDEX IF NOT EXISTS idx_evidence_provider_requests_cycle
    ON evidence_provider_requests(research_cycle_id, created_at, request_id);
CREATE INDEX IF NOT EXISTS idx_evidence_provider_requests_scheduler_run
    ON evidence_provider_requests(scheduler_run_id, created_at, request_id);
"""

_REQUEST_COLUMN_UPGRADES = {
    "correlation_mode": "TEXT NOT NULL DEFAULT 'LEGACY_MANUAL'",
    "research_cycle_id": "TEXT",
    "scheduler_run_id": "TEXT",
    "research_run_id": "TEXT",
    "symbol_attempt_id": "TEXT",
    "provider_request_group_id": "TEXT",
    "transport_failure_category": "TEXT NOT NULL DEFAULT 'NONE'",
}


def _ensure_request_columns(conn) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(evidence_provider_requests)").fetchall()}
    for name, declaration in _REQUEST_COLUMN_UPGRADES.items():
        if existing and name not in existing:
            conn.execute(f"ALTER TABLE evidence_provider_requests ADD COLUMN {name} {declaration}")


def apply_evidence_provider_schema(conn) -> None:
    conn.executescript(EVIDENCE_PROVIDER_DDL)
    _ensure_request_columns(conn)
    conn.executescript(EVIDENCE_PROVIDER_INDEXES)
    conn.commit()
