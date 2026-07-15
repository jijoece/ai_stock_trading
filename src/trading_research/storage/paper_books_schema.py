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

-- Milestone 9: deterministic exit decisions (HOLD/EXIT_*/SKIPPED_*),
-- immutable once persisted. `exit_decision_id` is a deterministic hash of
-- (book_id, symbol, as_of, policy_version), so re-running the same
-- lifecycle date never creates a duplicate decision row.
CREATE TABLE IF NOT EXISTS paper_book_exit_decisions (
    exit_decision_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    decision TEXT NOT NULL,
    quantity TEXT NOT NULL,
    reference_price TEXT,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    policy_version TEXT NOT NULL,
    manual_exit_request_id TEXT,
    created_at TEXT NOT NULL
);

-- Milestone 9: explicit, audited manual exit requests (Section 3 "Manual
-- exit"). Immutable once persisted — a request is never edited, only
-- consumed (its own row never changes; consumption is observable via the
-- exit decision/order it produced). `idempotency_key` is UNIQUE so a
-- retried identical request never creates a second row.
CREATE TABLE IF NOT EXISTS paper_book_manual_exit_requests (
    manual_exit_request_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    symbol TEXT NOT NULL,
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Milestone 9: one row per manual, explicit `run_paper_book_lifecycle`
-- invocation — the persistent audit trail a soak session is evaluated
-- against. `lifecycle_run_id` is a deterministic hash of `as_of` (+
-- config_hash), so retrying the same lifecycle date resolves to the same
-- row (idempotent insert; the function's return value always reflects a
-- fresh recompute of the actual current state, per-operation idempotency
-- guards it independently).
CREATE TABLE IF NOT EXISTS paper_book_lifecycle_runs (
    lifecycle_run_id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    processed_cycle_ids_json TEXT NOT NULL DEFAULT '[]',
    books_processed_json TEXT NOT NULL DEFAULT '[]',
    pending_orders_filled INTEGER NOT NULL DEFAULT 0,
    pending_orders_expired INTEGER NOT NULL DEFAULT 0,
    exit_decisions_json TEXT NOT NULL DEFAULT '[]',
    exit_orders_created INTEGER NOT NULL DEFAULT 0,
    exit_orders_filled INTEGER NOT NULL DEFAULT 0,
    snapshot_ids_json TEXT NOT NULL DEFAULT '{}',
    reconciliation_statuses_json TEXT NOT NULL DEFAULT '{}',
    metrics_ids_json TEXT NOT NULL DEFAULT '{}',
    failure_reasons_json TEXT NOT NULL DEFAULT '[]',
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Milestone 9: bounded per-book/per-symbol outcome for one lifecycle run
-- (pending-order processing or exit evaluation) — the queryable audit trail
-- for "why did/didn't this symbol do anything on this lifecycle date."
-- No REFERENCES paper_book_lifecycle_runs here on purpose: per-symbol
-- results are written *during* processing, before the run-summary row that
-- describes them is written at the very end (Section 7 step 11), so a FK
-- would invert the natural write order.
CREATE TABLE IF NOT EXISTS paper_book_lifecycle_symbol_results (
    lifecycle_run_id TEXT NOT NULL,
    book_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    stage TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    exit_decision_id TEXT,
    paper_order_intent_id TEXT,
    fill_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (lifecycle_run_id, book_id, symbol, stage)
);

-- Milestone 9.1: one row per manual `paper-soak-run` operator invocation —
-- the bounded audit trail for the single combined
-- validate/integrate/lifecycle/reconcile/report/readiness workflow.
-- `operator_run_id` is a deterministic hash of `as_of` + the explicit
-- requested cycle IDs, so replaying the identical command resolves to the
-- same row (idempotent insert-or-ignore, mirroring
-- `paper_book_lifecycle_runs` above). Immutable once persisted — never
-- updated, never a raw model output, never a credential.
CREATE TABLE IF NOT EXISTS paper_soak_operator_runs (
    operator_run_id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    requested_cycle_ids_json TEXT NOT NULL DEFAULT '[]',
    lifecycle_run_id TEXT NOT NULL,
    baseline_reconciliation_status TEXT,
    enhanced_reconciliation_status TEXT,
    soak_report_status TEXT NOT NULL,
    controlled_readiness_status TEXT NOT NULL,
    failure_reasons_json TEXT NOT NULL DEFAULT '[]',
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Milestone 9.2 (Sections 5-7): one row per authoritative
-- `verify_cross_book_integrity` invocation. `verification_id` is a
-- deterministic hash of (as_of, operator_run_id, lifecycle_run_id,
-- policy_version) — mirrors `paper_soak_operator_runs.operator_run_id`'s own
-- hashing convention, so a replay for the identical inputs always resolves
-- to the same row (INSERT OR IGNORE, immutable). Absence of an exception is
-- never treated as a persisted pass — this row (status=PASSED) is the only
-- authoritative "clean" signal.
CREATE TABLE IF NOT EXISTS paper_book_cross_book_verifications (
    verification_id TEXT PRIMARY KEY,
    verification_scope_id TEXT,
    source_state_hash TEXT,
    as_of TEXT NOT NULL,
    operator_run_id TEXT,
    lifecycle_run_id TEXT,
    status TEXT NOT NULL,
    violation_count INTEGER NOT NULL DEFAULT 0,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Bounded per-check detail rows for one verification (Section 7). No FK on
-- verification_id for the same "written during processing" ordering reason
-- documented on paper_book_lifecycle_symbol_results above.
CREATE TABLE IF NOT EXISTS paper_book_cross_book_verification_checks (
    verification_id TEXT NOT NULL,
    check_name TEXT NOT NULL,
    status TEXT NOT NULL,
    observed TEXT,
    expected TEXT,
    source TEXT NOT NULL,
    reason TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (verification_id, check_name)
);

-- Milestone 9.3: immutable controlled-soak campaign evidence. Campaign
-- headers are written only when a manual run reaches a terminal result;
-- individual requested dates (including post-blocker skips) remain visible.
CREATE TABLE IF NOT EXISTS paper_soak_campaigns (
    campaign_id TEXT PRIMARY KEY,
    manifest_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    start_as_of TEXT NOT NULL,
    end_as_of TEXT NOT NULL,
    requested_date_count INTEGER NOT NULL,
    requested_cycle_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    first_blocking_date TEXT,
    first_blocking_status TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_soak_campaign_days (
    campaign_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    requested_cycle_ids_json TEXT NOT NULL DEFAULT '[]',
    operator_run_id TEXT,
    lifecycle_run_id TEXT,
    cross_book_verification_id TEXT,
    cross_book_verification_status TEXT,
    controlled_readiness_status TEXT NOT NULL,
    all_failed_checks_json TEXT NOT NULL DEFAULT '[]',
    failure_reasons_json TEXT NOT NULL DEFAULT '[]',
    day_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (campaign_id, as_of)
);

CREATE TABLE IF NOT EXISTS paper_soak_activation_reviews (
    activation_review_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    campaign_manifest_hash TEXT NOT NULL,
    completed_market_days INTEGER NOT NULL,
    completed_cycles INTEGER NOT NULL,
    provider_provenance_counts_json TEXT NOT NULL,
    provider_success_counts_json TEXT NOT NULL,
    cross_book_verification_history_json TEXT NOT NULL,
    reconciliation_history_json TEXT NOT NULL,
    valuation_history_json TEXT NOT NULL,
    alert_summary_json TEXT NOT NULL,
    pause_and_kill_summary_json TEXT NOT NULL,
    performance_metrics_json TEXT NOT NULL,
    comparison_id TEXT,
    promotion_evidence_status TEXT NOT NULL,
    controlled_readiness_history_json TEXT NOT NULL,
    final_recommendation TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_paper_book_exit_decisions_book_symbol
    ON paper_book_exit_decisions(book_id, symbol, as_of);
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_book_manual_exit_requests_idem
    ON paper_book_manual_exit_requests(book_id, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_paper_book_manual_exit_requests_book_symbol
    ON paper_book_manual_exit_requests(book_id, symbol, requested_at);
CREATE INDEX IF NOT EXISTS idx_paper_book_lifecycle_symbol_results_run
    ON paper_book_lifecycle_symbol_results(lifecycle_run_id, book_id);
CREATE INDEX IF NOT EXISTS idx_paper_soak_operator_runs_asof
    ON paper_soak_operator_runs(as_of);
CREATE INDEX IF NOT EXISTS idx_paper_book_cross_book_verifications_scope
    ON paper_book_cross_book_verifications(verification_scope_id, as_of, created_at);
CREATE INDEX IF NOT EXISTS idx_paper_soak_campaign_days_campaign
    ON paper_soak_campaign_days(campaign_id, as_of);
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

CREATE TRIGGER IF NOT EXISTS trg_paper_book_exit_decisions_no_update
BEFORE UPDATE ON paper_book_exit_decisions
BEGIN SELECT RAISE(ABORT, 'paper_book_exit_decisions are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_exit_decisions_no_delete
BEFORE DELETE ON paper_book_exit_decisions
BEGIN SELECT RAISE(ABORT, 'paper_book_exit_decisions are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_manual_exit_requests_no_update
BEFORE UPDATE ON paper_book_manual_exit_requests
BEGIN SELECT RAISE(ABORT, 'paper_book_manual_exit_requests are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_manual_exit_requests_no_delete
BEFORE DELETE ON paper_book_manual_exit_requests
BEGIN SELECT RAISE(ABORT, 'paper_book_manual_exit_requests are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_lifecycle_runs_no_update
BEFORE UPDATE ON paper_book_lifecycle_runs
BEGIN SELECT RAISE(ABORT, 'paper_book_lifecycle_runs are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_lifecycle_runs_no_delete
BEFORE DELETE ON paper_book_lifecycle_runs
BEGIN SELECT RAISE(ABORT, 'paper_book_lifecycle_runs are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_lifecycle_symbol_results_no_update
BEFORE UPDATE ON paper_book_lifecycle_symbol_results
BEGIN SELECT RAISE(ABORT, 'paper_book_lifecycle_symbol_results are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_lifecycle_symbol_results_no_delete
BEFORE DELETE ON paper_book_lifecycle_symbol_results
BEGIN SELECT RAISE(ABORT, 'paper_book_lifecycle_symbol_results are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_operator_runs_no_update
BEFORE UPDATE ON paper_soak_operator_runs
BEGIN SELECT RAISE(ABORT, 'paper_soak_operator_runs are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_operator_runs_no_delete
BEFORE DELETE ON paper_soak_operator_runs
BEGIN SELECT RAISE(ABORT, 'paper_soak_operator_runs are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_cross_book_verifications_no_update
BEFORE UPDATE ON paper_book_cross_book_verifications
BEGIN SELECT RAISE(ABORT, 'paper_book_cross_book_verifications are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_cross_book_verifications_no_delete
BEFORE DELETE ON paper_book_cross_book_verifications
BEGIN SELECT RAISE(ABORT, 'paper_book_cross_book_verifications are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_cross_book_verification_checks_no_update
BEFORE UPDATE ON paper_book_cross_book_verification_checks
BEGIN SELECT RAISE(ABORT, 'paper_book_cross_book_verification_checks are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_book_cross_book_verification_checks_no_delete
BEFORE DELETE ON paper_book_cross_book_verification_checks
BEGIN SELECT RAISE(ABORT, 'paper_book_cross_book_verification_checks are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_campaigns_no_update
BEFORE UPDATE ON paper_soak_campaigns
BEGIN SELECT RAISE(ABORT, 'paper_soak_campaigns are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_campaigns_no_delete
BEFORE DELETE ON paper_soak_campaigns
BEGIN SELECT RAISE(ABORT, 'paper_soak_campaigns are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_campaign_days_no_update
BEFORE UPDATE ON paper_soak_campaign_days
BEGIN SELECT RAISE(ABORT, 'paper_soak_campaign_days are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_campaign_days_no_delete
BEFORE DELETE ON paper_soak_campaign_days
BEGIN SELECT RAISE(ABORT, 'paper_soak_campaign_days are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_activation_reviews_no_update
BEFORE UPDATE ON paper_soak_activation_reviews
BEGIN SELECT RAISE(ABORT, 'paper_soak_activation_reviews are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_activation_reviews_no_delete
BEFORE DELETE ON paper_soak_activation_reviews
BEGIN SELECT RAISE(ABORT, 'paper_soak_activation_reviews are immutable once persisted'); END;
"""

# Milestone 9.2: additive, nullable columns on the pre-existing
# `paper_soak_operator_runs` table — mirrors
# `shadow_alerts_schema.py::_ensure_columns`'s own upgrade pattern. Every
# pre-existing operator-run row predating this milestone simply reads back
# with both columns NULL, never fabricated.
_PAPER_BOOKS_COLUMN_UPGRADES = {
    "paper_soak_operator_runs": {
        "cross_book_verification_id": "TEXT",
        "cross_book_verification_status": "TEXT",
    },
    "paper_book_cross_book_verifications": {
        "verification_scope_id": "TEXT",
        "source_state_hash": "TEXT",
    },
}


def _ensure_columns(conn) -> None:
    for table, columns in _PAPER_BOOKS_COLUMN_UPGRADES.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            continue  # table doesn't exist yet; CREATE handles it in full
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def apply_paper_books_schema(conn) -> None:
    conn.executescript(PAPER_BOOKS_DDL)
    _ensure_columns(conn)
    conn.executescript(PAPER_BOOKS_INDEXES)
    conn.executescript(PAPER_BOOKS_TRIGGERS)
    conn.commit()
