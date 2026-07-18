"""Bounded, read-only research-cycle summaries and funnel details."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from dashboard.models.view_models import (
    ResearchCycleDetail,
    ResearchCycleFunnel,
    ResearchCycleSummary,
)
from dashboard.services.database import DashboardDatabaseError, connect_read_only
from dashboard.services.decision_service import DecisionFilters, DecisionService, MAX_LIMIT


DEFAULT_LIMIT = 50
MAX_CYCLE_LIMIT = 200


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _partitions(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(sorted(set(item for item in value.split("\x1e") if item)))


_SUMMARY_SELECT = """
SELECT
    c.cycle_id, c.universe_id, c.as_of, c.status, c.provider_mode,
    c.experiment_policy, c.started_at, c.completed_at,
    COALESCE(s.symbols_attempted,
        (SELECT COUNT(*) FROM research_cycle_symbol_results csr WHERE csr.cycle_id = c.cycle_id)
    ) AS symbols_total,
    COALESCE(s.symbols_completed,
        (SELECT COUNT(*) FROM research_cycle_symbol_results csr
         WHERE csr.cycle_id = c.cycle_id AND csr.status = 'COMPLETED')
    ) AS symbols_completed,
    COALESCE(s.symbols_skipped,
        (SELECT COUNT(*) FROM research_cycle_symbol_results csr
         WHERE csr.cycle_id = c.cycle_id AND csr.status LIKE 'SKIPPED%')
    ) AS symbols_skipped,
    COALESCE(s.provider_failures + s.research_failures,
        (SELECT COUNT(*) FROM research_cycle_symbol_results csr
         WHERE csr.cycle_id = c.cycle_id AND csr.status IN ('FAILED', 'ANALYSIS_INCOMPLETE'))
    ) AS symbols_failed,
    s.scheduler_run_id,
    (SELECT GROUP_CONCAT(partition_label, CHAR(30)) FROM (
        SELECT DISTINCT r.provider || ' / ' || r.model_name || ' / ' || r.run_mode AS partition_label
        FROM research_cycle_symbol_results csr
        JOIN research_committee_runs r ON r.research_run_id = csr.research_run_id
        WHERE csr.cycle_id = c.cycle_id
        ORDER BY partition_label
    )) AS research_provider_partitions
FROM research_cycles c
LEFT JOIN shadow_scheduler_runs s ON s.scheduler_run_id = (
    SELECT latest.scheduler_run_id FROM shadow_scheduler_runs latest
    WHERE latest.cycle_id = c.cycle_id ORDER BY latest.created_at DESC LIMIT 1
)
"""


def _summary(row: sqlite3.Row) -> ResearchCycleSummary:
    return ResearchCycleSummary(
        cycle_id=row["cycle_id"],
        universe_id=row["universe_id"],
        as_of=_datetime(row["as_of"]),  # type: ignore[arg-type]
        status=row["status"],
        provider_mode=row["provider_mode"],
        experiment_policy=row["experiment_policy"],
        symbols_total=int(row["symbols_total"] or 0),
        symbols_completed=int(row["symbols_completed"] or 0),
        symbols_skipped=int(row["symbols_skipped"] or 0),
        symbols_failed=int(row["symbols_failed"] or 0),
        started_at=_datetime(row["started_at"]),  # type: ignore[arg-type]
        completed_at=_datetime(row["completed_at"]),
        scheduler_run_id=row["scheduler_run_id"],
        research_provider_partitions=_partitions(row["research_provider_partitions"]),
    )


_FUNNEL_SELECT = """
SELECT
    COUNT(*) AS selected,
    SUM(CASE WHEN COALESCE(er.side, br.side) = 'screened_out' THEN 1 ELSE 0 END) AS screened_out,
    SUM(CASE WHEN evidence.screening_completeness NOT IN ('COMPLETE', 'NOT_APPLICABLE')
                  OR evidence.research_completeness NOT IN ('COMPLETE', 'NOT_APPLICABLE')
             THEN 1 ELSE 0 END) AS evidence_incomplete,
    SUM(CASE WHEN run.status IN ('ANALYSIS_INCOMPLETE', 'PARTIALLY_COMPLETE', 'FAILED')
                  OR COALESCE(er.side, br.side) = 'analysis_incomplete'
             THEN 1 ELSE 0 END) AS research_incomplete,
    SUM(CASE WHEN EXISTS (
        SELECT 1 FROM paper_book_risk_decisions risk
        WHERE risk.cycle_id = csr.cycle_id AND risk.symbol = csr.symbol
          AND risk.decision LIKE 'REJECTED%'
    ) THEN 1 ELSE 0 END) AS policy_rejected,
    SUM(CASE WHEN COALESCE(er.side, br.side) = 'buy_candidate' THEN 1 ELSE 0 END) AS buy_candidates,
    SUM(CASE WHEN csr.baseline_paper_submitted = 1 OR EXISTS (
        SELECT 1 FROM paper_book_orders orders
        WHERE orders.cycle_id = csr.cycle_id AND orders.symbol = csr.symbol
          AND orders.status IN ('SUBMITTED', 'PARTIALLY_FILLED', 'FILLED')
    ) THEN 1 ELSE 0 END) AS paper_submitted,
    SUM(CASE WHEN EXISTS (
        SELECT 1 FROM paper_book_orders orders
        JOIN paper_book_fills fills
          ON fills.book_id = orders.book_id
         AND fills.paper_order_intent_id = orders.paper_order_intent_id
        WHERE orders.cycle_id = csr.cycle_id AND orders.symbol = csr.symbol
    ) THEN 1 ELSE 0 END) AS filled,
    SUM(CASE WHEN (csr.baseline_paper_submitted = 1 OR EXISTS (
        SELECT 1 FROM paper_book_orders orders
        WHERE orders.cycle_id = csr.cycle_id AND orders.symbol = csr.symbol
          AND orders.status IN ('SUBMITTED', 'PARTIALLY_FILLED', 'FILLED')
    )) AND NOT EXISTS (
        SELECT 1 FROM paper_book_orders orders
        JOIN paper_book_fills fills
          ON fills.book_id = orders.book_id
         AND fills.paper_order_intent_id = orders.paper_order_intent_id
        WHERE orders.cycle_id = csr.cycle_id AND orders.symbol = csr.symbol
    ) THEN 1 ELSE 0 END) AS not_filled
FROM research_cycle_symbol_results csr
LEFT JOIN recommendations br ON br.rec_id = csr.baseline_recommendation_id
LEFT JOIN recommendations er ON er.rec_id = csr.enhanced_recommendation_id
LEFT JOIN research_cycle_symbol_evidence_status evidence
  ON evidence.cycle_id = csr.cycle_id AND evidence.symbol = csr.symbol
LEFT JOIN research_committee_runs run ON run.research_run_id = csr.research_run_id
WHERE csr.cycle_id = ?
"""


class CycleService:
    def __init__(self, database_path: str | Path | None = None):
        self._database_path = database_path

    def list_cycles(self, *, limit: int = DEFAULT_LIMIT) -> tuple[ResearchCycleSummary, ...]:
        if limit < 1 or limit > MAX_CYCLE_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_CYCLE_LIMIT}")
        query = _SUMMARY_SELECT + " ORDER BY c.started_at DESC LIMIT ?"
        try:
            with connect_read_only(self._database_path) as connection:
                rows = connection.execute(query, (limit,)).fetchall()
        except sqlite3.Error as exc:
            raise DashboardDatabaseError("Dashboard research-cycle data is unavailable.") from exc
        return tuple(_summary(row) for row in rows)

    def get_cycle_detail(self, cycle_id: str) -> ResearchCycleDetail | None:
        if not cycle_id or len(cycle_id) > 200:
            raise ValueError("research cycle identifier is invalid")
        try:
            with connect_read_only(self._database_path) as connection:
                row = connection.execute(_SUMMARY_SELECT + " WHERE c.cycle_id = ? LIMIT 1", (cycle_id,)).fetchone()
                if row is None:
                    return None
                funnel_row = connection.execute(_FUNNEL_SELECT, (cycle_id,)).fetchone()
        except sqlite3.Error as exc:
            raise DashboardDatabaseError("Dashboard research-cycle detail is unavailable.") from exc

        decisions = DecisionService(self._database_path).list_decisions(
            DecisionFilters(research_cycle_id=cycle_id), limit=MAX_LIMIT
        )
        funnel = ResearchCycleFunnel(**{
            field: int(funnel_row[field] or 0)
            for field in (
                "selected", "screened_out", "evidence_incomplete", "research_incomplete",
                "policy_rejected", "buy_candidates", "paper_submitted", "filled", "not_filled",
            )
        })
        return ResearchCycleDetail(summary=_summary(row), funnel=funnel, decisions=decisions)
