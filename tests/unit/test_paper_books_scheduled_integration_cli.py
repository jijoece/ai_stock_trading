"""Tests for the `paper-book-integrate-cycle` CLI support function
(docs/milestone-8.1.md Step 11/12 "CLI"). `paper_book_integrate_cycle_cli`
loads an ACTUAL persisted cycle (never a fixture recommendation) and returns
sanitized, deterministic JSON — no raw Claude prompt/response content.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.paper_books import cli_support
from trading_research.paper_books.config import (
    ExecutionSection,
    PaperBookDefinition,
    PaperBooksConfiguration,
    RiskSection,
    ScheduledIntegrationSection,
    ValuationSection,
)
from trading_research.recommendations.builder import FrozenRecommendation, SIDE_BUY_CANDIDATE, STATUS_ACTIVE
from trading_research.research.evidence_completeness import STATUS_COMPLETE_FOR_SCREENING
from trading_research.research.models import EvidenceItem, EvidenceSnapshot, SourceRecord
from trading_research.research.scheduled_cycle import SymbolCycleResult
from trading_research.storage.database import connect
from trading_research.storage.research_cycle_repositories import SQLiteResearchCycleRepository, save_symbol_evidence_status
from trading_research.storage.research_repositories import save_evidence_snapshot
from trading_research.storage.trading_repositories import save_frozen_recommendation

AS_OF = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)


def _enabled_config() -> PaperBooksConfiguration:
    return PaperBooksConfiguration(
        version=1, enabled=True,
        baseline=PaperBookDefinition(enabled=True, book_id="BASELINE", starting_cash_usd=Decimal("100000.00")),
        enhanced=PaperBookDefinition(enabled=True, book_id="ENHANCED", starting_cash_usd=Decimal("100000.00")),
        execution=ExecutionSection(provider="local_simulated", allow_external_paper_broker=False, allow_live_broker=False),
        risk=RiskSection(
            max_position_weight=Decimal("0.50"), max_order_notional_usd=Decimal("100000.00"),
            max_daily_new_notional_usd=Decimal("100000.00"), minimum_cash_buffer_weight=Decimal("0.05"),
            max_open_positions=20, max_symbol_concentration_weight=Decimal("0.50"), reject_stale_market_price_seconds=900,
        ),
        valuation=ValuationSection(price_source="evidence_snapshot", maximum_price_age_seconds=900, missing_price_policy="MARK_UNVALUED"),
        scheduled_integration=ScheduledIntegrationSection(enabled=True),
        config_hash="cli-test-config-hash", raw={},
    )


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "cli_integration_test.db"


def _setup_persisted_cycle(db_path: Path, cycle_id: str, symbol: str = "AAPL") -> None:
    conn = connect(db_path)
    try:
        source = SourceRecord(
            source_id=f"src-{symbol}", source_type="market", provider="fixture-market", source_locator=None,
            retrieved_at=AS_OF, published_at=AS_OF, effective_at=AS_OF, available_at=AS_OF, content_hash="hash",
            status="ok", is_stale=False, point_in_time_safe=True, error_code=None,
        )
        item = EvidenceItem(
            evidence_id=f"{symbol}-market", source_id=source.source_id, category="market", title="market",
            summary="market", normalized_values={"latest_close": 150.0, "bid": 148.90, "ask": 149.10}, as_of=AS_OF,
            confidence="high", stale=False,
        )
        snapshot = EvidenceSnapshot(
            snapshot_id=f"snap-{cycle_id}-{symbol}", symbol=symbol, as_of=AS_OF, created_at=AS_OF,
            source_records=(source,), evidence_items=(item,), deterministic_factors={}, sentiment_metrics={},
            portfolio_context=None, missing_data_reasons=(), conflict_reasons=(), point_in_time_safe=True,
            config_hash="cfg", git_sha="sha",
        )
        save_evidence_snapshot(conn, snapshot)

        baseline_payload = {
            "rec_id": f"rec-b-{cycle_id}", "run_id": "run-b", "symbol": symbol, "side": SIDE_BUY_CANDIDATE,
            "ts": AS_OF.isoformat(), "price_at_rec": 150.0, "score": 80.0, "confidence": "high",
            "status": STATUS_ACTIVE, "acted": False, "rationale_text": "test", "factors": [],
            "model_version": "test-v1", "prompt_version": "test-v1", "config_hash": "cfg", "git_sha": "sha",
            "risk_plan": {
                "shares": 10, "entry_price": 150.0, "stop_price": 135.0, "target_price": 180.0,
                "risk_per_share": 15.0, "dollars_at_risk": 150.0, "position_value": 1500.0, "reward_risk": 2.0,
                "warnings": [],
            },
        }
        enhanced_payload = dict(baseline_payload, rec_id=f"rec-e-{cycle_id}", run_id="run-e")
        save_frozen_recommendation(conn, FrozenRecommendation(payload=baseline_payload))
        save_frozen_recommendation(conn, FrozenRecommendation(payload=enhanced_payload))

        cycle_repo = SQLiteResearchCycleRepository(conn)
        cycle_repo.save_cycle_started(cycle_id, "test-universe", AS_OF, "cfg-hash", "SHADOW_ENHANCED", "fixture", AS_OF)
        cycle_repo.save_symbol_result(
            cycle_id,
            SymbolCycleResult(
                symbol=symbol, status="COMPLETED", snapshot_id=snapshot.snapshot_id, research_run_id=None,
                experiment_id=f"exp-{cycle_id}-{symbol}", baseline_recommendation_id=baseline_payload["rec_id"],
                enhanced_recommendation_id=enhanced_payload["rec_id"], baseline_paper_submitted=False,
            ),
            AS_OF, AS_OF,
        )
        cycle_repo.mark_cycle_finished(cycle_id, "COMPLETED", AS_OF)
        save_symbol_evidence_status(
            conn,
            {
                "cycle_id": cycle_id, "symbol": symbol, "snapshot_id": snapshot.snapshot_id,
                "corporate_status_evidence_id": None, "completeness_result_id": None,
                "screening_completeness": STATUS_COMPLETE_FOR_SCREENING, "research_completeness": STATUS_COMPLETE_FOR_SCREENING,
                "blocking_categories_json": "[]", "policy_version": "test-v1", "created_at": AS_OF.isoformat(),
            },
        )
    finally:
        conn.close()


def test_actual_persisted_cycle_produces_sanitized_deterministic_json(db_path, monkeypatch):
    monkeypatch.setattr(cli_support, "load_paper_books_config", _enabled_config)
    _setup_persisted_cycle(db_path, "cli-cycle-1")

    outcome = cli_support.paper_book_integrate_cycle_cli(db_path, cycle_id="cli-cycle-1", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS")

    assert "error" not in outcome
    assert outcome["cycle_id"] == "cli-cycle-1"
    assert len(outcome["symbol_outcomes"]) == 2
    arms = {o["arm"] for o in outcome["symbol_outcomes"]}
    assert arms == {"BASELINE", "ENHANCED"}
    assert "BASELINE" in outcome["reconciliations"]
    assert "ENHANCED" in outcome["reconciliations"]
    # Sanitized: no raw prompt/response keys ever appear anywhere in the payload.
    serialized = str(outcome)
    for forbidden in ("prompt", "raw_response", "chain_of_thought", "anthropic_api_key"):
        assert forbidden not in serialized.lower().replace("prompt_version", "")

    # Deterministic ordering across repeated calls.
    outcome2 = cli_support.paper_book_integrate_cycle_cli(db_path, cycle_id="cli-cycle-1", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS")
    assert [o["symbol"] for o in outcome["symbol_outcomes"]] == [o["symbol"] for o in outcome2["symbol_outcomes"]]
    assert [o["arm"] for o in outcome["symbol_outcomes"]] == [o["arm"] for o in outcome2["symbol_outcomes"]]


def test_disabled_scheduled_integration_fails_closed(db_path, monkeypatch):
    disabled_cfg = PaperBooksConfiguration(
        version=1, enabled=True,
        baseline=PaperBookDefinition(enabled=True, book_id="BASELINE", starting_cash_usd=Decimal("100000.00")),
        enhanced=PaperBookDefinition(enabled=True, book_id="ENHANCED", starting_cash_usd=Decimal("100000.00")),
        execution=ExecutionSection(provider="local_simulated", allow_external_paper_broker=False, allow_live_broker=False),
        risk=RiskSection(
            max_position_weight=Decimal("0.50"), max_order_notional_usd=Decimal("100000.00"),
            max_daily_new_notional_usd=Decimal("100000.00"), minimum_cash_buffer_weight=Decimal("0.05"),
            max_open_positions=20, max_symbol_concentration_weight=Decimal("0.50"), reject_stale_market_price_seconds=900,
        ),
        valuation=ValuationSection(price_source="evidence_snapshot", maximum_price_age_seconds=900, missing_price_policy="MARK_UNVALUED"),
        scheduled_integration=ScheduledIntegrationSection(enabled=False),
        config_hash="cli-test-config-hash-disabled", raw={},
    )
    monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: disabled_cfg)
    _setup_persisted_cycle(db_path, "cli-cycle-2")

    outcome = cli_support.paper_book_integrate_cycle_cli(db_path, cycle_id="cli-cycle-2", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS")
    assert "error" in outcome


def test_shipped_default_config_is_disabled(db_path):
    """No monkeypatch — proves the actual shipped `config/paper_books.yaml`
    fails this command closed by default."""
    _setup_persisted_cycle(db_path, "cli-cycle-3")
    outcome = cli_support.paper_book_integrate_cycle_cli(db_path, cycle_id="cli-cycle-3", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS")
    assert "error" in outcome


def test_missing_cycle_fails_closed(db_path, monkeypatch):
    monkeypatch.setattr(cli_support, "load_paper_books_config", _enabled_config)
    connect(db_path).close()  # initialize schema, no cycle written

    outcome = cli_support.paper_book_integrate_cycle_cli(db_path, cycle_id="does-not-exist", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS")
    assert "error" in outcome
