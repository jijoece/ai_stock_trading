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
    partial_stage_id INTEGER,
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

CREATE TABLE IF NOT EXISTS paper_soak_campaign_definition_dates (
    campaign_id TEXT NOT NULL REFERENCES paper_soak_campaigns(campaign_id),
    as_of TEXT NOT NULL,
    requested_cycle_ids_json TEXT NOT NULL DEFAULT '[]',
    lifecycle_only INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (campaign_id, as_of)
);

-- Milestone 9.3.1 separates the immutable definition above from resumable
-- execution attempts. Attempt headers transition RUNNING -> terminal; day
-- rows are append-only evidence and are never rewritten by continuation.
CREATE TABLE IF NOT EXISTS paper_soak_campaign_attempts (
    campaign_attempt_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES paper_soak_campaigns(campaign_id),
    manifest_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    previous_attempt_id TEXT REFERENCES paper_soak_campaign_attempts(campaign_attempt_id),
    attempt_number INTEGER NOT NULL,
    continue_after_blocker INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    first_blocking_date TEXT,
    first_blocking_status TEXT,
    failure_code TEXT,
    failure_stage TEXT,
    sanitized_message TEXT,
    operator TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (campaign_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS paper_soak_campaign_attempt_days (
    campaign_attempt_id TEXT NOT NULL REFERENCES paper_soak_campaign_attempts(campaign_attempt_id),
    campaign_id TEXT NOT NULL REFERENCES paper_soak_campaigns(campaign_id),
    as_of TEXT NOT NULL,
    requested_cycle_ids_json TEXT NOT NULL DEFAULT '[]',
    lifecycle_only INTEGER NOT NULL DEFAULT 0,
    operator_run_id TEXT,
    lifecycle_run_id TEXT,
    cross_book_verification_id TEXT,
    cross_book_verification_status TEXT,
    controlled_readiness_status TEXT NOT NULL,
    all_failed_checks_json TEXT NOT NULL DEFAULT '[]',
    failure_codes_json TEXT NOT NULL DEFAULT '[]',
    failure_reasons_json TEXT NOT NULL DEFAULT '[]',
    day_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (campaign_attempt_id, as_of)
);

CREATE TABLE IF NOT EXISTS paper_soak_activation_reviews (
    activation_review_id TEXT PRIMARY KEY,
    activation_review_scope_id TEXT,
    campaign_id TEXT NOT NULL,
    campaign_attempt_id TEXT,
    campaign_manifest_hash TEXT NOT NULL,
    config_hash TEXT,
    evidence_state_hash TEXT,
    supersedes_activation_review_id TEXT,
    campaign_start_as_of TEXT,
    campaign_end_as_of TEXT,
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

-- Milestone 10: append-only human activation audit. Current state is
-- derived from the latest event; configuration never writes this table.
CREATE TABLE IF NOT EXISTS paper_recurring_activation_events (
    activation_event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    previous_state TEXT NOT NULL,
    new_state TEXT NOT NULL,
    activation_review_id TEXT,
    campaign_id TEXT,
    request_event_id TEXT,
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_schedule_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    policy_version TEXT NOT NULL
);

-- Explicit bounded queue. The frozen-state hash detects changes to a cycle's
-- persisted recommendation/evidence linkage after enqueue.
CREATE TABLE IF NOT EXISTS paper_recurring_cycle_queue (
    queue_item_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    status TEXT NOT NULL,
    frozen_state_hash TEXT NOT NULL,
    retry_of_queue_item_id TEXT,
    enqueued_by TEXT NOT NULL,
    enqueue_reason TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    claimed_by_run_id TEXT,
    claimed_at TEXT,
    processed_operator_run_id TEXT,
    processed_at TEXT,
    failure_reason TEXT,
    cancelled_by TEXT,
    cancel_reason TEXT,
    cancelled_at TEXT,
    created_at TEXT NOT NULL
);

-- Paper-specific singleton lease. It deliberately has no execution or book
-- fields: acquiring it alone cannot mutate a paper portfolio.
CREATE TABLE IF NOT EXISTS paper_recurring_scheduler_leases (
    lease_name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    scheduler_run_id TEXT,
    status TEXT NOT NULL,
    released_at TEXT
);

-- A row starts as RUNNING for crash recovery and becomes immutable at its
-- first terminal status. The deterministic ID is derived from the intended
-- schedule identity.
CREATE TABLE IF NOT EXISTS paper_recurring_scheduler_runs (
    scheduler_run_id TEXT PRIMARY KEY,
    intended_schedule_id TEXT NOT NULL,
    intended_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    owner_id TEXT NOT NULL,
    lease_name TEXT NOT NULL,
    activation_event_id TEXT,
    activation_review_id TEXT,
    queue_item_ids_json TEXT NOT NULL DEFAULT '[]',
    requested_cycle_ids_json TEXT NOT NULL DEFAULT '[]',
    processed_cycle_ids_json TEXT NOT NULL DEFAULT '[]',
    operator_run_id TEXT,
    lifecycle_run_id TEXT,
    cross_book_verification_id TEXT,
    cross_book_verification_status TEXT,
    controlled_readiness_status TEXT,
    all_failed_checks_json TEXT NOT NULL DEFAULT '[]',
    lifecycle_only INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    failure_reasons_json TEXT NOT NULL DEFAULT '[]',
    config_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (intended_schedule_id)
);

-- Milestone 11: immutable manual external-paper evidence. None of these
-- tables contains credentials, authorization headers, raw account IDs, or
-- broker response bodies.
CREATE TABLE IF NOT EXISTS paper_external_order_previews (
    preview_id TEXT PRIMARY KEY,
    paper_order_intent_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    client_order_id TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    previewed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    operator TEXT NOT NULL,
    result TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    config_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_external_order_events (
    external_order_event_id TEXT PRIMARY KEY,
    external_order_scope_id TEXT NOT NULL,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    paper_order_intent_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    broker_order_id TEXT,
    account_fingerprint TEXT NOT NULL,
    previous_state TEXT NOT NULL,
    new_state TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    quantity TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    runtime_request_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    attempt_number INTEGER NOT NULL DEFAULT 0,
    scope_sequence INTEGER
);

CREATE TABLE IF NOT EXISTS paper_external_broker_fills (
    external_fill_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    paper_order_intent_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    broker_order_id TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    filled_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_external_order_lookups (
    lookup_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    paper_order_intent_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    result TEXT NOT NULL,
    authoritative INTEGER NOT NULL,
    runtime_request_id TEXT,
    created_at TEXT NOT NULL,
    attempt_number INTEGER,
    ambiguous_event_id TEXT,
    payload_hash TEXT,
    lookup_started_at TEXT,
    lookup_completed_at TEXT,
    consumed_by_retry_event_id TEXT
);

CREATE TABLE IF NOT EXISTS paper_external_reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    paper_order_intent_id TEXT,
    client_order_id TEXT,
    account_fingerprint TEXT,
    status TEXT NOT NULL,
    statuses_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    critical INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    config_hash TEXT NOT NULL
);

-- Milestone 23 Part A3: the isolated reconciliation boundary for one
-- externally-enabled book. Captured once, on the first successful external
-- reconciliation call for the book (idempotent insert-or-ignore), this row
-- freezes the local settled cash/positions and the broker cash/positions at
-- that instant. All later reconciliation compares *deltas* from this
-- baseline rather than raw totals, so pre-existing local-simulated state
-- that was never mirrored to the broker (e.g. an earlier local-only fill in
-- the same book) cancels out instead of surfacing as a false
-- CASH_MISMATCH/POSITION_MISMATCH.
CREATE TABLE IF NOT EXISTS paper_external_reconciliation_baseline (
    book_id TEXT PRIMARY KEY REFERENCES paper_books(book_id),
    snapshot_timestamp TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    local_settled_cash_usd TEXT NOT NULL,
    local_positions_json TEXT NOT NULL,
    broker_cash_usd TEXT NOT NULL,
    broker_positions_json TEXT NOT NULL,
    source_environment TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_external_submission_queue (
    queue_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    paper_order_intent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (book_id, paper_order_intent_id)
);

-- Milestone 11.1: append-only share-reservation evidence for external
-- closing SELL orders (Part 3). paper_book_positions.reserved_quantity is
-- the derived running total; these rows are the audit trail behind it.
CREATE TABLE IF NOT EXISTS paper_external_position_reservation_events (
    reservation_event_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    symbol TEXT NOT NULL,
    paper_order_intent_id TEXT NOT NULL,
    client_order_id TEXT,
    quantity TEXT NOT NULL,
    event_type TEXT NOT NULL,
    operator TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_external_order_leases (
    lease_key TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    client_order_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT,
    status TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1
);

-- Milestone 11.3.1 Item 6: durable per-intent execution-namespace claim.
-- Absence of a row means UNCLAIMED. A claim is immutable once the owning
-- execution path has created economically meaningful evidence (a reservation
-- or a broker preview) -- there is no release/abandonment path by design
-- (the simpler fail-closed policy: docs/milestone-11.3.1.md Item 6 Part B).
CREATE TABLE IF NOT EXISTS paper_order_execution_claims (
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    paper_order_intent_id TEXT NOT NULL,
    execution_namespace TEXT NOT NULL CHECK (execution_namespace IN ('LOCAL_SIMULATED', 'EXTERNAL_PAPER')),
    claim_generation INTEGER NOT NULL DEFAULT 1,
    claimed_at TEXT NOT NULL,
    claimed_by TEXT NOT NULL,
    PRIMARY KEY (book_id, paper_order_intent_id)
);

-- Milestone 13 advanced deterministic risk controls. All financial and
-- ratio columns are exact Decimal TEXT, never SQLite REAL.
CREATE TABLE IF NOT EXISTS paper_book_daily_risk_states (
    risk_state_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    market_date TEXT NOT NULL,
    as_of TEXT NOT NULL,
    start_of_day_equity TEXT NOT NULL,
    current_equity TEXT NOT NULL,
    realized_pnl_today TEXT NOT NULL,
    unrealized_pnl_today TEXT NOT NULL,
    total_pnl_today TEXT NOT NULL,
    net_external_cash_flow TEXT NOT NULL,
    daily_loss_fraction TEXT NOT NULL,
    historical_peak_equity TEXT NOT NULL,
    current_drawdown_fraction TEXT NOT NULL,
    valuation_status TEXT NOT NULL,
    source_snapshot_ids_json TEXT NOT NULL,
    reconciliation_status TEXT NOT NULL,
    calculation_policy_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (book_id, market_date, as_of, config_hash)
);

CREATE TABLE IF NOT EXISTS paper_book_position_lifecycle_states (
    lifecycle_state_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    symbol TEXT NOT NULL,
    originating_intent_id TEXT NOT NULL,
    entry_fill_id TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    original_quantity TEXT NOT NULL,
    remaining_quantity TEXT NOT NULL,
    average_entry_price TEXT NOT NULL,
    entry_atr TEXT NOT NULL,
    atr_period INTEGER NOT NULL,
    initial_stop_price TEXT NOT NULL,
    current_stop_price TEXT NOT NULL,
    initial_target_price TEXT NOT NULL,
    highest_eligible_price_since_entry TEXT NOT NULL,
    trailing_stop_active INTEGER NOT NULL,
    breakeven_active INTEGER NOT NULL,
    partial_profit_stage INTEGER NOT NULL,
    policy_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    last_evaluated_at TEXT NOT NULL,
    source_market_data_id TEXT NOT NULL,
    stop_change_reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_book_lifecycle_state_events (
    lifecycle_event_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    symbol TEXT NOT NULL,
    previous_state_id TEXT,
    resulting_state_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    complete INTEGER NOT NULL,
    reasons_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_book_partial_exit_stages (
    partial_stage_event_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    symbol TEXT NOT NULL,
    stage_id INTEGER NOT NULL,
    trigger_r_multiple TEXT NOT NULL,
    evaluated_price TEXT NOT NULL,
    quantity_before TEXT NOT NULL,
    quantity_requested TEXT NOT NULL,
    quantity_approved TEXT NOT NULL,
    quantity_filled TEXT NOT NULL,
    quantity_remaining TEXT NOT NULL,
    resulting_stop_state_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    lifecycle_evaluation_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (book_id, symbol, stage_id, lifecycle_evaluation_id)
);

CREATE TABLE IF NOT EXISTS economic_calendar_events (
    event_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    market TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    originally_published_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    importance TEXT NOT NULL,
    status TEXT NOT NULL,
    actual_value TEXT,
    forecast_value TEXT,
    previous_value TEXT,
    source_provider TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    point_in_time_safe INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (event_id, content_hash)
);

CREATE TABLE IF NOT EXISTS economic_blackout_decisions (
    blackout_decision_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    order_evaluation_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    matched_event_ids_json TEXT NOT NULL,
    blackout_start TEXT,
    blackout_end TEXT,
    reason_codes_json TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (book_id, order_evaluation_id, policy_version, configuration_hash)
);

CREATE TABLE IF NOT EXISTS paper_book_safety_events (
    safety_event_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    state TEXT NOT NULL CHECK (state IN ('PAUSED', 'RESUMED')),
    reason_code TEXT NOT NULL,
    source_risk_state_id TEXT,
    operator TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Milestone 24 Part A2: atomic, account-wide external-paper daily notional
-- reservation. Scoped by (account_fingerprint, reservation_date) rather than
-- book_id so the same Alpaca paper account cannot exceed
-- external_broker.maximum_daily_notional_usd by spreading submissions
-- across more than one local book. client_order_id is globally unique
-- (deterministically derived per order), so it alone is the primary key; a
-- duplicate submission for the same client_order_id reuses this row instead
-- of creating a second reservation.
-- DEPRECATED (Milestone 25 Part A2): legacy table kept for backward compatibility,
-- replaced by paper_external_attempt_reservations with attempt-level identity.
CREATE TABLE IF NOT EXISTS paper_external_daily_reservations (
    client_order_id TEXT PRIMARY KEY,
    account_fingerprint TEXT NOT NULL,
    reservation_date TEXT NOT NULL,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    reserved_notional_usd TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'SUBMITTED', 'RELEASED', 'RECONCILIATION_REQUIRED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Milestone 25 Part A2: attempt-scoped external-paper daily notional
-- reservation. Each submission attempt gets its own reservation identity
-- combining (client_order_id, attempt_number, account_fingerprint, reservation_date).
-- Supports cross-day retries where an old ambiguous reservation from July 19 may be
-- superseded by a NOT_FOUND lookup and a new reservation for July 20. A reservation
-- in any unresolved state (RESERVED, RECONCILIATION_REQUIRED) continues counting
-- against its reservation_date cap even if later retried on a different date.
CREATE TABLE IF NOT EXISTS paper_external_attempt_reservations (
    reservation_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    account_fingerprint TEXT NOT NULL,
    reservation_date TEXT NOT NULL,
    book_id TEXT NOT NULL REFERENCES paper_books(book_id),
    reserved_notional_usd TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'SUBMITTED', 'RELEASED', 'RECONCILIATION_REQUIRED', 'SUPERSEDED_BY_RETRY')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    backtest_run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    symbols_json TEXT NOT NULL,
    initial_cash TEXT NOT NULL,
    ending_equity TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    code_version TEXT NOT NULL,
    ambiguity_policy TEXT NOT NULL,
    status TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_daily_states (
    backtest_run_id TEXT NOT NULL REFERENCES backtest_runs(backtest_run_id),
    market_date TEXT NOT NULL,
    cash TEXT NOT NULL,
    equity TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    drawdown_fraction TEXT NOT NULL,
    state_json TEXT NOT NULL,
    PRIMARY KEY (backtest_run_id, market_date)
);

CREATE TABLE IF NOT EXISTS backtest_orders (
    backtest_run_id TEXT NOT NULL REFERENCES backtest_runs(backtest_run_id),
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    eligible_date TEXT NOT NULL,
    status TEXT NOT NULL,
    rejection_reason TEXT,
    PRIMARY KEY (backtest_run_id, order_id)
);

CREATE TABLE IF NOT EXISTS backtest_fills (
    backtest_run_id TEXT NOT NULL REFERENCES backtest_runs(backtest_run_id),
    fill_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL,
    fill_price TEXT NOT NULL,
    fees TEXT NOT NULL,
    slippage TEXT NOT NULL,
    market_date TEXT NOT NULL,
    exit_reason TEXT,
    PRIMARY KEY (backtest_run_id, fill_id)
);

CREATE TABLE IF NOT EXISTS backtest_metrics (
    backtest_run_id TEXT PRIMARY KEY REFERENCES backtest_runs(backtest_run_id),
    metrics_json TEXT NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_paper_soak_attempts_campaign
    ON paper_soak_campaign_attempts(campaign_id, attempt_number);

CREATE INDEX IF NOT EXISTS idx_paper_soak_attempt_days_campaign
    ON paper_soak_campaign_attempt_days(campaign_id, as_of, campaign_attempt_id);

CREATE INDEX IF NOT EXISTS idx_paper_soak_reviews_latest
    ON paper_soak_activation_reviews(campaign_id, created_at, activation_review_id);
CREATE INDEX IF NOT EXISTS idx_paper_recurring_activation_created
    ON paper_recurring_activation_events(created_at, activation_event_id);
CREATE INDEX IF NOT EXISTS idx_paper_recurring_queue_order
    ON paper_recurring_cycle_queue(status, enqueued_at, queue_item_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_recurring_queue_active_cycle
    ON paper_recurring_cycle_queue(cycle_id)
    WHERE status IN ('QUEUED', 'CLAIMED');
CREATE INDEX IF NOT EXISTS idx_paper_recurring_runs_schedule
    ON paper_recurring_scheduler_runs(intended_schedule_id, status);
CREATE INDEX IF NOT EXISTS idx_paper_external_events_client
    ON paper_external_order_events(book_id, client_order_id, created_at, external_order_event_id);
-- Part 7: no two events may claim the same previous_state as concurrent
-- children -- a monotonic per-scope sequence number with this uniqueness
-- constraint rejects a forked chain at the database level even if the
-- order-scope lease (Part 6) were somehow bypassed. NULLs (pre-upgrade rows)
-- are not compared for uniqueness by SQLite, so this is safe on upgrade.
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_external_events_scope_sequence
    ON paper_external_order_events(book_id, client_order_id, scope_sequence);
CREATE INDEX IF NOT EXISTS idx_paper_external_previews_intent
    ON paper_external_order_previews(book_id, paper_order_intent_id, previewed_at);
CREATE INDEX IF NOT EXISTS idx_paper_external_fills_order
    ON paper_external_broker_fills(book_id, client_order_id, filled_at);
CREATE INDEX IF NOT EXISTS idx_paper_external_reconciliations_order
    ON paper_external_reconciliations(book_id, client_order_id, created_at);
CREATE INDEX IF NOT EXISTS idx_paper_external_reservation_events_intent
    ON paper_external_position_reservation_events(book_id, paper_order_intent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_paper_external_reservation_events_symbol
    ON paper_external_position_reservation_events(book_id, symbol, created_at);
CREATE INDEX IF NOT EXISTS idx_paper_external_order_leases_client_order
    ON paper_external_order_leases(book_id, client_order_id);
CREATE INDEX IF NOT EXISTS idx_daily_risk_book_asof
    ON paper_book_daily_risk_states(book_id, as_of);
CREATE INDEX IF NOT EXISTS idx_lifecycle_state_book_symbol
    ON paper_book_position_lifecycle_states(book_id, symbol, last_evaluated_at);
CREATE INDEX IF NOT EXISTS idx_partial_stage_book_symbol
    ON paper_book_partial_exit_stages(book_id, symbol, stage_id, created_at);
CREATE INDEX IF NOT EXISTS idx_economic_events_schedule
    ON economic_calendar_events(market, scheduled_at, available_at);
CREATE INDEX IF NOT EXISTS idx_safety_events_book
    ON paper_book_safety_events(book_id, created_at);
CREATE INDEX IF NOT EXISTS idx_paper_external_daily_reservations_scope
    ON paper_external_daily_reservations(account_fingerprint, reservation_date, state);
CREATE INDEX IF NOT EXISTS idx_paper_external_attempt_reservations_scope
    ON paper_external_attempt_reservations(account_fingerprint, reservation_date, state);
CREATE INDEX IF NOT EXISTS idx_paper_external_attempt_reservations_client_attempt
    ON paper_external_attempt_reservations(client_order_id, attempt_number);
CREATE INDEX IF NOT EXISTS idx_paper_external_attempt_reservations_id
    ON paper_external_attempt_reservations(reservation_id);
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

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_campaign_definition_dates_no_update
BEFORE UPDATE ON paper_soak_campaign_definition_dates
BEGIN SELECT RAISE(ABORT, 'paper_soak_campaign_definition_dates are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_campaign_definition_dates_no_delete
BEFORE DELETE ON paper_soak_campaign_definition_dates
BEGIN SELECT RAISE(ABORT, 'paper_soak_campaign_definition_dates are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_campaign_attempts_no_delete
BEFORE DELETE ON paper_soak_campaign_attempts
BEGIN SELECT RAISE(ABORT, 'paper_soak_campaign_attempts cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_campaign_attempt_days_no_update
BEFORE UPDATE ON paper_soak_campaign_attempt_days
BEGIN SELECT RAISE(ABORT, 'paper_soak_campaign_attempt_days are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_campaign_attempt_days_no_delete
BEFORE DELETE ON paper_soak_campaign_attempt_days
BEGIN SELECT RAISE(ABORT, 'paper_soak_campaign_attempt_days are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_activation_reviews_no_update
BEFORE UPDATE ON paper_soak_activation_reviews
BEGIN SELECT RAISE(ABORT, 'paper_soak_activation_reviews are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_soak_activation_reviews_no_delete
BEFORE DELETE ON paper_soak_activation_reviews
BEGIN SELECT RAISE(ABORT, 'paper_soak_activation_reviews are immutable once persisted'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_recurring_activation_events_no_update
BEFORE UPDATE ON paper_recurring_activation_events
BEGIN SELECT RAISE(ABORT, 'paper recurring activation events are append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_recurring_activation_events_no_delete
BEFORE DELETE ON paper_recurring_activation_events
BEGIN SELECT RAISE(ABORT, 'paper recurring activation events are append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_recurring_scheduler_runs_terminal_no_update
BEFORE UPDATE ON paper_recurring_scheduler_runs
WHEN OLD.status <> 'RUNNING'
BEGIN SELECT RAISE(ABORT, 'terminal paper recurring scheduler runs are immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_recurring_scheduler_runs_no_delete
BEFORE DELETE ON paper_recurring_scheduler_runs
BEGIN SELECT RAISE(ABORT, 'paper recurring scheduler runs are immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_external_previews_no_update
BEFORE UPDATE ON paper_external_order_previews
BEGIN SELECT RAISE(ABORT, 'paper external previews are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_external_previews_no_delete
BEFORE DELETE ON paper_external_order_previews
BEGIN SELECT RAISE(ABORT, 'paper external previews are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_external_events_no_update
BEFORE UPDATE ON paper_external_order_events
BEGIN SELECT RAISE(ABORT, 'paper external order events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_external_events_no_delete
BEFORE DELETE ON paper_external_order_events
BEGIN SELECT RAISE(ABORT, 'paper external order events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_external_fills_no_update
BEFORE UPDATE ON paper_external_broker_fills
BEGIN SELECT RAISE(ABORT, 'paper external broker fills are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_external_fills_no_delete
BEFORE DELETE ON paper_external_broker_fills
BEGIN SELECT RAISE(ABORT, 'paper external broker fills are immutable'); END;
-- Every field is immutable except the one-time transition of
-- consumed_by_retry_event_id from NULL to a retry event ID (Part 5: a
-- consumed lookup can never be reused to authorize another retry). This
-- trigger's *body* is versioned explicitly in _TRIGGER_UPGRADES below
-- (Milestone 11.2 Part 3/18) — CREATE TRIGGER IF NOT EXISTS alone cannot
-- change the behavior of a trigger that already exists under this name on
-- a database created by older code (e.g. a pre-11.2 "prohibit every
-- update" or the interim "only checks OLD.consumed_by_retry_event_id"
-- version), so upgrading here only affects brand-new databases.
CREATE TRIGGER IF NOT EXISTS trg_paper_external_lookups_no_update
BEFORE UPDATE ON paper_external_order_lookups
WHEN NOT (
    NEW.lookup_id IS OLD.lookup_id
    AND NEW.book_id IS OLD.book_id
    AND NEW.paper_order_intent_id IS OLD.paper_order_intent_id
    AND NEW.client_order_id IS OLD.client_order_id
    AND NEW.account_fingerprint IS OLD.account_fingerprint
    AND NEW.result IS OLD.result
    AND NEW.authoritative IS OLD.authoritative
    AND NEW.runtime_request_id IS OLD.runtime_request_id
    AND NEW.created_at IS OLD.created_at
    AND NEW.attempt_number IS OLD.attempt_number
    AND NEW.ambiguous_event_id IS OLD.ambiguous_event_id
    AND NEW.payload_hash IS OLD.payload_hash
    AND NEW.lookup_started_at IS OLD.lookup_started_at
    AND NEW.lookup_completed_at IS OLD.lookup_completed_at
    AND OLD.consumed_by_retry_event_id IS NULL
    AND NEW.consumed_by_retry_event_id IS NOT NULL
)
BEGIN SELECT RAISE(ABORT, 'paper external order lookup rows are immutable except a single NULL to non-NULL consumed_by_retry_event_id transition'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_external_lookups_no_delete
BEFORE DELETE ON paper_external_order_lookups
BEGIN SELECT RAISE(ABORT, 'paper external order lookups are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_external_reconciliations_no_update
BEFORE UPDATE ON paper_external_reconciliations
BEGIN SELECT RAISE(ABORT, 'paper external reconciliations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_external_reconciliations_no_delete
BEFORE DELETE ON paper_external_reconciliations
BEGIN SELECT RAISE(ABORT, 'paper external reconciliations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_external_queue_no_delete
BEFORE DELETE ON paper_external_submission_queue
BEGIN SELECT RAISE(ABORT, 'paper external submission queue is auditable'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_external_reservation_events_no_update
BEFORE UPDATE ON paper_external_position_reservation_events
BEGIN SELECT RAISE(ABORT, 'paper external position reservation events are append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_external_reservation_events_no_delete
BEFORE DELETE ON paper_external_position_reservation_events
BEGIN SELECT RAISE(ABORT, 'paper external position reservation events are append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_external_order_leases_no_delete
BEFORE DELETE ON paper_external_order_leases
BEGIN SELECT RAISE(ABORT, 'paper external order leases are auditable'); END;

CREATE TRIGGER IF NOT EXISTS trg_daily_risk_states_no_update
BEFORE UPDATE ON paper_book_daily_risk_states
BEGIN SELECT RAISE(ABORT, 'daily risk states are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_daily_risk_states_no_delete
BEFORE DELETE ON paper_book_daily_risk_states
BEGIN SELECT RAISE(ABORT, 'daily risk states are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_lifecycle_states_no_update
BEFORE UPDATE ON paper_book_position_lifecycle_states
BEGIN SELECT RAISE(ABORT, 'lifecycle states are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_lifecycle_states_no_delete
BEFORE DELETE ON paper_book_position_lifecycle_states
BEGIN SELECT RAISE(ABORT, 'lifecycle states are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_lifecycle_state_events_no_update
BEFORE UPDATE ON paper_book_lifecycle_state_events
BEGIN SELECT RAISE(ABORT, 'lifecycle state events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_partial_exit_stages_no_delete
BEFORE DELETE ON paper_book_partial_exit_stages
BEGIN SELECT RAISE(ABORT, 'partial exit stage evidence is auditable'); END;
CREATE TRIGGER IF NOT EXISTS trg_economic_events_no_update
BEFORE UPDATE ON economic_calendar_events
BEGIN SELECT RAISE(ABORT, 'economic calendar versions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_blackout_decisions_no_update
BEFORE UPDATE ON economic_blackout_decisions
BEGIN SELECT RAISE(ABORT, 'blackout decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_safety_events_no_update
BEFORE UPDATE ON paper_book_safety_events
BEGIN SELECT RAISE(ABORT, 'safety events are append-only'); END;

-- Milestone 24 Part A2: only state/updated_at may change on a reservation
-- (a RESERVED -> SUBMITTED/RELEASED/RECONCILIATION_REQUIRED transition);
-- every other field is frozen once the reservation is created.
CREATE TRIGGER IF NOT EXISTS trg_paper_external_daily_reservations_core_immutable
BEFORE UPDATE ON paper_external_daily_reservations
WHEN NEW.client_order_id != OLD.client_order_id OR NEW.account_fingerprint != OLD.account_fingerprint
    OR NEW.reservation_date != OLD.reservation_date OR NEW.book_id != OLD.book_id
    OR NEW.reserved_notional_usd != OLD.reserved_notional_usd OR NEW.created_at != OLD.created_at
BEGIN SELECT RAISE(ABORT, 'paper_external_daily_reservations core fields are immutable — only state/updated_at may change'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_external_daily_reservations_no_delete
BEFORE DELETE ON paper_external_daily_reservations
BEGIN SELECT RAISE(ABORT, 'paper_external_daily_reservations rows are never deleted'); END;

-- Milestone 25 Part A7: immutability for attempt-scoped reservations.
-- Only state/updated_at may change (RESERVED -> SUBMITTED/RELEASED/RECONCILIATION_REQUIRED/SUPERSEDED_BY_RETRY);
-- every other field is frozen once the reservation is created.
CREATE TRIGGER IF NOT EXISTS trg_paper_external_attempt_reservations_core_immutable
BEFORE UPDATE ON paper_external_attempt_reservations
WHEN NEW.reservation_id != OLD.reservation_id OR NEW.client_order_id != OLD.client_order_id
    OR NEW.attempt_number != OLD.attempt_number OR NEW.account_fingerprint != OLD.account_fingerprint
    OR NEW.reservation_date != OLD.reservation_date OR NEW.book_id != OLD.book_id
    OR NEW.reserved_notional_usd != OLD.reserved_notional_usd OR NEW.created_at != OLD.created_at
BEGIN SELECT RAISE(ABORT, 'paper_external_attempt_reservations core fields are immutable — only state/updated_at may change'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_external_attempt_reservations_no_delete
BEFORE DELETE ON paper_external_attempt_reservations
BEGIN SELECT RAISE(ABORT, 'paper_external_attempt_reservations rows are never deleted'); END;
"""

# Milestone 9.2: additive, nullable columns on the pre-existing
# `paper_soak_operator_runs` table — mirrors
# `shadow_alerts_schema.py::_ensure_columns`'s own upgrade pattern. Every
# pre-existing operator-run row predating this milestone simply reads back
# with both columns NULL, never fabricated.
_PAPER_BOOKS_COLUMN_UPGRADES = {
    "paper_book_exit_decisions": {
        "partial_stage_id": "INTEGER",
    },
    # Milestone 24 Part C1/C2: backtest identity must include the signal set,
    # not just the run configuration, and a strategy backtest must record
    # which single strategy version/config it ran so mixed-definition runs
    # are traceable after the fact.
    "backtest_runs": {
        "input_hash": "TEXT",
        "signal_set_hash": "TEXT",
        "strategy_id": "TEXT",
        "strategy_version": "TEXT",
        "strategy_config_hash": "TEXT",
    },
    "paper_soak_operator_runs": {
        "cross_book_verification_id": "TEXT",
        "cross_book_verification_status": "TEXT",
    },
    "paper_book_cross_book_verifications": {
        "verification_scope_id": "TEXT",
        "source_state_hash": "TEXT",
    },
    "paper_soak_activation_reviews": {
        "activation_review_scope_id": "TEXT",
        "campaign_attempt_id": "TEXT",
        "config_hash": "TEXT",
        "evidence_state_hash": "TEXT",
        "supersedes_activation_review_id": "TEXT",
        "campaign_start_as_of": "TEXT",
        "campaign_end_as_of": "TEXT",
    },
    "paper_recurring_cycle_queue": {
        "retry_of_queue_item_id": "TEXT",
    },
    "paper_external_order_events": {
        "scope_sequence": "INTEGER",
    },
    "paper_external_order_lookups": {
        "attempt_number": "INTEGER",
        "ambiguous_event_id": "TEXT",
        "payload_hash": "TEXT",
        "lookup_started_at": "TEXT",
        "lookup_completed_at": "TEXT",
        "consumed_by_retry_event_id": "TEXT",
    },
    # Milestone 11.2 Part 10: a fencing generation, incremented on every
    # acquisition (fresh or reclaimed-stale), so a stale owner's write after
    # a takeover can be rejected even if it still believes it holds the lease.
    "paper_external_order_leases": {
        "generation": "INTEGER NOT NULL DEFAULT 1",
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


# Milestone 11.2 Part 3/18: `CREATE TRIGGER IF NOT EXISTS` cannot change the
# *behavior* of a trigger that already exists under the same name on a
# database created by older code. Any trigger whose semantics change across
# a milestone must bump its version here so `_upgrade_triggers` explicitly
# drops the stale definition before the (versionless) CREATE statements in
# `PAPER_BOOKS_TRIGGERS` recreate it with current behavior.
_TRIGGER_VERSIONS = {
    # v1 (Milestone 11.1): `WHEN OLD.consumed_by_retry_event_id IS NOT NULL`
    #     — blocks re-consumption but otherwise permits arbitrary field
    #     changes on an unconsumed row.
    # v2 (Milestone 11.2 Part 18): permits only the single controlled
    #     NULL -> non-NULL `consumed_by_retry_event_id` transition; every
    #     other field, and any other kind of change to that field, aborts.
    "trg_paper_external_lookups_no_update": 2,
}


def _upgrade_triggers(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS paper_books_trigger_versions ("
        "trigger_name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    stored = {
        row[0]: row[1]
        for row in conn.execute("SELECT trigger_name, version FROM paper_books_trigger_versions").fetchall()
    }
    existing_triggers = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()
    }
    for name, current_version in _TRIGGER_VERSIONS.items():
        recorded = stored.get(name)
        if recorded is None:
            # Unversioned trigger predates this table: if it already exists
            # on disk it may carry stale (e.g. pre-Milestone-11.2) behavior,
            # so treat it as version 0 and force the drop/recreate below.
            recorded = 0 if name in existing_triggers else current_version
        if recorded < current_version:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute(
                "INSERT INTO paper_books_trigger_versions (trigger_name, version) VALUES (?, ?) "
                "ON CONFLICT(trigger_name) DO UPDATE SET version = excluded.version",
                (name, current_version),
            )


def apply_paper_books_schema(conn) -> None:
    conn.executescript(PAPER_BOOKS_DDL)
    _ensure_columns(conn)
    conn.executescript(PAPER_BOOKS_INDEXES)
    _upgrade_triggers(conn)
    conn.executescript(PAPER_BOOKS_TRIGGERS)
    conn.commit()
