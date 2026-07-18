from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest


SCHEMA = """
CREATE TABLE research_cycles (
    cycle_id TEXT PRIMARY KEY, universe_id TEXT, as_of TEXT, configuration_hash TEXT,
    experiment_policy TEXT, provider_mode TEXT, status TEXT, started_at TEXT, completed_at TEXT
);
CREATE TABLE research_cycle_symbol_results (
    cycle_id TEXT, symbol TEXT, status TEXT, snapshot_id TEXT, research_run_id TEXT,
    experiment_id TEXT, baseline_recommendation_id TEXT, enhanced_recommendation_id TEXT,
    baseline_paper_submitted INTEGER, failure_reason TEXT, created_at TEXT, completed_at TEXT
);
CREATE TABLE recommendations (
    rec_id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, side TEXT, ts TEXT, price_at_rec REAL,
    score REAL, confidence TEXT, status TEXT, acted INTEGER, rationale_text TEXT,
    model_version TEXT, prompt_version TEXT, config_hash TEXT, git_sha TEXT, frozen INTEGER
);
CREATE TABLE research_cycle_symbol_evidence_status (
    cycle_id TEXT, symbol TEXT, snapshot_id TEXT, corporate_status_evidence_id TEXT,
    completeness_result_id TEXT, screening_completeness TEXT, research_completeness TEXT,
    blocking_categories_json TEXT, policy_version TEXT, created_at TEXT
);
CREATE TABLE research_committee_runs (
    research_run_id TEXT PRIMARY KEY, snapshot_id TEXT, provider TEXT, model_name TEXT,
    roles_json TEXT, run_mode TEXT, status TEXT, config_hash TEXT, created_at TEXT, completed_at TEXT
);
CREATE TABLE shadow_scheduler_runs (
    scheduler_run_id TEXT PRIMARY KEY, intended_schedule_id TEXT, scheduled_time TEXT,
    actual_start_at TEXT, actual_finish_at TEXT, cycle_id TEXT, configuration_hash TEXT,
    mode TEXT, lease_owner TEXT, lease_expires_at TEXT, status TEXT, pause_state TEXT,
    budget_reservation_id TEXT, budget_reserved_usd TEXT, budget_consumed_usd TEXT,
    symbols_attempted INTEGER DEFAULT 0, symbols_completed INTEGER DEFAULT 0,
    symbols_skipped INTEGER DEFAULT 0, provider_failures INTEGER DEFAULT 0,
    research_failures INTEGER DEFAULT 0, paper_submissions INTEGER DEFAULT 0,
    alert_count INTEGER DEFAULT 0, failure_reason TEXT, operator_action TEXT,
    deployment_source TEXT, created_at TEXT
);
CREATE TABLE paper_book_risk_decisions (
    risk_decision_id TEXT PRIMARY KEY, book_id TEXT, cycle_id TEXT, recommendation_id TEXT,
    symbol TEXT, decision TEXT, requested_notional_usd TEXT, approved_notional_usd TEXT,
    approved_quantity TEXT, reasons_json TEXT, policy_version TEXT,
    portfolio_snapshot_id TEXT, created_at TEXT
);
CREATE TABLE paper_book_orders (
    book_id TEXT, paper_order_intent_id TEXT, experiment_arm TEXT, cycle_id TEXT,
    recommendation_id TEXT, symbol TEXT, side TEXT, order_type TEXT, quantity TEXT,
    limit_price TEXT, notional_usd TEXT, time_in_force TEXT, as_of TEXT,
    risk_decision_id TEXT, portfolio_snapshot_id TEXT, config_hash TEXT,
    created_at TEXT, status TEXT
);
CREATE TABLE paper_book_fills (
    book_id TEXT, fill_id TEXT, paper_order_intent_id TEXT, symbol TEXT, side TEXT,
    simulated_market_price TEXT, limit_price TEXT, fill_quantity TEXT, fill_price TEXT,
    fees_usd TEXT, slippage_usd TEXT, fill_timestamp TEXT,
    simulation_rule_version TEXT, created_at TEXT
);
CREATE TABLE research_attempt_failures (
    failure_id TEXT PRIMARY KEY, research_run_id TEXT, code TEXT, stage TEXT, occurred_at TEXT
);
CREATE TABLE research_attempts (
    attempt_id TEXT PRIMARY KEY, research_run_id TEXT, success INTEGER,
    failure_code TEXT, failure_metadata_json TEXT, created_at TEXT
);
CREATE TABLE shadow_role_budget_checks (
    check_id TEXT PRIMARY KEY, cycle_id TEXT, symbol TEXT, decision TEXT, checked_at TEXT
);
CREATE TABLE research_decisions (
    decision_id TEXT PRIMARY KEY, research_run_id TEXT, payload_json TEXT
);
CREATE TABLE research_overlay_decisions (
    overlay_id TEXT PRIMARY KEY, research_decision_id TEXT, action TEXT,
    policy_version TEXT, payload_json TEXT, created_at TEXT
);
CREATE TABLE paper_book_snapshots (
    book_id TEXT, snapshot_id TEXT, as_of TEXT, cash_available_usd TEXT,
    cash_reserved_usd TEXT, gross_market_value_usd TEXT, net_liquidation_value_usd TEXT,
    total_cost_basis_usd TEXT, unrealized_pnl_usd TEXT, realized_pnl_usd TEXT,
    position_count INTEGER, unvalued_position_count INTEGER, stale_position_count INTEGER,
    valuation_status TEXT, source_hash TEXT, created_at TEXT
);
CREATE TABLE paper_books (
    book_id TEXT PRIMARY KEY, experiment_arm TEXT, currency TEXT, starting_cash_usd TEXT,
    status TEXT, created_at TEXT, config_hash TEXT
);
CREATE TABLE paper_book_positions (
    book_id TEXT, symbol TEXT, quantity TEXT, available_quantity TEXT,
    reserved_quantity TEXT, average_cost_usd TEXT, realized_pnl_usd TEXT,
    fees_usd TEXT, latest_valuation_price TEXT, unrealized_pnl_usd TEXT,
    valuation_timestamp TEXT, valuation_status TEXT, updated_at TEXT
);
CREATE TABLE paper_book_snapshot_positions (
    book_id TEXT, snapshot_id TEXT, symbol TEXT, quantity TEXT, cost_basis_usd TEXT,
    price TEXT, price_provider TEXT, price_timestamp TEXT, price_available_at TEXT,
    point_in_time_safe INTEGER, source_record_id TEXT, staleness_seconds INTEGER,
    market_value_usd TEXT, unrealized_pnl_usd TEXT, valuation_status TEXT
);
CREATE TABLE shadow_pause_state (
    id INTEGER PRIMARY KEY, state TEXT, is_current INTEGER, created_at TEXT
);
CREATE TABLE paper_recurring_activation_events (
    activation_event_id TEXT PRIMARY KEY, event_type TEXT, previous_state TEXT,
    new_state TEXT, activation_review_id TEXT, campaign_id TEXT, request_event_id TEXT,
    operator TEXT, reason TEXT, requested_schedule_json TEXT, created_at TEXT, policy_version TEXT
);
CREATE TABLE paper_recurring_scheduler_runs (
    scheduler_run_id TEXT PRIMARY KEY, intended_schedule_id TEXT, intended_at TEXT,
    started_at TEXT, ended_at TEXT, owner_id TEXT, lease_name TEXT, activation_event_id TEXT,
    activation_review_id TEXT, queue_item_ids_json TEXT, requested_cycle_ids_json TEXT,
    processed_cycle_ids_json TEXT, operator_run_id TEXT, lifecycle_run_id TEXT,
    cross_book_verification_id TEXT, cross_book_verification_status TEXT,
    controlled_readiness_status TEXT, all_failed_checks_json TEXT, lifecycle_only INTEGER,
    status TEXT, failure_reasons_json TEXT, config_hash TEXT, policy_version TEXT, created_at TEXT
);
CREATE TABLE evidence_provider_requests (
    request_id TEXT PRIMARY KEY, provider TEXT, operation TEXT, symbol TEXT,
    requested_as_of TEXT, retrieved_at TEXT, provider_response_timestamp TEXT,
    http_status INTEGER, content_hash TEXT, normalized_record_hash TEXT, cache_status TEXT,
    rate_limited INTEGER, retry_count INTEGER, latency_ms INTEGER, success INTEGER,
    error_code TEXT, retryable INTEGER, licensing_classification TEXT,
    raw_payload_stored INTEGER, raw_payload_json TEXT, correlation_mode TEXT,
    research_cycle_id TEXT, scheduler_run_id TEXT, research_run_id TEXT,
    symbol_attempt_id TEXT, provider_request_group_id TEXT,
    transport_failure_category TEXT, created_at TEXT
);
CREATE TABLE evidence_provider_health_snapshots (
    id INTEGER PRIMARY KEY, provider TEXT, window_start TEXT, window_end TEXT,
    total_requests INTEGER, success_count INTEGER, timeout_count INTEGER,
    rate_limited_count INTEGER, invalid_response_count INTEGER, cache_hit_count INTEGER,
    average_latency_ms REAL, p95_latency_ms REAL, status TEXT, created_at TEXT
);
CREATE TABLE research_cycle_provider_provenance (
    cycle_id TEXT, research_run_id TEXT, symbol TEXT, provider_category TEXT,
    provider_name TEXT, provider_mode TEXT, is_fixture INTEGER, is_real INTEGER,
    request_or_source_id TEXT, status TEXT, normalized_outcome TEXT,
    observed_at TEXT, classification_version TEXT, created_at TEXT
);
CREATE TABLE shadow_run_summaries (
    scheduler_run_id TEXT PRIMARY KEY, intended_schedule_id TEXT, policy_version TEXT,
    health_status TEXT, health_reasons_json TEXT, provider_success_rate REAL,
    evidence_completeness_rate REAL, claude_role_success_rate REAL, retry_rate REAL,
    retry_exhaustion_rate REAL, unsupported_claim_rate REAL, output_truncation_rate REAL,
    latency_seconds REAL, input_tokens INTEGER, output_tokens INTEGER, cost_usd TEXT,
    paper_reconciliation_mismatch INTEGER, duplicate_prevention_violation INTEGER,
    cycle_duration_seconds REAL, created_at TEXT
);
CREATE TABLE shadow_health_hysteresis_state (
    scope TEXT PRIMARY KEY, policy_version TEXT, policy_hash TEXT, decision TEXT,
    consecutive_failures INTEGER, consecutive_recoveries INTEGER,
    qualified_cycle_count INTEGER, failing_cycle_count INTEGER, window_start TEXT,
    window_end TEXT, last_cycle_id TEXT, last_evaluated_at TEXT,
    reasons_json TEXT, per_provider_metrics_json TEXT
);
CREATE TABLE shadow_budget_reservations (
    reservation_id TEXT PRIMARY KEY, idempotency_key TEXT, cycle_intent TEXT,
    reserved_estimated_cost_usd TEXT, reserved_input_tokens INTEGER,
    reserved_output_tokens INTEGER, reserved_latency_seconds INTEGER, status TEXT,
    consumed_cost_usd TEXT, consumed_input_tokens INTEGER, consumed_output_tokens INTEGER,
    consumed_latency_seconds INTEGER, emergency_margin_breached INTEGER,
    created_at TEXT, settled_at TEXT
);
CREATE TABLE paper_book_safety_events (
    safety_event_id TEXT PRIMARY KEY, book_id TEXT, state TEXT, reason_code TEXT,
    source_risk_state_id TEXT, operator TEXT, reason TEXT, created_at TEXT
);
"""


@pytest.fixture
def empty_dashboard_database(tmp_path: Path) -> Path:
    path = tmp_path / "empty-dashboard.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.close()
    return path


@pytest.fixture
def dashboard_database(tmp_path: Path) -> Path:
    path = tmp_path / "dashboard.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO research_cycles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("cycle-1", "universe-1", "2026-07-17T14:00:00+00:00", "hash", "SHADOW",
         "fixture", "COMPLETED", "2026-07-17T14:00:00+00:00", "2026-07-17T14:10:00+00:00"),
    )
    for symbol, rec_id, submitted, failure in (
        ("ABC", "rec-abc", 1, None),
        ("XYZ", "rec-xyz", 0, "Rejected by the persisted risk policy."),
    ):
        connection.execute(
            "INSERT INTO research_cycle_symbol_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("cycle-1", symbol, "COMPLETED", f"snapshot-{symbol.lower()}",
             f"run-{symbol.lower()}", "experiment-1", rec_id, None, submitted, failure,
             "2026-07-17T14:01:00+00:00", "2026-07-17T14:08:00+00:00"),
        )
        connection.execute(
            "INSERT INTO recommendations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec_id, "screen-1", symbol, "buy_candidate", "2026-07-17T14:02:00+00:00",
             100 if symbol == "ABC" else 50, 8.5 if symbol == "ABC" else 4.0,
             "HIGH" if symbol == "ABC" else "LOW", "frozen", 0, "not exposed", "v1", "v1",
             "hash", "sha", 1),
        )
        connection.execute(
            "INSERT INTO research_cycle_symbol_evidence_status VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("cycle-1", symbol, f"snapshot-{symbol.lower()}", None, None, "COMPLETE", "COMPLETE",
             "[]", "evidence-v1", "2026-07-17T14:03:00+00:00"),
        )
        connection.execute(
            "INSERT INTO research_committee_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"run-{symbol.lower()}", f"snapshot-{symbol.lower()}", "fixture", "fixture-model", "[]",
             "fixture", "COMPLETED", "hash", "2026-07-17T14:03:00+00:00",
             "2026-07-17T14:07:00+00:00"),
        )
        payload = {
            "bull_case": f"Structured bull case for {symbol}",
            "bear_case": f"Structured bear case for {symbol}",
            "catalysts": ["Earnings"],
            "risks": ["Execution"],
            "evidence_ids": [f"evidence-{symbol.lower()}"],
            "raw_response": "must never be displayed",
        }
        connection.execute(
            "INSERT INTO research_decisions VALUES (?, ?, ?)",
            (f"decision-{symbol.lower()}", f"run-{symbol.lower()}", json.dumps(payload)),
        )
        connection.execute(
            "INSERT INTO research_overlay_decisions VALUES (?, ?, ?, ?, ?, ?)",
            (f"overlay-{symbol.lower()}", f"decision-{symbol.lower()}", "KEEP_BASELINE",
             "overlay-v1", "{}", "2026-07-17T14:07:00+00:00"),
        )

    connection.execute("""
        INSERT INTO shadow_scheduler_runs (
            scheduler_run_id, intended_schedule_id, scheduled_time, actual_start_at,
            actual_finish_at, cycle_id, configuration_hash, mode, status,
            symbols_attempted, symbols_completed, symbols_skipped, provider_failures,
            research_failures, paper_submissions, deployment_source, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "scheduler-1", "schedule-1", "2026-07-17T14:00:00+00:00",
        "2026-07-17T14:00:00+00:00", "2026-07-17T14:11:00+00:00",
        "cycle-1", "hash", "SHADOW", "COMPLETED", 2, 2, 0, 0, 0, 1,
        "fixture", "2026-07-17T14:11:00+00:00",
    ))
    connection.execute(
        "INSERT INTO paper_book_risk_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("risk-abc", "book-1", "cycle-1", "rec-abc", "ABC", "APPROVED", "1000", "995", "9.95",
         '["POSITION_LIMIT_OK"]', "risk-v1", "portfolio-1", "2026-07-17T14:08:00+00:00"),
    )
    connection.execute(
        "INSERT INTO paper_book_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("book-1", "order-abc", "BASELINE", "cycle-1", "rec-abc", "ABC", "BUY", "LIMIT", "9.95",
         "99.50", "990.025", "DAY", "2026-07-17T14:08:00+00:00", "risk-abc", "portfolio-1",
         "hash", "2026-07-17T14:08:30+00:00", "FILLED"),
    )
    connection.execute(
        "INSERT INTO paper_book_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("book-1", "fill-abc", "order-abc", "ABC", "BUY", "99", "99.50", "9.95", "99.25", "0", "0",
         "2026-07-17T14:09:00+00:00", "sim-v1", "2026-07-17T14:09:00+00:00"),
    )
    connection.execute(
        "INSERT INTO paper_book_risk_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("risk-xyz", "book-1", "cycle-1", "rec-xyz", "XYZ", "REJECTED_MAX_OPEN_POSITIONS",
         "1000", None, None, '["MAX_OPEN_POSITIONS"]', "risk-v1", "portfolio-1",
         "2026-07-17T14:08:00+00:00"),
    )
    connection.execute(
        "INSERT INTO research_attempts (attempt_id, research_run_id, success, failure_metadata_json, created_at) VALUES (?, ?, ?, ?, ?)",
        ("attempt-xyz", "run-xyz", 0,
         '{"observed_value": "10", "threshold_value": "8", "raw_output": "hidden"}',
         "2026-07-17T14:06:00+00:00"),
    )
    connection.execute(
        "INSERT INTO paper_book_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("book-1", "portfolio-1", "2026-07-17T14:00:00+00:00", "9000", "500", "995", "9995",
         "990", "5", "25", 1, 0, 0, "COMPLETE", "source", "2026-07-17T14:12:00+00:00"),
    )
    connection.execute(
        "INSERT INTO paper_books VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("book-1", "BASELINE", "USD", "10000", "ACTIVE", "2026-07-01T00:00:00+00:00", "hash"),
    )
    connection.execute(
        "INSERT INTO paper_book_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("book-1", "ABC", "9.95", "9.95", "0", "99.50", "25", "0", "100", "5",
         "2026-07-17T14:12:00+00:00", "COMPLETE", "2026-07-17T14:12:00+00:00"),
    )
    connection.execute(
        "INSERT INTO paper_book_snapshot_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("book-1", "portfolio-1", "ABC", "9.95", "990", "100", "persisted-fixture-price",
         "2026-07-17T14:00:00+00:00", "2026-07-17T14:00:00+00:00", 1, "price-1", 0,
         "995", "5", "COMPLETE"),
    )
    connection.execute(
        "INSERT INTO shadow_pause_state VALUES (?, ?, ?, ?)",
        (1, "RUNNING", 1, "2026-07-17T14:12:00+00:00"),
    )
    connection.execute(
        "INSERT INTO paper_recurring_activation_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("activation-1", "ACTIVATED", "DISABLED", "ENABLED", None, None, None, "tester",
         "fixture", "{}", "2026-07-17T14:00:00+00:00", "paper-v1"),
    )
    connection.execute("""
        INSERT INTO paper_recurring_scheduler_runs (
            scheduler_run_id, intended_schedule_id, intended_at, started_at, ended_at,
            owner_id, lease_name, queue_item_ids_json, requested_cycle_ids_json,
            processed_cycle_ids_json, all_failed_checks_json, lifecycle_only, status,
            failure_reasons_json, config_hash, policy_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "paper-scheduler-1", "paper-schedule-1", "2026-07-17T15:00:00+00:00",
        "2026-07-17T15:00:00+00:00", "2026-07-17T15:05:00+00:00", "owner", "lease",
        "[]", '["cycle-1"]', '["cycle-1"]', "[]", 0, "COMPLETED", "[]", "hash",
        "paper-v1", "2026-07-17T15:00:00+00:00",
    ))
    for request_id, success, category, error_code, created_at in (
        ("provider-1", 1, "NONE", None, "2026-07-17T14:01:00+00:00"),
        ("provider-2", 0, "TIMEOUT", "ProviderTimeoutError", "2026-07-17T14:02:00+00:00"),
        ("provider-3", 1, "NONE", None, "2026-07-17T14:03:00+00:00"),
    ):
        connection.execute("""
            INSERT INTO evidence_provider_requests VALUES (
                ?, 'sec_edgar', 'filing', 'ABC', ?, ?, NULL, 200, NULL, NULL, 'MISS',
                0, 0, 50, ?, ?, 1, 'PUBLIC', 0, NULL, 'SCHEDULED', 'cycle-1',
                'scheduler-1', 'run-abc', NULL, NULL, ?, ?
            )
        """, (request_id, created_at, created_at, success, error_code, category, created_at))
    connection.execute(
        "INSERT INTO evidence_provider_health_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "sec_edgar", "2026-07-17T14:00:00+00:00", "2026-07-17T14:10:00+00:00",
         3, 2, 1, 0, 0, 0, 50.0, 50.0, "DEGRADED", "2026-07-17T14:10:00+00:00"),
    )
    connection.execute(
        "INSERT INTO research_cycle_provider_provenance VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("cycle-1", "run-abc", "ABC", "filings", "sec_edgar", "real", 0, 1,
         "provider-1", "SUCCESS", "COMPLETE", "2026-07-17T14:03:00+00:00", "v1",
         "2026-07-17T14:03:00+00:00"),
    )
    connection.execute(
        "INSERT INTO shadow_run_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("scheduler-1", "schedule-1", "health-v1", "HEALTHY", "[]", 0.9, 1.0, 1.0,
         0.1, 0.0, 0.0, 0.0, 10.0, 100, 50, "0.01", 0, 0, 600.0,
         "2026-07-17T14:11:00+00:00"),
    )
    connection.execute(
        "INSERT INTO shadow_health_hysteresis_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("default", "health-v1", "policy-hash", "HEALTHY", 0, 2, 3, 0,
         "2026-07-15T00:00:00+00:00", "2026-07-17T14:11:00+00:00", "cycle-1",
         "2026-07-17T14:11:00+00:00", "[]", "{}"),
    )
    connection.execute(
        "INSERT INTO shadow_budget_reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("budget-1", "idem-1", "cycle-1", "1", 100, 100, 60, "SETTLED", "0.01", 50,
         25, 10, 0, "2026-07-17T14:00:00+00:00", "2026-07-17T14:11:00+00:00"),
    )
    connection.execute(
        "INSERT INTO research_committee_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("run-codex-health", "snapshot-health", "codex", "gpt-5.4", "[]", "real",
         "FAILED", "hash", "2026-07-17T15:10:00+00:00", "2026-07-17T15:11:00+00:00"),
    )
    connection.execute(
        "INSERT INTO research_attempts (attempt_id, research_run_id, success, failure_code, failure_metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("attempt-codex-health", "run-codex-health", 0, "CODEX_NOT_AUTHENTICATED", "{}",
         "2026-07-17T15:10:00+00:00"),
    )
    connection.execute(
        "INSERT INTO research_attempt_failures VALUES (?, ?, ?, ?, ?)",
        ("failure-codex-health", "run-codex-health", "CODEX_NOT_AUTHENTICATED",
         "provider_request", "2026-07-17T15:10:00+00:00"),
    )
    connection.commit()
    connection.close()
    return path
