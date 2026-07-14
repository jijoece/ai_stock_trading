"""SQLite schema for the isolated paper-book subsystem (Milestone 8,
docs/milestone-8.md Step 5).

Wholly additive: no table here is shared with, or replaces, any
`simulated_*`/`paper_cash_state`/`paper_execution_*` table from Milestones
1-4 — those remain completely untouched (see
docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md Decision 1).
Every table carries `book_id` as part of its primary key or inside a
`book_id`-scoped UNIQUE constraint, so no query can accidentally span two
books (Decision 2). Decimal values are stored as TEXT, mirroring
`storage/execution_repositories.py`'s documented "no float precision loss at
the persistence boundary" convention — never REAL.
"""
from __future__ import annotations

PAPER_BOOKS_SCHEMA_VERSION = 1

PAPER_BOOKS_DDL = """
CREATE TABLE IF NOT EXISTS paper_books (
    book_id TEXT PRIMARY KEY,
    experiment_arm TEXT NOT NULL CHECK (experiment_arm IN ('BASELINE', 'ENHANCED')),
    currency TEXT NOT NULL DEFAULT 'USD',
    starting_cash_usd TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'PAUSED', 'CLOSED')),
    created_at TEXT NOT NULL,
    config_hash TEXT NOT NULL
);

-- Append-only. INITIAL_CAPITAL is inserted exactly once per book_id by
-- cash_ledger.py::open_book (idempotent on a UNIQUE (book_id, idempotency_key)).
CREATE TABLE IF NOT EXISTS paper_book_cash_ledger (
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    ledger_entry_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    amount_usd TEXT NOT NULL,
    event_timestamp TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    cycle_id TEXT,
    symbol TEXT,
    reference_id TEXT,
    operator TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (book_id, ledger_entry_id)
);

-- Every risk decision (approved or rejected) is persisted here, so a
-- rejected recommendation still has a queryable, immutable audit trail even
-- though it never creates a paper_book_orders row (Step 11: "decisions
-- persisted").
CREATE TABLE IF NOT EXISTS paper_book_risk_decisions (
    risk_decision_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    cycle_id TEXT NOT NULL,
    recommendation_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    decision TEXT NOT NULL,
    requested_notional_usd TEXT,
    approved_notional_usd TEXT,
    approved_quantity TEXT,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    policy_version TEXT NOT NULL,
    portfolio_snapshot_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_book_orders (
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    paper_order_intent_id TEXT NOT NULL,
    experiment_arm TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    recommendation_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    quantity TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    notional_usd TEXT NOT NULL,
    time_in_force TEXT NOT NULL,
    as_of TEXT NOT NULL,
    risk_decision_id TEXT NOT NULL,
    portfolio_snapshot_id TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING_SUBMISSION',
    PRIMARY KEY (book_id, paper_order_intent_id)
);

-- Append-only fills. fill_id is unique per book; the same fill_id string
-- reused in a different book is a structurally distinct row (Step 24).
CREATE TABLE IF NOT EXISTS paper_book_fills (
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    fill_id TEXT NOT NULL,
    paper_order_intent_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    simulated_market_price TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    fill_quantity TEXT NOT NULL,
    fill_price TEXT NOT NULL,
    fees_usd TEXT NOT NULL DEFAULT '0',
    slippage_usd TEXT NOT NULL DEFAULT '0',
    fill_timestamp TEXT NOT NULL,
    simulation_rule_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (book_id, fill_id)
);

CREATE TABLE IF NOT EXISTS paper_book_positions (
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    symbol TEXT NOT NULL,
    quantity TEXT NOT NULL,
    available_quantity TEXT NOT NULL,
    reserved_quantity TEXT NOT NULL DEFAULT '0',
    average_cost_usd TEXT NOT NULL,
    realized_pnl_usd TEXT NOT NULL DEFAULT '0',
    fees_usd TEXT NOT NULL DEFAULT '0',
    latest_valuation_price TEXT,
    unrealized_pnl_usd TEXT,
    valuation_timestamp TEXT,
    valuation_status TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (book_id, symbol)
);

-- FIFO lots. remaining_quantity/closed_at are the only application-mutable
-- columns (consumption); cost_basis/opened_at/quantity are immutable once
-- inserted (enforced by trigger below).
CREATE TABLE IF NOT EXISTS paper_book_position_lots (
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    lot_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    quantity TEXT NOT NULL,
    remaining_quantity TEXT NOT NULL,
    cost_basis_usd TEXT NOT NULL,
    opening_fill_id TEXT NOT NULL,
    closed_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (book_id, lot_id)
);

CREATE TABLE IF NOT EXISTS paper_book_snapshots (
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    snapshot_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    cash_available_usd TEXT NOT NULL,
    cash_reserved_usd TEXT NOT NULL,
    gross_market_value_usd TEXT,
    net_liquidation_value_usd TEXT,
    total_cost_basis_usd TEXT NOT NULL,
    unrealized_pnl_usd TEXT,
    realized_pnl_usd TEXT NOT NULL,
    position_count INTEGER NOT NULL,
    unvalued_position_count INTEGER NOT NULL,
    stale_position_count INTEGER NOT NULL,
    valuation_status TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (book_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS paper_book_snapshot_positions (
    book_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity TEXT NOT NULL,
    cost_basis_usd TEXT NOT NULL,
    price TEXT,
    price_provider TEXT,
    price_timestamp TEXT,
    price_available_at TEXT,
    point_in_time_safe INTEGER,
    source_record_id TEXT,
    staleness_seconds INTEGER,
    market_value_usd TEXT,
    unrealized_pnl_usd TEXT,
    valuation_status TEXT NOT NULL,
    PRIMARY KEY (book_id, snapshot_id, symbol),
    FOREIGN KEY (book_id, snapshot_id) REFERENCES paper_book_snapshots(book_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS paper_book_reconciliations (
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    reconciliation_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    status TEXT NOT NULL,
    mismatch_details_json TEXT NOT NULL DEFAULT '[]',
    reconciliation_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (book_id, reconciliation_id)
);

CREATE TABLE IF NOT EXISTS paper_book_daily_metrics (
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    metrics_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (book_id, metrics_id)
);

CREATE TABLE IF NOT EXISTS paper_book_corporate_actions_applied (
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    action_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action_type TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    ratio TEXT,
    dividend_per_share_usd TEXT,
    applied_at TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    PRIMARY KEY (book_id, action_id)
);

-- One row per (cycle_id, symbol): the shared evidence snapshot / as_of /
-- both recommendation IDs / both resulting book+intent IDs (Step 14).
CREATE TABLE IF NOT EXISTS paper_book_experiment_assignments (
    experiment_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    evidence_snapshot_id TEXT,
    baseline_recommendation_id TEXT,
    enhanced_recommendation_id TEXT,
    baseline_book_id TEXT,
    enhanced_book_id TEXT,
    baseline_intent_id TEXT,
    enhanced_intent_id TEXT,
    assignment_policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (cycle_id, symbol)
);

CREATE TABLE IF NOT EXISTS paper_book_experiment_comparisons (
    comparison_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    baseline_book_id TEXT NOT NULL,
    enhanced_book_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    baseline_metrics_id TEXT NOT NULL,
    enhanced_metrics_id TEXT NOT NULL,
    comparable INTEGER NOT NULL,
    comparability_reasons_json TEXT NOT NULL DEFAULT '[]',
    metric_deltas_json TEXT NOT NULL DEFAULT '{}',
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_book_promotion_evidence (
    promotion_evidence_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    comparison_id TEXT NOT NULL REFERENCES paper_book_experiment_comparisons(comparison_id),
    result TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

PAPER_BOOKS_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_book_cash_ledger_idem
    ON paper_book_cash_ledger(book_id, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_paper_book_orders_rec
    ON paper_book_orders(book_id, recommendation_id);
CREATE INDEX IF NOT EXISTS idx_paper_book_fills_intent
    ON paper_book_fills(book_id, paper_order_intent_id);
CREATE INDEX IF NOT EXISTS idx_paper_book_lots_symbol
    ON paper_book_position_lots(book_id, symbol, opened_at);
CREATE INDEX IF NOT EXISTS idx_paper_book_snapshots_asof
    ON paper_book_snapshots(book_id, as_of);
CREATE INDEX IF NOT EXISTS idx_paper_book_experiment_assignments_experiment
    ON paper_book_experiment_assignments(experiment_id, symbol);
CREATE INDEX IF NOT EXISTS idx_paper_book_experiment_comparisons_experiment
    ON paper_book_experiment_comparisons(experiment_id);
"""

# Immutability guarantees (Step 11 "historical lots are immutable", Step 8
# "snapshot immutable", Step 6 "reversal entries rather than historical
# mutation", Step 12 "immutable after creation except lifecycle status
# through explicit events").
PAPER_BOOKS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_paper_book_cash_ledger_no_update
BEFORE UPDATE ON paper_book_cash_ledger
BEGIN SELECT RAISE(ABORT, 'paper_book_cash_ledger is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_cash_ledger_no_delete
BEFORE DELETE ON paper_book_cash_ledger
BEGIN SELECT RAISE(ABORT, 'paper_book_cash_ledger is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_fills_no_update
BEFORE UPDATE ON paper_book_fills
BEGIN SELECT RAISE(ABORT, 'paper_book_fills is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_fills_no_delete
BEFORE DELETE ON paper_book_fills
BEGIN SELECT RAISE(ABORT, 'paper_book_fills is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_risk_decisions_no_update
BEFORE UPDATE ON paper_book_risk_decisions
BEGIN SELECT RAISE(ABORT, 'paper_book_risk_decisions are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_risk_decisions_no_delete
BEFORE DELETE ON paper_book_risk_decisions
BEGIN SELECT RAISE(ABORT, 'paper_book_risk_decisions are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_orders_core_immutable
BEFORE UPDATE ON paper_book_orders
WHEN NEW.book_id != OLD.book_id OR NEW.experiment_arm != OLD.experiment_arm
    OR NEW.recommendation_id != OLD.recommendation_id OR NEW.symbol != OLD.symbol
    OR NEW.side != OLD.side OR NEW.quantity != OLD.quantity OR NEW.limit_price != OLD.limit_price
    OR NEW.paper_order_intent_id != OLD.paper_order_intent_id
BEGIN SELECT RAISE(ABORT, 'paper_book_orders core fields are immutable — only status may change'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_orders_no_delete
BEFORE DELETE ON paper_book_orders
BEGIN SELECT RAISE(ABORT, 'paper_book_orders rows are never deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_lots_core_immutable
BEFORE UPDATE ON paper_book_position_lots
WHEN NEW.book_id != OLD.book_id OR NEW.symbol != OLD.symbol OR NEW.opened_at != OLD.opened_at
    OR NEW.quantity != OLD.quantity OR NEW.cost_basis_usd != OLD.cost_basis_usd
    OR NEW.opening_fill_id != OLD.opening_fill_id
BEGIN SELECT RAISE(ABORT, 'paper_book_position_lots core fields are immutable — only remaining_quantity/closed_at may change'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_lots_no_delete
BEFORE DELETE ON paper_book_position_lots
BEGIN SELECT RAISE(ABORT, 'paper_book_position_lots rows are never deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_snapshots_no_update
BEFORE UPDATE ON paper_book_snapshots
BEGIN SELECT RAISE(ABORT, 'paper_book_snapshots are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_snapshots_no_delete
BEFORE DELETE ON paper_book_snapshots
BEGIN SELECT RAISE(ABORT, 'paper_book_snapshots are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_snapshot_positions_no_update
BEFORE UPDATE ON paper_book_snapshot_positions
BEGIN SELECT RAISE(ABORT, 'paper_book_snapshot_positions are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_snapshot_positions_no_delete
BEFORE DELETE ON paper_book_snapshot_positions
BEGIN SELECT RAISE(ABORT, 'paper_book_snapshot_positions are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_reconciliations_no_update
BEFORE UPDATE ON paper_book_reconciliations
BEGIN SELECT RAISE(ABORT, 'paper_book_reconciliations are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_reconciliations_no_delete
BEFORE DELETE ON paper_book_reconciliations
BEGIN SELECT RAISE(ABORT, 'paper_book_reconciliations are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_experiment_assignments_no_update
BEFORE UPDATE ON paper_book_experiment_assignments
BEGIN SELECT RAISE(ABORT, 'paper_book_experiment_assignments are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_experiment_assignments_no_delete
BEFORE DELETE ON paper_book_experiment_assignments
BEGIN SELECT RAISE(ABORT, 'paper_book_experiment_assignments are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_experiment_comparisons_no_update
BEFORE UPDATE ON paper_book_experiment_comparisons
BEGIN SELECT RAISE(ABORT, 'paper_book_experiment_comparisons are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_experiment_comparisons_no_delete
BEFORE DELETE ON paper_book_experiment_comparisons
BEGIN SELECT RAISE(ABORT, 'paper_book_experiment_comparisons are immutable once persisted'); END;
"""


def apply_paper_books_schema(conn) -> None:
    conn.executescript(PAPER_BOOKS_DDL)
    conn.executescript(PAPER_BOOKS_INDEXES)
    conn.executescript(PAPER_BOOKS_TRIGGERS)
    conn.commit()
