"""Offline, deterministic end-to-end proof of Milestone 8.1 (docs/milestone-8.1.md
Step 13):

    real scheduled research cycle
    -> persisted frozen baseline and enhanced recommendations
    -> one shared evidence snapshot and as_of
    -> experiment assignment
    -> BASELINE portfolio snapshot
    -> ENHANCED portfolio snapshot
    -> independent risk decisions
    -> distinct order intents
    -> local simulated execution
    -> separate cash and positions
    -> separate reconciliation
    -> repeat integration is idempotent
    -> no live execution

Part A drives the REAL, unmodified `run_scheduled_research_cycle` (the exact
fixture/deterministic-provider harness `tests/integration/
test_scheduled_research_cycle.py` already uses) to prove the integration
module reads genuinely real persisted scheduled-cycle output — the fixture
inputs make AAPL fail the (unrelated, Milestone 2) `max_share_price` screen,
so both arms are correctly, deterministically classified
SKIPPED_RECOMMENDATION_INVALID rather than fabricated as executable.

Part B builds an executable buy_candidate scenario directly through the same
persistence primitives `run_scheduled_research_cycle` itself uses
(`save_frozen_recommendation`, `save_evidence_snapshot`,
`SQLiteResearchCycleRepository`) — proving the full portfolio-valuation ->
risk -> order-intent -> local-simulated-fill -> reconciliation chain, using
intentionally different starting cash so the two books produce different
deterministic approved quantities for the identical recommendation shape.

No network, no Claude, no SEC/Alpaca/Reddit/broker call anywhere in this file.
"""
from __future__ import annotations

import ast
import socket
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.analysis.scorer import load_scoring_config
from trading_research.analysis.screener import load_screening_config
from trading_research.evidence_providers.evidence_adapters import (
    RealFilingEvidenceProvider,
    RealFundamentalsEvidenceProvider,
    RealMarketEvidenceProvider,
)
from trading_research.evidence_providers.models import CompanyFactValue, PriceBar
from trading_research.models.trading_models import PortfolioState
from trading_research.paper_books import cash_ledger, positions
from trading_research.paper_books.config import (
    ExecutionSection,
    PaperBookDefinition,
    PaperBooksConfiguration,
    RiskSection,
    ScheduledIntegrationSection,
    ValuationSection,
)
from trading_research.paper_books.scheduled_integration import (
    OUTCOME_EXECUTED,
    OUTCOME_SKIPPED_RECOMMENDATION_INVALID,
    integrate_scheduled_cycle_into_paper_books,
)
from trading_research.recommendations.builder import FrozenRecommendation, SIDE_BUY_CANDIDATE, STATUS_ACTIVE
from trading_research.research.configuration import load_research_config
from trading_research.research.deterministic_provider import DeterministicResearchProvider
from trading_research.research.evidence_completeness import STATUS_COMPLETE_FOR_SCREENING
from trading_research.research.models import EvidenceItem, EvidenceSnapshot, SourceRecord
from trading_research.research.prompt_registry import PromptRegistry
from trading_research.research.scheduled_cycle import (
    PROVIDER_MODE_REAL,
    EvidenceProviderRegistry,
    ScheduledResearchConfiguration,
    SymbolCycleResult,
    run_scheduled_research_cycle,
)
from trading_research.research import experiment_policy
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect
from trading_research.storage.research_cycle_repositories import SQLiteResearchCycleRepository, save_symbol_evidence_status
from trading_research.storage.research_repositories import SQLiteResearchRepository, save_evidence_snapshot
from trading_research.storage.trading_repositories import load_recommendation, save_frozen_recommendation
from trading_research.universe.tickers import default_universe

AS_OF_REAL_CYCLE = datetime(2026, 7, 11, 13, 0, 0, tzinfo=timezone.utc)  # matches test_scheduled_research_cycle.py
AS_OF = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("Milestone 8.1 offline e2e must never open a real socket")

    monkeypatch.setattr(socket.socket, "connect", _blocked)


def _config(*, baseline_cash: Decimal, enhanced_cash: Decimal) -> PaperBooksConfiguration:
    return PaperBooksConfiguration(
        version=1, enabled=True,
        baseline=PaperBookDefinition(enabled=True, book_id="BASELINE", starting_cash_usd=baseline_cash),
        enhanced=PaperBookDefinition(enabled=True, book_id="ENHANCED", starting_cash_usd=enhanced_cash),
        execution=ExecutionSection(provider="local_simulated", allow_external_paper_broker=False, allow_live_broker=False),
        risk=RiskSection(
            max_position_weight=Decimal("0.90"), max_order_notional_usd=Decimal("1000000.00"),
            max_daily_new_notional_usd=Decimal("1000000.00"), minimum_cash_buffer_weight=Decimal("0.02"),
            max_open_positions=20, max_symbol_concentration_weight=Decimal("0.90"), reject_stale_market_price_seconds=900,
        ),
        valuation=ValuationSection(price_source="evidence_snapshot", maximum_price_age_seconds=900, missing_price_policy="MARK_UNVALUED"),
        scheduled_integration=ScheduledIntegrationSection(enabled=True),
        config_hash="e2e-config-hash", raw={},
    )


# --- Part A: real run_scheduled_research_cycle -------------------------------


class _FakeSecClient:
    def get_company_facts(self, symbol: str, *, as_of: datetime):
        if symbol != "AAPL":
            return ()
        from datetime import date

        return (
            CompanyFactValue(
                concept="Revenues", unit="USD", value=Decimal("265595000000"), period_start=date(2023, 10, 1),
                period_end=date(2024, 9, 28), fiscal_year=2024, fiscal_period="FY", form_type="10-K",
                filed_at=date(2024, 11, 1), frame=None,
            ),
        )

    def list_filings(self, symbol: str, *, available_by: datetime):
        return ()


class _FakeMarketDataClient:
    def get_price_history(self, symbol, *, start, end, as_of):
        if symbol != "AAPL":
            return ()
        return (
            PriceBar(
                symbol=symbol, session_date=start, open=Decimal("300.00"), high=Decimal("302.00"),
                low=Decimal("298.00"), close=Decimal("300.00"), volume=1_000_000, adjusted=False, provider="fake",
            ),
        )

    def get_quote(self, symbol, *, as_of):
        return None


def _real_cycle_providers() -> EvidenceProviderRegistry:
    sec = _FakeSecClient()
    market = _FakeMarketDataClient()
    return EvidenceProviderRegistry(
        fundamentals=RealFundamentalsEvidenceProvider(sec), market=RealMarketEvidenceProvider(market),
        filings=RealFilingEvidenceProvider(sec), news=None, sentiment=None, portfolio_context=None,
        market_data_client=market, sec_client=sec,
    )


def test_real_scheduled_cycle_output_feeds_the_mapping_correctly(tmp_path):
    conn = connect(tmp_path / "real_cycle.sqlite3")
    research_config = load_research_config()
    portfolio = PortfolioState(account_equity=Decimal("100000"), settled_cash=Decimal("100000"), as_of=AS_OF_REAL_CYCLE)
    cycle_config = ScheduledResearchConfiguration(
        universe_id="test-universe", max_candidates_per_cycle=5, experiment_policy=experiment_policy.SHADOW_ENHANCED,
        submit_paper_orders=False, require_complete_evidence=True, require_point_in_time_safe=True,
        continue_on_symbol_failure=True, provider_mode=PROVIDER_MODE_REAL, config_hash="real-e2e-config-hash",
    )
    cycle_result = run_scheduled_research_cycle(
        as_of=AS_OF_REAL_CYCLE, symbols=("AAPL",), configuration=cycle_config, conn=conn,
        cycle_repository=SQLiteResearchCycleRepository(conn), universe=default_universe(),
        screening_config=load_screening_config(), scoring_config=load_scoring_config(),
        evidence_providers=_real_cycle_providers(), research_provider=DeterministicResearchProvider(),
        research_provider_name="deterministic", research_model_name="deterministic-v1",
        research_configuration=research_config, research_repository=SQLiteResearchRepository(conn),
        prompt_registry=PromptRegistry(), portfolio=portfolio, paper_submitter=None,
        clock=lambda: AS_OF_REAL_CYCLE, git_sha="test-sha",
    )
    sr = cycle_result.symbol_results[0]
    assert sr.baseline_recommendation_id is not None and sr.enhanced_recommendation_id is not None

    cfg = _config(baseline_cash=Decimal("100000.00"), enhanced_cash=Decimal("100000.00"))
    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id=cycle_result.cycle_id, experiment_policy="BOTH_SEPARATE_PAPER_BOOKS",
        paper_books_config=cfg, clock=lambda: AS_OF_REAL_CYCLE,
    )
    assert result.as_of == AS_OF_REAL_CYCLE
    outcomes = {o.arm: o for o in result.symbol_outcomes}
    assert set(outcomes) == {"BASELINE", "ENHANCED"}
    # AAPL's real, unmodified deterministic screen correctly rejects a
    # $300 share price against `config/screening.yaml`'s
    # `max_share_price: 25.0` -> screened_out -> not an executable
    # buy_candidate -> both arms correctly classified INVALID, never
    # fabricated as executable.
    assert outcomes["BASELINE"].outcome == OUTCOME_SKIPPED_RECOMMENDATION_INVALID
    assert outcomes["ENHANCED"].outcome == OUTCOME_SKIPPED_RECOMMENDATION_INVALID
    assert outcomes["BASELINE"].recommendation_id == sr.baseline_recommendation_id
    assert outcomes["ENHANCED"].recommendation_id == sr.enhanced_recommendation_id

    # Shared evidence_snapshot_id / as_of across both arms — persisted once,
    # before either arm's eligibility was ever evaluated.
    assignment = repo.load_experiment_assignment(conn, cycle_result.cycle_id, "AAPL")
    assert assignment["evidence_snapshot_id"] == sr.snapshot_id
    assert assignment["baseline_recommendation_id"] == sr.baseline_recommendation_id
    assert assignment["enhanced_recommendation_id"] == sr.enhanced_recommendation_id
    assert assignment["baseline_book_id"] == "BASELINE"
    assert assignment["enhanced_book_id"] == "ENHANCED"


# --- Part B: executable buy_candidate scenario, direct persistence ----------


def _evidence_snapshot(symbol: str, close: Decimal, as_of: datetime, bid: Decimal, ask: Decimal) -> EvidenceSnapshot:
    source = SourceRecord(
        source_id=f"src-{symbol}", source_type="market", provider="fixture-market", source_locator=None,
        retrieved_at=as_of, published_at=as_of, effective_at=as_of, available_at=as_of, content_hash="hash",
        status="ok", is_stale=False, point_in_time_safe=True, error_code=None,
    )
    item = EvidenceItem(
        evidence_id=f"{symbol}-market", source_id=source.source_id, category="market", title="market",
        summary="market", normalized_values={"latest_close": float(close), "bid": float(bid), "ask": float(ask)},
        as_of=as_of, confidence="high", stale=False,
    )
    return EvidenceSnapshot(
        snapshot_id=f"snap-e2e-{symbol}", symbol=symbol, as_of=as_of, created_at=as_of, source_records=(source,),
        evidence_items=(item,), deterministic_factors={}, sentiment_metrics={}, portfolio_context=None,
        missing_data_reasons=(), conflict_reasons=(), point_in_time_safe=True, config_hash="cfg", git_sha="sha",
    )


class _MsftPriceProvider:
    """Fallback (tier-2) price source for symbols other than the cycle's own
    symbol — mirrors `evaluation/price_provider.py::PriceProvider`, never a
    live quote."""

    def get_close(self, symbol, as_of):
        from trading_research.evaluation.price_provider import PricePoint

        if symbol != "MSFT":
            return None
        return PricePoint(symbol=symbol, as_of=as_of, close=Decimal("300.00"), source="fixture")


def _rec_payload(rec_id: str, symbol: str, shares: int, entry_price: float) -> dict:
    return {
        "rec_id": rec_id, "run_id": f"run-{rec_id}", "symbol": symbol, "side": SIDE_BUY_CANDIDATE,
        "ts": AS_OF.isoformat(), "price_at_rec": entry_price, "score": 85.0, "confidence": "high",
        "status": STATUS_ACTIVE, "acted": False, "rationale_text": "e2e test", "factors": [],
        "model_version": "test-v1", "prompt_version": "test-v1", "config_hash": "cfg", "git_sha": "sha",
        "risk_plan": {
            "shares": shares, "entry_price": entry_price, "stop_price": entry_price * 0.9,
            "target_price": entry_price * 1.2, "risk_per_share": entry_price * 0.1,
            "dollars_at_risk": shares * entry_price * 0.1, "position_value": shares * entry_price,
            "reward_risk": 2.0, "warnings": [],
        },
    }


def test_full_isolated_dual_book_pipeline_via_scheduled_integration(tmp_path):
    conn = connect(tmp_path / "e2e_book_pipeline.sqlite3")
    cycle_id = "cycle-m8-1-e2e"
    symbol = "AAPL"
    snapshot = _evidence_snapshot(symbol, Decimal("150.00"), AS_OF, Decimal("148.90"), Decimal("149.10"))
    save_evidence_snapshot(conn, snapshot)

    baseline_payload = _rec_payload("rec-b-e2e", symbol, shares=100, entry_price=150.0)
    enhanced_payload = _rec_payload("rec-e-e2e", symbol, shares=100, entry_price=150.0)
    save_frozen_recommendation(conn, FrozenRecommendation(payload=baseline_payload))
    save_frozen_recommendation(conn, FrozenRecommendation(payload=enhanced_payload))

    cycle_repo = SQLiteResearchCycleRepository(conn)
    cycle_repo.save_cycle_started(cycle_id, "test-universe", AS_OF, "cfg-hash", "SHADOW_ENHANCED", "fixture", AS_OF)
    cycle_repo.save_symbol_result(
        cycle_id,
        SymbolCycleResult(
            symbol=symbol, status="COMPLETED", snapshot_id=snapshot.snapshot_id, research_run_id=None,
            experiment_id="exp-m8-1-e2e", baseline_recommendation_id="rec-b-e2e",
            enhanced_recommendation_id="rec-e-e2e", baseline_paper_submitted=False,
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

    # ENHANCED already holds a large pre-existing MSFT position, so the
    # *same* AAPL recommendation is sized differently across books for a
    # deterministic, persisted portfolio reason — not randomness, not
    # cross-book contamination.
    cfg = _config(baseline_cash=Decimal("100000.00"), enhanced_cash=Decimal("100000.00"))
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=cfg.enhanced.starting_cash_usd, config_hash=cfg.config_hash, clock=lambda: AS_OF)
    from trading_research.paper_books.models import PaperBookOrderIntent as _PriorIntent

    prior_intent = _PriorIntent(
        paper_order_intent_id="prior-intent-msft", book_id="ENHANCED", experiment_arm="ENHANCED", cycle_id="cyc-prior",
        recommendation_id="rec-prior-msft", symbol="MSFT", side="BUY", order_type="LIMIT", quantity=Decimal("280"),
        limit_price=Decimal("300.00"), notional_usd=Decimal("84000.00"), time_in_force="DAY", as_of=AS_OF,
        risk_decision_id="rd-prior", portfolio_snapshot_id="snap-prior", config_hash=cfg.config_hash, created_at=AS_OF,
        status="FILLED",
    )
    repo.save_order_intent(conn, prior_intent)
    repo.save_fill(conn, {
        "book_id": "ENHANCED", "fill_id": "prior-fill-msft", "paper_order_intent_id": "prior-intent-msft",
        "symbol": "MSFT", "side": "BUY", "simulated_market_price": Decimal("300.00"), "limit_price": Decimal("300.00"),
        "fill_quantity": Decimal("280"), "fill_price": Decimal("300.00"), "fees_usd": Decimal("0"),
        "slippage_usd": Decimal("0"), "fill_timestamp": AS_OF, "simulation_rule_version": "v1",
    })
    positions.apply_buy_fill(conn, "ENHANCED", "MSFT", "prior-fill-msft", Decimal("280"), Decimal("300.00"), AS_OF)
    cash_ledger.settle_buy(conn, "ENHANCED", "prior-fill-msft", Decimal("84000.00"), Decimal("0"), Decimal("0"), AS_OF)

    result = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id=cycle_id, experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=cfg,
        clock=lambda: AS_OF, price_provider=_MsftPriceProvider(),
    )
    outcomes = {o.arm: o for o in result.symbol_outcomes}
    assert outcomes["BASELINE"].outcome == OUTCOME_EXECUTED
    assert outcomes["ENHANCED"].outcome == OUTCOME_EXECUTED

    # -- distinct order intents / fills --------------------------------------
    assert outcomes["BASELINE"].paper_order_intent_id != outcomes["ENHANCED"].paper_order_intent_id
    assert outcomes["BASELINE"].fill_id != outcomes["ENHANCED"].fill_id

    # -- independent, deterministic risk sizing ------------------------------
    baseline_decision = repo.load_risk_decision(conn, outcomes["BASELINE"].risk_decision_id)
    enhanced_decision = repo.load_risk_decision(conn, outcomes["ENHANCED"].risk_decision_id)
    assert Decimal(enhanced_decision["approved_quantity"]) < Decimal(baseline_decision["approved_quantity"])

    # -- separate cash and positions -----------------------------------------
    baseline_cash = cash_ledger.available_cash(conn, "BASELINE")
    enhanced_cash = cash_ledger.available_cash(conn, "ENHANCED")
    assert baseline_cash != enhanced_cash
    baseline_position = repo.load_position(conn, "BASELINE", symbol)
    enhanced_position = repo.load_position(conn, "ENHANCED", symbol)
    assert baseline_position["quantity"] != enhanced_position["quantity"]
    assert repo.load_position(conn, "BASELINE", "MSFT") is None  # never contaminated

    # -- separate reconciliation ----------------------------------------------
    assert result.reconciliations["BASELINE"]["status"] == "MATCHED"
    assert result.reconciliations["ENHANCED"]["status"] == "MATCHED"

    # -- repeat integration is idempotent --------------------------------------
    second = integrate_scheduled_cycle_into_paper_books(
        conn, cycle_id=cycle_id, experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", paper_books_config=cfg,
        clock=lambda: AS_OF, price_provider=_MsftPriceProvider(),
    )
    assert cash_ledger.available_cash(conn, "BASELINE") == baseline_cash
    assert cash_ledger.available_cash(conn, "ENHANCED") == enhanced_cash
    assert repo.load_position(conn, "BASELINE", symbol) == baseline_position
    assert len(repo.list_fills(conn, "BASELINE")) == 1
    assert len(repo.list_fills(conn, "ENHANCED")) == 2  # the pre-existing MSFT fill + the new AAPL fill, never re-applied
    assert len(repo.list_experiment_assignments(conn, "exp-m8-1-e2e")) == 1
    second_outcomes = {o.arm: o for o in second.symbol_outcomes}
    assert second_outcomes["BASELINE"].paper_order_intent_id == outcomes["BASELINE"].paper_order_intent_id


def test_no_live_execution_path_in_scheduled_integration():
    """Structural proof: `scheduled_integration.py` never imports a
    broker/live-trading module and no `--live` flag exists."""
    import subprocess

    import trading_research.paper_books.scheduled_integration as mod

    forbidden_modules = ("lumibot", "alpaca", "robinhood", "anthropic", "trading_paper_runtime")
    tree = ast.parse(Path(mod.__file__).read_text(), filename=mod.__file__)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            for forbidden in forbidden_modules:
                assert not name.startswith(forbidden), f"scheduled_integration.py imports forbidden module {name!r}"

    cli_help = subprocess.run(
        [sys.executable, "-m", "trading_research.cli", "--help"], capture_output=True, text=True, check=True,
    )
    assert "--live" not in cli_help.stdout
