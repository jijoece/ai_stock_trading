"""Schedule and catch-up semantics (docs/milestone-7.md Step 19, ADR 0005
Decision 2). Pure function: given the current time, the last completed
*intended* schedule time, and `config/shadow_operations.yaml`'s
`schedule`/`shadow_operations` sections, decide whether a scheduled shadow
cycle is due right now.

This module does NOT import `shadow/lease.py` or `shadow/pause.py` — the
`LEASE_HELD`/`PAUSED`/`KILLED` statuses named in the milestone doc's overall
status list are determined by `shadow/scheduler.py`'s orchestration *after*
consulting this module, not by this module itself (docs/milestone-7.md Step
19's status list is the union of what this module can determine plus what
the scheduler determines from lease/pause state it separately checks).

Deterministic "intended schedule time" for a given calendar day is
`schedule.intended_local_time` interpreted in `shadow_operations.
run_window_timezone`, using `zoneinfo` (stdlib) exactly like
`evaluation/market_calendar.py::is_market_open` already does — no new
timezone-conversion helper is added since none existed elsewhere to reuse
and `zoneinfo` is already the repository's one timezone-conversion primitive.

Only whole calendar days are iterated when walking backward for catch-up
candidates (`max_catch_up_cycles` bounds how many *missed* intended times
are considered, not how many trading days). Market-day gating reuses
`evaluation/market_calendar.py::is_trading_day` — this module inherits that
module's documented limitation (no early-close half-day modeling).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..evaluation.market_calendar import is_trading_day
from .config import ShadowOperationsConfiguration

STATUS_NOT_DUE = "NOT_DUE"
STATUS_DUE = "DUE"
STATUS_MISSED_WITHIN_CATCHUP = "MISSED_WITHIN_CATCHUP"
STATUS_MISSED_TOO_OLD = "MISSED_TOO_OLD"
STATUS_MARKET_HOLIDAY = "MARKET_HOLIDAY"
STATUS_OUTSIDE_RUN_WINDOW = "OUTSIDE_RUN_WINDOW"
STATUS_ALREADY_COMPLETED = "ALREADY_COMPLETED"

ACTIONABLE_STATUSES = (STATUS_DUE, STATUS_MISSED_WITHIN_CATCHUP)

Clock = Callable[[], datetime]


class ScheduleResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScheduleResolution:
    status: str
    intended_schedule_time: datetime | None
    intended_schedule_id: str | None
    reason: str

    @property
    def is_actionable(self) -> bool:
        return self.status in ACTIONABLE_STATUSES


def _parse_local_time(value: str) -> time:
    hour_str, minute_str = value.split(":")
    return time(int(hour_str), int(minute_str))


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleResolutionError(
            f"timezone database entry for {tz_name!r} is not available in this environment — "
            "cannot determine the intended schedule time, refusing to guess"
        ) from exc


def intended_schedule_time_for_day(day: date, config: ShadowOperationsConfiguration) -> datetime:
    """The deterministic, timezone-aware intended schedule moment for
    calendar day `day`. Naive local wall-clock time + IANA zone via
    `zoneinfo` correctly resolves DST offsets for that specific date (unlike
    a fixed UTC offset), including on spring-forward/fall-back days."""
    tz = _zone(config.shadow_operations.run_window_timezone)
    local_time = _parse_local_time(config.schedule.intended_local_time)
    return datetime.combine(day, local_time, tzinfo=tz)


def intended_schedule_id(intended_time: datetime) -> str:
    """Deterministic identity for one intended schedule slot — the
    scheduler's lease key and idempotency key are derived from this
    (ADR 0005 Decision 2), not from wall-clock invocation time. ISO-8601
    with explicit UTC offset so two representations of the same instant
    always produce the same id."""
    return f"shadow-cycle-{intended_time.astimezone(ZoneInfo('UTC')).isoformat()}"


def _run_window_bounds(day_intended_time: datetime, config: ShadowOperationsConfiguration) -> tuple[datetime, datetime]:
    tz = day_intended_time.tzinfo
    start = _parse_local_time(config.shadow_operations.run_window_start)
    end = _parse_local_time(config.shadow_operations.run_window_end)
    window_start = datetime.combine(day_intended_time.date(), start, tzinfo=tz)
    window_end = datetime.combine(day_intended_time.date(), end, tzinfo=tz)
    return window_start, window_end


_TOO_OLD_LOOKBACK_MARGIN_DAYS = 30
"""How many extra calendar days *beyond* the actionable catch-up window are
examined purely to detect MISSED_TOO_OLD (docs/milestone-7.md Step 19: "skip
cycles older than the catch-up window"). Only consulted when
`last_completed_intended_time` is known (see `resolve_due_status`'s Phase 1)
— without looking slightly further back than the window in that situation,
a stale backlog slot would silently vanish rather than surface as
MISSED_TOO_OLD. Bounded (not unbounded) so the lookback never grows
without limit."""


def actionable_window_days(today: date, max_catch_up_cycles: int) -> list[date]:
    """Oldest-first: today's own slot plus up to `max_catch_up_cycles` prior
    calendar days — the window within which a missed slot is actionable as
    MISSED_WITHIN_CATCHUP (docs/milestone-7.md Step 19: "maximum catch-up
    count")."""
    return list(reversed([today - timedelta(days=offset) for offset in range(max_catch_up_cycles + 1)]))


def too_old_margin_days(today: date, max_catch_up_cycles: int) -> list[date]:
    """Nearest-first: the bounded lookback margin strictly older than the
    actionable window, used only to detect a backlog slot that fell off the
    back of the catch-up window (MISSED_TOO_OLD). Ordered nearest-to-window
    first so the most recently missed backlog slot is the one reported."""
    start_offset = max_catch_up_cycles + 1
    end_offset = max_catch_up_cycles + _TOO_OLD_LOOKBACK_MARGIN_DAYS
    return [today - timedelta(days=offset) for offset in range(start_offset, end_offset + 1)]


def resolve_due_status(
    now: datetime,
    last_completed_intended_time: datetime | None,
    config: ShadowOperationsConfiguration,
    clock: Clock,
) -> ScheduleResolution:
    """Pure decision function. `clock` is accepted (and must agree with
    `now`, per repository convention of injecting a clock everywhere) but
    this function itself never calls it for anything other than validating
    it produces a timezone-aware `now`-shaped value is out of scope — `now`
    is the authoritative input; `clock` is accepted so callers that already
    hold a `Clock` can pass `clock()` directly without a second seam.

    Two phases:

    1. Check the *lookback margin* strictly older than the actionable
       catch-up window for any unresolved (not completed, not a skippable
       holiday) trading-day slot. If one exists, the backlog has genuinely
       aged out of the catch-up window — MISSED_TOO_OLD is returned
       immediately, ahead of today's own status, since a stale unaddressed
       backlog is a more urgent condition than today's own schedule state.
    2. Otherwise, walk the *actionable window* (today, then up to
       `max_catch_up_cycles` prior calendar days), oldest first, looking for
       the oldest slot that is not in the future, not already completed,
       and (when `require_market_open_day`) falls on a trading day. The
       first such slot found is reported — as MISSED_WITHIN_CATCHUP if it's
       a prior day, or DUE/OUTSIDE_RUN_WINDOW if it's today. Oldest-first
       ensures a genuinely missed prior slot within the window is always
       resolved before today's own slot is reported "DUE" — one process
       invocation addresses at most one intended slot.

    Never returns DUE/MISSED_* for a time that has not yet arrived relative
    to `now` — "no future cycle execution" (docs/milestone-7.md Step 19).
    """
    if now.tzinfo is None:
        raise ScheduleResolutionError("resolve_due_status requires a timezone-aware `now`")

    tz = _zone(config.shadow_operations.run_window_timezone)
    local_now = now.astimezone(tz)
    today = local_now.date()
    max_catch_up = config.shadow_operations.max_catch_up_cycles
    require_market_open_day = config.shadow_operations.require_market_open_day

    today_intended = intended_schedule_time_for_day(today, config)

    # Today's own slot has not arrived yet -> NOT_DUE, full stop. Never
    # "due early" (docs/milestone-7.md Step 19: "no future cycle execution").
    if local_now < today_intended:
        return ScheduleResolution(
            status=STATUS_NOT_DUE, intended_schedule_time=None, intended_schedule_id=None,
            reason=f"today's intended schedule time {today_intended.isoformat()} has not arrived yet",
        )

    window_start, window_end = _run_window_bounds(today_intended, config)
    outside_window_today = not (window_start <= local_now < window_end)

    def _is_completed(intended_time: datetime) -> bool:
        return last_completed_intended_time is not None and last_completed_intended_time >= intended_time

    def _is_skippable_holiday(day: date) -> bool:
        return require_market_open_day and not is_trading_day(day)

    # --- Phase 1: lookback margin — a backlog slot older than the catch-up
    # window that was missed since the last known completion. Only
    # consulted when `last_completed_intended_time` is known (a system that
    # has run before and fell behind) — a `None` watermark means "never run
    # before", which has no meaningful backlog to report (there is nothing
    # to have "missed" prior to a first-ever run; it simply starts fresh at
    # today/the actionable window, exactly like a brand-new deployment).
    # Checked before the actionable window so a genuinely stale backlog is
    # never masked by today's own (more recent, less urgent) status.
    if last_completed_intended_time is not None:
        for day in too_old_margin_days(today, max_catch_up):
            intended_time = intended_schedule_time_for_day(day, config)
            if local_now < intended_time:
                continue
            if intended_time <= last_completed_intended_time:
                continue  # at or before the last known completion — not a gap
            if _is_completed(intended_time):
                continue
            if _is_skippable_holiday(day):
                continue
            sid = intended_schedule_id(intended_time)
            return ScheduleResolution(
                status=STATUS_MISSED_TOO_OLD, intended_schedule_time=intended_time, intended_schedule_id=sid,
                reason=(
                    f"intended schedule time {intended_time.isoformat()} is older than the "
                    f"{max_catch_up}-cycle catch-up window and was never completed"
                ),
            )

    # --- Phase 2: the actionable catch-up window, oldest first -------------
    for day in actionable_window_days(today, max_catch_up):
        intended_time = intended_schedule_time_for_day(day, config)
        if local_now < intended_time:
            continue  # future slot relative to now — never due
        sid = intended_schedule_id(intended_time)
        is_today = day == today

        if _is_completed(intended_time):
            if is_today:
                return ScheduleResolution(
                    status=STATUS_ALREADY_COMPLETED, intended_schedule_time=intended_time,
                    intended_schedule_id=sid, reason=f"intended schedule time {intended_time.isoformat()} already completed",
                )
            continue

        if _is_skippable_holiday(day):
            if is_today:
                return ScheduleResolution(
                    status=STATUS_MARKET_HOLIDAY, intended_schedule_time=intended_time, intended_schedule_id=sid,
                    reason=f"{day.isoformat()} is not a market trading day",
                )
            continue

        # Found the oldest unresolved, non-holiday slot in the window.
        if is_today:
            if outside_window_today:
                return ScheduleResolution(
                    status=STATUS_OUTSIDE_RUN_WINDOW, intended_schedule_time=intended_time, intended_schedule_id=sid,
                    reason=(
                        f"now {local_now.isoformat()} is outside today's run window "
                        f"[{window_start.time()}, {window_end.time()})"
                    ),
                )
            return ScheduleResolution(
                status=STATUS_DUE, intended_schedule_time=intended_time, intended_schedule_id=sid,
                reason=f"intended schedule time {intended_time.isoformat()} is due now",
            )

        if outside_window_today:
            return ScheduleResolution(
                status=STATUS_OUTSIDE_RUN_WINDOW, intended_schedule_time=intended_time, intended_schedule_id=sid,
                reason=(
                    f"missed cycle {intended_time.isoformat()} is within the catch-up window but now "
                    f"{local_now.isoformat()} is outside today's run window "
                    f"[{window_start.time()}, {window_end.time()})"
                ),
            )
        return ScheduleResolution(
            status=STATUS_MISSED_WITHIN_CATCHUP, intended_schedule_time=intended_time, intended_schedule_id=sid,
            reason=f"intended schedule time {intended_time.isoformat()} was missed but is within the catch-up window",
        )

    # Every candidate in the actionable window (today plus catch-up days)
    # is already completed or was a non-trading day, and no older backlog
    # slot is outstanding — nothing left to do.
    return ScheduleResolution(
        status=STATUS_ALREADY_COMPLETED, intended_schedule_time=today_intended,
        intended_schedule_id=intended_schedule_id(today_intended),
        reason="all intended schedule times within the catch-up window are already completed or were non-trading days",
    )
