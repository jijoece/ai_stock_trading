"""Idempotent, resumable scheduled research-cycle service (docs/milestone-6.md
Step 14). This is the vertical-slice glue: it is a *loop* over a bounded
candidate set that calls existing, unchanged Milestone 1-5 machinery per
symbol — it does not reimplement screening, scoring, risk sizing,
recommendation freezing, research orchestration, the deterministic overlay,
or experiment assignment.

Ownership, exactly as docs/milestone-6.md requires:
    Evidence providers   -> retrieve raw facts (evidence_providers/)
    Claude                -> analyze supplied evidence only (research/orchestration.py, unchanged)
    This module            -> sequence calls, decide execution eligibility via
                              experiment_policy.py, persist cycle bookkeeping
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Protocol

from ..analysis.scorer import ScoringConfig
from ..analysis.screener import ScreeningConfig
from ..evidence_providers.evidence_adapters import (
    RealFilingEvidenceProvider,
    RealFundamentalsEvidenceProvider,
    RealMarketEvidenceProvider,
    RealNewsEvidenceProvider,
    RealSentimentEvidenceProvider,
)
from ..evidence_providers.fundamentals import normalize_fundamentals
from ..evidence_providers.market_data_provider import AlpacaMarketDataClient
from ..evidence_providers.normalization import BLOCKING_OUTCOMES, classify_snapshot_outcome
from ..evidence_providers.portfolio_context import LedgerPortfolioContextProvider
from ..evidence_providers.sec_provider import SecEdgarClient
from ..execution.config import ExecutionConfig
from ..models.trading_models import (
    DataFreshness,
    FundamentalSnapshot,
    MarketDataSnapshot,
    PortfolioState,
    SecuritySnapshot,
)
from ..recommendations.builder import FrozenRecommendation, SIDE_BUY_CANDIDATE
from ..services.analyze_candidate import AnalysisResult, CandidateInput, analyze_candidate
from ..storage.trading_repositories import load_recommendation, save_frozen_recommendation
from ..universe.tickers import TickerUniverse
from . import experiment_policy
from .configuration import ResearchConfiguration
from .evidence import EvidenceBundle, build_evidence_snapshot
from .experiment import build_experiment_assignments
from .models import EvidenceSnapshot, ExperimentAssignment, ResearchOverlayDecision
from .orchestration import ResearchRepository, analyze_with_research_committee
from .overlay import apply_research_overlay
from .prompt_registry import PromptRegistry
from .provider_protocol import ResearchModelProvider
from .recommendation_overlay import apply_overlay_to_recommendation

CYCLE_STATUS_RUNNING = "RUNNING"
CYCLE_STATUS_COMPLETED = "COMPLETED"
CYCLE_STATUS_PARTIALLY_COMPLETE = "PARTIALLY_COMPLETE"
CYCLE_STATUS_FAILED = "FAILED"

SYMBOL_STATUS_COMPLETED = "COMPLETED"
SYMBOL_STATUS_SKIPPED = "SKIPPED"
SYMBOL_STATUS_FAILED = "FAILED"

PROVIDER_MODE_FIXTURE = "fixture"
PROVIDER_MODE_REAL = "real"
KNOWN_PROVIDER_MODES = (PROVIDER_MODE_FIXTURE, PROVIDER_MODE_REAL)

DEFAULT_STOP_LOSS_FRACTION = Decimal("0.08")  # matches cli.py::analyze's existing PoC convention


class ScheduledResearchConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScheduledResearchConfiguration:
    universe_id: str
    max_candidates_per_cycle: int
    experiment_policy: str
    submit_paper_orders: bool
    require_complete_evidence: bool
    require_point_in_time_safe: bool
    continue_on_symbol_failure: bool
    provider_mode: str
    config_hash: str

    def __post_init__(self) -> None:
        if self.provider_mode not in KNOWN_PROVIDER_MODES:
            raise ScheduledResearchConfigError(f"provider_mode {self.provider_mode!r} is not one of {KNOWN_PROVIDER_MODES} — fails closed")
        experiment_policy.validate_experiment_policy(self.experiment_policy)
        if self.max_candidates_per_cycle <= 0:
            raise ScheduledResearchConfigError("max_candidates_per_cycle must be > 0")


@dataclass(frozen=True)
class EvidenceProviderRegistry:
    """Bundles the concrete real-or-fixture evidence adapters for one cycle
    run — everything here already satisfies `research/evidence.py`'s
    existing Protocols (`.fetch(symbol, as_of) -> EvidenceBundle`).

    `news`/`sentiment` are `None` when the corresponding real provider is not
    configured (no API key / no Reddit credentials) — the caller decides
    this explicitly (docs/milestone-6.md: "no provider selected silently"),
    and an unconfigured optional provider is excluded from the evidence
    snapshot entirely rather than contributing a permanent missing-data
    entry (see `build_real_evidence_snapshot`)."""

    fundamentals: Any
    market: Any
    filings: Any
    news: Any | None
    sentiment: Any | None
    portfolio_context: Any | None
    market_data_client: Any  # exposes get_quote/get_price_history for baseline CandidateInput mapping
    sec_client: Any | None = None  # exposes get_company_facts for baseline fundamentals mapping


@dataclass(frozen=True)
class SymbolCycleResult:
    symbol: str
    status: str
    snapshot_id: str | None = None
    evidence_outcome: str | None = None
    research_run_id: str | None = None
    experiment_id: str | None = None
    baseline_recommendation_id: str | None = None
    enhanced_recommendation_id: str | None = None
    baseline_side: str | None = None
    enhanced_side: str | None = None
    baseline_paper_submitted: bool = False
    failure_reason: str | None = None


@dataclass(frozen=True)
class ResearchCycleResult:
    cycle_id: str
    universe_id: str
    as_of: datetime
    status: str
    symbol_results: tuple[SymbolCycleResult, ...]
    reused_existing_cycle: bool


def derive_cycle_id(universe_id: str, as_of: datetime, configuration_hash: str) -> str:
    payload = json.dumps({"universe_id": universe_id, "as_of": as_of.isoformat(), "config_hash": configuration_hash}, sort_keys=True)
    return "cycle-" + hashlib.sha256(payload.encode()).hexdigest()[:32]


def _candidate_run_id(cycle_id: str, symbol: str) -> str:
    return f"{cycle_id}-{symbol}"


# --- Real-data CandidateInput mapping (baseline arm) ------------------------

def build_real_candidate_input(
    symbol: str, as_of: datetime, *, universe: TickerUniverse, providers: EvidenceProviderRegistry,
    run_id: str, idempotency_key: str, portfolio: PortfolioState | None,
) -> tuple[CandidateInput, dict[str, float]]:
    """Maps real evidence-provider output into the existing typed
    `CandidateInput` snapshots the deterministic screener/scorer/risk engine
    already consume — no new deterministic logic, only data mapping.

    Known limitation (see docs/milestone6-real-evidence-continuous-evaluation.md):
    corporate-status flags this milestone's minimum provider set does not
    source (operating_history_years, bankruptcy_or_distress,
    going_concern_warning, shell_company_flag) are left `None` (unknown) —
    the screener's own fail-closed design (docs/milestone-2.md: "unknown
    critical input is itself a hard failure") then legitimately screens the
    candidate out or marks it incomplete rather than this mapper fabricating
    a favorable default.
    """
    security = universe.get(symbol)
    sec_snapshot = SecuritySnapshot(
        symbol=symbol, name=security.name if security else symbol, exchange=security.exchange if security else "",
        sector=security.sector if security else "", is_otc=security.is_otc if security else None,
        is_active=security.is_active if security else None,
        freshness=DataFreshness(source="universe", as_of=as_of),
    )

    lookback_start = (as_of - timedelta(days=30)).date()
    bars = (
        providers.market_data_client.get_price_history(symbol, start=lookback_start, end=as_of.date(), as_of=as_of)
        if providers.market_data_client is not None else ()
    )
    price = Decimal(str(bars[-1].close)) if bars else None
    avg_daily_dollar_volume = (
        Decimal(str(sum(b.volume for b in bars) / len(bars))) * price if bars and price else None
    )
    bar_freshness = (
        DataFreshness(source="alpaca-data", as_of=datetime(bars[-1].session_date.year, bars[-1].session_date.month, bars[-1].session_date.day, 21, 0, tzinfo=as_of.tzinfo))
        if bars else None
    )

    deterministic_factors: dict[str, float] = {}
    fundamentals_snapshot = FundamentalSnapshot(symbol=symbol)
    market_cap = None
    if providers.sec_client is not None:
        facts = providers.sec_client.get_company_facts(symbol, as_of=as_of)
        normalized = normalize_fundamentals(facts)
        if normalized:
            deterministic_factors = {k: float(v.value) for k, v in normalized.items()}
            shares = normalized.get("shares_outstanding")
            fundamentals_snapshot = FundamentalSnapshot(
                symbol=symbol,
                as_of=datetime(normalized["revenue"].filed_at.year, normalized["revenue"].filed_at.month, normalized["revenue"].filed_at.day, tzinfo=as_of.tzinfo) if "revenue" in normalized else None,
                revenue_growth_yoy=float(normalized["revenue_growth_yoy"].value) if "revenue_growth_yoy" in normalized else None,
                gross_margin=float(normalized["gross_margin"].value) if "gross_margin" in normalized else None,
                operating_margin=float(normalized["operating_margin"].value) if "operating_margin" in normalized else None,
                cash=normalized["cash"].value if "cash" in normalized else None,
                debt=normalized["long_term_debt"].value if "long_term_debt" in normalized else None,
                shares_outstanding=shares.value if shares else None,
                freshness=DataFreshness(source="sec-edgar", as_of=as_of),
            )
            if shares is not None and price is not None:
                market_cap = price * shares.value

    market_snapshot = MarketDataSnapshot(
        symbol=symbol, price=price, avg_daily_dollar_volume=avg_daily_dollar_volume,
        market_cap=market_cap, freshness=bar_freshness,
    )

    stop_price = float(price * (1 - DEFAULT_STOP_LOSS_FRACTION)) if price is not None else None

    candidate = CandidateInput(
        symbol=symbol, run_id=run_id, idempotency_key=idempotency_key, security=sec_snapshot,
        market=market_snapshot, fundamentals=fundamentals_snapshot, technical=None, catalyst=None,
        portfolio=portfolio, stop_price=stop_price, model_version="milestone6-real-0.1",
        prompt_version="none-deterministic",
    )
    return candidate, deterministic_factors


def build_real_evidence_snapshot(
    symbol: str, as_of: datetime, *, providers: EvidenceProviderRegistry, deterministic_factors: dict[str, float],
    config_hash: str, git_sha: str, clock: Callable[[], datetime],
    max_evidence_items: int, max_items_per_source_category: int,
) -> EvidenceSnapshot:
    sentiment_result = providers.sentiment.fetch(symbol, as_of) if providers.sentiment is not None else None
    sentiment_metrics: dict[str, Any] = {}
    if isinstance(sentiment_result, EvidenceBundle) and sentiment_result.evidence_items:
        sentiment_metrics = dict(sentiment_result.evidence_items[0].normalized_values)

    # News/sentiment are optional, environmentally-pending providers in this
    # milestone's minimum provider set (docs/milestone-6.md: "no provider
    # selected silently"). An *unconfigured* optional provider is excluded
    # from the snapshot's provider list entirely rather than included only to
    # immediately contribute a permanent "missing data" entry — this mirrors
    # Milestone 5's own precedent of scoping the role set down to what has
    # real evidence behind it. `EvidenceProviderRegistry.news`/`.sentiment`
    # are `None` when not configured; the caller decides that, not this
    # function.
    # Every provider slot may legitimately be `None` (not configured /
    # explicitly disabled) — excluded from the snapshot's provider list
    # entirely rather than passed through and crashing `build_evidence_snapshot`
    # (docs/milestone-6.md: "absent credentials fail closed").
    active_providers = [
        p for p in (providers.fundamentals, providers.market, providers.filings, providers.news, providers.sentiment)
        if p is not None
    ]

    return build_evidence_snapshot(
        symbol, as_of, deterministic_factors=deterministic_factors, sentiment_metrics=sentiment_metrics,
        providers=tuple(active_providers),
        portfolio_context_provider=providers.portfolio_context, config_hash=config_hash, git_sha=git_sha,
        clock=clock, max_evidence_items=max_evidence_items, max_items_per_source_category=max_items_per_source_category,
    )


class ResearchCycleRepository(Protocol):
    def get_cycle_status(self, cycle_id: str) -> str | None: ...
    def save_cycle_started(self, cycle_id: str, universe_id: str, as_of: datetime, configuration_hash: str, experiment_policy: str, provider_mode: str, started_at: datetime) -> None: ...
    def get_symbol_result(self, cycle_id: str, symbol: str) -> SymbolCycleResult | None: ...
    def save_symbol_result(self, cycle_id: str, result: SymbolCycleResult, created_at: datetime, completed_at: datetime | None) -> None: ...
    def mark_cycle_finished(self, cycle_id: str, status: str, completed_at: datetime) -> None: ...


def run_scheduled_research_cycle(
    *,
    as_of: datetime,
    symbols: tuple[str, ...],
    configuration: ScheduledResearchConfiguration,
    conn: sqlite3.Connection,
    cycle_repository: ResearchCycleRepository,
    universe: TickerUniverse,
    screening_config: ScreeningConfig,
    scoring_config: ScoringConfig,
    evidence_providers: EvidenceProviderRegistry,
    research_provider: ResearchModelProvider,
    research_provider_name: str,
    research_model_name: str,
    research_configuration: ResearchConfiguration,
    research_repository: ResearchRepository,
    prompt_registry: PromptRegistry,
    portfolio: PortfolioState | None,
    paper_submitter: Callable[[str], Any] | None,
    clock: Callable[[], datetime],
    git_sha: str,
) -> ResearchCycleResult:
    """`paper_submitter`, when supplied, is a callable `(recommendation_id) ->
    PaperExecutionOutcome` — kept injectable so this module never imports the
    isolated-runtime client directly; the CLI wires a real one only when
    `configuration.submit_paper_orders` is true."""
    bounded_symbols = tuple(symbols[: configuration.max_candidates_per_cycle])
    cycle_id = derive_cycle_id(configuration.universe_id, as_of, configuration.config_hash)

    existing_status = cycle_repository.get_cycle_status(cycle_id)
    reused = existing_status is not None
    if not reused:
        cycle_repository.save_cycle_started(
            cycle_id, configuration.universe_id, as_of, configuration.config_hash,
            configuration.experiment_policy, configuration.provider_mode, clock(),
        )

    results: list[SymbolCycleResult] = []
    for symbol in bounded_symbols:
        existing = cycle_repository.get_symbol_result(cycle_id, symbol)
        if existing is not None and existing.status in (SYMBOL_STATUS_COMPLETED, SYMBOL_STATUS_SKIPPED):
            results.append(existing)
            continue

        try:
            result = _run_symbol(
                symbol, as_of, cycle_id=cycle_id, configuration=configuration, conn=conn, universe=universe,
                screening_config=screening_config, scoring_config=scoring_config, evidence_providers=evidence_providers,
                research_provider=research_provider, research_provider_name=research_provider_name,
                research_model_name=research_model_name, research_configuration=research_configuration,
                research_repository=research_repository, prompt_registry=prompt_registry, portfolio=portfolio,
                paper_submitter=paper_submitter, clock=clock, git_sha=git_sha,
            )
        except Exception as exc:  # per-symbol failure isolation — never loses the whole cycle
            result = SymbolCycleResult(symbol=symbol, status=SYMBOL_STATUS_FAILED, failure_reason=str(exc))
            if not configuration.continue_on_symbol_failure:
                cycle_repository.save_symbol_result(cycle_id, result, clock(), clock())
                cycle_repository.mark_cycle_finished(cycle_id, CYCLE_STATUS_FAILED, clock())
                raise

        cycle_repository.save_symbol_result(cycle_id, result, clock(), clock())
        results.append(result)

    failed = sum(1 for r in results if r.status == SYMBOL_STATUS_FAILED)
    if failed == 0:
        cycle_status = CYCLE_STATUS_COMPLETED
    elif failed < len(results):
        cycle_status = CYCLE_STATUS_PARTIALLY_COMPLETE
    else:
        cycle_status = CYCLE_STATUS_FAILED
    cycle_repository.mark_cycle_finished(cycle_id, cycle_status, clock())

    return ResearchCycleResult(
        cycle_id=cycle_id, universe_id=configuration.universe_id, as_of=as_of, status=cycle_status,
        symbol_results=tuple(results), reused_existing_cycle=reused,
    )


def _run_symbol(
    symbol: str, as_of: datetime, *, cycle_id: str, configuration: ScheduledResearchConfiguration,
    conn: sqlite3.Connection, universe: TickerUniverse, screening_config: ScreeningConfig,
    scoring_config: ScoringConfig, evidence_providers: EvidenceProviderRegistry,
    research_provider: ResearchModelProvider, research_provider_name: str, research_model_name: str,
    research_configuration: ResearchConfiguration, research_repository: ResearchRepository,
    prompt_registry: PromptRegistry, portfolio: PortfolioState | None,
    paper_submitter: Callable[[str], Any] | None, clock: Callable[[], datetime], git_sha: str,
) -> SymbolCycleResult:
    candidate_run_id = _candidate_run_id(cycle_id, symbol)
    idempotency_key = f"{cycle_id}:{symbol}:baseline"

    candidate, deterministic_factors = build_real_candidate_input(
        symbol, as_of, universe=universe, providers=evidence_providers, run_id=candidate_run_id,
        idempotency_key=idempotency_key, portfolio=portfolio,
    )
    baseline_analysis: AnalysisResult = analyze_candidate(candidate, universe, screening_config, scoring_config, conn, as_of)
    baseline_rec = baseline_analysis.recommendation

    snapshot = build_real_evidence_snapshot(
        symbol, as_of, providers=evidence_providers, deterministic_factors=deterministic_factors,
        config_hash=research_configuration.config_hash, git_sha=git_sha, clock=clock,
        max_evidence_items=research_configuration.max_evidence_items,
        max_items_per_source_category=research_configuration.max_items_per_source_category,
    )
    from ..storage.research_repositories import save_evidence_snapshot
    save_evidence_snapshot(conn, snapshot)

    required_categories = ("fundamentals",)
    outcome, _reasons = classify_snapshot_outcome(
        snapshot, required_categories=required_categories,
        require_point_in_time_safe=configuration.require_point_in_time_safe,
    )
    evidence_blocks_enhanced = configuration.require_complete_evidence and outcome in BLOCKING_OUTCOMES

    if evidence_blocks_enhanced:
        orchestration_status = "ANALYSIS_INCOMPLETE"
        decision = None
        research_run_id = None
    else:
        orchestration_result = analyze_with_research_committee(
            snapshot, provider=research_provider, provider_name=research_provider_name,
            model_name=research_model_name, prompt_registry=prompt_registry,
            research_repository=research_repository, configuration=research_configuration,
            clock=clock, run_mode=research_provider_name,
        )
        orchestration_status = orchestration_result.status
        decision = orchestration_result.decision
        research_run_id = orchestration_result.research_run_id

    baseline_score = None
    factors = baseline_rec.payload.get("factors") or []
    if baseline_rec.payload.get("score") is not None:
        baseline_score = Decimal(str(baseline_rec.payload["score"]))

    overlay: ResearchOverlayDecision = apply_research_overlay(
        decision, orchestration_status=orchestration_status, baseline_score=baseline_score,
        configuration=research_configuration,
    )
    from ..storage.research_repositories import save_overlay_decision
    save_overlay_decision(conn, overlay, clock())

    enhanced_rec: FrozenRecommendation = apply_overlay_to_recommendation(baseline_rec, overlay)
    save_frozen_recommendation(conn, enhanced_rec)

    baseline_assignment, enhanced_assignment = build_experiment_assignments(
        candidate_run_id=candidate_run_id, symbol=symbol, as_of=as_of,
        baseline_recommendation_id=baseline_rec.payload["rec_id"], enhanced_recommendation_id=enhanced_rec.payload["rec_id"],
    )
    from ..storage.research_repositories import save_experiment_assignment
    save_experiment_assignment(conn, baseline_assignment, clock())
    save_experiment_assignment(conn, enhanced_assignment, clock())

    baseline_submitted = False
    if (
        configuration.submit_paper_orders and paper_submitter is not None
        and experiment_policy.may_submit_baseline(configuration.experiment_policy)
        and baseline_rec.payload["side"] == SIDE_BUY_CANDIDATE
    ):
        paper_submitter(baseline_rec.payload["rec_id"])
        baseline_submitted = True
    # The enhanced arm never submits — experiment_policy.may_submit_enhanced() is
    # always False for every supported policy; no call site exists for it here.

    return SymbolCycleResult(
        symbol=symbol, status=SYMBOL_STATUS_COMPLETED, snapshot_id=snapshot.snapshot_id,
        evidence_outcome=outcome, research_run_id=research_run_id, experiment_id=baseline_assignment.experiment_id,
        baseline_recommendation_id=baseline_rec.payload["rec_id"], enhanced_recommendation_id=enhanced_rec.payload["rec_id"],
        baseline_side=baseline_rec.payload["side"], enhanced_side=enhanced_rec.payload["side"],
        baseline_paper_submitted=baseline_submitted,
    )
