"""Immutable, read-only contracts for the Streamlit dashboard.

The outcome mapper deliberately accepts normalized values read from persisted
columns.  It never classifies free-form rationale, failure, or reason text.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import re


class DashboardOutcome(str, Enum):
    BOUGHT_OR_SUBMITTED = "BOUGHT_OR_SUBMITTED"
    BUY_CANDIDATE_NOT_SUBMITTED = "BUY_CANDIDATE_NOT_SUBMITTED"
    REJECTED = "REJECTED"
    SCREENED_OUT = "SCREENED_OUT"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    RESEARCH_INCOMPLETE = "RESEARCH_INCOMPLETE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    BUDGET_BLOCKED = "BUDGET_BLOCKED"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    DUPLICATE_PREVENTED = "DUPLICATE_PREVENTED"
    PRICE_CONDITION_NOT_MET = "PRICE_CONDITION_NOT_MET"
    NO_ACTION = "NO_ACTION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class OutcomeMapping:
    outcome: DashboardOutcome
    primary_reason_code: str
    friendly_reason: str


@dataclass(frozen=True, slots=True)
class DashboardOverview:
    as_of: datetime | None
    portfolio_value: Decimal | None
    cash: Decimal | None
    reserved_cash: Decimal | None
    open_positions: int | None
    realized_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    candidates_considered: int | None
    bought_or_submitted: int | None
    rejected: int | None
    incomplete: int | None
    pause_state: str | None
    latest_scheduler_run_id: str | None
    latest_research_cycle_id: str | None


@dataclass(frozen=True, slots=True)
class CandidateDecisionSummary:
    symbol: str
    timestamp: datetime
    research_cycle_id: str | None
    scheduler_run_id: str | None
    final_outcome: DashboardOutcome
    primary_reason_code: str
    friendly_reason: str
    baseline_result: str | None
    enhanced_result: str | None
    paper_order_status: str | None
    score: Decimal | None
    confidence: str | None


@dataclass(frozen=True, slots=True)
class CandidateDecisionDetail:
    summary: CandidateDecisionSummary
    screening_status: str | None
    evidence_screening_completeness: str | None
    evidence_research_completeness: str | None
    blocking_categories: tuple[str, ...]
    research_status: str | None
    research_failure_codes: tuple[str, ...]
    baseline_recommendation_id: str | None
    enhanced_recommendation_id: str | None
    risk_decision: str | None
    risk_reason_codes: tuple[str, ...]
    paper_book_id: str | None
    paper_order_intent_id: str | None
    fill_id: str | None
    provider_failure_codes: tuple[str, ...]
    bull_thesis: str | None = None
    bear_case: str | None = None
    catalysts: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()
    policy_checks: tuple[str, ...] = ()
    reference_price: Decimal | None = None
    limit_price: Decimal | None = None
    quantity: Decimal | None = None
    fill_status: str | None = None
    failed_stage: str | None = None
    observed_value: str | None = None
    required_threshold: str | None = None
    block_category: str | None = None


@dataclass(frozen=True, slots=True)
class ResearchCycleSummary:
    cycle_id: str
    universe_id: str
    as_of: datetime
    status: str
    provider_mode: str
    experiment_policy: str
    symbols_total: int
    symbols_completed: int
    symbols_skipped: int
    symbols_failed: int
    started_at: datetime
    completed_at: datetime | None
    scheduler_run_id: str | None
    research_provider_partitions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchCycleFunnel:
    selected: int
    screened_out: int
    evidence_incomplete: int
    research_incomplete: int
    policy_rejected: int
    buy_candidates: int
    paper_submitted: int
    filled: int
    not_filled: int


@dataclass(frozen=True, slots=True)
class ResearchCycleDetail:
    summary: ResearchCycleSummary
    funnel: ResearchCycleFunnel
    decisions: tuple[CandidateDecisionSummary, ...]


@dataclass(frozen=True, slots=True)
class PositionSummary:
    book_id: str
    symbol: str
    quantity: Decimal
    available_quantity: Decimal
    reserved_quantity: Decimal
    average_cost: Decimal
    latest_price: Decimal | None
    market_value: Decimal | None
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    valuation_status: str | None
    valued_at: datetime | None
    price_source: str | None
    allocation_percentage: Decimal | None


@dataclass(frozen=True, slots=True)
class PaperOrderSummary:
    book_id: str
    order_id: str
    symbol: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    status: str
    cycle_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PaperFillSummary:
    book_id: str
    fill_id: str
    order_id: str
    symbol: str
    side: str
    quantity: Decimal
    fill_price: Decimal
    filled_at: datetime


@dataclass(frozen=True, slots=True)
class PortfolioSummary:
    book_id: str
    experiment_arm: str
    as_of: datetime | None
    status: str
    cash_available: Decimal | None
    cash_reserved: Decimal | None
    gross_market_value: Decimal | None
    net_liquidation_value: Decimal | None
    realized_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    valuation_status: str | None
    positions: tuple[PositionSummary, ...]
    orders: tuple[PaperOrderSummary, ...]
    fills: tuple[PaperFillSummary, ...]


@dataclass(frozen=True, slots=True)
class ProviderHealthSummary:
    provider_kind: str
    provider: str
    model: str | None
    mode: str | None
    is_production: bool
    status: str
    window_start: datetime | None
    window_end: datetime | None
    total_requests: int | None
    successful_requests: int | None
    success_rate: float | None
    timeout_count: int | None
    rate_limited_count: int | None
    average_latency_ms: float | None
    p95_latency_ms: float | None
    latest_error_code: str | None
    failure_streak: int
    recovery_streak: int
    authentication_failures: int
    configuration_failures: int
    timeout_failures: int
    rate_limit_failures: int
    quota_failures: int


@dataclass(frozen=True, slots=True)
class SystemStatusSummary:
    as_of: datetime | None
    shadow_pause_state: str | None
    recurring_activation_state: str | None
    latest_shadow_scheduler_status: str | None
    latest_recurring_scheduler_status: str | None
    latest_successful_run_at: datetime | None
    health_status: str | None
    hysteresis_status: str | None
    hysteresis_reasons: tuple[str, ...]
    active_safety_pauses: tuple[str, ...]
    budget_status: str | None
    active_policy_version: str | None
    active_policy_hash: str | None


@dataclass(frozen=True, slots=True)
class SystemHealthView:
    status: SystemStatusSummary
    providers: tuple[ProviderHealthSummary, ...]


_SUBMITTED_ORDER_STATUSES = frozenset({"SUBMITTED", "PARTIALLY_FILLED", "FILLED"})
_PENDING_ORDER_STATUSES = frozenset({"PENDING_SUBMISSION", "PREVIEWED", "SUBMISSION_REQUESTED"})
_EVIDENCE_INCOMPLETE_STATUSES = frozenset({
    "PARTIAL_NONCRITICAL",
    "MISSING_CRITICAL_CORPORATE_STATUS",
    "MISSING_CRITICAL_MARKET_DATA",
    "MISSING_CRITICAL_FUNDAMENTALS",
    "MISSING_NEWS",
    "MISSING_SENTIMENT",
    "CONFLICTING_CRITICAL_DATA",
    "POINT_IN_TIME_UNSAFE",
    "PROVIDER_UNAVAILABLE",
})
_RESEARCH_INCOMPLETE_STATUSES = frozenset({
    "ANALYSIS_INCOMPLETE", "PARTIALLY_COMPLETE", "FAILED",
})
_BUDGET_STATUSES = frozenset({
    "BUDGET_REJECTED", "SKIPPED_BUDGET_EXHAUSTED",
    "REJECTED_INSUFFICIENT_CASH", "REJECTED_DAILY_NOTIONAL_LIMIT",
})
_POLICY_RISK_DECISIONS = frozenset({
    "REJECTED_BOOK_PAUSED", "REJECTED_ARM_MISMATCH",
    "RISK_REJECTED_DAILY_LOSS_LIMIT", "RISK_REJECTED_DRAWDOWN_LIMIT",
    "RISK_REJECTED_RISK_STATE_UNAVAILABLE", "RISK_REJECTED_RISK_STATE_STALE",
    "RISK_REJECTED_RISK_STATE_UNRECONCILED", "RISK_REJECTED_ECONOMIC_EVENT_BLACKOUT",
})
_KNOWN_RECOMMENDATION_SIDES = frozenset({
    "buy_candidate", "screened_out", "analysis_incomplete", "no_action", "watch",
})


def _clean_code(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _friendly_code(code: str) -> str:
    return code.replace("_", " ").lower().capitalize()


def _unknown_explanation(explanation: str | None) -> str:
    if not explanation:
        return "No stable persisted outcome code is available."
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", explanation)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "No stable persisted outcome code is available."
    return cleaned[:237] + ("..." if len(cleaned) > 237 else "")


def _mapped(outcome: DashboardOutcome, code: str, friendly: str | None = None) -> OutcomeMapping:
    return OutcomeMapping(outcome, code, friendly or _friendly_code(code))


def map_dashboard_outcome(
    *,
    baseline_paper_submitted: bool = False,
    paper_order_status: str | None = None,
    recommendation_side: str | None = None,
    recommendation_status: str | None = None,
    evidence_screening_completeness: str | None = None,
    evidence_research_completeness: str | None = None,
    research_status: str | None = None,
    risk_decision: str | None = None,
    scheduler_status: str | None = None,
    role_budget_decision: str | None = None,
    provider_failure_code: str | None = None,
    lifecycle_outcome: str | None = None,
    unknown_explanation: str | None = None,
) -> OutcomeMapping:
    """Map stable persisted fields to a dashboard-only outcome.

    Priority follows the decision pipeline: confirmed execution evidence,
    explicit budget/policy/risk gates, provider/evidence/research
    completeness, then recommendation disposition. Unknown codes are never
    guessed from human-readable text.
    """
    order = _clean_code(paper_order_status)
    side = _clean_code(recommendation_side)
    rec_status = _clean_code(recommendation_status)
    screening = _clean_code(evidence_screening_completeness)
    research_completeness = _clean_code(evidence_research_completeness)
    research = _clean_code(research_status)
    risk = _clean_code(risk_decision)
    scheduler = _clean_code(scheduler_status)
    budget = _clean_code(role_budget_decision)
    provider_failure = _clean_code(provider_failure_code)
    lifecycle = _clean_code(lifecycle_outcome)

    if baseline_paper_submitted:
        return _mapped(DashboardOutcome.BOUGHT_OR_SUBMITTED, "BASELINE_PAPER_SUBMITTED")
    if order in _SUBMITTED_ORDER_STATUSES:
        return _mapped(DashboardOutcome.BOUGHT_OR_SUBMITTED, order)
    if scheduler in _BUDGET_STATUSES:
        return _mapped(DashboardOutcome.BUDGET_BLOCKED, scheduler)
    if budget in _BUDGET_STATUSES:
        return _mapped(DashboardOutcome.BUDGET_BLOCKED, budget)
    if risk in _BUDGET_STATUSES:
        return _mapped(DashboardOutcome.BUDGET_BLOCKED, risk)
    if risk in _POLICY_RISK_DECISIONS:
        return _mapped(DashboardOutcome.POLICY_BLOCKED, risk)
    if risk is not None and risk.startswith("REJECTED_"):
        return _mapped(DashboardOutcome.REJECTED, risk)
    if provider_failure:
        return _mapped(DashboardOutcome.PROVIDER_FAILURE, provider_failure)
    if screening in _EVIDENCE_INCOMPLETE_STATUSES:
        return _mapped(DashboardOutcome.EVIDENCE_INCOMPLETE, screening)
    if research_completeness in _EVIDENCE_INCOMPLETE_STATUSES:
        return _mapped(DashboardOutcome.EVIDENCE_INCOMPLETE, research_completeness)
    if research in _RESEARCH_INCOMPLETE_STATUSES or side == "analysis_incomplete" or rec_status == "analysis_incomplete":
        code = research or rec_status or side or "ANALYSIS_INCOMPLETE"
        return _mapped(DashboardOutcome.RESEARCH_INCOMPLETE, code)
    if side == "screened_out":
        return _mapped(DashboardOutcome.SCREENED_OUT, "SCREENED_OUT")
    if side == "buy_candidate" or order in _PENDING_ORDER_STATUSES:
        code = order or "BUY_CANDIDATE"
        return _mapped(DashboardOutcome.BUY_CANDIDATE_NOT_SUBMITTED, code)
    if side in {"no_action", "watch"}:
        return _mapped(DashboardOutcome.NO_ACTION, side.upper())

    supplied_codes = tuple(filter(None, (
        order, side, rec_status, screening, research_completeness, research,
        risk, scheduler, budget, lifecycle,
    )))
    if supplied_codes or (side is not None and side not in _KNOWN_RECOMMENDATION_SIDES):
        explanation = unknown_explanation or f"Unmapped persisted code: {supplied_codes[0]}"
    else:
        explanation = unknown_explanation
    return _mapped(DashboardOutcome.UNKNOWN, "UNKNOWN", _unknown_explanation(explanation))
