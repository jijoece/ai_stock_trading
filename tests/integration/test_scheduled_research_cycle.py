"""Offline, full-pipeline integration tests for the Milestone 6 scheduled
research cycle: fixture universe -> deterministic screen/score -> fake
real-shaped provider responses -> normalized EvidenceSnapshot -> deterministic
baseline -> scripted/deterministic Claude research -> deterministic overlay
-> frozen recommendations -> shadow experiment -> no duplicate on rerun.

Everything here runs on injected fakes against a temporary SQLite database —
no network, no LumiBot, no Claude credentials.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.analysis.scorer import load_scoring_config
from trading_research.analysis.screener import load_screening_config
from trading_research.evidence_providers.evidence_adapters import (
    RealFilingEvidenceProvider,
    RealFundamentalsEvidenceProvider,
    RealMarketEvidenceProvider,
)
from trading_research.evidence_providers.models import CompanyFactValue, FilingRecord, PriceBar
from trading_research.models.trading_models import PortfolioState
from trading_research.research.configuration import load_research_config
from trading_research.research.deterministic_provider import DeterministicResearchProvider
from trading_research.research.prompt_registry import PromptRegistry
from trading_research.research.scheduled_cycle import (
    CYCLE_STATUS_COMPLETED,
    PROVIDER_MODE_REAL,
    SYMBOL_STATUS_COMPLETED,
    EvidenceProviderRegistry,
    ScheduledResearchConfiguration,
    run_scheduled_research_cycle,
)
from trading_research.research import experiment_policy
from trading_research.storage.database import connect
from trading_research.storage.research_cycle_repositories import SQLiteResearchCycleRepository
from trading_research.storage.research_repositories import SQLiteResearchRepository
from trading_research.storage.trading_repositories import load_recommendation
from trading_research.universe.tickers import default_universe

AS_OF = datetime(2026, 7, 11, 13, 0, 0, tzinfo=timezone.utc)  # Saturday — safely after Friday's close


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("scheduled-cycle offline tests must never open a real socket")

    monkeypatch.setattr(socket.socket, "connect", _blocked)


class FakeSecClient:
    """Offline stand-in exposing the exact methods `evidence_adapters.py` and
    `scheduled_cycle.py` call on a real `SecEdgarClient` — no HTTP."""

    def get_company_facts(self, symbol: str, *, as_of: datetime):
        if symbol != "AAPL":
            return ()
        return (
            CompanyFactValue(
                concept="Revenues", unit="USD", value=Decimal("265595000000"),
                period_start=date(2023, 10, 1), period_end=date(2024, 9, 28),
                fiscal_year=2024, fiscal_period="FY", form_type="10-K", filed_at=date(2024, 11, 1), frame=None,
            ),
            CompanyFactValue(
                concept="Revenues", unit="USD", value=Decimal("229234000000"),
                period_start=date(2022, 9, 25), period_end=date(2023, 9, 30),
                fiscal_year=2023, fiscal_period="FY", form_type="10-K", filed_at=date(2023, 11, 3), frame=None,
            ),
            CompanyFactValue(
                concept="GrossProfit", unit="USD", value=Decimal("122000000000"),
                period_start=date(2023, 10, 1), period_end=date(2024, 9, 28),
                fiscal_year=2024, fiscal_period="FY", form_type="10-K", filed_at=date(2024, 11, 1), frame=None,
            ),
            CompanyFactValue(
                concept="OperatingIncomeLoss", unit="USD", value=Decimal("100000000000"),
                period_start=date(2023, 10, 1), period_end=date(2024, 9, 28),
                fiscal_year=2024, fiscal_period="FY", form_type="10-K", filed_at=date(2024, 11, 1), frame=None,
            ),
            CompanyFactValue(
                concept="CommonStockSharesOutstanding", unit="shares", value=Decimal("15000000000"),
                period_start=None, period_end=date(2024, 9, 28), fiscal_year=2024, fiscal_period="FY",
                form_type="10-K", filed_at=date(2024, 11, 1), frame=None,
            ),
        )

    def list_filings(self, symbol: str, *, available_by: datetime):
        if symbol != "AAPL":
            return ()
        return (
            FilingRecord(
                accession_number="0000320193-24-000123", cik="0000320193", symbol=symbol, form_type="8-K",
                filing_date=date(2026, 6, 1), accepted_at=datetime(2026, 6, 1, 21, 0, tzinfo=timezone.utc),
                report_period=None, primary_document_url="https://example.invalid/8k", is_amendment=False,
            ),
        )


class FakeMarketDataClient:
    def get_price_history(self, symbol, *, start, end, as_of):
        if symbol != "AAPL":
            return ()
        bars = []
        d = start
        price = Decimal("300.00")
        while d <= end:
            if d.weekday() < 5:
                bars.append(PriceBar(symbol=symbol, session_date=d, open=price, high=price + 2, low=price - 2, close=price, volume=1_000_000, adjusted=False, provider="fake"))
                price += Decimal("0.10")
            d += timedelta(days=1)
        return tuple(bars)

    def get_quote(self, symbol, *, as_of):
        return None


def _providers() -> EvidenceProviderRegistry:
    sec = FakeSecClient()
    market = FakeMarketDataClient()
    return EvidenceProviderRegistry(
        fundamentals=RealFundamentalsEvidenceProvider(sec),
        market=RealMarketEvidenceProvider(market),
        filings=RealFilingEvidenceProvider(sec),
        news=None,  # not configured in this environment — excluded, not included-and-missing
        sentiment=None,  # Reddit credentials not configured — excluded, not included-and-missing
        portfolio_context=None,
        market_data_client=market,
        sec_client=sec,
    )


def _config(**overrides) -> ScheduledResearchConfiguration:
    base = dict(
        universe_id="test-universe", max_candidates_per_cycle=5, experiment_policy=experiment_policy.SHADOW_ENHANCED,
        submit_paper_orders=False, require_complete_evidence=True, require_point_in_time_safe=True,
        continue_on_symbol_failure=True, provider_mode=PROVIDER_MODE_REAL, config_hash="test-config-hash",
    )
    base.update(overrides)
    return ScheduledResearchConfiguration(**base)


def _run(conn, symbols=("AAPL",), **overrides):
    research_config = load_research_config()
    portfolio = PortfolioState(account_equity=Decimal("100000"), settled_cash=Decimal("100000"), as_of=AS_OF)
    return run_scheduled_research_cycle(
        as_of=AS_OF, symbols=symbols, configuration=_config(**overrides), conn=conn,
        cycle_repository=SQLiteResearchCycleRepository(conn), universe=default_universe(),
        screening_config=load_screening_config(), scoring_config=load_scoring_config(),
        evidence_providers=_providers(), research_provider=DeterministicResearchProvider(),
        research_provider_name="deterministic", research_model_name="deterministic-v1",
        research_configuration=research_config, research_repository=SQLiteResearchRepository(conn),
        prompt_registry=PromptRegistry(), portfolio=portfolio, paper_submitter=None,
        clock=lambda: AS_OF, git_sha="test-sha",
    )


def test_scheduled_cycle_end_to_end(tmp_path):
    conn = connect(tmp_path / "cycle.sqlite3")
    result = _run(conn)

    assert result.status == CYCLE_STATUS_COMPLETED
    assert result.reused_existing_cycle is False
    assert len(result.symbol_results) == 1
    symbol_result = result.symbol_results[0]
    assert symbol_result.status == SYMBOL_STATUS_COMPLETED
    assert symbol_result.evidence_outcome == "COMPLETE"
    assert symbol_result.snapshot_id is not None
    assert symbol_result.baseline_recommendation_id is not None
    assert symbol_result.enhanced_recommendation_id is not None
    assert symbol_result.baseline_recommendation_id != symbol_result.enhanced_recommendation_id

    baseline = load_recommendation(conn, symbol_result.baseline_recommendation_id)
    enhanced = load_recommendation(conn, symbol_result.enhanced_recommendation_id)
    assert baseline is not None and enhanced is not None
    assert baseline["side"] in ("buy_candidate", "watch", "no_action", "screened_out", "analysis_incomplete")

    experiment_rows = conn.execute(
        "SELECT arm FROM research_experiment_assignments WHERE experiment_id = ? ORDER BY arm",
        (symbol_result.experiment_id,),
    ).fetchall()
    assert {r["arm"] for r in experiment_rows} == {"BASELINE", "ENHANCED"}


def test_scheduled_cycle_rerun_is_idempotent(tmp_path):
    conn = connect(tmp_path / "cycle.sqlite3")
    first = _run(conn)
    second = _run(conn)

    assert second.cycle_id == first.cycle_id
    assert second.reused_existing_cycle is True
    assert second.symbol_results[0].baseline_recommendation_id == first.symbol_results[0].baseline_recommendation_id
    assert second.symbol_results[0].enhanced_recommendation_id == first.symbol_results[0].enhanced_recommendation_id

    snapshot_count = conn.execute("SELECT COUNT(*) AS c FROM research_evidence_snapshots").fetchone()["c"]
    assert snapshot_count == 1  # no duplicate evidence snapshot on rerun

    assignment_count = conn.execute("SELECT COUNT(*) AS c FROM research_experiment_assignments").fetchone()["c"]
    assert assignment_count == 2  # BASELINE + ENHANCED, not duplicated


def test_scheduled_cycle_symbol_with_no_provider_data_is_isolated_not_fatal(tmp_path):
    conn = connect(tmp_path / "cycle.sqlite3")
    result = _run(conn, symbols=("AAPL", "MSFT"))

    assert len(result.symbol_results) == 2
    msft_result = next(r for r in result.symbol_results if r.symbol == "MSFT")
    # FakeSecClient/FakeMarketDataClient return nothing for MSFT -> required
    # evidence is missing -> the cycle must not crash, and the symbol result
    # must still be recorded (never silently dropped).
    assert msft_result.status in (SYMBOL_STATUS_COMPLETED, "FAILED", "SKIPPED")
    aapl_result = next(r for r in result.symbol_results if r.symbol == "AAPL")
    assert aapl_result.status == SYMBOL_STATUS_COMPLETED


def test_unknown_experiment_policy_fails_closed():
    with pytest.raises(Exception):
        _config(experiment_policy="NOT_A_REAL_POLICY")


def test_unsupported_experiment_policy_fails_closed():
    with pytest.raises(Exception):
        _config(experiment_policy=experiment_policy.ENHANCED_ONLY)
