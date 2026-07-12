"""Offline single-candidate analysis service (closes stories 1B.2/1C.1-1C.4).

A deterministic APPLICATION SERVICE, not an autonomous agent: it takes
already-fetched, typed fixture data for one candidate and runs the fixed
pipeline — validate symbol, screen, aggregate pre-classified Reddit records,
score, size risk, build+validate a frozen recommendation, persist. It never
retrieves live data and never calls Reddit, Robinhood, or Claude. It never
places a paper fill or a real order — paper-order simulation is a later
slice (paper/ledger.py already exists for that, untouched here).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..analysis.scorer import ScoringConfig, ScoringIncompleteError, compute_composite_score
from ..analysis.screener import ScreeningCandidate, ScreeningConfig, screen_candidate
from ..analysis.sentiment import Classifier, KeywordClassifier, RedditRecord, aggregate
from ..hashing import hash_config
from ..models.trading_models import (
    CatalystRiskFlags,
    FundamentalSnapshot,
    MarketDataSnapshot,
    PortfolioState,
    SecuritySnapshot,
    TechnicalFactorInput,
)
from ..recommendations.builder import FrozenRecommendation, build_recommendation
from ..risk.position_sizing import IncompleteStateError, RiskInputs, compute_position_plan
from ..storage.trading_repositories import save_candidate_score, save_frozen_recommendation, save_screening_run
from ..universe.tickers import TickerUniverse, UnknownSymbolError


@dataclass(frozen=True)
class CandidateInput:
    symbol: str
    run_id: str
    idempotency_key: str
    security: SecuritySnapshot
    market: MarketDataSnapshot
    fundamentals: FundamentalSnapshot
    technical: TechnicalFactorInput | None = None
    catalyst: CatalystRiskFlags | None = None
    reddit_records: tuple[RedditRecord, ...] = ()
    reddit_window_start: float | None = None
    reddit_window_end: float | None = None
    portfolio: PortfolioState | None = None
    stop_price: float | None = None
    model_version: str = "milestone2-0.1"
    prompt_version: str = "none-deterministic"


@dataclass(frozen=True)
class AnalysisResult:
    recommendation: FrozenRecommendation
    newly_persisted: bool


def analyze_candidate(
    candidate: CandidateInput,
    universe: TickerUniverse,
    screening_config: ScreeningConfig,
    scoring_config: ScoringConfig,
    conn: sqlite3.Connection,
    now: datetime,
    classifier: Classifier | None = None,
) -> AnalysisResult:
    """Run the full offline pipeline for one candidate and persist the result.

    Never touches the network, a broker, Reddit, or Claude — every input is
    already-fetched, typed fixture data supplied by the caller.
    """
    missing_data_reasons: list[str] = []
    data_timestamps: dict[str, str] = {}

    try:
        universe.require(candidate.symbol)
    except UnknownSymbolError as exc:
        missing_data_reasons.append(f"symbol validation: {exc}")

    if candidate.security.freshness:
        data_timestamps["security"] = candidate.security.freshness.as_of.isoformat()
    if candidate.market.freshness:
        data_timestamps["market"] = candidate.market.freshness.as_of.isoformat()
    if candidate.fundamentals.freshness:
        data_timestamps["fundamentals"] = candidate.fundamentals.freshness.as_of.isoformat()

    screening_result = None
    composite_score = None
    position_plan = None
    reddit_component = None

    if not missing_data_reasons:
        # 2. Screener — hard eligibility gates only.
        screen_input = ScreeningCandidate(
            security=candidate.security, market=candidate.market, fundamentals=candidate.fundamentals,
            technical=candidate.technical, catalyst=candidate.catalyst,
        )
        screening_result = screen_candidate(screen_input, screening_config, now)
        # One candidate per run_id in this offline slice; a future batch
        # screener would set universe_count/passed_count to the full run size.
        save_screening_run(
            conn, candidate.run_id, screening_config.config_hash,
            universe_count=1, passed_count=1 if screening_result.passed else 0, ran_at=now.isoformat(),
        )

        if screening_result.passed:
            # 3. Aggregate already-stored, already-classified Reddit records.
            reddit_net_sentiment = None
            if candidate.reddit_records and candidate.reddit_window_start is not None and candidate.reddit_window_end is not None:
                agg = aggregate(
                    list(candidate.reddit_records), candidate.symbol,
                    candidate.reddit_window_start, candidate.reddit_window_end,
                    classifier=classifier or KeywordClassifier(),
                )
                reddit_net_sentiment = agg.net_sentiment
                reddit_component = {
                    "weight": scoring_config.weights["reddit"],
                    "net_sentiment": round(agg.net_sentiment, 4),
                    "total_mentions": agg.total_mentions,
                    "unique_authors": agg.unique_authors,
                    "note": "sentiment, not fact; untrusted source; capped at 10% of score",
                }
                data_timestamps["reddit"] = now.isoformat()

            # 4. Deterministic composite score.
            try:
                composite_score = compute_composite_score(
                    candidate.symbol, scoring_config, candidate.fundamentals, candidate.technical,
                    candidate.catalyst, reddit_net_sentiment, now, data_timestamp=now.isoformat(),
                )
                save_candidate_score(conn, candidate.run_id, composite_score)
            except ScoringIncompleteError as exc:
                missing_data_reasons.extend(exc.reasons)

            # 5. Risk engine — only attempted once screening passed and scoring succeeded.
            if composite_score is not None:
                position_plan = _run_risk_engine(candidate, missing_data_reasons, now)

    combined_config_hash = hash_config({
        "screening": screening_config.config_hash,
        "scoring": scoring_config.config_hash,
    })

    rec = build_recommendation(
        symbol=candidate.symbol,
        idempotency_key=candidate.idempotency_key,
        run_id=candidate.run_id,
        screening_result=screening_result,
        composite_score=composite_score,
        position_plan=position_plan,
        price_at_rec=float(candidate.market.price) if candidate.market.price is not None else None,
        data_timestamps=data_timestamps,
        warnings=[],
        missing_data_reasons=missing_data_reasons,
        reddit_component=reddit_component,
        model_version=candidate.model_version,
        prompt_version=candidate.prompt_version,
        config_hash=combined_config_hash,
        now=now,
        rationale_text=_rationale(candidate.symbol, screening_result, composite_score, missing_data_reasons),
    )

    newly_persisted = save_frozen_recommendation(conn, rec)
    return AnalysisResult(recommendation=rec, newly_persisted=newly_persisted)


def _run_risk_engine(candidate: CandidateInput, missing_data_reasons: list[str], now: datetime):
    try:
        if candidate.market.price is None:
            raise IncompleteStateError("market price is unknown — fail closed (NO_ACTION)")
        if candidate.stop_price is None:
            raise IncompleteStateError("stop_price is unknown — fail closed (NO_ACTION)")
        if candidate.market.freshness is None:
            raise IncompleteStateError("market data freshness is unknown — fail closed (NO_ACTION)")
        portfolio = candidate.portfolio
        if portfolio is None or portfolio.as_of is None:
            raise IncompleteStateError("portfolio state is unknown — fail closed (NO_ACTION)")

        sector = candidate.security.sector
        plan = compute_position_plan(RiskInputs(
            account_equity=float(portfolio.account_equity) if portfolio.account_equity is not None else None,
            settled_cash=float(portfolio.settled_cash) if portfolio.settled_cash is not None else None,
            entry_price=float(candidate.market.price),
            stop_price=candidate.stop_price,
            price_as_of_epoch=candidate.market.freshness.as_of.timestamp(),
            now_epoch=now.timestamp(),
            existing_position_shares=portfolio.shares_held(candidate.symbol),
            portfolio_exposure_fraction=portfolio.portfolio_exposure_fraction,
            account_state_as_of_epoch=portfolio.as_of.timestamp(),
            avg_daily_dollar_volume=(
                float(candidate.market.avg_daily_dollar_volume)
                if candidate.market.avg_daily_dollar_volume is not None else None
            ),
            days_to_earnings=candidate.catalyst.days_to_earnings if candidate.catalyst else None,
            earnings_date_known=bool(candidate.catalyst and candidate.catalyst.earnings_date_known),
            sector=sector,
            sector_exposure_fraction=portfolio.sector_exposure_fraction.get(sector, 0.0),
            current_daily_loss_fraction=portfolio.daily_loss_fraction,
            current_drawdown_fraction=portfolio.drawdown_fraction,
        ))
        return plan
    except IncompleteStateError as exc:
        missing_data_reasons.append(f"risk engine: {exc}")
        return None


def _rationale(symbol: str, screening_result, composite_score, missing_data_reasons: list[str]) -> str:
    if screening_result is None:
        screen_state = "not screened (fail-closed before screening)"
    elif not screening_result.passed:
        screen_state = "screened out"
    else:
        screen_state = "screened in"
    score_state = f"score={composite_score.total_score}" if composite_score else "score=n/a"
    return (
        f"Deterministic offline analysis of {symbol}: {screen_state}, {score_state}, "
        f"{len(missing_data_reasons)} missing-data reason(s). All numbers computed by Python, none by an LLM."
    )
