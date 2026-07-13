"""Unit tests for evaluation/time_to_fill.py — Milestone 6 docs/milestone-6.md
Step 22 category J (time to acknowledgement / first fill / terminal state)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from trading_research.evaluation.time_to_fill import (
    STATUS_CENSORED_UNFILLED,
    STATUS_INSUFFICIENT_DATA,
    STATUS_OK,
    compute_time_to_fill,
)

T0 = datetime(2026, 7, 1, 14, 30, tzinfo=timezone.utc)


@dataclass
class _Event:
    event_type: str
    occurred_at: datetime
    filled_quantity: int = 0


def test_full_fill_computes_all_three_durations():
    events = [
        _Event("SUBMITTED", T0),
        _Event("ACCEPTED", T0 + timedelta(seconds=2)),
        _Event("PARTIALLY_FILLED", T0 + timedelta(seconds=5), filled_quantity=5),
        _Event("FILLED", T0 + timedelta(seconds=10), filled_quantity=10),
    ]
    result = compute_time_to_fill("intent-1", events)
    assert result.status == STATUS_OK
    assert result.time_to_acknowledgement_seconds == 2
    assert result.time_to_first_fill_seconds == 5
    assert result.time_to_full_fill_seconds == 10


def test_cancelled_order_is_censored_not_zero():
    events = [_Event("SUBMITTED", T0), _Event("ACCEPTED", T0 + timedelta(seconds=1)), _Event("CANCELLED", T0 + timedelta(seconds=30))]
    result = compute_time_to_fill("intent-2", events)
    assert result.status == STATUS_CENSORED_UNFILLED
    assert result.time_to_full_fill_seconds is None  # never a fabricated zero


def test_rejected_order_is_censored():
    events = [_Event("SUBMITTED", T0), _Event("REJECTED", T0 + timedelta(seconds=1))]
    result = compute_time_to_fill("intent-3", events)
    assert result.status == STATUS_CENSORED_UNFILLED


def test_no_terminal_state_yet_is_censored_unfilled():
    events = [_Event("SUBMITTED", T0), _Event("ACCEPTED", T0 + timedelta(seconds=1))]
    result = compute_time_to_fill("intent-4", events)
    assert result.status == STATUS_CENSORED_UNFILLED
    assert result.time_to_acknowledgement_seconds == 1


def test_no_events_is_insufficient_data():
    result = compute_time_to_fill("intent-5", [])
    assert result.status == STATUS_INSUFFICIENT_DATA


def test_no_submitted_event_is_insufficient_data():
    events = [_Event("ACCEPTED", T0)]
    result = compute_time_to_fill("intent-6", events)
    assert result.status == STATUS_INSUFFICIENT_DATA
