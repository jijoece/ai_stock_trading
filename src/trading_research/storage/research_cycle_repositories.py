"""Persistence for scheduled research cycles (Milestone 6), operating on
`research_cycle_schema.py`'s tables. `SQLiteResearchCycleRepository` satisfies
`research.scheduled_cycle.ResearchCycleRepository` structurally, mirroring
`research_repositories.py::SQLiteResearchRepository`'s relationship to
`research.orchestration.ResearchRepository`.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from ..research.scheduled_cycle import SymbolCycleResult


class SQLiteResearchCycleRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get_cycle_status(self, cycle_id: str) -> str | None:
        row = self._conn.execute("SELECT status FROM research_cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
        return row["status"] if row is not None else None

    def save_cycle_started(
        self, cycle_id: str, universe_id: str, as_of: datetime, configuration_hash: str,
        experiment_policy: str, provider_mode: str, started_at: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO research_cycles "
            "(cycle_id, universe_id, as_of, configuration_hash, experiment_policy, provider_mode, status, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, NULL)",
            (cycle_id, universe_id, as_of.isoformat(), configuration_hash, experiment_policy, provider_mode, started_at.isoformat()),
        )
        self._conn.commit()

    def get_symbol_result(self, cycle_id: str, symbol: str) -> SymbolCycleResult | None:
        row = self._conn.execute(
            "SELECT * FROM research_cycle_symbol_results WHERE cycle_id = ? AND symbol = ?", (cycle_id, symbol),
        ).fetchone()
        if row is None:
            return None
        return SymbolCycleResult(
            symbol=row["symbol"], status=row["status"], snapshot_id=row["snapshot_id"],
            research_run_id=row["research_run_id"], experiment_id=row["experiment_id"],
            baseline_recommendation_id=row["baseline_recommendation_id"],
            enhanced_recommendation_id=row["enhanced_recommendation_id"],
            baseline_paper_submitted=bool(row["baseline_paper_submitted"]), failure_reason=row["failure_reason"],
        )

    def save_symbol_result(
        self, cycle_id: str, result: SymbolCycleResult, created_at: datetime, completed_at: datetime | None,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO research_cycle_symbol_results "
            "(cycle_id, symbol, status, snapshot_id, research_run_id, experiment_id, "
            "baseline_recommendation_id, enhanced_recommendation_id, baseline_paper_submitted, "
            "failure_reason, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cycle_id, result.symbol, result.status, result.snapshot_id, result.research_run_id,
                result.experiment_id, result.baseline_recommendation_id, result.enhanced_recommendation_id,
                int(result.baseline_paper_submitted), result.failure_reason, created_at.isoformat(),
                completed_at.isoformat() if completed_at else None,
            ),
        )
        self._conn.commit()

    def mark_cycle_finished(self, cycle_id: str, status: str, completed_at: datetime) -> None:
        self._conn.execute(
            "UPDATE research_cycles SET status = ?, completed_at = ? WHERE cycle_id = ?",
            (status, completed_at.isoformat(), cycle_id),
        )
        self._conn.commit()

    def get_cycle(self, cycle_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM research_cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_symbol_results(self, cycle_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM research_cycle_symbol_results WHERE cycle_id = ? ORDER BY symbol", (cycle_id,)
        ).fetchall()
        return [dict(r) for r in rows]
