"""Controlled recurring execution for the local simulated paper books.

This module only consumes explicitly queued, completed persisted cycles and
delegates their deterministic integration/lifecycle to Milestone 9.3's
``run_controlled_soak_day`` service.  It has no research, provider, external
broker, credential, or live-execution imports.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from ..evaluation.market_calendar import is_trading_day
from ..hashing import hash_config
from ..research.provider_provenance import compute_real_provider_history
from ..shadow import pause as pause_mod
from ..shadow import readiness as shadow_readiness
from ..storage import paper_books_repositories as pb_repo
from ..storage import shadow_alerts_repositories as alerts_repo
from ..storage.research_cycle_repositories import SQLiteResearchCycleRepository
from .config import PaperBooksConfiguration
from .controlled_soak_readiness import evaluate_controlled_soak_readiness
from .cross_book_verification import verification_is_stale
from .soak_campaign import (
    CAMPAIGN_COMPLETED_READY,
    RECOMMENDATION_READY,
    run_controlled_soak_day,
)

POLICY_VERSION = "paper-recurring-local/v1"
LEASE_NAME = "paper-recurring-local"
MAX_TEXT_LENGTH = 1000

STATE_INACTIVE = "INACTIVE"
STATE_ACTIVATION_REQUESTED = "ACTIVATION_REQUESTED"
STATE_ACTIVE = "ACTIVE"
STATE_PAUSED_BY_SAFETY = "PAUSED_BY_SAFETY"
STATE_DEACTIVATED = "DEACTIVATED"
ACTIVATION_STATES = (
    STATE_INACTIVE, STATE_ACTIVATION_REQUESTED, STATE_ACTIVE,
    STATE_PAUSED_BY_SAFETY, STATE_DEACTIVATED,
)

EVENT_ACTIVATION_REQUESTED = "ACTIVATION_REQUESTED"
EVENT_ACTIVATED = "ACTIVATED"
EVENT_SAFETY_PAUSED = "SAFETY_PAUSED"
EVENT_DEACTIVATED = "DEACTIVATED"

QUEUE_QUEUED = "QUEUED"
QUEUE_CLAIMED = "CLAIMED"
QUEUE_PROCESSED = "PROCESSED"
QUEUE_FAILED = "FAILED"
QUEUE_CANCELLED = "CANCELLED"
QUEUE_STATUSES = (QUEUE_QUEUED, QUEUE_CLAIMED, QUEUE_PROCESSED, QUEUE_FAILED, QUEUE_CANCELLED)

STATUS_COMPLETED = "COMPLETED"
STATUS_COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
STATUS_SKIPPED_INACTIVE = "SKIPPED_INACTIVE"
STATUS_SKIPPED_NOT_DUE = "SKIPPED_NOT_DUE"
STATUS_SKIPPED_ALREADY_COMPLETED = "SKIPPED_ALREADY_COMPLETED"
STATUS_SKIPPED_LEASE_HELD = "SKIPPED_LEASE_HELD"
STATUS_BLOCKED_SAFETY = "BLOCKED_SAFETY"
STATUS_FAILED = "FAILED"

TERMINAL_RUN_STATUSES = {
    STATUS_COMPLETED, STATUS_COMPLETED_WITH_WARNINGS, STATUS_BLOCKED_SAFETY, STATUS_FAILED,
}


class RecurringPaperError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActivationState:
    state: str
    event: dict | None

    @property
    def is_active(self) -> bool:
        return self.state == STATE_ACTIVE


@dataclass(frozen=True)
class ReviewValidation:
    valid: bool
    reasons: tuple[str, ...]
    review: dict | None


@dataclass(frozen=True)
class DueSlot:
    due: bool
    intended_schedule_id: str
    intended_at: datetime
    reason: str


@dataclass(frozen=True)
class SafetyGate:
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class RecurringLeaseHandle:
    lease_name: str
    owner_id: str
    scheduler_run_id: str
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class RecurringLeaseConflict:
    lease_name: str
    held_by: str
    expires_at: datetime


@dataclass(frozen=True)
class RecurringPaperSchedulerResult:
    status: str
    scheduler_run_id: str | None
    intended_schedule_id: str | None
    intended_at: datetime | None
    all_failed_checks: tuple[str, ...] = ()
    failure_reasons: tuple[str, ...] = ()
    queue_item_ids: tuple[str, ...] = ()
    requested_cycle_ids: tuple[str, ...] = ()
    processed_cycle_ids: tuple[str, ...] = ()
    operator_run_id: str | None = None
    lifecycle_run_id: str | None = None
    cross_book_verification_id: str | None = None
    cross_book_verification_status: str | None = None
    controlled_readiness_status: str | None = None
    lifecycle_only: bool = False


def _bounded_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecurringPaperError(f"{field} is required and must be non-empty")
    return value.strip()[:MAX_TEXT_LENGTH]


def _aware(value: datetime, field: str = "timestamp") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RecurringPaperError(f"{field} must be timezone-aware")
    return value


def _digest(prefix: str, payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:32]}"


def recurring_schedule_snapshot(config: PaperBooksConfiguration) -> dict:
    recurring = config.recurring
    return {
        "timezone": recurring.schedule.timezone,
        "market_days_only": recurring.schedule.market_days_only,
        "hour": recurring.schedule.hour,
        "minute": recurring.schedule.minute,
        "maximum_cycles_per_run": recurring.maximum_cycles_per_run,
        "maximum_runtime_seconds": recurring.maximum_runtime_seconds,
        "lease_ttl_seconds": recurring.lease_ttl_seconds,
        "activation_review_max_age_market_days": recurring.activation_review_max_age_market_days,
        "pause_on_safety_block": recurring.pause_on_safety_block,
    }


def recurring_config_hash(config: PaperBooksConfiguration) -> str:
    return hash_config({
        "paper_books_config_hash": config.config_hash,
        "recurring_enabled": config.recurring.enabled,
        "recurring": recurring_schedule_snapshot(config),
        "policy_version": POLICY_VERSION,
    })


def current_activation_state(conn: sqlite3.Connection) -> ActivationState:
    state = STATE_INACTIVE
    latest_valid = None
    transitions = {
        EVENT_ACTIVATION_REQUESTED: STATE_ACTIVATION_REQUESTED,
        EVENT_ACTIVATED: STATE_ACTIVE,
        EVENT_SAFETY_PAUSED: STATE_PAUSED_BY_SAFETY,
        EVENT_DEACTIVATED: STATE_DEACTIVATED,
    }
    allowed_previous = {
        EVENT_ACTIVATION_REQUESTED: {STATE_INACTIVE, STATE_PAUSED_BY_SAFETY, STATE_DEACTIVATED},
        EVENT_ACTIVATED: {STATE_ACTIVATION_REQUESTED},
        EVENT_SAFETY_PAUSED: {STATE_ACTIVE},
        EVENT_DEACTIVATED: {STATE_ACTIVATION_REQUESTED, STATE_ACTIVE, STATE_PAUSED_BY_SAFETY},
    }
    for event in pb_repo.list_recurring_activation_events(conn):
        expected_new = transitions.get(event["event_type"])
        if (
            expected_new is None or event["previous_state"] != state
            or state not in allowed_previous[event["event_type"]] or event["new_state"] != expected_new
        ):
            continue
        state = event["new_state"]
        latest_valid = event
    return ActivationState(state, latest_valid)


def _market_day_age(created: datetime, now: datetime, timezone_name: str) -> int:
    zone = ZoneInfo(timezone_name)
    start = created.astimezone(zone).date()
    end = now.astimezone(zone).date()
    if end <= start:
        return 0
    age = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if is_trading_day(cursor):
            age += 1
        cursor += timedelta(days=1)
    return age


def validate_activation_review(
    conn: sqlite3.Connection, *, activation_review_id: str, now: datetime,
    paper_books_config: PaperBooksConfiguration,
) -> ReviewValidation:
    _aware(now, "now")
    review = pb_repo.load_soak_activation_review(conn, activation_review_id)
    if review is None:
        return ReviewValidation(False, (f"activation review {activation_review_id!r} does not exist",), None)

    reasons: list[str] = []
    campaign = pb_repo.load_soak_campaign(conn, review["campaign_id"])
    if campaign is None or campaign["status"] != CAMPAIGN_COMPLETED_READY:
        reasons.append("activation review does not belong to a completed ready campaign")
    if review["final_recommendation"] != RECOMMENDATION_READY:
        reasons.append(f"final recommendation is {review['final_recommendation']!r}, not {RECOMMENDATION_READY}")

    readiness_history = review.get("controlled_readiness_history") or []
    final_readiness = readiness_history[-1] if readiness_history else None
    if final_readiness is None:
        reasons.append("activation review has no final controlled-readiness evidence")
    elif final_readiness.get("all_failed_checks"):
        reasons.append(f"activation review has unresolved failed checks: {final_readiness['all_failed_checks']}")

    verification_history = review.get("cross_book_verification_history") or []
    successful_verifications = [
        item for item in verification_history
        if item.get("status") == "PASSED" and not item.get("stale_at_final_review")
    ]
    if not successful_verifications:
        reasons.append("activation review has no successful, fresh cross-book verification")

    success_count = int((review.get("provider_success_counts") or {}).get("real_provider_success_cycles", 0))
    required = paper_books_config.soak_campaign.minimum_successful_real_provider_cycles
    if success_count < required:
        reasons.append(f"successful real-provider cycles {success_count} < required {required}")

    created_at = datetime.fromisoformat(review["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age = _market_day_age(created_at, now, paper_books_config.recurring.schedule.timezone)
    if age > paper_books_config.recurring.activation_review_max_age_market_days:
        reasons.append(
            f"activation review age {age} market days exceeds maximum "
            f"{paper_books_config.recurring.activation_review_max_age_market_days}"
        )

    critical = alerts_repo.list_alerts(conn, severity="CRITICAL", unresolved_only=True)
    newer_critical = [a["alert_id"] for a in critical if a["created_at"] > review["created_at"]]
    if newer_critical:
        reasons.append(f"newer unresolved CRITICAL alerts supersede the review: {newer_critical}")
    warnings = alerts_repo.list_alerts(conn, severity="WARNING", unresolved_only=True)
    if len(warnings) > paper_books_config.soak_campaign.maximum_unresolved_warnings:
        reasons.append(
            f"unresolved warnings {len(warnings)} exceed configured maximum "
            f"{paper_books_config.soak_campaign.maximum_unresolved_warnings}"
        )
    pause_state = pause_mod.current_state(conn)
    if pause_state.is_blocking:
        reasons.append(f"shadow operational state is {pause_state.state}: {pause_state.reason}")

    latest_verification = pb_repo.latest_cross_book_verification_upto(conn, now.isoformat())
    if latest_verification is None:
        reasons.append("no current cross-book verification is available")
    else:
        if latest_verification["status"] == "FAILED":
            reasons.append(f"newer/current cross-book verification {latest_verification['verification_id']} is FAILED")
        if verification_is_stale(conn, latest_verification, now):
            reasons.append(f"cross-book verification {latest_verification['verification_id']} is stale")

    mismatch = conn.execute(
        "SELECT reconciliation_id FROM paper_book_reconciliations "
        "WHERE created_at > ? AND status <> 'MATCHED' ORDER BY created_at, reconciliation_id LIMIT 1",
        (review["created_at"],),
    ).fetchone()
    if mismatch is not None:
        reasons.append(f"newer reconciliation {mismatch['reconciliation_id']} is blocking")
    unsafe_snapshot = conn.execute(
        "SELECT snapshot_id FROM paper_book_snapshots WHERE created_at > ? "
        "AND valuation_status IN ('POINT_IN_TIME_UNSAFE','SOURCE_UNAVAILABLE') "
        "ORDER BY created_at, snapshot_id LIMIT 1", (review["created_at"],),
    ).fetchone()
    if unsafe_snapshot is not None:
        reasons.append(f"newer paper snapshot {unsafe_snapshot['snapshot_id']} has unsafe valuation")
    lifecycle_failure = conn.execute(
        "SELECT lifecycle_run_id FROM paper_book_lifecycle_runs WHERE created_at > ? "
        "AND failure_reasons_json <> '[]' ORDER BY created_at, lifecycle_run_id LIMIT 1", (review["created_at"],),
    ).fetchone()
    if lifecycle_failure is not None:
        reasons.append(f"newer lifecycle run {lifecycle_failure['lifecycle_run_id']} has unresolved failures")
    return ReviewValidation(not reasons, tuple(reasons), review)


def _activation_event(
    *, event_type: str, previous_state: str, new_state: str, activation_review_id: str | None,
    campaign_id: str | None, request_event_id: str | None, operator: str, reason: str,
    requested_schedule: dict, created_at: datetime,
) -> dict:
    payload = {
        "event_type": event_type, "previous_state": previous_state, "new_state": new_state,
        "activation_review_id": activation_review_id, "campaign_id": campaign_id,
        "request_event_id": request_event_id, "operator": operator, "reason": reason,
        "requested_schedule": requested_schedule,
    }
    return {
        **payload, "activation_event_id": _digest("prae", {**payload, "created_at": created_at.isoformat()}),
        "created_at": created_at, "policy_version": POLICY_VERSION,
    }


def request_recurring_activation(
    conn: sqlite3.Connection, *, activation_review_id: str, operator: str, reason: str,
    paper_books_config: PaperBooksConfiguration, now: datetime | None = None,
    requested_schedule: dict | None = None,
) -> dict:
    operator = _bounded_text(operator, "operator")
    reason = _bounded_text(reason, "reason")
    now = _aware(now or datetime.now(timezone.utc), "now")
    schedule = requested_schedule or recurring_schedule_snapshot(paper_books_config)
    if not isinstance(schedule, dict) or not schedule:
        raise RecurringPaperError("requested schedule is required")
    validation = validate_activation_review(
        conn, activation_review_id=activation_review_id, now=now, paper_books_config=paper_books_config,
    )
    if not validation.valid:
        raise RecurringPaperError("activation review is not valid: " + "; ".join(validation.reasons))
    state = current_activation_state(conn)
    if state.state == STATE_ACTIVE:
        raise RecurringPaperError("recurring paper execution is already ACTIVE")
    event = _activation_event(
        event_type=EVENT_ACTIVATION_REQUESTED, previous_state=state.state,
        new_state=STATE_ACTIVATION_REQUESTED, activation_review_id=activation_review_id,
        campaign_id=validation.review["campaign_id"], request_event_id=None, operator=operator,
        reason=reason, requested_schedule=schedule, created_at=now,
    )
    if state.state == STATE_ACTIVATION_REQUESTED:
        if state.event and state.event["activation_event_id"] == event["activation_event_id"]:
            return state.event
        raise RecurringPaperError("another activation request is already pending")
    pb_repo.save_recurring_activation_event(conn, event)
    return pb_repo.load_recurring_activation_event(conn, event["activation_event_id"])


def activate_recurring(
    conn: sqlite3.Connection, *, request_event_id: str, operator: str,
    paper_books_config: PaperBooksConfiguration, now: datetime | None = None,
) -> dict:
    operator = _bounded_text(operator, "operator")
    now = _aware(now or datetime.now(timezone.utc), "now")
    request = pb_repo.load_recurring_activation_event(conn, request_event_id)
    if request is None or request["event_type"] != EVENT_ACTIVATION_REQUESTED:
        raise RecurringPaperError(f"unknown activation request event {request_event_id!r}")
    existing = conn.execute(
        "SELECT activation_event_id FROM paper_recurring_activation_events "
        "WHERE request_event_id = ? AND event_type = ? ORDER BY created_at LIMIT 1",
        (request_event_id, EVENT_ACTIVATED),
    ).fetchone()
    if existing is not None:
        persisted = pb_repo.load_recurring_activation_event(conn, existing["activation_event_id"])
        if persisted["operator"] == operator:
            return persisted
        raise RecurringPaperError("activation request has already been activated")
    state = current_activation_state(conn)
    if state.state != STATE_ACTIVATION_REQUESTED or not state.event or state.event["activation_event_id"] != request_event_id:
        raise RecurringPaperError("activation request is no longer current or valid")
    if request["requested_schedule"] != recurring_schedule_snapshot(paper_books_config):
        raise RecurringPaperError("requested schedule no longer matches current recurring configuration")
    validation = validate_activation_review(
        conn, activation_review_id=request["activation_review_id"], now=now,
        paper_books_config=paper_books_config,
    )
    if not validation.valid:
        raise RecurringPaperError("activation review failed approval-time revalidation: " + "; ".join(validation.reasons))
    event = _activation_event(
        event_type=EVENT_ACTIVATED, previous_state=STATE_ACTIVATION_REQUESTED, new_state=STATE_ACTIVE,
        activation_review_id=request["activation_review_id"], campaign_id=request["campaign_id"],
        request_event_id=request_event_id, operator=operator, reason=f"approved activation request {request_event_id}",
        requested_schedule=request["requested_schedule"], created_at=now,
    )
    pb_repo.save_recurring_activation_event(conn, event)
    return pb_repo.load_recurring_activation_event(conn, event["activation_event_id"])


def deactivate_recurring(
    conn: sqlite3.Connection, *, operator: str, reason: str, now: datetime | None = None,
) -> dict:
    operator = _bounded_text(operator, "operator")
    reason = _bounded_text(reason, "reason")
    now = _aware(now or datetime.now(timezone.utc), "now")
    state = current_activation_state(conn)
    if state.state in (STATE_INACTIVE, STATE_DEACTIVATED):
        if state.state == STATE_DEACTIVATED and state.event and state.event["operator"] == operator and state.event["reason"] == reason:
            return state.event
        raise RecurringPaperError("recurring paper execution is not active or pending")
    event = _activation_event(
        event_type=EVENT_DEACTIVATED, previous_state=state.state, new_state=STATE_DEACTIVATED,
        activation_review_id=state.event.get("activation_review_id") if state.event else None,
        campaign_id=state.event.get("campaign_id") if state.event else None,
        request_event_id=state.event.get("request_event_id") if state.event else None,
        operator=operator, reason=reason,
        requested_schedule=state.event.get("requested_schedule", {}) if state.event else {}, created_at=now,
    )
    pb_repo.save_recurring_activation_event(conn, event)
    return pb_repo.load_recurring_activation_event(conn, event["activation_event_id"])


def pause_recurring_for_safety(
    conn: sqlite3.Connection, *, operator: str, reason: str, now: datetime,
) -> dict | None:
    operator = _bounded_text(operator, "operator")
    reason = _bounded_text(reason, "reason")
    state = current_activation_state(conn)
    if state.state != STATE_ACTIVE or state.event is None:
        return None
    event = _activation_event(
        event_type=EVENT_SAFETY_PAUSED, previous_state=STATE_ACTIVE, new_state=STATE_PAUSED_BY_SAFETY,
        activation_review_id=state.event.get("activation_review_id"), campaign_id=state.event.get("campaign_id"),
        request_event_id=state.event.get("request_event_id"), operator=operator, reason=reason,
        requested_schedule=state.event.get("requested_schedule", {}), created_at=_aware(now, "now"),
    )
    pb_repo.save_recurring_activation_event(conn, event)
    return pb_repo.load_recurring_activation_event(conn, event["activation_event_id"])


def _cycle_frozen_state(conn: sqlite3.Connection, cycle_id: str) -> tuple[dict, str]:
    cycle = SQLiteResearchCycleRepository(conn).get_cycle(cycle_id)
    if cycle is None:
        raise RecurringPaperError(f"unknown cycle_id {cycle_id!r}")
    symbol_rows = [dict(row) for row in conn.execute(
        "SELECT * FROM research_cycle_symbol_results WHERE cycle_id = ? ORDER BY symbol", (cycle_id,)
    ).fetchall()]
    snapshot_ids = sorted({row["snapshot_id"] for row in symbol_rows if row.get("snapshot_id")})
    recommendation_ids = sorted({
        value for row in symbol_rows
        for value in (row.get("baseline_recommendation_id"), row.get("enhanced_recommendation_id")) if value
    })
    state = {
        "cycle": cycle,
        "symbols": symbol_rows,
        "evidence_status": [dict(row) for row in conn.execute(
            "SELECT * FROM research_cycle_symbol_evidence_status WHERE cycle_id = ? ORDER BY symbol", (cycle_id,)
        ).fetchall()],
        "provider_provenance": [dict(row) for row in conn.execute(
            "SELECT * FROM research_cycle_provider_provenance WHERE cycle_id = ? ORDER BY symbol, provider_category",
            (cycle_id,),
        ).fetchall()],
        "snapshots": [dict(row) for row in conn.execute(
            f"SELECT * FROM research_evidence_snapshots WHERE snapshot_id IN ({','.join('?' for _ in snapshot_ids)}) ORDER BY snapshot_id",
            snapshot_ids,
        ).fetchall()] if snapshot_ids else [],
        "recommendations": [dict(row) for row in conn.execute(
            f"SELECT * FROM recommendations WHERE rec_id IN ({','.join('?' for _ in recommendation_ids)}) ORDER BY rec_id",
            recommendation_ids,
        ).fetchall()] if recommendation_ids else [],
    }
    return cycle, hashlib.sha256(json.dumps(state, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def enqueue_recurring_cycle(
    conn: sqlite3.Connection, *, cycle_id: str, operator: str, reason: str,
    now: datetime | None = None,
) -> dict:
    operator = _bounded_text(operator, "operator")
    reason = _bounded_text(reason, "reason")
    now = _aware(now or datetime.now(timezone.utc), "now")
    cycle, frozen_hash = _cycle_frozen_state(conn, cycle_id)
    if cycle["status"] != "COMPLETED" or not cycle.get("completed_at"):
        raise RecurringPaperError(f"cycle {cycle_id!r} is not completed and frozen")
    existing = pb_repo.active_recurring_queue_item_for_cycle(conn, cycle_id)
    if existing is not None:
        return existing
    latest = conn.execute(
        "SELECT * FROM paper_recurring_cycle_queue WHERE cycle_id = ? ORDER BY enqueued_at DESC, rowid DESC LIMIT 1",
        (cycle_id,),
    ).fetchone()
    latest = dict(latest) if latest is not None else None
    if latest is not None and latest["status"] == QUEUE_PROCESSED:
        return latest
    retry_of = latest["queue_item_id"] if latest is not None and latest["status"] in (QUEUE_FAILED, QUEUE_CANCELLED) else None
    item = {
        "queue_item_id": _digest("prq", {
            "cycle_id": cycle_id, "frozen_state_hash": frozen_hash, "retry_of_queue_item_id": retry_of,
        }),
        "cycle_id": cycle_id, "status": QUEUE_QUEUED, "frozen_state_hash": frozen_hash,
        "retry_of_queue_item_id": retry_of,
        "enqueued_by": operator, "enqueue_reason": reason, "enqueued_at": now, "created_at": now,
    }
    try:
        pb_repo.save_recurring_queue_item(conn, item)
    except sqlite3.IntegrityError as exc:
        existing = pb_repo.active_recurring_queue_item_for_cycle(conn, cycle_id)
        if existing is not None:
            return existing
        raise RecurringPaperError(f"could not enqueue cycle {cycle_id!r}: {exc}") from exc
    return pb_repo.load_recurring_queue_item(conn, item["queue_item_id"])


def cancel_recurring_cycle(
    conn: sqlite3.Connection, *, queue_item_id: str, operator: str, reason: str,
    now: datetime | None = None,
) -> dict:
    operator = _bounded_text(operator, "operator")
    reason = _bounded_text(reason, "reason")
    now = _aware(now or datetime.now(timezone.utc), "now")
    item = pb_repo.load_recurring_queue_item(conn, queue_item_id)
    if item is None:
        raise RecurringPaperError(f"unknown queue_item_id {queue_item_id!r}")
    if item["status"] == QUEUE_CANCELLED:
        if item["cancelled_by"] == operator and item["cancel_reason"] == reason:
            return item
        raise RecurringPaperError("queue item is already cancelled")
    if item["status"] != QUEUE_QUEUED:
        raise RecurringPaperError(f"only QUEUED items can be cancelled; item is {item['status']}")
    conn.execute(
        "UPDATE paper_recurring_cycle_queue SET status = ?, cancelled_by = ?, cancel_reason = ?, cancelled_at = ? "
        "WHERE queue_item_id = ? AND status = ?",
        (QUEUE_CANCELLED, operator, reason, now.isoformat(), queue_item_id, QUEUE_QUEUED),
    )
    conn.commit()
    return pb_repo.load_recurring_queue_item(conn, queue_item_id)


def calculate_due_slot(now: datetime, config: PaperBooksConfiguration) -> DueSlot:
    _aware(now, "now")
    schedule = config.recurring.schedule
    zone = ZoneInfo(schedule.timezone)
    local_now = now.astimezone(zone)
    intended_at = datetime(
        local_now.year, local_now.month, local_now.day, schedule.hour, schedule.minute, tzinfo=zone,
    )
    # Normalize a configured wall time that falls in a spring-forward gap;
    # ambiguous fall-back times deterministically use fold=0. Market-day-only
    # schedules normally avoid both Sunday transitions, but the behavior is
    # defined for market_days_only=false as well.
    round_trip = intended_at.astimezone(timezone.utc).astimezone(zone)
    if (round_trip.hour, round_trip.minute) != (schedule.hour, schedule.minute):
        intended_at = round_trip
    identity = (
        f"paper-recurring:{local_now.date().isoformat()}:{schedule.hour:02d}:{schedule.minute:02d}:"
        f"{recurring_config_hash(config)}"
    )
    if schedule.market_days_only and not is_trading_day(local_now.date()):
        return DueSlot(False, identity, intended_at, "local date is not a configured market day")
    if local_now < intended_at:
        return DueSlot(False, identity, intended_at, "configured local schedule time has not arrived")
    return DueSlot(True, identity, intended_at, "current local-date slot is due")


def _scheduler_run_id(intended_schedule_id: str) -> str:
    return _digest("prun", intended_schedule_id)


def acquire_recurring_lease(
    conn: sqlite3.Connection, *, owner_id: str, scheduler_run_id: str, now: datetime, ttl_seconds: int,
) -> RecurringLeaseHandle | RecurringLeaseConflict:
    owner_id = _bounded_text(owner_id, "owner_id")
    now = _aware(now, "now")
    expires = now + timedelta(seconds=ttl_seconds)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM paper_recurring_scheduler_leases WHERE lease_name = ?", (LEASE_NAME,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO paper_recurring_scheduler_leases "
                "(lease_name, owner_id, acquired_at, heartbeat_at, expires_at, scheduler_run_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'HELD')",
                (LEASE_NAME, owner_id, now.isoformat(), now.isoformat(), expires.isoformat(), scheduler_run_id),
            )
        else:
            row_expires = datetime.fromisoformat(row["expires_at"])
            if row["status"] == "HELD" and row_expires > now:
                conn.rollback()
                return RecurringLeaseConflict(LEASE_NAME, row["owner_id"], row_expires)
            conn.execute(
                "UPDATE paper_recurring_scheduler_leases SET owner_id = ?, acquired_at = ?, heartbeat_at = ?, "
                "expires_at = ?, scheduler_run_id = ?, status = 'HELD', released_at = NULL WHERE lease_name = ?",
                (owner_id, now.isoformat(), now.isoformat(), expires.isoformat(), scheduler_run_id, LEASE_NAME),
            )
        conn.commit()
        return RecurringLeaseHandle(LEASE_NAME, owner_id, scheduler_run_id, now, expires)
    except Exception:
        conn.rollback()
        raise


def heartbeat_recurring_lease(
    conn: sqlite3.Connection, *, owner_id: str, now: datetime, ttl_seconds: int,
) -> RecurringLeaseHandle:
    now = _aware(now, "now")
    expires = now + timedelta(seconds=ttl_seconds)
    cursor = conn.execute(
        "UPDATE paper_recurring_scheduler_leases SET heartbeat_at = ?, expires_at = ? "
        "WHERE lease_name = ? AND owner_id = ? AND status = 'HELD'",
        (now.isoformat(), expires.isoformat(), LEASE_NAME, owner_id),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise RecurringPaperError("cannot heartbeat recurring lease not held by this owner")
    row = conn.execute(
        "SELECT scheduler_run_id, acquired_at FROM paper_recurring_scheduler_leases WHERE lease_name = ?", (LEASE_NAME,)
    ).fetchone()
    return RecurringLeaseHandle(LEASE_NAME, owner_id, row["scheduler_run_id"], datetime.fromisoformat(row["acquired_at"]), expires)


def release_recurring_lease(conn: sqlite3.Connection, *, owner_id: str, now: datetime) -> None:
    cursor = conn.execute(
        "UPDATE paper_recurring_scheduler_leases SET status = 'RELEASED', released_at = ? "
        "WHERE lease_name = ? AND owner_id = ? AND status = 'HELD'",
        (_aware(now, "now").isoformat(), LEASE_NAME, owner_id),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise RecurringPaperError("cannot release recurring lease not held by this owner")


_HARD_READINESS_STATUSES = {
    "NOT_READY_SHADOW_PAUSED", "NOT_READY_SHADOW_KILLED", "NOT_READY_HEALTH_UNEXPLAINED",
    "NOT_READY_CRITICAL_ALERTS", "NOT_READY_RECONCILIATION", "NOT_READY_VALUATION",
    "NOT_READY_CROSS_BOOK",
}


def evaluate_pre_run_safety_gates(
    conn: sqlite3.Connection, *, now: datetime, paper_books_config: PaperBooksConfiguration,
    shadow_config, due_slot: DueSlot | None = None, lease_acquired: bool | None = None,
) -> tuple[SafetyGate, ...]:
    _aware(now, "now")
    gates: list[SafetyGate] = []
    add = lambda name, passed, reason: gates.append(SafetyGate(name, bool(passed), str(reason)[:MAX_TEXT_LENGTH]))
    add("recurring_configuration_enabled", paper_books_config.recurring.enabled,
        "enabled" if paper_books_config.recurring.enabled else "paper_books.recurring.enabled is false")
    local_only = (
        paper_books_config.enabled and paper_books_config.lifecycle.enabled
        and paper_books_config.execution.provider == "local_simulated"
        and not paper_books_config.execution.allow_external_paper_broker
        and not paper_books_config.execution.allow_live_broker
    )
    add("local_paper_lifecycle_enabled", local_only,
        "local simulated paper books and lifecycle enabled" if local_only
        else "paper books/lifecycle are disabled or execution is not local_simulated-only")

    state = current_activation_state(conn)
    add("activation_state_active", state.is_active, f"current activation state is {state.state}")
    review_validation = ReviewValidation(False, ("no active activation event",), None)
    if state.event and state.event.get("activation_review_id"):
        review_validation = validate_activation_review(
            conn, activation_review_id=state.event["activation_review_id"], now=now,
            paper_books_config=paper_books_config,
        )
    add("activation_review_valid", review_validation.valid, "; ".join(review_validation.reasons) or "valid")

    pause_state = pause_mod.current_state(conn)
    add("shadow_kill_state", not pause_state.is_killed, pause_state.reason)
    add("shadow_pause_state", not pause_state.is_blocking, f"{pause_state.state}: {pause_state.reason}")
    unexplained = shadow_readiness._unexplained_pause_required_runs(conn)
    add("unexplained_pause_required", not unexplained, f"unexplained scheduler runs: {unexplained}" if unexplained else "none")
    critical = alerts_repo.list_alerts(conn, severity="CRITICAL", unresolved_only=True)
    add("unresolved_critical_alerts", not critical,
        f"unresolved CRITICAL alert_ids: {[a['alert_id'] for a in critical]}" if critical else "none")

    try:
        readiness = evaluate_controlled_soak_readiness(conn, now, paper_books_config, shadow_config)
        hard = readiness.status in _HARD_READINESS_STATUSES
        add("controlled_readiness_hard_block", not hard, readiness.status)
    except Exception as exc:
        add("controlled_readiness_hard_block", False, f"readiness unavailable: {exc}")

    provenance = compute_real_provider_history(conn, now)
    required = paper_books_config.soak_campaign.minimum_successful_real_provider_cycles
    add("real_provider_success_history", provenance.real_provider_success_cycle_count >= required,
        f"successful real-provider cycles {provenance.real_provider_success_cycle_count}; required {required}")

    latest = pb_repo.latest_cross_book_verification_upto(conn, now.isoformat())
    add("cross_book_verification_not_failed", latest is not None and latest["status"] != "FAILED",
        "missing" if latest is None else f"{latest['verification_id']}={latest['status']}")
    stale = True if latest is None else verification_is_stale(conn, latest, now)
    add("cross_book_verification_fresh", latest is not None and not stale,
        "missing" if latest is None else ("stale" if stale else "fresh"))

    try:
        conn.execute("SELECT 1").fetchone()
        database_ok, database_reason = True, "SQLite connection available"
    except sqlite3.Error as exc:
        database_ok, database_reason = False, f"database unavailable: {exc}"
    add("persistent_database_available", database_ok, database_reason)

    slot = due_slot or calculate_due_slot(now, paper_books_config)
    add("scheduler_slot_due", slot.due, slot.reason)
    if lease_acquired is not None:
        add("singleton_lease_acquired", lease_acquired, "acquired" if lease_acquired else "held by another owner")
    return tuple(gates)


def _failed(gates: tuple[SafetyGate, ...]) -> tuple[SafetyGate, ...]:
    return tuple(gate for gate in gates if not gate.passed)


def _claim_queue_items(
    conn: sqlite3.Connection, *, scheduler_run_id: str, now: datetime, maximum: int,
    lease_ttl_seconds: int,
) -> list[dict]:
    cutoff = (now - timedelta(seconds=lease_ttl_seconds)).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Recover only abandoned claims: same deterministic retry run, a
        # terminal failed run, or an expired orphan with no run evidence.
        conn.execute(
            "UPDATE paper_recurring_cycle_queue SET status = 'QUEUED', claimed_by_run_id = NULL, claimed_at = NULL "
            "WHERE status = 'CLAIMED' AND (claimed_by_run_id = ? OR claimed_at <= ? OR claimed_by_run_id IN "
            "(SELECT scheduler_run_id FROM paper_recurring_scheduler_runs WHERE status = 'FAILED'))",
            (scheduler_run_id, cutoff),
        )
        rows = conn.execute(
            "SELECT * FROM paper_recurring_cycle_queue WHERE status = 'QUEUED' "
            "ORDER BY enqueued_at, queue_item_id LIMIT ?", (maximum,),
        ).fetchall()
        claimed: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                cycle, current_hash = _cycle_frozen_state(conn, item["cycle_id"])
            except RecurringPaperError as exc:
                conn.execute(
                    "UPDATE paper_recurring_cycle_queue SET status = 'FAILED', failure_reason = ? WHERE queue_item_id = ?",
                    (str(exc)[:MAX_TEXT_LENGTH], item["queue_item_id"]),
                )
                continue
            if cycle["status"] != "COMPLETED" or current_hash != item["frozen_state_hash"]:
                conn.execute(
                    "UPDATE paper_recurring_cycle_queue SET status = 'FAILED', failure_reason = ? WHERE queue_item_id = ?",
                    ("cycle eligibility or frozen recommendation/evidence state changed after enqueue", item["queue_item_id"]),
                )
                continue
            conn.execute(
                "UPDATE paper_recurring_cycle_queue SET status = 'CLAIMED', claimed_by_run_id = ?, claimed_at = ?, "
                "failure_reason = NULL WHERE queue_item_id = ? AND status = 'QUEUED'",
                (scheduler_run_id, now.isoformat(), item["queue_item_id"]),
            )
            item.update(status=QUEUE_CLAIMED, claimed_by_run_id=scheduler_run_id, claimed_at=now.isoformat())
            claimed.append(item)
        conn.commit()
        return claimed
    except Exception:
        conn.rollback()
        raise


def _finalize_queue_items(
    conn: sqlite3.Connection, *, items: list[dict], processed_cycle_ids: tuple[str, ...],
    operator_run_id: str | None, now: datetime,
) -> None:
    processed = set(processed_cycle_ids)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in items:
            if item["cycle_id"] in processed:
                conn.execute(
                    "UPDATE paper_recurring_cycle_queue SET status = 'PROCESSED', processed_operator_run_id = ?, "
                    "processed_at = ?, failure_reason = NULL WHERE queue_item_id = ? AND status = 'CLAIMED'",
                    (operator_run_id, now.isoformat(), item["queue_item_id"]),
                )
            else:
                conn.execute(
                    "UPDATE paper_recurring_cycle_queue SET status = 'FAILED', failure_reason = ? "
                    "WHERE queue_item_id = ? AND status = 'CLAIMED'",
                    ("controlled soak did not confirm cycle integration", item["queue_item_id"]),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _release_claims_after_failure(conn: sqlite3.Connection, items: list[dict], reason: str) -> None:
    for item in items:
        conn.execute(
            "UPDATE paper_recurring_cycle_queue SET status = 'QUEUED', claimed_by_run_id = NULL, claimed_at = NULL, "
            "failure_reason = ? WHERE queue_item_id = ? AND status = 'CLAIMED'",
            (reason[:MAX_TEXT_LENGTH], item["queue_item_id"]),
        )
    conn.commit()


def _result_from_run(status: str, slot: DueSlot, scheduler_run_id: str | None, **kwargs) -> RecurringPaperSchedulerResult:
    return RecurringPaperSchedulerResult(
        status=status, scheduler_run_id=scheduler_run_id, intended_schedule_id=slot.intended_schedule_id,
        intended_at=slot.intended_at, **kwargs,
    )


def run_recurring_paper_scheduler(
    conn: sqlite3.Connection, *, now: datetime, paper_books_config: PaperBooksConfiguration,
    shadow_config, owner_id: str, price_provider=None,
    audit_clock: Callable[[], datetime] | None = None,
) -> RecurringPaperSchedulerResult:
    """Run at most the current local-date intended slot.

    A late invocation remains eligible only on that same local calendar date;
    this function never catches up prior dates or discovers unqueued cycles.
    """
    now = _aware(now, "now")
    owner_id = _bounded_text(owner_id, "owner_id")
    audit_clock = audit_clock or (lambda: datetime.now(timezone.utc))
    started_at = _aware(audit_clock(), "audit_clock")
    slot = calculate_due_slot(now, paper_books_config)
    scheduler_run_id = _scheduler_run_id(slot.intended_schedule_id)

    pre_gates = evaluate_pre_run_safety_gates(
        conn, now=now, paper_books_config=paper_books_config, shadow_config=shadow_config, due_slot=slot,
    )
    pre_failed = _failed(pre_gates)
    state = current_activation_state(conn)
    if not paper_books_config.recurring.enabled or not state.is_active:
        relevant = tuple(g for g in pre_failed if g.name in (
            "recurring_configuration_enabled", "activation_state_active", "activation_review_valid",
        ))
        return _result_from_run(
            STATUS_SKIPPED_INACTIVE, slot, None,
            all_failed_checks=tuple(g.name for g in relevant),
            failure_reasons=tuple(g.reason for g in relevant),
        )
    if not slot.due:
        return _result_from_run(
            STATUS_SKIPPED_NOT_DUE, slot, None,
            all_failed_checks=("scheduler_slot_due",), failure_reasons=(slot.reason,),
        )

    existing = pb_repo.load_recurring_scheduler_run_for_schedule(conn, slot.intended_schedule_id)
    if existing and existing["status"] in TERMINAL_RUN_STATUSES:
        return _result_from_run(
            STATUS_SKIPPED_ALREADY_COMPLETED, slot, existing["scheduler_run_id"],
            failure_reasons=(f"intended slot already has terminal status {existing['status']}",),
            queue_item_ids=tuple(existing["queue_item_ids"]),
            requested_cycle_ids=tuple(existing["requested_cycle_ids"]),
            processed_cycle_ids=tuple(existing["processed_cycle_ids"]),
            operator_run_id=existing["operator_run_id"], lifecycle_run_id=existing["lifecycle_run_id"],
            cross_book_verification_id=existing["cross_book_verification_id"],
            cross_book_verification_status=existing["cross_book_verification_status"],
            controlled_readiness_status=existing["controlled_readiness_status"],
            lifecycle_only=existing["lifecycle_only"],
        )

    lease = acquire_recurring_lease(
        conn, owner_id=owner_id, scheduler_run_id=scheduler_run_id, now=started_at,
        ttl_seconds=paper_books_config.recurring.lease_ttl_seconds,
    )
    if isinstance(lease, RecurringLeaseConflict):
        return _result_from_run(
            STATUS_SKIPPED_LEASE_HELD, slot, None, all_failed_checks=("singleton_lease_acquired",),
            failure_reasons=(f"lease held by {lease.held_by} until {lease.expires_at.isoformat()}",),
        )

    claimed: list[dict] = []
    try:
        under_lease_gates = evaluate_pre_run_safety_gates(
            conn, now=now, paper_books_config=paper_books_config, shadow_config=shadow_config,
            due_slot=slot, lease_acquired=True,
        )
        failed = _failed(under_lease_gates)
        non_lease_failed = tuple(g for g in failed if g.name != "singleton_lease_acquired")

        activation_event = current_activation_state(conn).event
        if existing is None:
            pb_repo.save_recurring_scheduler_run_started(conn, {
                "scheduler_run_id": scheduler_run_id, "intended_schedule_id": slot.intended_schedule_id,
                "intended_at": slot.intended_at, "started_at": started_at, "owner_id": owner_id,
                "lease_name": LEASE_NAME,
                "activation_event_id": activation_event["activation_event_id"] if activation_event else None,
                "activation_review_id": activation_event.get("activation_review_id") if activation_event else None,
                "config_hash": recurring_config_hash(paper_books_config), "policy_version": POLICY_VERSION,
                "created_at": started_at,
            })
        else:
            conn.execute(
                "UPDATE paper_recurring_scheduler_runs SET owner_id = ?, started_at = ? "
                "WHERE scheduler_run_id = ? AND status = 'RUNNING'",
                (owner_id, started_at.isoformat(), scheduler_run_id),
            )
            conn.commit()

        if non_lease_failed:
            ended = _aware(audit_clock(), "audit_clock")
            pb_repo.finalize_recurring_scheduler_run(conn, scheduler_run_id, {
                "ended_at": ended, "status": STATUS_BLOCKED_SAFETY,
                "all_failed_checks": [g.name for g in non_lease_failed],
                "failure_reasons": [g.reason for g in non_lease_failed], "lifecycle_only": False,
            })
            if paper_books_config.recurring.pause_on_safety_block:
                pause_recurring_for_safety(
                    conn, operator=f"scheduler:{owner_id}",
                    reason="pre-run safety gates failed: " + ", ".join(g.name for g in non_lease_failed), now=ended,
                )
            return _result_from_run(
                STATUS_BLOCKED_SAFETY, slot, scheduler_run_id,
                all_failed_checks=tuple(g.name for g in non_lease_failed),
                failure_reasons=tuple(g.reason for g in non_lease_failed),
            )

        claimed = _claim_queue_items(
            conn, scheduler_run_id=scheduler_run_id, now=started_at,
            maximum=paper_books_config.recurring.maximum_cycles_per_run,
            lease_ttl_seconds=paper_books_config.recurring.lease_ttl_seconds,
        )
        heartbeat_at = _aware(audit_clock(), "audit_clock")
        heartbeat_recurring_lease(
            conn, owner_id=owner_id, now=heartbeat_at,
            ttl_seconds=paper_books_config.recurring.lease_ttl_seconds,
        )
        if heartbeat_at > started_at + timedelta(seconds=paper_books_config.recurring.maximum_runtime_seconds):
            raise RecurringPaperError("maximum_runtime_seconds exceeded before lifecycle processing")
        cycle_ids = tuple(item["cycle_id"] for item in claimed)
        day = run_controlled_soak_day(
            conn, as_of=slot.intended_at, cycle_ids=cycle_ids, paper_books_config=paper_books_config,
            shadow_config=shadow_config, audit_clock=audit_clock, price_provider=price_provider,
        )
        if day["blocked_before_lifecycle"]:
            raise RecurringPaperError(f"controlled soak blocked before lifecycle: {day['block_reason']}")
        lifecycle_result = day["lifecycle_result"]
        operator_run = day["operator_run"]
        verification = day["verification"]
        readiness = day["controlled_readiness"]
        processed_cycle_ids = tuple(lifecycle_result.processed_cycle_ids)
        ended = _aware(audit_clock(), "audit_clock")
        _finalize_queue_items(
            conn, items=claimed, processed_cycle_ids=processed_cycle_ids,
            operator_run_id=operator_run["operator_run_id"] if operator_run else None, now=ended,
        )

        all_failed = tuple(check.name for check in readiness.checks if check.passed is False)
        post_reasons = list(lifecycle_result.failure_reasons)
        if ended > started_at + timedelta(seconds=paper_books_config.recurring.maximum_runtime_seconds):
            post_reasons.append("maximum_runtime_seconds exceeded during controlled lifecycle processing")
        if verification.status == "FAILED":
            post_reasons.append(f"cross-book verification {verification.verification_id} FAILED")
        for book_id, recon_status in lifecycle_result.reconciliation_statuses.items():
            if recon_status != "MATCHED":
                post_reasons.append(f"{book_id} reconciliation is {recon_status}")
        hard = bool(post_reasons) or readiness.status in _HARD_READINESS_STATUSES
        status = STATUS_BLOCKED_SAFETY if hard else (
            STATUS_COMPLETED_WITH_WARNINGS if all_failed or verification.status != "PASSED" else STATUS_COMPLETED
        )
        pb_repo.finalize_recurring_scheduler_run(conn, scheduler_run_id, {
            "ended_at": ended, "queue_item_ids": [item["queue_item_id"] for item in claimed],
            "requested_cycle_ids": cycle_ids, "processed_cycle_ids": processed_cycle_ids,
            "operator_run_id": operator_run["operator_run_id"] if operator_run else None,
            "lifecycle_run_id": lifecycle_result.lifecycle_run_id,
            "cross_book_verification_id": verification.verification_id,
            "cross_book_verification_status": verification.status,
            "controlled_readiness_status": readiness.status, "all_failed_checks": all_failed,
            "lifecycle_only": not cycle_ids, "status": status, "failure_reasons": post_reasons,
        })
        if hard and paper_books_config.recurring.pause_on_safety_block:
            pause_recurring_for_safety(
                conn, operator=f"scheduler:{owner_id}",
                reason="post-run safety block: " + "; ".join(post_reasons or [readiness.status]), now=ended,
            )
        return _result_from_run(
            status, slot, scheduler_run_id, all_failed_checks=all_failed,
            failure_reasons=tuple(post_reasons), queue_item_ids=tuple(item["queue_item_id"] for item in claimed),
            requested_cycle_ids=cycle_ids, processed_cycle_ids=processed_cycle_ids,
            operator_run_id=operator_run["operator_run_id"] if operator_run else None,
            lifecycle_run_id=lifecycle_result.lifecycle_run_id,
            cross_book_verification_id=verification.verification_id,
            cross_book_verification_status=verification.status,
            controlled_readiness_status=readiness.status, lifecycle_only=not cycle_ids,
        )
    except Exception as exc:
        reason = str(exc)[:MAX_TEXT_LENGTH]
        if claimed:
            _release_claims_after_failure(conn, claimed, reason)
        persisted = pb_repo.load_recurring_scheduler_run(conn, scheduler_run_id)
        if persisted and persisted["status"] == "RUNNING":
            pb_repo.finalize_recurring_scheduler_run(conn, scheduler_run_id, {
                "ended_at": _aware(audit_clock(), "audit_clock"), "status": STATUS_FAILED,
                "failure_reasons": [reason], "queue_item_ids": [i["queue_item_id"] for i in claimed],
                "requested_cycle_ids": [i["cycle_id"] for i in claimed], "lifecycle_only": not claimed,
            })
        return _result_from_run(
            STATUS_FAILED, slot, scheduler_run_id, failure_reasons=(reason,),
            queue_item_ids=tuple(i["queue_item_id"] for i in claimed),
            requested_cycle_ids=tuple(i["cycle_id"] for i in claimed), lifecycle_only=not claimed,
        )
    finally:
        try:
            release_recurring_lease(conn, owner_id=owner_id, now=_aware(audit_clock(), "audit_clock"))
        except RecurringPaperError:
            pass


# Short compatibility aliases for callers/tests that prefer state-machine verbs.
request_activation = request_recurring_activation
activate = activate_recurring
deactivate = deactivate_recurring
enqueue_cycle = enqueue_recurring_cycle
cancel_cycle = cancel_recurring_cycle
