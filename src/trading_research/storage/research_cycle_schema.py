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
"""

RESEARCH_CYCLE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_research_cycles_universe ON research_cycles(universe_id, as_of);
CREATE INDEX IF NOT EXISTS idx_research_cycle_symbol_results_cycle ON research_cycle_symbol_results(cycle_id);
"""


def apply_research_cycle_schema(conn) -> None:
    conn.executescript(RESEARCH_CYCLE_DDL)
    conn.executescript(RESEARCH_CYCLE_INDEXES)
    conn.commit()
