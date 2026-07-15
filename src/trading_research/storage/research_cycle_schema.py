"""SQLite schema for scheduled research-cycle persistence (Milestone 6,
docs/milestone-6.md Step 14). Separate concern from `research_schema.py`
(which persists per-symbol evidence/research-run state) — a cycle is the
coordination unit across a bounded candidate universe for one `as_of`.

`research_cycles` is the one table that legitimately transitions status
(RUNNING -> COMPLETED/PARTIALLY_COMPLETE/FAILED), mirroring
`research_committee_runs`'s same pattern. `research_cycle_symbol_results` is
keyed by `(cycle_id, symbol)` so a resumed/rerun cycle upserts per-symbol
progress instead of duplicating it — this is what makes
`run_scheduled_research_cycle` idempotent and resumable.
"""
from __future__ import annotations

RESEARCH_CYCLE_SCHEMA_VERSION = 1

RESEARCH_CYCLE_DDL = """
CREATE TABLE IF NOT EXISTS research_cycles (
    cycle_id TEXT PRIMARY KEY,
    universe_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    experiment_policy TEXT NOT NULL,
    provider_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS research_cycle_symbol_results (
    cycle_id TEXT NOT NULL REFERENCES research_cycles(cycle_id),
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    snapshot_id TEXT,
    research_run_id TEXT,
    experiment_id TEXT,
    baseline_recommendation_id TEXT,
    enhanced_recommendation_id TEXT,
    baseline_paper_submitted INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (cycle_id, symbol)
);

-- Milestone 7.1 (docs/milestone-7.1.md Step 7): associates the per-cycle,
-- per-symbol corporate-status evidence and evidence-completeness result
-- with the cycle/symbol that produced them. Additive to the existing
-- research_cycle_symbol_results table rather than overloading it, since
-- that table is already keyed/consumed by Milestone 6's own resumability
-- contract (ResearchCycleRepository Protocol) — adding new required-shape
-- columns there would touch a Protocol every prior milestone depends on.
-- INSERT OR REPLACE keyed by (cycle_id, symbol) — idempotent save, mirrors
-- save_symbol_result's own pattern.
CREATE TABLE IF NOT EXISTS research_cycle_symbol_evidence_status (
    cycle_id TEXT NOT NULL REFERENCES research_cycles(cycle_id),
    symbol TEXT NOT NULL,
    snapshot_id TEXT,
    corporate_status_evidence_id TEXT,
    completeness_result_id TEXT,
    screening_completeness TEXT NOT NULL,
    research_completeness TEXT NOT NULL,
    blocking_categories_json TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (cycle_id, symbol)
);

-- Milestone 9.2 (docs/milestone-9.2.md Section 3): authoritative,
-- per-(cycle_id, symbol, provider_category) provider-provenance record.
-- Additive — never retrofits research_committee_runs/research_attempts
-- (already-authoritative for Claude, joined by research_run_id) or
-- evidence_provider_requests (still lacks a fixture/real column). Immutable
-- (INSERT OR IGNORE keyed by (cycle_id, symbol, provider_category)); a
-- record with insufficient metadata is simply never written, so its absence
-- reads back as UNKNOWN rather than a fabricated guess. No raw provider
-- payload/Claude output/credential is ever stored here — only the
-- classification facts themselves.
CREATE TABLE IF NOT EXISTS research_cycle_provider_provenance (
    cycle_id TEXT NOT NULL REFERENCES research_cycles(cycle_id),
    research_run_id TEXT,
    symbol TEXT NOT NULL,
    provider_category TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    provider_mode TEXT NOT NULL,
    is_fixture INTEGER NOT NULL,
    is_real INTEGER NOT NULL,
    request_or_source_id TEXT,
    status TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    classification_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (cycle_id, symbol, provider_category)
);
"""

RESEARCH_CYCLE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_research_cycles_universe ON research_cycles(universe_id, as_of);
CREATE INDEX IF NOT EXISTS idx_research_cycle_symbol_results_cycle ON research_cycle_symbol_results(cycle_id);
CREATE INDEX IF NOT EXISTS idx_research_cycle_symbol_evidence_status_cycle ON research_cycle_symbol_evidence_status(cycle_id);
CREATE INDEX IF NOT EXISTS idx_research_cycle_symbol_evidence_status_symbol ON research_cycle_symbol_evidence_status(symbol);
"""


def apply_research_cycle_schema(conn) -> None:
    conn.executescript(RESEARCH_CYCLE_DDL)
    conn.executescript(RESEARCH_CYCLE_INDEXES)
    conn.commit()
