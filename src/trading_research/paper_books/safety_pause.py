"""Append-only paper-book safety pause and explicit operator resume."""
from __future__ import annotations

import hashlib
from datetime import datetime

from ..storage import paper_books_repositories as repo


class SafetyPauseError(RuntimeError):
    pass


def _event_id(book_id: str, state: str, source: str, at: datetime) -> str:
    raw = f"{book_id}:{state}:{source}:{at.isoformat()}"
    return "pb-safety-" + hashlib.sha256(raw.encode()).hexdigest()[:40]


def is_paused(conn, book_id: str) -> bool:
    latest = repo.latest_book_safety_event(conn, book_id)
    return latest is not None and latest["state"] == "PAUSED"


def pause_for_risk_state(
    conn, *, book_id: str, reason_code: str, risk_state_id: str,
    reason: str, at: datetime,
) -> bool:
    if at.tzinfo is None:
        raise SafetyPauseError("pause timestamp must be timezone-aware")
    return repo.record_book_safety_event(
        conn, safety_event_id=_event_id(book_id, "PAUSED", risk_state_id, at),
        book_id=book_id, state="PAUSED", reason_code=reason_code,
        source_risk_state_id=risk_state_id, operator=None, reason=reason,
        created_at=at,
    )


def resume(
    conn, *, book_id: str, operator: str, reason: str, at: datetime,
) -> bool:
    if at.tzinfo is None:
        raise SafetyPauseError("resume timestamp must be timezone-aware")
    if not operator.strip() or not reason.strip():
        raise SafetyPauseError("explicit operator and reason are required")
    if not is_paused(conn, book_id):
        raise SafetyPauseError("book has no active advanced-risk pause")
    source = f"{operator}:{reason}"
    return repo.record_book_safety_event(
        conn, safety_event_id=_event_id(book_id, "RESUMED", source, at),
        book_id=book_id, state="RESUMED", reason_code="EXPLICIT_OPERATOR_RESUME",
        source_risk_state_id=None, operator=operator, reason=reason, created_at=at,
    )
