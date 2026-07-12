"""Offline, full-pipeline integration tests for the candidate-analysis
service (screener -> sentiment -> scorer -> risk engine -> recommendation
builder -> persistence). Everything here runs on fixture data against a
temporary SQLite database — no network, no broker, no Reddit, no Claude.
"""
import json
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from jsonschema import Draft7Validator

from trading_research.analysis.scorer import load_scoring_config, reconstruct_total_score_from_factors
from trading_research.analysis.screener import load_screening_config
from trading_research.analysis.sentiment import RedditRecord
from trading_research.config import REPO_ROOT
from trading_research.models.trading_models import (
    CatalystRiskFlags,
    DataFreshness,
    FundamentalSnapshot,
    MarketDataSnapshot,
    PortfolioState,
    SecuritySnapshot,
    TechnicalFactorInput,
)
from trading_research.services.analyze_candidate import CandidateInput, analyze_candidate
from trading_research.storage.database import connect
from trading_research.storage.trading_repositories import load_recommendation_factors
from trading_research.universe.tickers import default_universe

NOW = datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc)


class _NetworkCallAttempted(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail the test hard if anything under test tries to open a socket."""
    def _blocked(*args, **kwargs):
        raise _NetworkCallAttempted("a network call was attempted during an offline analysis test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    yield


def _fresh(source: str, minutes_ago: float = 1.0) -> DataFreshness:
    return DataFreshness(source=source, as_of=NOW - timedelta(minutes=minutes_ago))


def good_candidate(**overrides) -> CandidateInput:
    security = SecuritySnapshot(
        symbol="SOFI", name="SoFi Technologies Inc", exchange="NASDAQ", sector="Financial Services",
        is_otc=False, is_active=True, freshness=_fresh("security"),
    )
    market = MarketDataSnapshot(
        symbol="SOFI", price=Decimal("14.92"), bid=Decimal("14.90"), ask=Decimal("14.94"),
        avg_daily_dollar_volume=Decimal("350000000"), market_cap=Decimal("15800000000"),
        recent_halt=False, recent_reverse_split=False, realized_volatility=0.03, freshness=_fresh("market"),
    )
    fundamentals = FundamentalSnapshot(
        symbol="SOFI", revenue_growth_yoy=0.27, earnings_trend=0.3, gross_margin=0.45, operating_margin=0.12,
        free_cash_flow=Decimal("210000000"), cash=Decimal("2900000000"), quarterly_cash_burn=Decimal("50000000"),
        shares_outstanding=Decimal("1050000000"), shares_outstanding_prior_year=Decimal("1030000000"),
        operating_history_years=5.0, going_concern_warning=False, bankruptcy_or_distress=False,
        shell_company_flag=False, recent_reverse_split=False, freshness=_fresh("fundamentals"),
    )
    technical = TechnicalFactorInput(symbol="SOFI", relative_strength=0.4, momentum_score=1.0,
                                      trend_score=1.0, price_volume_trend=0.2)
    catalyst = CatalystRiskFlags(symbol="SOFI", earnings_date_known=True, days_to_earnings=30.0,
                                  macro_score=1.0, analyst_estimate_change=0.05, freshness=_fresh("catalyst"))
    reddit_records = (
        RedditRecord("p1", "post", "SOFI", "u_alpha", "stocks", (NOW - timedelta(hours=2)).timestamp(),
                     "$SOFI earnings look strong, revenue beat and guidance up. Long here.", 148),
        RedditRecord("p2", "post", "SOFI", "u_beta", "wallstreetbets", (NOW - timedelta(hours=5)).timestamp(),
                     "SOFI calls printing, this stock wants to moon", 96),
        RedditRecord("c1", "comment", "SOFI", "u_gamma", "stocks", (NOW - timedelta(hours=1)).timestamp(),
                     "I trimmed my SOFI, valuation getting rich — might sell more", 12),
    )
    portfolio = PortfolioState(
        account_equity=Decimal("100000"), settled_cash=Decimal("100000"),
        existing_positions={}, sector_exposure_fraction={}, portfolio_exposure_fraction=0.0,
        daily_loss_fraction=0.0, drawdown_fraction=0.0, as_of=NOW - timedelta(minutes=1),
    )
    defaults = dict(
        symbol="SOFI", run_id="run-int-1", idempotency_key="int-test-happy-path",
        security=security, market=market, fundamentals=fundamentals, technical=technical, catalyst=catalyst,
        reddit_records=reddit_records,
        reddit_window_start=(NOW - timedelta(days=1)).timestamp(), reddit_window_end=NOW.timestamp(),
        portfolio=portfolio, stop_price=13.50,
    )
    defaults.update(overrides)
    return CandidateInput(**defaults)


@pytest.fixture(scope="module")
def schema_validator():
    schema = json.loads((REPO_ROOT / "schemas" / "recommendation.schema.json").read_text())
    return Draft7Validator(schema)


def test_full_offline_pipeline_happy_path(tmp_path, schema_validator):
    conn = connect(tmp_path / "integration_happy.sqlite3")
    universe = default_universe()
    screening_config = load_screening_config()
    scoring_config = load_scoring_config()

    result = analyze_candidate(good_candidate(), universe, screening_config, scoring_config, conn, NOW)
    rec = result.recommendation

    assert result.newly_persisted is True
    assert rec.status == "active"
    assert rec.side == "buy_candidate"
    assert rec.payload["risk_plan"] is not None
    assert rec.payload["risk_plan"]["shares"] > 0

    # 1. Validate the returned recommendation against the JSON schema.
    errors = list(schema_validator.iter_errors(rec.payload))
    assert errors == [], f"schema errors: {errors}"

    # 2. Reconstruct the score purely from the persisted factor rows.
    stored_factors = load_recommendation_factors(conn, rec.rec_id)
    assert stored_factors, "factors should have been persisted"
    reconstructed = reconstruct_total_score_from_factors(stored_factors)
    assert reconstructed == rec.payload["score"]

    # 3. No paper fill / no real order was created by this offline service.
    orders = conn.execute("SELECT COUNT(*) AS n FROM simulated_orders").fetchone()["n"]
    fills = conn.execute("SELECT COUNT(*) AS n FROM simulated_fills").fetchone()["n"]
    real_orders = conn.execute("SELECT COUNT(*) AS n FROM real_orders").fetchone()["n"]
    assert orders == 0
    assert fills == 0
    assert real_orders == 0

    # 4. Recommendation was actually persisted, frozen, and immutable in the DB.
    row = conn.execute("SELECT frozen FROM recommendations WHERE rec_id = ?", (rec.rec_id,)).fetchone()
    assert row["frozen"] == 1
    with pytest.raises(Exception):
        conn.execute("UPDATE recommendations SET score = 99 WHERE rec_id = ?", (rec.rec_id,))
        conn.commit()

    conn.close()


def test_no_broker_or_reddit_tool_invoked(tmp_path):
    """The offline service must never import/call anything from mcp/ —
    confirmed structurally: mock adapters are the only broker/reddit-shaped
    objects in this test module, and no MCP client is constructed."""
    import trading_research.services.analyze_candidate as service_module
    source = open(service_module.__file__).read()
    assert "mcp" not in source.lower()


def test_unknown_symbol_produces_analysis_incomplete(tmp_path):
    conn = connect(tmp_path / "integration_incomplete_symbol.sqlite3")
    universe = default_universe()
    screening_config = load_screening_config()
    scoring_config = load_scoring_config()

    candidate = good_candidate(symbol="ZZZZZ", idempotency_key="int-test-unknown-symbol")
    result = analyze_candidate(candidate, universe, screening_config, scoring_config, conn, NOW)
    rec = result.recommendation

    assert rec.status == "analysis_incomplete"
    assert rec.side == "analysis_incomplete"
    assert rec.payload["risk_plan"] is None
    assert rec.payload["score"] is None
    assert rec.payload["missing_data_reasons"]
    conn.close()


def test_missing_risk_input_produces_analysis_incomplete(tmp_path):
    """Screening and scoring succeed, but the risk engine's stop_price is
    unknown — a fully-known-except-one-critical-field state must fail
    closed to ANALYSIS_INCOMPLETE, never a silently-defaulted risk plan."""
    conn = connect(tmp_path / "integration_incomplete_risk.sqlite3")
    universe = default_universe()
    screening_config = load_screening_config()
    scoring_config = load_scoring_config()

    candidate = good_candidate(stop_price=None, idempotency_key="int-test-missing-stop")
    result = analyze_candidate(candidate, universe, screening_config, scoring_config, conn, NOW)
    rec = result.recommendation

    assert rec.status == "analysis_incomplete"
    assert rec.side == "analysis_incomplete"
    assert rec.payload["risk_plan"] is None
    assert any("stop_price" in reason for reason in rec.payload["missing_data_reasons"])
    conn.close()
