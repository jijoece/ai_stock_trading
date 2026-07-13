"""Shared Milestone 5 research-layer test support: an in-memory fake
ResearchRepository so orchestrator/replay tests can exercise persistence
semantics (idempotency, resumption, append-only attempts) without a real
SQLite connection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from trading_research.research.models import ResearchDecision, RoleResearchReport
from trading_research.research.orchestration import ResearchAttemptRecord


@dataclass
class FakeResearchRepository:
    runs: dict = field(default_factory=dict)  # research_run_id -> dict(status=..., snapshot_id=..., ...)
    attempts: list = field(default_factory=list)
    role_reports: dict = field(default_factory=dict)  # (research_run_id, role) -> RoleResearchReport
    decisions: dict = field(default_factory=dict)  # research_run_id -> ResearchDecision

    def get_run_status(self, research_run_id: str) -> str | None:
        run = self.runs.get(research_run_id)
        return run["status"] if run else None

    def get_decision_for_run(self, research_run_id: str) -> ResearchDecision | None:
        return self.decisions.get(research_run_id)

    def get_role_reports_for_run(self, research_run_id: str) -> tuple[RoleResearchReport, ...]:
        return tuple(r for (rid, role), r in self.role_reports.items() if rid == research_run_id)

    def save_run_started(self, research_run_id, snapshot_id, provider, model_name, roles, run_mode, config_hash, created_at) -> None:
        self.runs.setdefault(research_run_id, {
            "status": "RUNNING", "snapshot_id": snapshot_id, "provider": provider, "model_name": model_name,
            "roles": roles, "run_mode": run_mode, "config_hash": config_hash, "created_at": created_at,
        })

    def save_attempt(self, attempt: ResearchAttemptRecord) -> None:
        self.attempts.append(attempt)

    def save_role_report(self, report: RoleResearchReport, attempt_id: str, created_at: datetime) -> None:
        self.role_reports[(report.research_run_id, report.role)] = report

    def save_decision(self, decision: ResearchDecision, created_at: datetime) -> None:
        self.decisions[decision.research_run_id] = decision

    def mark_run_finished(self, research_run_id: str, status: str, completed_at: datetime) -> None:
        self.runs[research_run_id]["status"] = status
