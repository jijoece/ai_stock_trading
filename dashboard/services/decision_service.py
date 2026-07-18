"""Bounded, read-only decision explorer queries."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
from typing import Any

from dashboard.models.view_models import (
    CandidateDecisionDetail,
    CandidateDecisionSummary,
    DashboardOutcome,
    map_dashboard_outcome,
)
from dashboard.services.database import DashboardDatabaseError, connect_read_only


DEFAULT_LIMIT = 100
MAX_LIMIT = 200
MAX_FILTER_SCAN = 1000


@dataclass(frozen=True, slots=True)
class DecisionFilters:
    start_date: date | None = None
    end_date: date | None = None
    symbol: str | None = None
    outcome: DashboardOutcome | None = None
    primary_reason: str | None = None
    research_cycle_id: str | None = None


_SELECT = """
SELECT
    csr.cycle_id,
    csr.symbol,
    COALESCE(csr.completed_at, csr.created_at, c.as_of) AS decision_timestamp,
    csr.status AS symbol_status,
    csr.research_run_id,
    csr.baseline_recommendation_id,
    csr.enhanced_recommendation_id,
    csr.baseline_paper_submitted,
    csr.failure_reason,
    COALESCE(er.side, br.side) AS recommendation_side,
    br.side AS baseline_side,
    er.side AS enhanced_side,
    COALESCE(er.status, br.status) AS recommendation_status,
    COALESCE(er.score, br.score) AS score,
    COALESCE(er.confidence, br.confidence) AS confidence,
    COALESCE(er.price_at_rec, br.price_at_rec) AS reference_price,
    evidence.screening_completeness,
    evidence.research_completeness,
    evidence.blocking_categories_json,
    run.status AS research_status,
    scheduler.scheduler_run_id,
    scheduler.status AS scheduler_status,
    risk.risk_decision_id,
    risk.book_id,
    risk.decision AS risk_decision,
    risk.reasons_json AS risk_reasons_json,
    risk.policy_version AS risk_policy_version,
    paper_order.paper_order_intent_id,
    paper_order.status AS paper_order_status,
    paper_order.limit_price,
    paper_order.quantity,
    fill.fill_id,
    structured_failure.code AS provider_failure_code,
    structured_failure.stage AS failure_stage,
    attempt.failure_metadata_json,
    budget.decision AS role_budget_decision,
    research_decision.payload_json AS decision_payload_json,
    overlay.action AS overlay_action,
    overlay.policy_version AS overlay_policy_version
FROM research_cycle_symbol_results AS csr
JOIN research_cycles AS c ON c.cycle_id = csr.cycle_id
LEFT JOIN recommendations AS br ON br.rec_id = csr.baseline_recommendation_id
LEFT JOIN recommendations AS er ON er.rec_id = csr.enhanced_recommendation_id
LEFT JOIN research_cycle_symbol_evidence_status AS evidence
    ON evidence.cycle_id = csr.cycle_id AND evidence.symbol = csr.symbol
LEFT JOIN research_committee_runs AS run ON run.research_run_id = csr.research_run_id
LEFT JOIN shadow_scheduler_runs AS scheduler ON scheduler.scheduler_run_id = (
    SELECT s.scheduler_run_id FROM shadow_scheduler_runs AS s
    WHERE s.cycle_id = csr.cycle_id ORDER BY s.created_at DESC LIMIT 1
)
LEFT JOIN paper_book_risk_decisions AS risk ON risk.risk_decision_id = (
    SELECT r.risk_decision_id FROM paper_book_risk_decisions AS r
    WHERE r.cycle_id = csr.cycle_id AND r.symbol = csr.symbol
    ORDER BY r.created_at DESC LIMIT 1
)
LEFT JOIN paper_book_orders AS paper_order
    ON paper_order.book_id = risk.book_id
   AND paper_order.paper_order_intent_id = (
       SELECT o.paper_order_intent_id FROM paper_book_orders AS o
       WHERE o.cycle_id = csr.cycle_id AND o.symbol = csr.symbol
       ORDER BY o.created_at DESC LIMIT 1
   )
LEFT JOIN paper_book_fills AS fill
    ON fill.book_id = paper_order.book_id
   AND fill.fill_id = (
       SELECT f.fill_id FROM paper_book_fills AS f
       WHERE f.book_id = paper_order.book_id
         AND f.paper_order_intent_id = paper_order.paper_order_intent_id
       ORDER BY f.created_at DESC LIMIT 1
   )
LEFT JOIN research_attempt_failures AS structured_failure
    ON structured_failure.failure_id = (
        SELECT f.failure_id FROM research_attempt_failures AS f
        WHERE f.research_run_id = csr.research_run_id
        ORDER BY f.occurred_at DESC LIMIT 1
    )
LEFT JOIN research_attempts AS attempt ON attempt.attempt_id = (
    SELECT a.attempt_id FROM research_attempts AS a
    WHERE a.research_run_id = csr.research_run_id AND a.success = 0
    ORDER BY a.created_at DESC LIMIT 1
)
LEFT JOIN shadow_role_budget_checks AS budget ON budget.check_id = (
    SELECT b.check_id FROM shadow_role_budget_checks AS b
    WHERE b.cycle_id = csr.cycle_id AND b.symbol = csr.symbol
    ORDER BY b.checked_at DESC LIMIT 1
)
LEFT JOIN research_decisions AS research_decision
    ON research_decision.research_run_id = csr.research_run_id
LEFT JOIN research_overlay_decisions AS overlay
    ON overlay.research_decision_id = research_decision.decision_id
"""


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_strings(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item)[:240] for item in parsed if isinstance(item, (str, int, float)))


def _block_category(outcome: DashboardOutcome) -> str | None:
    if outcome is DashboardOutcome.PROVIDER_FAILURE:
        return "Provider-related"
    if outcome in {DashboardOutcome.POLICY_BLOCKED, DashboardOutcome.BUDGET_BLOCKED}:
        return "Policy-related"
    if outcome in {
        DashboardOutcome.REJECTED,
        DashboardOutcome.SCREENED_OUT,
        DashboardOutcome.EVIDENCE_INCOMPLETE,
        DashboardOutcome.RESEARCH_INCOMPLETE,
    }:
        return "Deterministic"
    return None


def _failed_stage(
    outcome: DashboardOutcome,
    persisted_failure_stage: str | None,
    symbol_status: str | None,
) -> str | None:
    if persisted_failure_stage:
        return persisted_failure_stage
    if outcome in {
        DashboardOutcome.REJECTED,
        DashboardOutcome.POLICY_BLOCKED,
        DashboardOutcome.BUDGET_BLOCKED,
    }:
        return "Risk and policy evaluation"
    if outcome is DashboardOutcome.SCREENED_OUT:
        return "Screening"
    if outcome is DashboardOutcome.EVIDENCE_INCOMPLETE:
        return "Evidence completeness"
    if outcome in {DashboardOutcome.RESEARCH_INCOMPLETE, DashboardOutcome.PROVIDER_FAILURE}:
        return "Research result"
    if outcome is DashboardOutcome.BUY_CANDIDATE_NOT_SUBMITTED:
        return "Paper-order eligibility"
    return symbol_status if outcome is not DashboardOutcome.BOUGHT_OR_SUBMITTED else None


def _summary(row: sqlite3.Row) -> CandidateDecisionSummary:
    provider_failure = row["provider_failure_code"] if row["research_status"] in {
        "ANALYSIS_INCOMPLETE", "PARTIALLY_COMPLETE", "FAILED",
    } else None
    mapping = map_dashboard_outcome(
        baseline_paper_submitted=bool(row["baseline_paper_submitted"]),
        paper_order_status=row["paper_order_status"],
        recommendation_side=row["recommendation_side"],
        recommendation_status=row["recommendation_status"],
        evidence_screening_completeness=row["screening_completeness"],
        evidence_research_completeness=row["research_completeness"],
        research_status=row["research_status"],
        risk_decision=row["risk_decision"],
        scheduler_status=row["scheduler_status"],
        role_budget_decision=row["role_budget_decision"],
        provider_failure_code=provider_failure,
        unknown_explanation=row["failure_reason"],
    )
    return CandidateDecisionSummary(
        symbol=row["symbol"],
        timestamp=_parse_datetime(row["decision_timestamp"]),
        research_cycle_id=row["cycle_id"],
        scheduler_run_id=row["scheduler_run_id"],
        final_outcome=mapping.outcome,
        primary_reason_code=mapping.primary_reason_code,
        friendly_reason=mapping.friendly_reason,
        baseline_result=row["baseline_side"],
        enhanced_result=row["enhanced_side"],
        paper_order_status=row["paper_order_status"],
        score=_decimal(row["score"]),
        confidence=row["confidence"],
    )


def _detail(row: sqlite3.Row) -> CandidateDecisionDetail:
    summary = _summary(row)
    payload = _json_dict(row["decision_payload_json"])
    failure_metadata = _json_dict(row["failure_metadata_json"])
    risk_reasons = _json_strings(row["risk_reasons_json"])
    policy_checks = tuple(filter(None, (
        row["overlay_action"],
        f"Overlay policy: {row['overlay_policy_version']}" if row["overlay_policy_version"] else None,
        row["risk_decision"],
        f"Risk policy: {row['risk_policy_version']}" if row["risk_policy_version"] else None,
        *risk_reasons,
    )))
    provider_codes = tuple(filter(None, (row["provider_failure_code"],)))
    failure_codes = provider_codes
    return CandidateDecisionDetail(
        summary=summary,
        screening_status=row["symbol_status"],
        evidence_screening_completeness=row["screening_completeness"],
        evidence_research_completeness=row["research_completeness"],
        blocking_categories=_json_strings(row["blocking_categories_json"]),
        research_status=row["research_status"],
        research_failure_codes=failure_codes,
        baseline_recommendation_id=row["baseline_recommendation_id"],
        enhanced_recommendation_id=row["enhanced_recommendation_id"],
        risk_decision=row["risk_decision"],
        risk_reason_codes=risk_reasons,
        paper_book_id=row["book_id"],
        paper_order_intent_id=row["paper_order_intent_id"],
        fill_id=row["fill_id"],
        provider_failure_codes=provider_codes,
        bull_thesis=str(payload.get("bull_case"))[:2000] if payload.get("bull_case") else None,
        bear_case=str(payload.get("bear_case"))[:2000] if payload.get("bear_case") else None,
        catalysts=tuple(str(item)[:500] for item in payload.get("catalysts", []) if isinstance(item, str)),
        risks=tuple(str(item)[:500] for item in payload.get("risks", []) if isinstance(item, str)),
        evidence_references=tuple(
            str(item)[:200] for item in payload.get("evidence_ids", []) if isinstance(item, str)
        ),
        policy_checks=policy_checks,
        reference_price=_decimal(row["reference_price"]),
        limit_price=_decimal(row["limit_price"]),
        quantity=_decimal(row["quantity"]),
        fill_status=(row["paper_order_status"] if row["fill_id"] else None),
        failed_stage=_failed_stage(summary.final_outcome, row["failure_stage"], row["symbol_status"]),
        observed_value=(str(failure_metadata.get("observed_value"))[:240]
                        if failure_metadata.get("observed_value") is not None else None),
        required_threshold=(str(failure_metadata.get("threshold_value"))[:240]
                            if failure_metadata.get("threshold_value") is not None else None),
        block_category=_block_category(summary.final_outcome),
    )


class DecisionService:
    def __init__(self, database_path: str | Path | None = None):
        self._database_path = database_path

    def list_decisions(
        self,
        filters: DecisionFilters | None = None,
        *,
        limit: int = DEFAULT_LIMIT,
    ) -> tuple[CandidateDecisionSummary, ...]:
        filters = filters or DecisionFilters()
        if limit < 1 or limit > MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")

        clauses: list[str] = []
        parameters: list[Any] = []
        if filters.start_date:
            clauses.append("COALESCE(csr.completed_at, csr.created_at, c.as_of) >= ?")
            parameters.append(datetime.combine(filters.start_date, time.min).isoformat())
        if filters.end_date:
            clauses.append("COALESCE(csr.completed_at, csr.created_at, c.as_of) < ?")
            parameters.append(datetime.combine(filters.end_date, time.max).isoformat())
        if filters.symbol:
            symbol = filters.symbol.strip().upper()
            if len(symbol) > 16 or not symbol.replace(".", "").replace("-", "").isalnum():
                raise ValueError("symbol filter is invalid")
            clauses.append("csr.symbol = ?")
            parameters.append(symbol)
        if filters.research_cycle_id:
            if len(filters.research_cycle_id) > 200:
                raise ValueError("research cycle filter is invalid")
            clauses.append("csr.cycle_id = ?")
            parameters.append(filters.research_cycle_id)

        scan_limit = MAX_FILTER_SCAN if (filters.outcome or filters.primary_reason) else limit
        query = _SELECT
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY decision_timestamp DESC, csr.symbol ASC LIMIT ?"
        parameters.append(scan_limit)

        try:
            with connect_read_only(self._database_path) as connection:
                rows = connection.execute(query, parameters).fetchall()
        except sqlite3.Error as exc:
            raise DashboardDatabaseError("Dashboard decision data is unavailable.") from exc

        summaries = (_summary(row) for row in rows)
        if filters.outcome:
            summaries = (item for item in summaries if item.final_outcome is filters.outcome)
        if filters.primary_reason:
            reason = filters.primary_reason.strip().upper()
            summaries = (item for item in summaries if item.primary_reason_code.upper() == reason)
        return tuple(list(summaries)[:limit])

    def get_decision_detail(self, cycle_id: str, symbol: str) -> CandidateDecisionDetail | None:
        if not cycle_id or len(cycle_id) > 200 or not symbol or len(symbol) > 16:
            raise ValueError("decision identifier is invalid")
        query = _SELECT + " WHERE csr.cycle_id = ? AND csr.symbol = ? LIMIT 1"
        try:
            with connect_read_only(self._database_path) as connection:
                row = connection.execute(query, (cycle_id, symbol.upper())).fetchone()
        except sqlite3.Error as exc:
            raise DashboardDatabaseError("Dashboard decision detail is unavailable.") from exc
        return _detail(row) if row else None
