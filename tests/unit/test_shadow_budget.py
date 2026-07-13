"""Tests for shadow/budget.py (docs/milestone-7.md Step 16, Step 27 section I)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.research.usage import PricingEntry
from trading_research.shadow.budget import (
    BudgetConfigError,
    BudgetRejected,
    CycleIntent,
    ReservationHandle,
    check_emergency_margin_breach,
    estimate_cycle_cost,
    expire_abandoned_reservations,
    record_actual_usage,
    remaining_reservation_budget,
    reserve_budget,
    settle_reservation,
)
from trading_research.storage.database import connect

BASE_TIME = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)


def _clock_at(t: datetime):
    return lambda: t


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "shadow_budget_test.db")
        yield c
        c.close()


def _pricing() -> tuple[PricingEntry, ...]:
    return (
        PricingEntry(
            provider="anthropic", model="claude-sonnet-5", effective_date="2026-01-01", currency="USD",
            input_price_per_million=Decimal("3.00"), output_price_per_million=Decimal("15.00"),
            pricing_version="v1",
        ),
    )


def _deterministic_intent(**overrides) -> CycleIntent:
    defaults = dict(
        provider="deterministic", model_name=None, max_symbols_per_cycle=2, max_roles_per_symbol=2,
        max_attempts_per_role=1, max_output_tokens_per_cycle=1000, max_input_tokens_per_cycle=2000,
        max_latency_seconds_per_cycle=60,
    )
    defaults.update(overrides)
    return CycleIntent(**defaults)


def _claude_intent(**overrides) -> CycleIntent:
    defaults = dict(
        provider="anthropic", model_name="claude-sonnet-5", max_symbols_per_cycle=2, max_roles_per_symbol=2,
        max_attempts_per_role=1, max_output_tokens_per_cycle=1000, max_input_tokens_per_cycle=2000,
        max_latency_seconds_per_cycle=60,
    )
    defaults.update(overrides)
    return CycleIntent(**defaults)


# --- estimate: pricing missing blocks real-Claude, not deterministic --------


def test_estimate_deterministic_provider_exempt_from_pricing():
    estimate = estimate_cycle_cost(_deterministic_intent(), (), "2026-07-13")
    assert estimate.pricing_required is False
    assert estimate.estimated_cost_usd is None


def test_estimate_scripted_provider_exempt_from_pricing():
    estimate = estimate_cycle_cost(_deterministic_intent(provider="scripted"), (), "2026-07-13")
    assert estimate.pricing_required is False


def test_estimate_anthropic_provider_with_pricing_succeeds():
    estimate = estimate_cycle_cost(_claude_intent(), _pricing(), "2026-07-13")
    assert estimate.pricing_required is True
    assert estimate.estimated_cost_usd is not None
    assert estimate.estimated_cost_usd > 0


def test_estimate_anthropic_provider_without_pricing_blocks():
    with pytest.raises(BudgetConfigError):
        estimate_cycle_cost(_claude_intent(), (), "2026-07-13")


def test_estimate_bounded_by_symbols_roles_attempts_output_tokens():
    intent = _claude_intent(max_symbols_per_cycle=3, max_roles_per_symbol=4, max_attempts_per_role=2, max_output_tokens_per_cycle=100)
    estimate = estimate_cycle_cost(intent, _pricing(), "2026-07-13")
    # 3 * 4 * 2 * 100 = 2400 max output tokens.
    assert estimate.max_output_tokens == 2400


# --- reservation ---------------------------------------------------------------


def test_reserve_budget_succeeds_for_deterministic_provider(conn):
    intent = _deterministic_intent()
    estimate = estimate_cycle_cost(intent, (), "2026-07-13")
    result = reserve_budget(
        conn, "cycle-key-1", intent, estimate,
        max_actual_cost_per_day_usd=Decimal("10"), max_actual_cost_per_month_usd=Decimal("100"),
        clock=_clock_at(BASE_TIME),
    )
    assert isinstance(result, ReservationHandle)
    assert result.status == "RESERVED"


def test_duplicate_reservation_idempotent(conn):
    intent = _deterministic_intent()
    estimate = estimate_cycle_cost(intent, (), "2026-07-13")
    first = reserve_budget(
        conn, "cycle-key-dup", intent, estimate, max_actual_cost_per_day_usd=Decimal("10"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME),
    )
    second = reserve_budget(
        conn, "cycle-key-dup", intent, estimate, max_actual_cost_per_day_usd=Decimal("10"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME + timedelta(seconds=5)),
    )
    assert first.reservation_id == second.reservation_id
    rows = conn.execute("SELECT COUNT(*) AS n FROM shadow_budget_reservations").fetchone()
    assert rows["n"] == 1


# --- concurrent reservation counting --------------------------------------------


def test_concurrent_reservations_counted_toward_cap(conn):
    intent = _claude_intent(
        max_symbols_per_cycle=1, max_roles_per_symbol=1, max_attempts_per_role=1,
        max_output_tokens_per_cycle=100000, max_input_tokens_per_cycle=200000,
    )
    estimate = estimate_cycle_cost(intent, _pricing(), "2026-07-13")
    # estimate cost = (200000/1e6)*3 + (100000/1e6)*15 = 0.6 + 1.5 = 2.1
    assert estimate.estimated_cost_usd == Decimal("2.1")

    first = reserve_budget(
        conn, "cycle-a", intent, estimate, max_actual_cost_per_day_usd=Decimal("3"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME),
    )
    assert isinstance(first, ReservationHandle)

    # Second concurrent reservation (still-live, unsettled) must be counted
    # against the same daily cap -- 2.1 + 2.1 = 4.2 > 3.0 daily cap.
    second = reserve_budget(
        conn, "cycle-b", intent, estimate, max_actual_cost_per_day_usd=Decimal("3"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME),
    )
    assert isinstance(second, BudgetRejected)
    assert second.cap_name == "daily_cost"


# --- daily cap -------------------------------------------------------------------


def test_daily_cap_rejects_when_exceeded(conn):
    intent = _claude_intent(max_symbols_per_cycle=1, max_roles_per_symbol=1, max_attempts_per_role=1, max_output_tokens_per_cycle=100000)
    estimate = estimate_cycle_cost(intent, _pricing(), "2026-07-13")
    result = reserve_budget(
        conn, "cycle-daily", intent, estimate, max_actual_cost_per_day_usd=Decimal("1.0"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME),
    )
    assert isinstance(result, BudgetRejected)
    assert result.cap_name == "daily_cost"


# --- monthly cap -------------------------------------------------------------------


def test_monthly_cap_rejects_when_exceeded(conn):
    intent = _claude_intent(max_symbols_per_cycle=1, max_roles_per_symbol=1, max_attempts_per_role=1, max_output_tokens_per_cycle=100000)
    estimate = estimate_cycle_cost(intent, _pricing(), "2026-07-13")
    result = reserve_budget(
        conn, "cycle-monthly", intent, estimate, max_actual_cost_per_day_usd=Decimal("100"),
        max_actual_cost_per_month_usd=Decimal("1.0"), clock=_clock_at(BASE_TIME),
    )
    assert isinstance(result, BudgetRejected)
    assert result.cap_name == "monthly_cost"


# --- token cap / output cap (bounded via estimate) -------------------------------


def test_reservation_records_token_caps(conn):
    intent = _claude_intent(max_symbols_per_cycle=1, max_roles_per_symbol=1, max_attempts_per_role=1, max_output_tokens_per_cycle=500, max_input_tokens_per_cycle=700)
    estimate = estimate_cycle_cost(intent, _pricing(), "2026-07-13")
    result = reserve_budget(
        conn, "cycle-tokens", intent, estimate, max_actual_cost_per_day_usd=Decimal("100"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME),
    )
    assert result.reserved_output_tokens == 500
    assert result.reserved_input_tokens == 700


# --- cost cap: pricing missing -----------------------------------------------------


def test_pricing_missing_blocks_estimate_for_anthropic_before_reservation():
    with pytest.raises(BudgetConfigError):
        estimate_cycle_cost(_claude_intent(), (), "2026-07-13")


def test_pricing_missing_does_not_block_deterministic_reservation(conn):
    intent = _deterministic_intent()
    estimate = estimate_cycle_cost(intent, (), "2026-07-13")
    result = reserve_budget(
        conn, "cycle-det", intent, estimate, max_actual_cost_per_day_usd=Decimal("10"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME),
    )
    assert isinstance(result, ReservationHandle)


# --- partial settlement ------------------------------------------------------------


def test_record_actual_usage_and_settle_releases_unused_portion(conn):
    intent = _claude_intent(max_symbols_per_cycle=1, max_roles_per_symbol=1, max_attempts_per_role=1, max_output_tokens_per_cycle=1000, max_input_tokens_per_cycle=1000)
    estimate = estimate_cycle_cost(intent, _pricing(), "2026-07-13")
    reservation = reserve_budget(
        conn, "cycle-settle", intent, estimate, max_actual_cost_per_day_usd=Decimal("100"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME),
    )
    record_actual_usage(
        conn, reservation.reservation_id, actual_cost_usd=Decimal("0.01"), actual_input_tokens=100,
        actual_output_tokens=50, actual_latency_seconds=5, provider="anthropic", model_name="claude-sonnet-5",
        clock=_clock_at(BASE_TIME + timedelta(seconds=30)),
    )
    settle_reservation(conn, reservation.reservation_id, clock=_clock_at(BASE_TIME + timedelta(seconds=31)))
    row = conn.execute(
        "SELECT * FROM shadow_budget_reservations WHERE reservation_id = ?", (reservation.reservation_id,)
    ).fetchone()
    assert row["status"] == "SETTLED"
    assert Decimal(row["consumed_cost_usd"]) == Decimal("0.01")
    remaining = remaining_reservation_budget(conn, reservation.reservation_id)
    assert remaining["remaining_output_tokens"] == 950


def test_settle_is_idempotent(conn):
    intent = _deterministic_intent()
    estimate = estimate_cycle_cost(intent, (), "2026-07-13")
    reservation = reserve_budget(
        conn, "cycle-settle-2x", intent, estimate, max_actual_cost_per_day_usd=Decimal("10"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME),
    )
    settle_reservation(conn, reservation.reservation_id, clock=_clock_at(BASE_TIME))
    settle_reservation(conn, reservation.reservation_id, clock=_clock_at(BASE_TIME + timedelta(seconds=5)))
    row = conn.execute(
        "SELECT * FROM shadow_budget_reservations WHERE reservation_id = ?", (reservation.reservation_id,)
    ).fetchone()
    assert row["status"] == "SETTLED"


# --- abandoned reservation expiry ---------------------------------------------------


def test_expire_abandoned_reservations_sweeps_old_reserved_rows(conn):
    intent = _deterministic_intent()
    estimate = estimate_cycle_cost(intent, (), "2026-07-13")
    reservation = reserve_budget(
        conn, "cycle-abandoned", intent, estimate, max_actual_cost_per_day_usd=Decimal("10"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME),
    )
    much_later = BASE_TIME + timedelta(hours=6)
    expired_ids = expire_abandoned_reservations(conn, clock=_clock_at(much_later), max_age_seconds=3600)
    assert reservation.reservation_id in expired_ids
    row = conn.execute(
        "SELECT * FROM shadow_budget_reservations WHERE reservation_id = ?", (reservation.reservation_id,)
    ).fetchone()
    assert row["status"] == "EXPIRED"


def test_expire_abandoned_reservations_does_not_touch_recent_ones(conn):
    intent = _deterministic_intent()
    estimate = estimate_cycle_cost(intent, (), "2026-07-13")
    reservation = reserve_budget(
        conn, "cycle-fresh", intent, estimate, max_actual_cost_per_day_usd=Decimal("10"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME),
    )
    shortly_after = BASE_TIME + timedelta(seconds=10)
    expired_ids = expire_abandoned_reservations(conn, clock=_clock_at(shortly_after), max_age_seconds=3600)
    assert reservation.reservation_id not in expired_ids


# --- emergency-margin breach detection ------------------------------------------------


def test_emergency_margin_breach_detected_when_exceeded(conn):
    intent = _claude_intent(max_symbols_per_cycle=1, max_roles_per_symbol=1, max_attempts_per_role=1, max_output_tokens_per_cycle=1000, max_input_tokens_per_cycle=1000)
    estimate = estimate_cycle_cost(intent, _pricing(), "2026-07-13")
    reservation = reserve_budget(
        conn, "cycle-margin", intent, estimate, max_actual_cost_per_day_usd=Decimal("100"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME),
    )
    # Consume far more than reserved + 10% margin.
    huge_cost = reservation.reserved_estimated_cost_usd * Decimal("2")
    record_actual_usage(
        conn, reservation.reservation_id, actual_cost_usd=huge_cost, actual_input_tokens=100,
        actual_output_tokens=100, actual_latency_seconds=5, provider="anthropic", model_name="claude-sonnet-5",
        clock=_clock_at(BASE_TIME + timedelta(seconds=30)),
    )
    report = check_emergency_margin_breach(conn, reservation.reservation_id, Decimal("0.10"))
    assert report.breached is True


def test_emergency_margin_not_breached_within_bound(conn):
    intent = _claude_intent(max_symbols_per_cycle=1, max_roles_per_symbol=1, max_attempts_per_role=1, max_output_tokens_per_cycle=1000, max_input_tokens_per_cycle=1000)
    estimate = estimate_cycle_cost(intent, _pricing(), "2026-07-13")
    reservation = reserve_budget(
        conn, "cycle-margin-ok", intent, estimate, max_actual_cost_per_day_usd=Decimal("100"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME),
    )
    slightly_over = reservation.reserved_estimated_cost_usd * Decimal("1.05")
    record_actual_usage(
        conn, reservation.reservation_id, actual_cost_usd=slightly_over, actual_input_tokens=100,
        actual_output_tokens=100, actual_latency_seconds=5, provider="anthropic", model_name="claude-sonnet-5",
        clock=_clock_at(BASE_TIME + timedelta(seconds=30)),
    )
    report = check_emergency_margin_breach(conn, reservation.reservation_id, Decimal("0.10"))
    assert report.breached is False


# --- no new symbol allowed to start after exhaustion (reservation-remaining level) ---


def test_no_further_spend_allowed_after_output_token_budget_exhausted(conn):
    intent = _claude_intent(max_symbols_per_cycle=1, max_roles_per_symbol=1, max_attempts_per_role=1, max_output_tokens_per_cycle=100, max_input_tokens_per_cycle=100)
    estimate = estimate_cycle_cost(intent, _pricing(), "2026-07-13")
    reservation = reserve_budget(
        conn, "cycle-exhaust", intent, estimate, max_actual_cost_per_day_usd=Decimal("100"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(BASE_TIME),
    )
    record_actual_usage(
        conn, reservation.reservation_id, actual_cost_usd=Decimal("0.001"), actual_input_tokens=100,
        actual_output_tokens=100, actual_latency_seconds=1, provider="anthropic", model_name="claude-sonnet-5",
        clock=_clock_at(BASE_TIME + timedelta(seconds=1)),
    )
    remaining = remaining_reservation_budget(conn, reservation.reservation_id)
    assert remaining["remaining_output_tokens"] == 0
    assert remaining["remaining_input_tokens"] == 0
