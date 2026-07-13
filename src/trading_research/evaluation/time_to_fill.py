"""Time-to-fill metrics (docs/milestone-6.md Step 17). Pure functions over an
intent's already-persisted `PaperExecutionEvent` stream
(`execution/models.py`, unchanged) — no new event types, no new persistence.

Cancelled, rejected, or still-unfilled orders are never treated as a zero
fill time (docs/milestone-6.md: "Do not treat cancelled or expired orders as
zero fill time") — they get an explicit `CENSORED_UNFILLED` status with
`time_to_full_fill_seconds=None` instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

STATUS_OK = "OK"
STATUS_CENSORED_UNFILLED = "CENSORED_UNFILLED"
STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

_TERMINAL_EVENT_TYPES = ("FILLED", "CANCELLED", "REJECTED", "ERROR")
_ACK_EVENT_TYPES = ("ACCEPTED", "PARTIALLY_FILLED", "FILLED")


@dataclass(frozen=True)
class TimeToFillResult:
    intent_id: str
    status: str
    submitted_at: datetime | None
    acknowledged_at: datetime | None
    first_fill_at: datetime | None
    terminal_at: datetime | None
    terminal_event_type: str | None
    time_to_acknowledgement_seconds: float | None
    time_to_first_fill_seconds: float | None
    time_to_full_fill_seconds: float | None
    reason: str | None = None


def compute_time_to_fill(intent_id: str, events: list) -> TimeToFillResult:
    if not events:
        return TimeToFillResult(
            intent_id=intent_id, status=STATUS_INSUFFICIENT_DATA, submitted_at=None, acknowledged_at=None,
            first_fill_at=None, terminal_at=None, terminal_event_type=None,
            time_to_acknowledgement_seconds=None, time_to_first_fill_seconds=None, time_to_full_fill_seconds=None,
            reason="no execution events recorded for this intent",
        )

    ordered = sorted(events, key=lambda e: e.occurred_at)
    submitted = next((e for e in ordered if e.event_type == "SUBMITTED"), None)
    if submitted is None:
        return TimeToFillResult(
            intent_id=intent_id, status=STATUS_INSUFFICIENT_DATA, submitted_at=None, acknowledged_at=None,
            first_fill_at=None, terminal_at=None, terminal_event_type=None,
            time_to_acknowledgement_seconds=None, time_to_first_fill_seconds=None, time_to_full_fill_seconds=None,
            reason="no SUBMITTED event recorded — cannot anchor time-to-fill",
        )

    acknowledged = next((e for e in ordered if e.event_type in _ACK_EVENT_TYPES), None)
    first_fill = next((e for e in ordered if e.filled_quantity and e.filled_quantity > 0), None)
    terminal = next((e for e in reversed(ordered) if e.event_type in _TERMINAL_EVENT_TYPES), None)

    ack_seconds = (acknowledged.occurred_at - submitted.occurred_at).total_seconds() if acknowledged else None
    first_fill_seconds = (first_fill.occurred_at - submitted.occurred_at).total_seconds() if first_fill else None

    if terminal is None or terminal.event_type != "FILLED":
        return TimeToFillResult(
            intent_id=intent_id, status=STATUS_CENSORED_UNFILLED, submitted_at=submitted.occurred_at,
            acknowledged_at=acknowledged.occurred_at if acknowledged else None,
            first_fill_at=first_fill.occurred_at if first_fill else None,
            terminal_at=terminal.occurred_at if terminal else None,
            terminal_event_type=terminal.event_type if terminal else None,
            time_to_acknowledgement_seconds=ack_seconds, time_to_first_fill_seconds=first_fill_seconds,
            time_to_full_fill_seconds=None,
            reason="order never reached a full FILLED terminal state" if terminal is None else f"terminal state was {terminal.event_type}, not FILLED",
        )

    full_fill_seconds = (terminal.occurred_at - submitted.occurred_at).total_seconds()
    return TimeToFillResult(
        intent_id=intent_id, status=STATUS_OK, submitted_at=submitted.occurred_at,
        acknowledged_at=acknowledged.occurred_at if acknowledged else None,
        first_fill_at=first_fill.occurred_at if first_fill else None, terminal_at=terminal.occurred_at,
        terminal_event_type=terminal.event_type, time_to_acknowledgement_seconds=ack_seconds,
        time_to_first_fill_seconds=first_fill_seconds, time_to_full_fill_seconds=full_fill_seconds,
    )


def average_time_to_full_fill(results: list[TimeToFillResult], *, min_sample_size: int = 3) -> dict:
    ok = [r for r in results if r.status == STATUS_OK and r.time_to_full_fill_seconds is not None]
    if len(ok) < min_sample_size:
        return {"status": "INSUFFICIENT_DATA", "value": None, "sample_size": len(ok)}
    value = sum(r.time_to_full_fill_seconds for r in ok) / len(ok)
    return {"status": "OK", "value": value, "sample_size": len(ok)}
