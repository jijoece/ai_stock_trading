"""SQLite schema for corporate-status evidence and evidence-completeness
persistence (Milestone 7, docs/milestone-7.md Step 4/11 persistence
requirement). Separate concern from `evidence_provider_schema.py` (which
persists raw HTTP request/response telemetry) — these two tables persist
the *normalized, derived* result objects
(`evidence_providers/corporate_status.py::CorporateStatusEvidence` and
`research/evidence_completeness.py::EvidenceCompletenessResult`), one row
per evaluation, following the same one-row-per-result convention as
`research_cycle_schema.py::research_cycle_symbol_results`.

`corporate_status_evidence` stores the full result as JSON (sub-structures:
filing references, risk-signal tuples, sources) alongside indexed scalar
columns needed for querying (`symbol`, `as_of`, `reporting_status`,
`completeness_status`) — mirrors `research_schema.py`'s existing
JSON-blob-plus-indexed-scalars pattern (see
`research/evidence.py::snapshot_to_row`).

`evidence_completeness_results` stores the evidence-completeness policy
result, including `policy_version`, so a persisted result is always
auditable against the exact policy version that produced it.

Idempotent (`CREATE TABLE IF NOT EXISTS`), applied from
`storage/database.py::connect`.
"""
from __future__ import annotations

CORPORATE_STATUS_SCHEMA_VERSION = 1

CORPORATE_STATUS_DDL = """
CREATE TABLE IF NOT EXISTS corporate_status_evidence (
    corporate_status_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    reporting_status TEXT NOT NULL,
    reporting_status_reason TEXT,
    earliest_reliable_filing_date TEXT,
    operating_history_years TEXT,
    completeness_status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_completeness_results (
    evidence_completeness_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    screening_completeness TEXT NOT NULL,
    research_completeness TEXT NOT NULL,
    blocking_categories_json TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

CORPORATE_STATUS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_corporate_status_evidence_symbol ON corporate_status_evidence(symbol, as_of);
CREATE INDEX IF NOT EXISTS idx_evidence_completeness_results_symbol ON evidence_completeness_results(symbol, created_at);
CREATE INDEX IF NOT EXISTS idx_evidence_completeness_results_policy ON evidence_completeness_results(policy_version);
"""


def apply_corporate_status_schema(conn) -> None:
    conn.executescript(CORPORATE_STATUS_DDL)
    conn.executescript(CORPORATE_STATUS_INDEXES)
    conn.commit()
