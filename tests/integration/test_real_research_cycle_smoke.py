"""Opt-in real scheduled-research-cycle smoke test (Milestone 6,
docs/milestone-6.md Step 24). Never runs automatically — requires
`RUN_REAL_RESEARCH_CYCLE=true` **and** `ALPACA_MARKET_DATA_API_KEY`/
`ALPACA_MARKET_DATA_API_SECRET`. Uses real SEC + real Alpaca market data for
one symbol at a fixed `as_of`, the deterministic research provider (not
Claude — this test validates real *evidence*, not real *Claude output*,
which Milestone 5 already validated separately), `SHADOW_ENHANCED` policy,
and `submit_paper_orders=False` — no paper order is ever submitted from this
test.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.analysis.scorer import load_scoring_config
from trading_research.analysis.screener import load_screening_config
from trading_research.evidence_providers.cache import ProviderCache
from trading_research.evidence_providers.evidence_adapters import RealFilingEvidenceProvider, RealFundamentalsEvidenceProvider, RealMarketEvidenceProvider
from trading_research.evidence_providers.http_client import HttpJsonClient
from trading_research.evidence_providers.market_data_provider import AlpacaMarketDataClient
from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter
from trading_research.evidence_providers.sec_provider import DEFAULT_USER_AGENT, SecEdgarClient
from trading_research.models.trading_models import PortfolioState
from trading_research.research.configuration import load_research_config
from trading_research.research.deterministic_provider import DeterministicResearchProvider
from trading_research.research.prompt_registry import PromptRegistry
from trading_research.research.scheduled_cycle import (
    PROVIDER_MODE_REAL,
    EvidenceProviderRegistry,
    ScheduledResearchConfiguration,
    run_scheduled_research_cycle,
)
from trading_research.research import experiment_policy
from trading_research.storage.database import connect
from trading_research.storage.research_cycle_repositories import SQLiteResearchCycleRepository
from trading_research.storage.research_repositories import SQLiteResearchRepository
from trading_research.universe.tickers import default_universe

pytestmark = pytest.mark.real_research_cycle

SKIP_REASON = "RUN_REAL_RESEARCH_CYCLE is not 'true' or ALPACA_MARKET_DATA_API_KEY/SECRET are absent — skipped by default"


def _should_run() -> bool:
    return (
        os.environ.get("RUN_REAL_RESEARCH_CYCLE") == "true"
        and bool(os.environ.get("ALPACA_MARKET_DATA_API_KEY"))
        and bool(os.environ.get("ALPACA_MARKET_DATA_API_SECRET"))
    )


@pytest.mark.skipif(not _should_run(), reason=SKIP_REASON)
def test_real_scheduled_research_cycle_shadow_no_paper_submission(tmp_path):
    as_of = datetime.now(timezone.utc) - timedelta(days=1)

    sec_http = HttpJsonClient(base_headers={"User-Agent": DEFAULT_USER_AGENT}, rate_limiter=MinIntervalRateLimiter(0.15), provider="sec-edgar")
    sec = SecEdgarClient(http_client=sec_http, cache=ProviderCache(clock=time.monotonic), user_agent=DEFAULT_USER_AGENT)

    market_http = HttpJsonClient(
        base_headers={"APCA-API-KEY-ID": os.environ["ALPACA_MARKET_DATA_API_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_MARKET_DATA_API_SECRET"]},
        rate_limiter=MinIntervalRateLimiter(0.35), provider="alpaca-data",
    )
    market = AlpacaMarketDataClient(
        api_key=os.environ["ALPACA_MARKET_DATA_API_KEY"], api_secret=os.environ["ALPACA_MARKET_DATA_API_SECRET"],
        http_client=market_http, cache=ProviderCache(clock=time.monotonic),
    )

    registry = EvidenceProviderRegistry(
        fundamentals=RealFundamentalsEvidenceProvider(sec), market=RealMarketEvidenceProvider(market),
        filings=RealFilingEvidenceProvider(sec), news=None, sentiment=None, portfolio_context=None,
        market_data_client=market, sec_client=sec,
    )

    config = ScheduledResearchConfiguration(
        universe_id="real-smoke", max_candidates_per_cycle=1, experiment_policy=experiment_policy.SHADOW_ENHANCED,
        submit_paper_orders=False, require_complete_evidence=True, require_point_in_time_safe=True,
        continue_on_symbol_failure=True, provider_mode=PROVIDER_MODE_REAL, config_hash="real-smoke-hash",
    )

    db_path = tmp_path / "real_cycle_smoke.sqlite3"
    conn = connect(db_path)
    research_config = load_research_config()

    result = run_scheduled_research_cycle(
        as_of=as_of, symbols=("AAPL",), configuration=config, conn=conn,
        cycle_repository=SQLiteResearchCycleRepository(conn), universe=default_universe(),
        screening_config=load_screening_config(), scoring_config=load_scoring_config(),
        evidence_providers=registry, research_provider=DeterministicResearchProvider(),
        research_provider_name="deterministic", research_model_name="deterministic-v1",
        research_configuration=research_config, research_repository=SQLiteResearchRepository(conn),
        prompt_registry=PromptRegistry(), portfolio=PortfolioState(account_equity=Decimal("100000"), settled_cash=Decimal("100000"), as_of=as_of),
        paper_submitter=None,  # structurally cannot submit — proves "no paper submission" for this smoke test
        clock=lambda: datetime.now(timezone.utc), git_sha="real-smoke-sha",
    )

    assert len(result.symbol_results) == 1
    symbol_result = result.symbol_results[0]
    assert symbol_result.status == "COMPLETED"
    assert symbol_result.snapshot_id is not None
    assert symbol_result.baseline_paper_submitted is False  # never submits in this test
    assert symbol_result.enhanced_recommendation_id is not None  # enhanced arm still generated + evaluable

    snapshot_row = conn.execute(
        "SELECT point_in_time_safe FROM research_evidence_snapshots WHERE snapshot_id = ?", (symbol_result.snapshot_id,),
    ).fetchone()
    assert snapshot_row is not None
    assert bool(snapshot_row["point_in_time_safe"]) is True
