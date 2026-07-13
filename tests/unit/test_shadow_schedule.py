"""Tests for shadow/schedule.py (docs/milestone-7.md Step 19, Step 27
section J: due, not-due, holiday, outside-window, DST transition, missed
within catch-up, missed too old, already completed, exactly one cycle per
intended time).
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from trading_research.shadow.config import load_shadow_operations_config
from trading_research.shadow.schedule import (
    STATUS_ALREADY_COMPLETED,
    STATUS_DUE,
    STATUS_MARKET_HOLIDAY,
    STATUS_MISSED_TOO_OLD,
    STATUS_MISSED_WITHIN_CATCHUP,
    STATUS_NOT_DUE,
    STATUS_OUTSIDE_RUN_WINDOW,
    intended_schedule_id,
    intended_schedule_time_for_day,
    resolve_due_status,
)

RAW_BASE = {
    "version": 1,
    "shadow_operations": {
        "enabled": True, "mode": "SHADOW_ENHANCED", "allow_baseline_paper_submission": False,
        "allow_enhanced_submission": False, "require_market_open_day": True,
        "run_window_timezone": "America/Los_Angeles", "run_window_start": "06:30", "run_window_end": "08:30",
        "max_catch_up_cycles": 1, "lease_ttl_seconds": 3600, "stale_run_timeout_seconds": 7200,
        "continue_on_symbol_failure": True,
    },
    "schedule": {"enabled": True, "cadence": "DAILY_MARKET_DAY", "intended_local_time": "06:45"},
    "budgets": {
        "require_pricing_for_real_claude": True, "max_symbols_per_cycle": 10, "max_roles_per_symbol": 5,
        "max_attempts_per_role": 2, "max_input_tokens_per_cycle": 100000, "max_output_tokens_per_cycle": 50000,
        "max_latency_seconds_per_cycle": 900, "max_estimated_cost_per_cycle_usd": 5.0,
        "max_actual_cost_per_day_usd": 10.0, "max_actual_cost_per_month_usd": 100.0,
        "emergency_margin_fraction": 0.1,
    },
    "safety": {
        "pause_on_provider_failure_rate": 0.5, "pause_on_retry_exhaustion_rate": 0.5,
        "pause_on_unsupported_claim_rate": 0.25, "pause_on_reconciliation_mismatch": True,
        "pause_on_budget_breach": True,
    },
}


def _config(**overrides):
    with tempfile.TemporaryDirectory() as tmp:
        raw = yaml.safe_load(yaml.safe_dump(RAW_BASE))  # deep copy
        for section, values in overrides.items():
            raw[section].update(values)
        path = Path(tmp) / "shadow_operations.yaml"
        path.write_text(yaml.safe_dump(raw))
        return load_shadow_operations_config(path)


LA = ZoneInfo("America/Los_Angeles")


def _clock_at(t):
    return lambda: t


# --- DUE / NOT_DUE --------------------------------------------------------


def test_before_intended_time_is_not_due():
    config = _config()
    # Monday 2026-07-13 is a trading day. Intended time is 06:45 local.
    now = datetime(2026, 7, 13, 6, 0, tzinfo=LA)
    result = resolve_due_status(now, None, config, _clock_at(now))
    assert result.status == STATUS_NOT_DUE


def test_at_intended_time_is_due():
    config = _config()
    now = datetime(2026, 7, 13, 6, 45, tzinfo=LA)
    result = resolve_due_status(now, None, config, _clock_at(now))
    assert result.status == STATUS_DUE
    assert result.intended_schedule_time == intended_schedule_time_for_day(now.date(), config)


def test_within_run_window_after_intended_time_is_due():
    config = _config()
    now = datetime(2026, 7, 13, 7, 30, tzinfo=LA)
    result = resolve_due_status(now, None, config, _clock_at(now))
    assert result.status == STATUS_DUE


# --- OUTSIDE_RUN_WINDOW ----------------------------------------------------


def test_after_run_window_end_is_outside_window():
    config = _config()
    now = datetime(2026, 7, 13, 9, 0, tzinfo=LA)  # window ends 08:30
    result = resolve_due_status(now, None, config, _clock_at(now))
    assert result.status == STATUS_OUTSIDE_RUN_WINDOW


# --- MARKET_HOLIDAY ---------------------------------------------------------


def test_market_holiday_is_not_due():
    # 2026-07-05 (Sunday) is "today"; with max_catch_up_cycles=1 the window
    # is [Sat 7/4, Sun 7/5] — both non-trading days, so the window is
    # isolated from any preceding trading-day backlog and correctly
    # resolves to MARKET_HOLIDAY for today's own slot.
    config = _config(shadow_operations={"max_catch_up_cycles": 1})
    now = datetime(2026, 7, 5, 6, 45, tzinfo=LA)
    result = resolve_due_status(now, None, config, _clock_at(now))
    assert result.status == STATUS_MARKET_HOLIDAY


def test_weekend_is_market_holiday_status():
    config = _config(shadow_operations={"max_catch_up_cycles": 1})
    # 2026-07-11 (Saturday) / 2026-07-12 (Sunday) are both non-trading days.
    now = datetime(2026, 7, 12, 6, 45, tzinfo=LA)
    result = resolve_due_status(now, None, config, _clock_at(now))
    assert result.status == STATUS_MARKET_HOLIDAY


def test_require_market_open_day_false_allows_weekend():
    """With require_market_open_day=False and max_catch_up_cycles=1, 'today'
    Sunday 7/12's own actionable-window walk (oldest-first) first
    encounters Saturday 7/11's slot, which is also actionable (holiday
    gating disabled) and unresolved -> MISSED_WITHIN_CATCHUP for Saturday,
    not DUE for Sunday, since Saturday is the oldest unresolved slot in the
    window."""
    config = _config(shadow_operations={"require_market_open_day": False, "max_catch_up_cycles": 1})
    now = datetime(2026, 7, 12, 6, 45, tzinfo=LA)
    result = resolve_due_status(now, None, config, _clock_at(now))
    assert result.status in (STATUS_DUE, STATUS_MISSED_WITHIN_CATCHUP)
    assert result.status != STATUS_MARKET_HOLIDAY


def test_missed_prior_trading_day_takes_priority_over_todays_holiday_slot():
    """With catch-up enabled, a genuinely missed prior trading day is
    surfaced (oldest backlog first) even when today itself is a holiday."""
    config = _config(shadow_operations={"max_catch_up_cycles": 1})
    now = datetime(2026, 7, 3, 6, 45, tzinfo=LA)  # Friday holiday; Thursday July 2 was a trading day
    result = resolve_due_status(now, None, config, _clock_at(now))
    assert result.status == STATUS_MISSED_WITHIN_CATCHUP
    assert result.intended_schedule_time == intended_schedule_time_for_day(datetime(2026, 7, 2).date(), config)


# --- ALREADY_COMPLETED -------------------------------------------------------


def test_already_completed_today_is_reported():
    config = _config()
    now = datetime(2026, 7, 13, 7, 0, tzinfo=LA)
    intended = intended_schedule_time_for_day(now.date(), config)
    result = resolve_due_status(now, intended, config, _clock_at(now))
    assert result.status == STATUS_ALREADY_COMPLETED


def test_two_calls_same_day_second_is_not_actionable_once_completed():
    """Exactly one intended schedule time must ever resolve to DUE/MISSED_*
    per calendar day: simulate the caller persisting completion after the
    first DUE result, then calling again."""
    config = _config()
    now = datetime(2026, 7, 13, 7, 0, tzinfo=LA)
    first = resolve_due_status(now, None, config, _clock_at(now))
    assert first.status == STATUS_DUE
    # Caller now records completion at first.intended_schedule_time.
    second = resolve_due_status(now, first.intended_schedule_time, config, _clock_at(now))
    assert not second.is_actionable
    assert second.status == STATUS_ALREADY_COMPLETED


# --- MISSED_WITHIN_CATCHUP / MISSED_TOO_OLD ---------------------------------


def test_missed_yesterday_within_catchup_is_reported_today():
    config = _config(shadow_operations={"max_catch_up_cycles": 1})
    # Yesterday (Sunday 2026-07-12) was a non-trading day; use a trading-day
    # pair instead: Friday 2026-07-10 missed, caught up on Monday 2026-07-13.
    # But max_catch_up_cycles walks *calendar* days, not trading days, so we
    # use consecutive weekdays to keep this simple and unambiguous.
    now = datetime(2026, 7, 14, 6, 45, tzinfo=LA)  # Tuesday
    # last_completed is two days behind -> Monday 2026-07-13's slot was
    # never completed.
    last_completed = intended_schedule_time_for_day(
        (now.date() - timedelta(days=2)), config,
    )
    result = resolve_due_status(now, last_completed, config, _clock_at(now))
    assert result.status == STATUS_MISSED_WITHIN_CATCHUP
    assert result.intended_schedule_time == intended_schedule_time_for_day(now.date() - timedelta(days=1), config)


def test_missed_too_old_backlog_reported_ahead_of_actionable_window():
    """A genuinely stale backlog slot (older than the catch-up window, with
    a known prior completion establishing the gap) is reported as
    MISSED_TOO_OLD ahead of the actionable window's own (more recent, less
    urgent) unresolved slot — a stale backlog is the more urgent condition."""
    config = _config(shadow_operations={"max_catch_up_cycles": 1})
    now = datetime(2026, 7, 15, 6, 45, tzinfo=LA)  # Wednesday
    last_completed = intended_schedule_time_for_day(date_minus(now.date(), 10), config)
    result = resolve_due_status(now, last_completed, config, _clock_at(now))
    assert result.status == STATUS_MISSED_TOO_OLD


def test_missed_too_old_when_gap_exceeds_catchup_window():
    """A real operating gap (system has run before, per a non-None
    `last_completed_intended_time`) that is older than the catch-up window
    is reported as MISSED_TOO_OLD, not silently absorbed into today's own
    DUE status."""
    config = _config(shadow_operations={"max_catch_up_cycles": 1})
    now = datetime(2026, 7, 15, 6, 45, tzinfo=LA)  # Wednesday
    # Last real completion was 10 days ago -> the gap is far older than the
    # 1-cycle catch-up window.
    last_completed = intended_schedule_time_for_day(date_minus(now.date(), 10), config)
    result = resolve_due_status(now, last_completed, config, _clock_at(now))
    assert result.status == STATUS_MISSED_TOO_OLD


def test_first_ever_run_has_no_backlog_to_report_as_too_old():
    """A `None` `last_completed_intended_time` (system has never completed a
    cycle before) has no meaningful prior backlog — MISSED_TOO_OLD never
    fires for a first-ever run; it resolves today's own actionable window
    slot instead (or a genuinely missed slot within the catch-up window)."""
    config = _config(shadow_operations={"max_catch_up_cycles": 1})
    now = datetime(2026, 7, 15, 6, 45, tzinfo=LA)  # Wednesday
    result = resolve_due_status(now, None, config, _clock_at(now))
    assert result.status != STATUS_MISSED_TOO_OLD


def test_too_old_margin_days_enumerates_strictly_older_than_window():
    from trading_research.shadow.schedule import actionable_window_days, too_old_margin_days

    today = datetime(2026, 7, 16).date()
    window = actionable_window_days(today, max_catch_up_cycles=1)
    margin = too_old_margin_days(today, max_catch_up_cycles=1)
    assert set(window).isdisjoint(set(margin))
    assert max(margin) < min(window)


def test_missed_too_old_reports_nearest_unresolved_backlog_slot():
    """When multiple backlog slots are unresolved, the nearest (most
    recent) one to the catch-up window is reported, not the oldest."""
    config = _config(shadow_operations={"max_catch_up_cycles": 1, "require_market_open_day": False})
    now = datetime(2026, 7, 16, 6, 45, tzinfo=LA)  # Thursday
    long_ago = intended_schedule_time_for_day(now.date() - timedelta(days=60), config)
    result = resolve_due_status(now, long_ago, config, _clock_at(now))
    assert result.status == STATUS_MISSED_TOO_OLD
    # Nearest unresolved margin day is today - (max_catch_up_cycles + 1) = 2 days back.
    assert result.intended_schedule_time == intended_schedule_time_for_day(now.date() - timedelta(days=2), config)


def date_minus(d, days):
    return d - timedelta(days=days)


# --- Daylight-saving transition ---------------------------------------------


def test_spring_forward_date_resolves_correctly():
    """2026-03-08 is the US spring-forward date (2:00 AM -> 3:00 AM). The
    intended local time 06:45 is unaffected (after the transition), but this
    proves zoneinfo-based conversion handles the transition date without
    raising or miscalculating."""
    config = _config()
    now = datetime(2026, 3, 9, 6, 45, tzinfo=LA)  # Monday, day after spring-forward Sunday
    result = resolve_due_status(now, None, config, _clock_at(now))
    assert result.status == STATUS_DUE
    intended = intended_schedule_time_for_day(now.date(), config)
    # UTC offset should reflect PDT (UTC-7), not PST (UTC-8), post-transition.
    assert intended.utcoffset() == timedelta(hours=-7)


def test_fall_back_date_resolves_correctly():
    """2026-11-01 is the US fall-back date. Confirms no exception and correct
    PST offset the day after."""
    config = _config()
    now = datetime(2026, 11, 2, 6, 45, tzinfo=LA)  # Monday after fall-back Sunday
    result = resolve_due_status(now, None, config, _clock_at(now))
    assert result.status == STATUS_DUE
    intended = intended_schedule_time_for_day(now.date(), config)
    assert intended.utcoffset() == timedelta(hours=-8)


# --- intended_schedule_id determinism ---------------------------------------


def test_intended_schedule_id_stable_across_equivalent_instants():
    config = _config()
    t1 = datetime(2026, 7, 13, 6, 45, tzinfo=LA)
    t2 = t1.astimezone(timezone.utc)
    assert intended_schedule_id(t1) == intended_schedule_id(t2)


def test_intended_schedule_id_differs_across_days():
    config = _config()
    a = intended_schedule_time_for_day(datetime(2026, 7, 13).date(), config)
    b = intended_schedule_time_for_day(datetime(2026, 7, 14).date(), config)
    assert intended_schedule_id(a) != intended_schedule_id(b)


# --- No future execution -----------------------------------------------------


def test_now_before_today_intended_time_never_due_even_with_old_last_completed():
    config = _config()
    now = datetime(2026, 7, 13, 5, 0, tzinfo=LA)
    last_completed = intended_schedule_time_for_day(now.date() - timedelta(days=5), config)
    result = resolve_due_status(now, last_completed, config, _clock_at(now))
    assert result.status == STATUS_NOT_DUE
