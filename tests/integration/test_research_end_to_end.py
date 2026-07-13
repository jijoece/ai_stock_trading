"""Category L: end-to-end offline vertical-slice integration tests
(docs/milestone-5.md Step 20.L).

fixture symbol -> deterministic screen -> deterministic score ->
fixture EvidenceSnapshot -> scripted Claude role outputs ->
validated ResearchDecision -> deterministic overlay ->
existing recommendation builder -> frozen recommendation -> experiment record.

Everything here runs offline against a temporary SQLite database — no
network, no broker, no Reddit, no real Claude API call.
"""
from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.analysis.scorer import load_scoring_config
from trading_research.analysis.screener import load_screening_config
from trading_research.models.trading_models import (
    CatalystRiskFlags,
    DataFreshness,
    FundamentalSnapshot,
    MarketDataSnapshot,
    PortfolioState,
    SecuritySnapshot,
    TechnicalFactorInput,
)
from trading_research.recommendations.builder import SIDE_ANALYSIS_INCOMPLETE, SIDE_BUY_CANDIDATE
from trading_research.research.configuration import ResearchConfiguration
from trading_research.research.deterministic_provider import ScriptedResearchProvider, ScriptedStep
from trading_research.research.experiment import build_experiment_assignments
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.orchestration import RUN_STATUS_ANALYSIS_INCOMPLETE, RUN_STATUS_COMPLETED, analyze_with_research_committee
from trading_research.research.overlay import apply_research_overlay
from trading_research.research.prompt_registry import PromptRegistry
from trading_research.research.recommendation_overlay import apply_overlay_to_recommendation
from trading_research.services.analyze_candidate import CandidateInput, analyze_candidate
from trading_research.storage.database import connect
from trading_research.storage.research_repositories import (
    SQLiteResearchRepository,
    save_evidence_snapshot,
    save_experiment_assignment,
    save_overlay_decision,
)
from trading_research.universe.tickers import default_universe

NOW = datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc)

ANALYST_PAYLOAD = {
    "stance": "BULLISH", "summary": "Deterministic factors and fixture evidence both point up.", "claims": [],
    "catalysts": ["product cycle"], "risks": ["valuation risk"], "uncertainties": [], "missing_data_reasons": [],
}
MANAGER_PAYLOAD = {
    "rating": "OVERWEIGHT", "confidence": 0.65, "thesis": "Supportive fundamentals and technicals.",
    "bull_case": "Growth continues, multiple expands.", "bear_case": "Growth decelerates, multiple compresses.",
    "catalysts": ["product cycle"], "risks": ["valuation risk"], "invalidation_conditions": ["growth < 0%"],
    "claims": [], "evidence_ids": [], "missing_data_reasons": [],
}


class _NetworkCallAttempted(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise _NetworkCallAttempted("a network call was attempted during an offline research test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    yield


def _fresh(source: str, minutes_ago: float = 1.0) -> DataFreshness:
    return DataFreshness(source=source, as_of=NOW - timedelta(minutes=minutes_ago))


def _good_aapl_candidate(**overrides) -> CandidateInput:
    # Note: config/screening.yaml caps max_share_price at $25 — these
    # market/fundamentals figures are deliberately SOFI-scale, not real
    # AAPL prices, so the deterministic screener actually passes. The
    # symbol is still "AAPL" purely to line up with research/fixtures.py's
    # fixture-backed evidence snapshot for this vertical slice.
    security = SecuritySnapshot(symbol="AAPL", name="Apple Inc", exchange="NASDAQ", sector="Technology", is_otc=False, is_active=True, freshness=_fresh("security"))
    market = MarketDataSnapshot(
        symbol="AAPL", price=Decimal("14.92"), bid=Decimal("14.90"), ask=Decimal("14.94"),
        avg_daily_dollar_volume=Decimal("350000000"), market_cap=Decimal("15800000000"),
        recent_halt=False, recent_reverse_split=False, realized_volatility=0.03, freshness=_fresh("market"),
    )
    fundamentals = FundamentalSnapshot(
        symbol="AAPL", revenue_growth_yoy=0.08, earnings_trend=0.2, gross_margin=0.46, operating_margin=0.30,
        free_cash_flow=Decimal("210000000"), cash=Decimal("2900000000"), quarterly_cash_burn=Decimal("50000000"),
        shares_outstanding=Decimal("1050000000"), shares_outstanding_prior_year=Decimal("1030000000"),
        operating_history_years=5.0, going_concern_warning=False, bankruptcy_or_distress=False,
        shell_company_flag=False, recent_reverse_split=False, freshness=_fresh("fundamentals"),
    )
    technical = TechnicalFactorInput(symbol="AAPL", relative_strength=0.3, momentum_score=0.8, trend_score=0.9, price_volume_trend=0.1)
    catalyst = CatalystRiskFlags(symbol="AAPL", earnings_date_known=True, days_to_earnings=45.0, macro_score=0.5, analyst_estimate_change=0.02, freshness=_fresh("catalyst"))
    portfolio = PortfolioState(
        account_equity=Decimal("100000"), settled_cash=Decimal("100000"), existing_positions={},
        sector_exposure_fraction={}, portfolio_exposure_fraction=0.0, daily_loss_fraction=0.0,
        drawdown_fraction=0.0, as_of=NOW - timedelta(minutes=1),
    )
    defaults = dict(
        symbol="AAPL", run_id="run-research-int-1", idempotency_key="research-int-happy-path",
        security=security, market=market, fundamentals=fundamentals, technical=technical, catalyst=catalyst,
        reddit_records=(), portfolio=portfolio, stop_price=13.50,
    )
    defaults.update(overrides)
    return CandidateInput(**defaults)


def _research_config(provider="scripted") -> ResearchConfiguration:
    return ResearchConfiguration(
        version=1, enabled=True, provider=provider, model="test-model", max_attempts_per_role=2,
        request_timeout_seconds=30, max_input_characters=100_000, max_evidence_items=100,
        max_items_per_source_category=25, max_claims_per_role=20, max_output_tokens=2000,
        require_point_in_time_safe=True, require_evidence_for_material_claims=True,
        fail_on_stale_required_evidence=True, allow_parallel_roles=False,
        roles=("fundamental", "technical", "bull", "bear", "manager"),
        overlay_policy_version="research-overlay.v1", overlay_allow_score_increase=False,
        overlay_allow_position_size_increase=False, overlay_incomplete_action="ANALYSIS_INCOMPLETE",
        overlay_critical_risk_action="FORCE_NO_ACTION", config_hash="c" * 64, raw={},
    )


def _happy_provider() -> ScriptedResearchProvider:
    return ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
        ("technical", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
        ("bull", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
        ("bear", 1): ScriptedStep(kind="response", payload=dict(ANALYST_PAYLOAD, stance="NEUTRAL")),
        ("manager", 1): ScriptedStep(kind="response", payload=MANAGER_PAYLOAD),
    })


def test_full_vertical_slice_happy_path(tmp_path):
    conn = connect(tmp_path / "research_e2e_happy.sqlite3")
    universe = default_universe()
    screening_config = load_screening_config()
    scoring_config = load_scoring_config()
    research_config = _research_config()

    # 1-2. Deterministic screen + score -> baseline (Arm A) recommendation.
    baseline_result = analyze_candidate(_good_aapl_candidate(), universe, screening_config, scoring_config, conn, NOW)
    baseline = baseline_result.recommendation
    assert baseline.side == SIDE_BUY_CANDIDATE
    assert baseline.payload["risk_plan"]["shares"] > 0

    # 3. Point-in-time evidence snapshot, persisted with full provenance.
    snapshot = build_fixture_snapshot("AAPL", NOW, config_hash=research_config.config_hash, git_sha="testsha", clock=lambda: NOW)
    assert save_evidence_snapshot(conn, snapshot) is True
    assert snapshot.source_records  # provenance present
    assert all(s.source_id for s in snapshot.source_records)

    # 4-10. Scripted Claude role outputs -> validated ResearchDecision.
    repo = SQLiteResearchRepository(conn)
    result = analyze_with_research_committee(
        snapshot, provider=_happy_provider(), provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=research_config,
        clock=lambda: NOW, run_mode="scripted",
    )
    assert result.status == RUN_STATUS_COMPLETED
    assert result.decision.rating == "OVERWEIGHT"

    # 11. Deterministic overlay -> existing recommendation builder -> frozen enhanced recommendation.
    overlay = apply_research_overlay(
        result.decision, orchestration_status=result.status,
        baseline_score=Decimal(str(baseline.payload["score"])), configuration=research_config,
    )
    assert overlay.action == "ALLOW_BASELINE"
    save_overlay_decision(conn, overlay, NOW)

    enhanced = apply_overlay_to_recommendation(baseline, overlay)
    assert enhanced.side == SIDE_BUY_CANDIDATE  # supportive research keeps the baseline buy
    assert enhanced.rec_id != baseline.rec_id

    from trading_research.storage.trading_repositories import save_frozen_recommendation

    assert save_frozen_recommendation(conn, enhanced) is True

    # 12. Experiment record — both arms.
    baseline_assignment, enhanced_assignment = build_experiment_assignments(
        candidate_run_id="run-research-int-1", symbol="AAPL", as_of=NOW,
        baseline_recommendation_id=baseline.rec_id, enhanced_recommendation_id=enhanced.rec_id,
    )
    save_experiment_assignment(conn, baseline_assignment, NOW)
    save_experiment_assignment(conn, enhanced_assignment, NOW)

    from trading_research.storage.research_repositories import list_experiment_assignments

    assignments = list_experiment_assignments(conn, baseline_assignment.experiment_id)
    assert {a.arm for a in assignments} == {"BASELINE", "ENHANCED"}

    # Safety: Claude never wrote anything to real_orders or simulated fills.
    assert conn.execute("SELECT COUNT(*) AS n FROM real_orders").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM simulated_fills").fetchone()["n"] == 0
    conn.close()


def test_missing_required_evidence_short_circuits_before_any_provider_call(tmp_path):
    conn = connect(tmp_path / "research_e2e_missing_evidence.sqlite3")
    research_config = _research_config()
    thin_snapshot = build_fixture_snapshot("XXXX", NOW, config_hash=research_config.config_hash, git_sha="testsha", clock=lambda: NOW)
    save_evidence_snapshot(conn, thin_snapshot)

    provider = ScriptedResearchProvider({})  # any call at all is a test failure
    repo = SQLiteResearchRepository(conn)
    result = analyze_with_research_committee(
        thin_snapshot, provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=research_config,
        clock=lambda: NOW, run_mode="scripted",
    )
    assert result.status == RUN_STATUS_ANALYSIS_INCOMPLETE
    assert provider.calls == []

    overlay = apply_research_overlay(result.decision, orchestration_status=result.status, baseline_score=None, configuration=research_config)
    assert overlay.action == "ANALYSIS_INCOMPLETE"

    # No paper-execution intent can exist for a symbol with zero recommendations.
    assert conn.execute("SELECT COUNT(*) AS n FROM paper_execution_intents").fetchone()["n"] == 0
    conn.close()


def test_malformed_output_retry_exhaustion_preserves_deterministic_baseline(tmp_path):
    conn = connect(tmp_path / "research_e2e_malformed.sqlite3")
    universe = default_universe()
    screening_config = load_screening_config()
    scoring_config = load_scoring_config()
    research_config = _research_config()

    baseline_result = analyze_candidate(
        _good_aapl_candidate(idempotency_key="research-int-malformed-path"), universe, screening_config, scoring_config, conn, NOW,
    )
    baseline = baseline_result.recommendation
    assert baseline.side == SIDE_BUY_CANDIDATE

    snapshot = build_fixture_snapshot("AAPL", NOW, config_hash=research_config.config_hash, git_sha="testsha", clock=lambda: NOW)
    save_evidence_snapshot(conn, snapshot)

    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="malformed", raw_text="not json at all"),
        ("fundamental", 2): ScriptedStep(kind="malformed", raw_text="still not json"),
        ("technical", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
        ("bull", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
        ("bear", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
    })
    repo = SQLiteResearchRepository(conn)
    result = analyze_with_research_committee(
        snapshot, provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=research_config,
        clock=lambda: NOW, run_mode="scripted",
    )
    assert result.status == RUN_STATUS_ANALYSIS_INCOMPLETE
    assert result.decision is None
    assert "manager" not in [c.role for c in provider.calls]  # never reached — fundamental exhausted first

    overlay = apply_research_overlay(result.decision, orchestration_status=result.status, baseline_score=None, configuration=research_config)
    enhanced = apply_overlay_to_recommendation(baseline, overlay)
    assert enhanced.side == SIDE_ANALYSIS_INCOMPLETE
    assert enhanced.payload["risk_plan"] is None

    # Deterministic baseline itself is completely untouched.
    assert baseline.side == SIDE_BUY_CANDIDATE
    assert baseline.payload["risk_plan"]["shares"] > 0
    conn.close()


def test_rerun_with_same_snapshot_and_prompt_version_reuses_completed_run(tmp_path):
    conn = connect(tmp_path / "research_e2e_rerun.sqlite3")
    research_config = _research_config()
    snapshot = build_fixture_snapshot("AAPL", NOW, config_hash=research_config.config_hash, git_sha="testsha", clock=lambda: NOW)
    save_evidence_snapshot(conn, snapshot)

    provider = _happy_provider()
    repo = SQLiteResearchRepository(conn)
    result1 = analyze_with_research_committee(
        snapshot, provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=research_config,
        clock=lambda: NOW, run_mode="scripted",
    )
    calls_after_first = len(provider.calls)

    result2 = analyze_with_research_committee(
        snapshot, provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=research_config,
        clock=lambda: NOW, run_mode="scripted",
    )
    assert result2.research_run_id == result1.research_run_id
    assert result2.reused_existing_run is True
    assert len(provider.calls) == calls_after_first  # no duplicate provider call

    decision_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM research_decisions WHERE research_run_id = ?", (result1.research_run_id,)
    ).fetchone()["n"]
    assert decision_rows == 1  # no duplicate recommendation/decision row
    conn.close()
