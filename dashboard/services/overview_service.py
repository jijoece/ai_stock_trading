"""Read-only overview aggregation over persisted dashboard data."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3

from dashboard.models.view_models import DashboardOutcome, DashboardOverview
from dashboard.services.database import DashboardDatabaseError, connect_read_only
from dashboard.services.decision_service import DecisionFilters, DecisionService, MAX_LIMIT


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _sum_decimal(rows: list[sqlite3.Row], column: str) -> Decimal | None:
    values = [Decimal(row[column]) for row in rows if row[column] is not None]
    return sum(values, Decimal("0")) if values else None


class OverviewService:
    def __init__(self, database_path: str | Path | None = None):
        self._database_path = database_path

    def load(self) -> DashboardOverview:
        try:
            with connect_read_only(self._database_path) as connection:
                snapshots = connection.execute("""
                    SELECT s.* FROM paper_book_snapshots AS s
                    WHERE s.created_at = (
                        SELECT MAX(latest.created_at) FROM paper_book_snapshots AS latest
                        WHERE latest.book_id = s.book_id
                    )
                """).fetchall()
                pause = connection.execute("""
                    SELECT state FROM shadow_pause_state
                    WHERE is_current = 1 ORDER BY created_at DESC LIMIT 1
                """).fetchone()
                scheduler = connection.execute("""
                    SELECT scheduler_run_id FROM shadow_scheduler_runs
                    ORDER BY created_at DESC LIMIT 1
                """).fetchone()
                cycle = connection.execute("""
                    SELECT cycle_id FROM research_cycles ORDER BY started_at DESC LIMIT 1
                """).fetchone()
                candidate_count = None
                if cycle:
                    candidate_count = connection.execute(
                        "SELECT COUNT(*) FROM research_cycle_symbol_results WHERE cycle_id = ?",
                        (cycle["cycle_id"],),
                    ).fetchone()[0]
        except sqlite3.Error as exc:
            raise DashboardDatabaseError("Dashboard overview data is unavailable.") from exc

        decisions = ()
        if cycle and candidate_count is not None and candidate_count <= MAX_LIMIT:
            decisions = DecisionService(self._database_path).list_decisions(
                DecisionFilters(research_cycle_id=cycle["cycle_id"]),
                limit=max(1, candidate_count),
            )

        bought = rejected = incomplete = None
        if cycle and len(decisions) == candidate_count:
            bought = sum(item.final_outcome is DashboardOutcome.BOUGHT_OR_SUBMITTED for item in decisions)
            rejected = sum(item.final_outcome in {
                DashboardOutcome.REJECTED,
                DashboardOutcome.SCREENED_OUT,
                DashboardOutcome.POLICY_BLOCKED,
                DashboardOutcome.BUDGET_BLOCKED,
            } for item in decisions)
            incomplete = sum(item.final_outcome in {
                DashboardOutcome.EVIDENCE_INCOMPLETE,
                DashboardOutcome.RESEARCH_INCOMPLETE,
                DashboardOutcome.PROVIDER_FAILURE,
                DashboardOutcome.UNKNOWN,
            } for item in decisions)

        as_of_values = tuple(filter(None, (_datetime(row["as_of"]) for row in snapshots)))
        return DashboardOverview(
            as_of=max(as_of_values) if as_of_values else None,
            portfolio_value=_sum_decimal(snapshots, "net_liquidation_value_usd"),
            cash=_sum_decimal(snapshots, "cash_available_usd"),
            reserved_cash=_sum_decimal(snapshots, "cash_reserved_usd"),
            open_positions=sum(row["position_count"] for row in snapshots) if snapshots else None,
            realized_pnl=_sum_decimal(snapshots, "realized_pnl_usd"),
            unrealized_pnl=_sum_decimal(snapshots, "unrealized_pnl_usd"),
            candidates_considered=candidate_count,
            bought_or_submitted=bought,
            rejected=rejected,
            incomplete=incomplete,
            pause_state=pause["state"] if pause else None,
            latest_scheduler_run_id=scheduler["scheduler_run_id"] if scheduler else None,
            latest_research_cycle_id=cycle["cycle_id"] if cycle else None,
        )
