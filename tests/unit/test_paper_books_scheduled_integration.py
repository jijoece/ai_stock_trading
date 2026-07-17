"""Tests for paper_books/scheduled_integration.py (docs/milestone-8.1.md).

Builds "as if a real scheduled cycle already ran" state directly via the
same repository functions `research/scheduled_cycle.py` itself uses
(`SQLiteResearchCycleRepository`, `save_frozen_recommendation`,
`save_evidence_snapshot`, `save_symbol_evidence_status`) rather than driving
the full screener/scorer/research-committee pipeline — this proves the
integration module correctly reads real persisted scheduled-cycle records
without re-testing Milestone 1-6 screening logic (out of scope here).
Nothing here calls Claude, SEC, Alpaca, Reddit, or any network provider.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.paper_books.config import (
    ExecutionSection,
    ExternalBrokerSection,
    PaperBookDefinition,
    PaperBooksConfiguration,
    RiskSection,
    ScheduledIntegrationSection,
    ValuationSection,
)
from trading_research.paper_books.scheduled_integration import (
    MARKET_SIMULATION_SOURCE_OBSERVED,
    MARKET_SIMULATION_SOURCE_SIMULATED,
    OUTCOME_EXECUTED,
    OUTCOME_AWAITING_OPERATOR_EXTERNAL_SUBMISSION,
    OUTCOME_INTENT_CREATED_PENDING_FILL,
    OUTCOME_REJECTED_BY_RISK,
    OUTCOME_SKIPPED_BOOK_DISABLED,
    OUTCOME_SKIPPED_EVIDENCE_INCOMPLETE,
    OUTCOME_SKIPPED_POLICY,
    OUTCOME_SKIPPED_RECOMMENDATION_INVALID,
    OUTCOME_SKIPPED_RECOMMENDATION_MISSING,
    OUTCOME_SKIPPED_SNAPSHOT_MISMATCH,
    ScheduledIntegrationError,
    integrate_scheduled_cycle_into_paper_books,
)
from trading_research.recommendations.builder import (
    SIDE_BUY_CANDIDATE,
    SIDE_SCREENED_OUT,
    STATUS_ACTIVE,
    STATUS_ANALYSIS_INCOMPLETE,
)
from trading_research.research.evidence_completeness import (
    STATUS_COMPLETE_FOR_SCREENING,
    STATUS_MISSING_CRITICAL_MARKET_DATA,
)
from trading_research.research.models import EvidenceItem, EvidenceSnapshot, SourceRecord
from trading_research.research.scheduled_cycle import SymbolCycleResult
from trading_research.storage.database import connect
from trading_research.storage.research_cycle_repositories import (
    SQLiteResearchCycleRepository,
    save_symbol_evidence_status,
)
from trading_research.storage.research_repositories import save_evidence_snapshot
from trading_research.storage.trading_repositories import save_frozen_recommendation
from trading_research.recommendations.builder import FrozenRecommendation

AS_OF = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "scheduled_integration_test.db")
        yield c
        c.close()


def _config(
    *, enabled=True, scheduled_integration_enabled=True, baseline_enabled=True, enhanced_enabled=True,
    baseline_cash=Decimal("100000.00"), enhanced_cash=Decimal("100000.00"),
    max_position_weight=Decimal("0.50"), max_order_notional_usd=Decimal("100000.00"),
    external_book_ids=(),
) -> PaperBooksConfiguration:
    return PaperBooksConfiguration(
        version=1, enabled=enabled,
        baseline=PaperBookDefinition(enabled=baseline_enabled, book_id="BASELINE", starting_cash_usd=baseline_cash),
        enhanced=PaperBookDefinition(enabled=enhanced_enabled, book_id="ENHANCED", starting_cash_usd=enhanced_cash),
        execution=ExecutionSection(provider="local_simulated", allow_external_paper_broker=False, allow_live_broker=False),
        risk=RiskSection(
            max_position_weight=max_position_weight, max_order_notional_usd=max_order_notional_usd,
            max_daily_new_notional_usd=Decimal("100000.00"), minimum_cash_buffer_weight=Decimal("0.05"),
            max_open_positions=20, max_symbol_concentration_weight=Decimal("0.50"),
            reject_stale_market_price_seconds=900,
        ),
        valuation=ValuationSection(price_source="evidence_snapshot", maximum_price_age_seconds=900, missing_price_policy="MARK_UNVALUED"),
        scheduled_integration=ScheduledIntegrationSection(enabled=scheduled_integration_enabled),
        external_broker=ExternalBrokerSection(
            bool(external_book_ids), "alpaca_paper", bool(external_book_ids), tuple(external_book_ids), True, 300,
            Decimal("100000.00"), ("limit",), ("day",), 1,
        ),
        config_hash="test-config-hash", raw={},
    )


def _evidence_snapshot(symbol: str, close: Decimal, as_of: datetime, *, bid: Decimal | None = None, ask: Decimal | None = None) -> EvidenceSnapshot:
    source = SourceRecord(
        source_id=f"src-{symbol}", source_type="market", provider="fixture-market", source_locator=None,
        retrieved_at=as_of, published_at=as_of, effective_at=as_of, available_at=as_of, content_hash="hash",
        status="ok", is_stale=False, point_in_time_safe=True, error_code=None,
    )
    values = {"latest_close": float(close)}
    if bid is not None:
        values["bid"] = float(bid)
    if ask is not None:
        values["ask"] = float(ask)
    item = EvidenceItem(
        evidence_id=f"{symbol}-market", source_id=source.source_id, category="market", title="market",
        summary="market", normalized_values=values, as_of=as_of, confidence="high", stale=False,
    )
    return EvidenceSnapshot(
        snapshot_id=f"snap-{symbol}-{as_of.isoformat()}", symbol=symbol, as_of=as_of, created_at=as_of,
        source_records=(source,), evidence_items=(item,), deterministic_factors={}, sentiment_metrics={},
        portfolio_context=None, missing_data_reasons=(), conflict_reasons=(), point_in_time_safe=True,
        config_hash="cfg", git_sha="sha",
    )


def _rec_payload(rec_id: str, symbol: str, *, side=SIDE_BUY_CANDIDATE, status=STATUS_ACTIVE, ts=AS_OF, shares=10, entry_price=150.0) -> dict:
    payload = {
        "rec_id": rec_id, "run_id": f"run-{rec_id}", "symbol": symbol, "side": side, "ts": ts.isoformat(),
        "price_at_rec": entry_price, "score": 80.0, "confidence": "high", "status": status, "acted": False,
        "rationale_text": "test recommendation", "factors": [], "model_version": "test-v1", "prompt_version": "test-v1",
        "config_hash": "cfg", "git_sha": "sha",
    }
    if shares is not None:
        payload["risk_plan"] = {
            "shares": shares, "entry_price": entry_price, "stop_price": entry_price * 0.9,
            "target_price": entry_price * 1.2, "risk_per_share": entry_price * 0.1,
            "dollars_at_risk": shares * entry_price * 0.1, "position_value": shares * entry_price,
            "reward_risk": 2.0, "warnings": [],
        }
    return payload


def _save_rec(conn, payload: dict) -> None:
    save_frozen_recommendation(conn, FrozenRecommendation(payload=payload))


def _setup_cycle(
    conn, *, cycle_id: str, symbol: str, as_of: datetime = AS_OF, baseline_payload: dict | None = None,
    enhanced_payload: dict | None = None, snapshot: EvidenceSnapshot | None = "default", evidence_complete: bool = True,
    symbol_status: str = "COMPLETED", cycle_experiment_policy: str = "SHADOW_ENHANCED",
) -> None:
    if snapshot == "default":
        snapshot = _evidence_snapshot(symbol, Decimal("150.00"), as_of)
    cycle_repo = SQLiteResearchCycleRepository(conn)
    cycle_repo.save_cycle_started(cycle_id, "test-universe", as_of, "cfg-hash", cycle_experiment_policy, "fixture", as_of)

    snapshot_id = None
    if snapshot is not None:
        save_evidence_snapshot(conn, snapshot)
        snapshot_id = snapshot.snapshot_id

    baseline_rec_id = None
    if baseline_payload is not None:
        _save_rec(conn, baseline_payload)
        baseline_rec_id = baseline_payload["rec_id"]
    enhanced_rec_id = None
    if enhanced_payload is not None:
        _save_rec(conn, enhanced_payload)
        enhanced_rec_id = enhanced_payload["rec_id"]

    cycle_repo.save_symbol_result(
        cycle_id,
        SymbolCycleResult(
            symbol=symbol, status=symbol_status, snapshot_id=snapshot_id, research_run_id=None,
            experiment_id=f"exp-{cycle_id}-{symbol}", baseline_recommendation_id=baseline_rec_id,
            enhanced_recommendation_id=enhanced_rec_id, baseline_paper_submitted=False,
        ),
        as_of, as_of,
    )
    cycle_repo.mark_cycle_finished(cycle_id, "COMPLETED", as_of)

    if evidence_complete:
        save_symbol_evidence_status(
            conn,
            {
                "cycle_id": cycle_id, "symbol": symbol, "snapshot_id": snapshot_id,
                "corporate_status_evidence_id": None, "completeness_result_id": None,
                "screening_completeness": STATUS_COMPLETE_FOR_SCREENING, "research_completeness": STATUS_COMPLETE_FOR_SCREENING,
                "blocking_categories_json": "[]", "policy_version": "test-v1", "created_at": as_of.isoformat(),
            },
        )
    else:
        save_symbol_evidence_status(
            conn,
            {
                "cycle_id": cycle_id, "symbol": symbol, "snapshot_id": snapshot_id,
                "corporate_status_evidence_id": None, "completeness_result_id": None,
                "screening_completeness": STATUS_MISSING_CRITICAL_MARKET_DATA, "research_completeness": STATUS_MISSING_CRITICAL_MARKET_DATA,
                "blocking_categories_json": "[]", "policy_version": "test-v1", "created_at": as_of.isoformat(),
            },
        )


def _outcomes_by_arm(result):
    return {o.arm: o for o in result.symbol_outcomes}


# --- disabled-config / unknown-cycle fail-closed ----------------------------


def test_disabled_paper_books_fails_closed(conn):
    _setup_cycle(conn, cycle_id="c1", symbol="AAPL", baseline_payload=_rec_payload("rec-b1", "AAPL"), enhanced_payload=_rec_payload("rec-e1", "AAPL"))
    with pytest.raises(ScheduledIntegrationError):
        integrate_scheduled_cycle_into_paper_books(
            conn, cycle_id="c1", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(enabled=False),
            clock=lambda: AS_OF,
        )


def test_disabled_scheduled_integration_fails_closed(conn):
    _setup_cycle(conn, cycle_id="c1", symbol="AAPL", baseline_payload=_rec_payload("rec-b1", "AAPL"), enhanced_payload=_rec_payload("rec-e1", "AAPL"))
    with pytest.raises(ScheduledIntegrationError):
        integrate_scheduled_cycle_into_paper_books(
            conn, cycle_id="c1", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS",
            paper_books_config=_config(scheduled_integration_enabled=False), clock=lambda: AS_OF,
        )


def test_unknown_cycle_id_fails_closed(conn):
    with pytest.raises(ScheduledIntegrationError):
        integrate_scheduled_cycle_into_paper_books(
            conn, cycle_id="does-not-exist", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(),
            clock=lambda: AS_OF,
        )


# --- scheduled-cycle output mapping -----------------------------------------


def test_baseline_and_enhanced_recommendations_found(conn):
    _setup_cycle(conn, cycle_id="c2", symbol="AAPL", baseline_payload=_rec_payload("rec-b2", "AAPL"), enhanced_payload=_rec_payload("rec-e2", "AAPL"))
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c2", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].recommendation_id == "rec-b2"
    assert outcomes["ENHANCED"].recommendation_id == "rec-e2"
    assert outcomes["BASELINE"].outcome in (OUTCOME_EXECUTED, OUTCOME_INTENT_CREATED_PENDING_FILL)
    assert outcomes["ENHANCED"].outcome in (OUTCOME_EXECUTED, OUTCOME_INTENT_CREATED_PENDING_FILL)


def test_external_enabled_book_is_queued_without_local_fill_or_runtime_mutation(conn):
    _setup_cycle(
        conn, cycle_id="external-queue", symbol="AAPL",
        baseline_payload=_rec_payload("rec-external", "AAPL"), enhanced_payload=None,
    )
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="external-queue", experiment_policy="BASELINE_ONLY",
        paper_books_config=_config(external_book_ids=("BASELINE",)), clock=lambda: AS_OF,
    )
    outcome = _outcomes_by_arm(result)["BASELINE"]
    assert outcome.outcome == OUTCOME_AWAITING_OPERATOR_EXTERNAL_SUBMISSION
    assert conn.execute("SELECT COUNT(*) FROM paper_book_fills WHERE book_id='BASELINE'").fetchone()[0] == 0
    queued = conn.execute(
        "SELECT status FROM paper_external_submission_queue WHERE book_id='BASELINE'"
    ).fetchone()
    assert queued["status"] == "AWAITING_OPERATOR_EXTERNAL_SUBMISSION"


def test_missing_baseline_recommendation(conn):
    _setup_cycle(conn, cycle_id="c3", symbol="AAPL", baseline_payload=None, enhanced_payload=_rec_payload("rec-e3", "AAPL"))
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c3", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome == OUTCOME_SKIPPED_RECOMMENDATION_MISSING
    assert outcomes["ENHANCED"].outcome != OUTCOME_SKIPPED_RECOMMENDATION_MISSING


def test_missing_enhanced_recommendation(conn):
    _setup_cycle(conn, cycle_id="c4", symbol="AAPL", baseline_payload=_rec_payload("rec-b4", "AAPL"), enhanced_payload=None)
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c4", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["ENHANCED"].outcome == OUTCOME_SKIPPED_RECOMMENDATION_MISSING
    assert outcomes["BASELINE"].outcome != OUTCOME_SKIPPED_RECOMMENDATION_MISSING


def test_missing_evidence_snapshot(conn):
    _setup_cycle(
        conn, cycle_id="c5", symbol="AAPL", baseline_payload=_rec_payload("rec-b5", "AAPL"),
        enhanced_payload=_rec_payload("rec-e5", "AAPL"), snapshot=None,
    )
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c5", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome == OUTCOME_SKIPPED_SNAPSHOT_MISMATCH
    assert outcomes["ENHANCED"].outcome == OUTCOME_SKIPPED_SNAPSHOT_MISMATCH


def test_symbol_mismatch_between_recommendation_and_snapshot(conn):
    # The recommendation payload's own symbol disagrees with the cycle's symbol.
    _setup_cycle(
        conn, cycle_id="c6", symbol="AAPL", baseline_payload=_rec_payload("rec-b6", "MSFT"),
        enhanced_payload=_rec_payload("rec-e6", "AAPL"),
    )
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c6", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome == OUTCOME_SKIPPED_SNAPSHOT_MISMATCH


def test_timestamp_mismatch_future_recommendation_rejected(conn):
    future_ts = AS_OF + timedelta(days=1)
    _setup_cycle(
        conn, cycle_id="c7", symbol="AAPL", baseline_payload=_rec_payload("rec-b7", "AAPL", ts=future_ts),
        enhanced_payload=_rec_payload("rec-e7", "AAPL"),
    )
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c7", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome == OUTCOME_SKIPPED_SNAPSHOT_MISMATCH


# --- policy routing ----------------------------------------------------------


def test_baseline_only_policy_never_touches_enhanced(conn):
    _setup_cycle(conn, cycle_id="c8", symbol="AAPL", baseline_payload=_rec_payload("rec-b8", "AAPL"), enhanced_payload=_rec_payload("rec-e8", "AAPL"))
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c8", experiment_policy="BASELINE_ONLY", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome in (OUTCOME_EXECUTED, OUTCOME_INTENT_CREATED_PENDING_FILL)
    assert outcomes["ENHANCED"].outcome == OUTCOME_SKIPPED_POLICY
    assert outcomes["ENHANCED"].paper_order_intent_id is None


def test_enhanced_only_policy_never_touches_baseline(conn):
    _setup_cycle(conn, cycle_id="c9", symbol="AAPL", baseline_payload=_rec_payload("rec-b9", "AAPL"), enhanced_payload=_rec_payload("rec-e9", "AAPL"))
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c9", experiment_policy="ENHANCED_ONLY", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["ENHANCED"].outcome in (OUTCOME_EXECUTED, OUTCOME_INTENT_CREATED_PENDING_FILL)
    assert outcomes["BASELINE"].outcome == OUTCOME_SKIPPED_POLICY
    assert outcomes["BASELINE"].paper_order_intent_id is None


def test_both_separate_paper_books_policy_targets_both_independently(conn):
    _setup_cycle(conn, cycle_id="c10", symbol="AAPL", baseline_payload=_rec_payload("rec-b10", "AAPL"), enhanced_payload=_rec_payload("rec-e10", "AAPL"))
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c10", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome in (OUTCOME_EXECUTED, OUTCOME_INTENT_CREATED_PENDING_FILL)
    assert outcomes["ENHANCED"].outcome in (OUTCOME_EXECUTED, OUTCOME_INTENT_CREATED_PENDING_FILL)
    assert outcomes["BASELINE"].paper_order_intent_id != outcomes["ENHANCED"].paper_order_intent_id


def test_observe_only_policy_submits_neither_arm(conn):
    _setup_cycle(conn, cycle_id="c11", symbol="AAPL", baseline_payload=_rec_payload("rec-b11", "AAPL"), enhanced_payload=_rec_payload("rec-e11", "AAPL"))
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c11", experiment_policy="OBSERVE_ONLY", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome == OUTCOME_SKIPPED_POLICY
    assert outcomes["ENHANCED"].outcome == OUTCOME_SKIPPED_POLICY


def test_disabled_baseline_book_fails_closed_no_fallback(conn):
    _setup_cycle(conn, cycle_id="c12", symbol="AAPL", baseline_payload=_rec_payload("rec-b12", "AAPL"), enhanced_payload=_rec_payload("rec-e12", "AAPL"))
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c12", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(baseline_enabled=False),
        clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome == OUTCOME_SKIPPED_BOOK_DISABLED
    # No fallback: enhanced arm never maps to the baseline book, and the
    # baseline arm's disabled-book skip never redirects to the enhanced book.
    assert outcomes["BASELINE"].book_id == "BASELINE"
    assert outcomes["ENHANCED"].book_id == "ENHANCED"


def test_disabled_enhanced_book_fails_closed_no_fallback(conn):
    _setup_cycle(conn, cycle_id="c13", symbol="AAPL", baseline_payload=_rec_payload("rec-b13", "AAPL"), enhanced_payload=_rec_payload("rec-e13", "AAPL"))
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c13", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(enhanced_enabled=False),
        clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["ENHANCED"].outcome == OUTCOME_SKIPPED_BOOK_DISABLED


# --- evidence completeness / recommendation validity ------------------------


def test_incomplete_evidence_blocks_both_arms(conn):
    _setup_cycle(
        conn, cycle_id="c14", symbol="AAPL", baseline_payload=_rec_payload("rec-b14", "AAPL"),
        enhanced_payload=_rec_payload("rec-e14", "AAPL"), evidence_complete=False,
    )
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c14", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome == OUTCOME_SKIPPED_EVIDENCE_INCOMPLETE
    assert outcomes["ENHANCED"].outcome == OUTCOME_SKIPPED_EVIDENCE_INCOMPLETE


def test_non_buy_candidate_recommendation_is_invalid_for_submission(conn):
    _setup_cycle(
        conn, cycle_id="c15", symbol="AAPL", baseline_payload=_rec_payload("rec-b15", "AAPL", side=SIDE_SCREENED_OUT, status=STATUS_ACTIVE, shares=None),
        enhanced_payload=_rec_payload("rec-e15", "AAPL"),
    )
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c15", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome == OUTCOME_SKIPPED_RECOMMENDATION_INVALID


def test_analysis_incomplete_status_is_invalid_for_submission(conn):
    _setup_cycle(
        conn, cycle_id="c16", symbol="AAPL",
        baseline_payload=_rec_payload("rec-b16", "AAPL", status=STATUS_ANALYSIS_INCOMPLETE, shares=None),
        enhanced_payload=_rec_payload("rec-e16", "AAPL"),
    )
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c16", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome == OUTCOME_SKIPPED_RECOMMENDATION_INVALID


# --- portfolio isolation ------------------------------------------------------


def test_different_starting_cash_produces_different_approved_quantity(conn):
    _setup_cycle(
        conn, cycle_id="c17", symbol="AAPL", baseline_payload=_rec_payload("rec-b17", "AAPL", shares=1000),
        enhanced_payload=_rec_payload("rec-e17", "AAPL", shares=1000),
    )
    cfg = _config(baseline_cash=Decimal("100000.00"), enhanced_cash=Decimal("5000.00"), max_order_notional_usd=Decimal("500000.00"))
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c17", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=cfg, clock=lambda: AS_OF,
    )
    from trading_research.storage import paper_books_repositories as repo

    baseline_decision = repo.load_risk_decision(conn, _outcomes_by_arm(result)["BASELINE"].risk_decision_id)
    enhanced_decision = repo.load_risk_decision(conn, _outcomes_by_arm(result)["ENHANCED"].risk_decision_id)
    assert baseline_decision["approved_quantity"] != enhanced_decision["approved_quantity"]
    assert Decimal(enhanced_decision["approved_quantity"] or 0) < Decimal(baseline_decision["approved_quantity"] or 0)


def test_one_books_existing_position_does_not_affect_the_other(conn):
    from trading_research.paper_books import cash_ledger, positions

    cfg = _config()
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=cfg.baseline.starting_cash_usd, config_hash=cfg.config_hash, clock=lambda: AS_OF)
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=cfg.enhanced.starting_cash_usd, config_hash=cfg.config_hash, clock=lambda: AS_OF)
    # ENHANCED already holds a large MSFT position eating into its capacity;
    # BASELINE never held or sized against MSFT.
    positions.apply_buy_fill(conn, "ENHANCED", "MSFT", "prior-fill", Decimal("300"), Decimal("300.00"), AS_OF)
    cash_ledger.settle_buy(conn, "ENHANCED", "prior-fill", Decimal("90000.00"), Decimal("0"), Decimal("0"), AS_OF)

    _setup_cycle(
        conn, cycle_id="c18", symbol="AAPL", baseline_payload=_rec_payload("rec-b18", "AAPL", shares=500),
        enhanced_payload=_rec_payload("rec-e18", "AAPL", shares=500),
    )
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c18", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=cfg, clock=lambda: AS_OF,
    )
    from trading_research.storage import paper_books_repositories as repo

    baseline_position = repo.load_position(conn, "BASELINE", "MSFT")
    assert baseline_position is None  # never contaminated by ENHANCED's own prior position
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].paper_order_intent_id != outcomes["ENHANCED"].paper_order_intent_id


# --- market simulation --------------------------------------------------------


def test_observed_bid_ask_in_evidence_snapshot_produces_executed_fill(conn):
    snapshot = _evidence_snapshot("AAPL", Decimal("150.00"), AS_OF, bid=Decimal("148.90"), ask=Decimal("149.10"))
    _setup_cycle(
        conn, cycle_id="c19", symbol="AAPL", baseline_payload=_rec_payload("rec-b19", "AAPL"),
        enhanced_payload=_rec_payload("rec-e19", "AAPL"), snapshot=snapshot,
    )
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c19", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome == OUTCOME_EXECUTED
    assert outcomes["BASELINE"].market_simulation_input_source == MARKET_SIMULATION_SOURCE_OBSERVED
    assert outcomes["BASELINE"].fill_id is not None


def test_no_bid_ask_falls_back_to_deterministic_simulated_spread(conn):
    # No bid/ask in the snapshot -> tier 2 (synthetic spread around the
    # point-in-time reference price). The synthetic ask+slippage is always
    # >= the risk-approved limit price (which is anchored at that same
    # reference price), so this never crosses — proving no guaranteed fill
    # merely because an intent exists (acceptance criterion #12).
    _setup_cycle(
        conn, cycle_id="c20", symbol="AAPL", baseline_payload=_rec_payload("rec-b20", "AAPL"),
        enhanced_payload=_rec_payload("rec-e20", "AAPL"),
    )
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c20", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome == OUTCOME_INTENT_CREATED_PENDING_FILL
    assert outcomes["BASELINE"].market_simulation_input_source == MARKET_SIMULATION_SOURCE_SIMULATED
    assert outcomes["BASELINE"].fill_id is None


def test_unavailable_price_leaves_intent_pending_never_fabricates_fill(conn):
    # No evidence snapshot market item at all (missing_data) and no
    # price_provider -> SOURCE_UNAVAILABLE -> risk rejects for missing price.
    empty_snapshot = EvidenceSnapshot(
        snapshot_id="snap-empty", symbol="AAPL", as_of=AS_OF, created_at=AS_OF, source_records=(), evidence_items=(),
        deterministic_factors={}, sentiment_metrics={}, portfolio_context=None, missing_data_reasons=("no market data",),
        conflict_reasons=(), point_in_time_safe=True, config_hash="cfg", git_sha="sha",
    )
    _setup_cycle(
        conn, cycle_id="c21", symbol="AAPL", baseline_payload=_rec_payload("rec-b21", "AAPL"),
        enhanced_payload=_rec_payload("rec-e21", "AAPL"), snapshot=empty_snapshot,
    )
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c21", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome == OUTCOME_REJECTED_BY_RISK
    assert outcomes["BASELINE"].fill_id is None
    assert outcomes["BASELINE"].paper_order_intent_id is None


def test_future_priced_market_item_is_never_used(conn):
    # The evidence snapshot's own market item is timestamped after as_of —
    # must never be used to build a market-simulation input.
    future_ts = AS_OF + timedelta(hours=2)
    source = SourceRecord(
        source_id="src-future", source_type="market", provider="fixture", source_locator=None, retrieved_at=future_ts,
        published_at=future_ts, effective_at=future_ts, available_at=future_ts, content_hash="hash", status="ok",
        is_stale=False, point_in_time_safe=True, error_code=None,
    )
    item = EvidenceItem(
        evidence_id="future-item", source_id=source.source_id, category="market", title="market", summary="market",
        normalized_values={"bid": 148.90, "ask": 149.10}, as_of=future_ts, confidence="high", stale=False,
    )
    snapshot = EvidenceSnapshot(
        snapshot_id="snap-future", symbol="AAPL", as_of=AS_OF, created_at=AS_OF, source_records=(source,),
        evidence_items=(item,), deterministic_factors={}, sentiment_metrics={}, portfolio_context=None,
        missing_data_reasons=(), conflict_reasons=(), point_in_time_safe=True, config_hash="cfg", git_sha="sha",
    )
    _setup_cycle(
        conn, cycle_id="c22", symbol="AAPL", baseline_payload=_rec_payload("rec-b22", "AAPL"),
        enhanced_payload=_rec_payload("rec-e22", "AAPL"), snapshot=snapshot,
    )
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c22", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    # Future bid/ask must never be used as OBSERVED — either rejected by risk
    # (no valid latest_close either) or, if priced, never labeled OBSERVED.
    assert outcomes["BASELINE"].market_simulation_input_source != MARKET_SIMULATION_SOURCE_OBSERVED


# --- idempotency ---------------------------------------------------------------


def test_reprocessing_same_cycle_is_idempotent(conn):
    snapshot = _evidence_snapshot("AAPL", Decimal("150.00"), AS_OF, bid=Decimal("148.90"), ask=Decimal("149.10"))
    _setup_cycle(
        conn, cycle_id="c23", symbol="AAPL", baseline_payload=_rec_payload("rec-b23", "AAPL"),
        enhanced_payload=_rec_payload("rec-e23", "AAPL"), snapshot=snapshot,
    )
    cfg = _config()
    first = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c23", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=cfg, clock=lambda: AS_OF,
    )
    from trading_research.paper_books import cash_ledger
    from trading_research.storage import paper_books_repositories as repo

    baseline_cash_after_1 = cash_ledger.available_cash(conn, "BASELINE")
    baseline_position_after_1 = repo.load_position(conn, "BASELINE", "AAPL")
    fills_after_1 = repo.list_fills(conn, "BASELINE")
    assignments_after_1 = repo.list_experiment_assignments(conn, f"exp-c23-AAPL")

    second = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c23", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=cfg, clock=lambda: AS_OF,
    )
    baseline_cash_after_2 = cash_ledger.available_cash(conn, "BASELINE")
    baseline_position_after_2 = repo.load_position(conn, "BASELINE", "AAPL")
    fills_after_2 = repo.list_fills(conn, "BASELINE")
    assignments_after_2 = repo.list_experiment_assignments(conn, f"exp-c23-AAPL")

    assert baseline_cash_after_1 == baseline_cash_after_2
    assert baseline_position_after_1 == baseline_position_after_2
    assert len(fills_after_1) == len(fills_after_2)
    # One assignment row per (cycle_id, symbol) carries both arms' identity —
    # never duplicated on reprocessing.
    assert len(assignments_after_1) == len(assignments_after_2) == 1

    first_outcomes = _outcomes_by_arm(first)
    second_outcomes = _outcomes_by_arm(second)
    assert first_outcomes["BASELINE"].paper_order_intent_id == second_outcomes["BASELINE"].paper_order_intent_id
    assert first_outcomes["BASELINE"].outcome == second_outcomes["BASELINE"].outcome == OUTCOME_EXECUTED
    # The second call's fill_id is None — the idempotent-replay path
    # recognizes the fill already exists and never re-applies it (never a
    # *new* fill dict), which is exactly what the unchanged cash/position/
    # fill-count assertions above prove.
    assert first_outcomes["BASELINE"].fill_id is not None
    assert second_outcomes["BASELINE"].fill_id is None


# --- failure handling ----------------------------------------------------------


def test_risk_rejection_is_recorded_and_persisted(conn):
    _setup_cycle(
        conn, cycle_id="c24", symbol="AAPL", baseline_payload=_rec_payload("rec-b24", "AAPL", shares=1_000_000),
        enhanced_payload=_rec_payload("rec-e24", "AAPL"),
    )
    cfg = _config(max_position_weight=Decimal("0.001"), max_order_notional_usd=Decimal("1.00"))
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c24", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=cfg, clock=lambda: AS_OF,
    )
    outcomes = _outcomes_by_arm(result)
    assert outcomes["BASELINE"].outcome == OUTCOME_REJECTED_BY_RISK
    assert outcomes["BASELINE"].risk_decision_id is not None
    from trading_research.storage import paper_books_repositories as repo

    persisted = repo.load_risk_decision(conn, outcomes["BASELINE"].risk_decision_id)
    assert persisted is not None  # a rejected recommendation still has a queryable audit trail


def test_research_result_is_never_mutated_by_paper_book_failure(conn):
    """A recommendation with no risk_plan.shares (invalid for submission)
    must be skipped, never mutated — the frozen payload is byte-identical
    before and after integration."""
    payload = _rec_payload("rec-b25", "AAPL", shares=None)
    _setup_cycle(conn, cycle_id="c25", symbol="AAPL", baseline_payload=payload, enhanced_payload=_rec_payload("rec-e25", "AAPL"))
    from trading_research.storage.trading_repositories import load_recommendation

    before = load_recommendation(conn, "rec-b25")
    integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c25", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    after = load_recommendation(conn, "rec-b25")
    assert before == after


def test_reconciliation_persisted_per_book_independently(conn):
    snapshot = _evidence_snapshot("AAPL", Decimal("150.00"), AS_OF, bid=Decimal("148.90"), ask=Decimal("149.10"))
    _setup_cycle(
        conn, cycle_id="c26", symbol="AAPL", baseline_payload=_rec_payload("rec-b26", "AAPL"),
        enhanced_payload=_rec_payload("rec-e26", "AAPL"), snapshot=snapshot,
    )
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id="c26", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=_config(), clock=lambda: AS_OF,
    )
    assert "BASELINE" in result.reconciliations
    assert "ENHANCED" in result.reconciliations
    assert result.reconciliations["BASELINE"]["status"] == "MATCHED"
    assert result.reconciliations["ENHANCED"]["status"] == "MATCHED"
