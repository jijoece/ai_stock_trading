"""Fair baseline-vs-enhanced experiment assignment for isolated paper books
(docs/milestone-8.md Step 14).

One row per `(cycle_id, symbol)`, written once at cycle time, before any
comparison or evaluation ever runs — so there is no path for "selective
assignment after seeing results" (Step 14's explicit requirement). Both arms
share the same evidence snapshot and `as_of`; nothing here re-fetches
evidence per arm or per book.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..storage import paper_books_repositories as repo

ASSIGNMENT_POLICY_VERSION = "paper-books-experiment-assignment-v1"


@dataclass(frozen=True)
class PaperBookExperimentAssignment:
    experiment_id: str
    cycle_id: str
    symbol: str
    as_of: datetime
    evidence_snapshot_id: str | None
    baseline_recommendation_id: str | None
    enhanced_recommendation_id: str | None
    baseline_book_id: str | None
    enhanced_book_id: str | None
    baseline_intent_id: str | None
    enhanced_intent_id: str | None
    assignment_policy_version: str = ASSIGNMENT_POLICY_VERSION


def save_assignment(conn, assignment: PaperBookExperimentAssignment, *, clock) -> bool:
    """Idempotent on `(cycle_id, symbol)` — a retried cycle invocation
    resolves to the same row and never overwrites it (the underlying table
    has a BEFORE UPDATE/DELETE trigger blocking any mutation)."""
    payload = {
        "experiment_id": assignment.experiment_id, "cycle_id": assignment.cycle_id, "symbol": assignment.symbol,
        "as_of": assignment.as_of, "evidence_snapshot_id": assignment.evidence_snapshot_id,
        "baseline_recommendation_id": assignment.baseline_recommendation_id,
        "enhanced_recommendation_id": assignment.enhanced_recommendation_id,
        "baseline_book_id": assignment.baseline_book_id, "enhanced_book_id": assignment.enhanced_book_id,
        "baseline_intent_id": assignment.baseline_intent_id, "enhanced_intent_id": assignment.enhanced_intent_id,
        "assignment_policy_version": assignment.assignment_policy_version, "created_at": clock(),
    }
    return repo.save_experiment_assignment(conn, payload)


def load_assignment(conn, cycle_id: str, symbol: str) -> dict | None:
    return repo.load_experiment_assignment(conn, cycle_id, symbol)


def list_assignments(conn, experiment_id: str) -> list[dict]:
    return repo.list_experiment_assignments(conn, experiment_id)
