"""SQLite schema for Milestone 3 paper-execution persistence.

Separate from `trading_schema.py` (recommendations/screening/paper-ledger
tables) for the same reason that file is separate from `migrations.py`: an
independently evolving concern. Applied idempotently from
`storage/database.py::connect`. No table here is ever written to by
anything resembling `real_orders` — that table remains reserved and
trigger-protected in `trading_schema.py`, untouched by this module.
"""
from __future__ import annotations

EXECUTION_SCHEMA_VERSION = 2  # Milestone 4 added paper_broker_submissions / *_reconciliations tables

EXECUTION_DDL = """
CREATE TABLE IF NOT EXISTS paper_execution_intents (
    intent_id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL REFERENCES recommendations(rec_id),
    execution_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    order_type TEXT NOT NULL,
    limit_price TEXT,
    reference_price TEXT NOT NULL,
    expected_notional TEXT NOT NULL,
    recommendation_created_at TEXT NOT NULL,
    recommendation_frozen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    git_sha TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- One active paper intent per recommendation + execution version
    -- (docs/milestone-3.md Step 7 hard requirement) — enforced at the
    -- database layer, not only in application code.
    UNIQUE (recommendation_id, execution_version)
);

CREATE TABLE IF NOT EXISTS paper_execution_events (
    event_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES paper_execution_intents(intent_id),
    recommendation_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,
    broker_order_id TEXT,
    quantity INTEGER NOT NULL,
    filled_quantity INTEGER NOT NULL,
    fill_price TEXT,
    occurred_at TEXT NOT NULL,
    raw_status TEXT,
    source TEXT NOT NULL,
    ledger_applied INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_execution_results (
    intent_id TEXT PRIMARY KEY REFERENCES paper_execution_intents(intent_id),
    recommendation_id TEXT NOT NULL,
    final_status TEXT NOT NULL,
    requested_quantity INTEGER NOT NULL,
    filled_quantity INTEGER NOT NULL,
    average_fill_price TEXT,
    fees TEXT NOT NULL,
    event_ids_json TEXT NOT NULL,
    completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_execution_reconciliations (
    intent_id TEXT PRIMARY KEY REFERENCES paper_execution_intents(intent_id),
    status TEXT NOT NULL,
    broker_quantity INTEGER NOT NULL,
    ledger_quantity INTEGER NOT NULL,
    broker_notional TEXT NOT NULL,
    ledger_notional TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    reconciled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_execution_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id TEXT,
    intent_id TEXT,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

-- Milestone 4: credentialed paper-broker submission/idempotency tracking
-- across the process boundary (docs/milestone-4.md Step 8). Additive only
-- -- nothing above this point is modified by Milestone 4.
CREATE TABLE IF NOT EXISTS paper_broker_submissions (
    intent_id TEXT PRIMARY KEY REFERENCES paper_execution_intents(intent_id),
    client_order_id TEXT NOT NULL UNIQUE,
    broker_order_id TEXT,
    submission_status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Milestone 4 Step 10: account-level reconciliation (broker cash/equity vs
-- the internal paper ledger).
CREATE TABLE IF NOT EXISTS paper_account_reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    broker_cash TEXT NOT NULL,
    ledger_cash TEXT NOT NULL,
    difference TEXT NOT NULL,
    tolerance TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    broker_as_of TEXT NOT NULL,
    reconciled_at TEXT NOT NULL
);

-- Milestone 4 Step 10: per-symbol position-level reconciliation (broker
-- position quantity vs the internal paper ledger's position).
CREATE TABLE IF NOT EXISTS paper_position_reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    broker_quantity TEXT NOT NULL,
    ledger_quantity TEXT NOT NULL,
    tolerance TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    broker_as_of TEXT NOT NULL,
    reconciled_at TEXT NOT NULL
);
"""

EXECUTION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_paper_execution_intents_rec ON paper_execution_intents(recommendation_id);
CREATE INDEX IF NOT EXISTS idx_paper_execution_events_intent ON paper_execution_events(intent_id);
CREATE INDEX IF NOT EXISTS idx_paper_execution_failures_rec ON paper_execution_failures(recommendation_id);
CREATE INDEX IF NOT EXISTS idx_paper_broker_submissions_status ON paper_broker_submissions(submission_status);
"""


def apply_execution_schema(conn) -> None:
    conn.executescript(EXECUTION_DDL)
    conn.executescript(EXECUTION_INDEXES)
    conn.commit()
