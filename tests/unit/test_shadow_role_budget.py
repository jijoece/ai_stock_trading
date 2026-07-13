"""Tests for shadow/role_budget.py (docs/milestone-7.md Step 17)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.research.usage import PricingEntry
from trading_research.shadow.budget import CycleIntent, estimate_cycle_cost, reserve_budget
from trading_research.shadow.role_budget import (
    DECISION_PROCEED,
    DECISION_SKIPPED_BUDGET_EXHAUSTED,
    RoleBudgetDecision,
    RoleBudgetError,
    check_role_budget,
)
from trading_research.storage.database import connect

BASE_TIME = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)
ALLOWED_ROLES = ("bull", "bear", "manager")


def _clock_at(t: datetime):
    return lambda: t


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "shadow_role_budget_test.db")
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


def _reservation(conn, *, max_output_tokens_per_cycle=2000, max_input_tokens_per_cycle=2000, max_latency_seconds_per_cycle=120):
    # max_roles_per_symbol=1, max_attempts_per_role=1 so the reservation's
    # worst-case totals equal the per-cycle values 1:1, keeping this
    # fixture's arithmetic simple for the token/latency/cost-cap tests below.
    intent = CycleIntent(
        provider="anthropic", model_name="claude-sonnet-5", max_symbols_per_cycle=1, max_roles_per_symbol=1,
        max_attempts_per_role=1, max_output_tokens_per_cycle=max_output_tokens_per_cycle,
        max_input_tokens_per_cycle=max_input_tokens_per_cycle,
        max_latency_seconds_per_cycle=max_latency_seconds_per_cycle,
    )
    estimate = estimate_cycle_cost(intent, _pricing(), "2026-07-13")
    key = f"cycle-{id(conn)}-{max_output_tokens_per_cycle}-{max_input_tokens_per_cycle}-{max_latency_seconds_per_cycle}"
    return reserve_budget(
        conn, key, intent, estimate,
        max_actual_cost_per_day_usd=Decimal("1000"), max_actual_cost_per_month_usd=Decimal("10000"),
        clock=_clock_at(BASE_TIME),
    )


def _check(conn, reservation, role_name="bull", role_index=0, attempt_number=1, **overrides):
    defaults = dict(
        allowed_roles=ALLOWED_ROLES, max_roles_per_symbol=3, max_attempts_per_role=2,
        max_possible_output_tokens_for_role=500, max_possible_input_tokens_for_role=500,
        max_possible_latency_seconds_for_role=30, estimated_cost_per_output_token=Decimal("15") / Decimal(1_000_000),
        estimated_cost_per_input_token=Decimal("3") / Decimal(1_000_000), clock=_clock_at(BASE_TIME),
    )
    defaults.update(overrides)
    return check_role_budget(conn, reservation, role_name, role_index, attempt_number, **defaults)


# --- allowed role / role count / attempt count ------------------------------


def test_proceed_when_within_all_limits(conn):
    reservation = _reservation(conn)
    decision = _check(conn, reservation)
    assert decision.decision == DECISION_PROCEED
    assert decision.proceed is True


def test_disallowed_role_skipped(conn):
    reservation = _reservation(conn)
    decision = _check(conn, reservation, role_name="rogue_role")
    assert decision.decision == DECISION_SKIPPED_BUDGET_EXHAUSTED


def test_role_index_beyond_max_roles_skipped(conn):
    reservation = _reservation(conn)
    decision = _check(conn, reservation, role_index=5, max_roles_per_symbol=3)
    assert decision.decision == DECISION_SKIPPED_BUDGET_EXHAUSTED


def test_attempt_beyond_max_attempts_skipped(conn):
    reservation = _reservation(conn)
    decision = _check(conn, reservation, attempt_number=3, max_attempts_per_role=2)
    assert decision.decision == DECISION_SKIPPED_BUDGET_EXHAUSTED


# --- token / latency / cost budgets -----------------------------------------


def test_output_token_budget_exceeded_skips(conn):
    reservation = _reservation(conn, max_output_tokens_per_cycle=100)
    decision = _check(conn, reservation, max_possible_output_tokens_for_role=200)
    assert decision.decision == DECISION_SKIPPED_BUDGET_EXHAUSTED
    assert "output" in decision.reason


def test_input_token_budget_exceeded_skips(conn):
    reservation = _reservation(conn, max_input_tokens_per_cycle=100)
    decision = _check(conn, reservation, max_possible_input_tokens_for_role=200, max_possible_output_tokens_for_role=10)
    assert decision.decision == DECISION_SKIPPED_BUDGET_EXHAUSTED
    assert "input" in decision.reason


def test_latency_budget_exceeded_skips(conn):
    reservation = _reservation(conn, max_latency_seconds_per_cycle=10)
    decision = _check(conn, reservation, max_possible_latency_seconds_for_role=20)
    assert decision.decision == DECISION_SKIPPED_BUDGET_EXHAUSTED
    assert "latency" in decision.reason


def test_cost_budget_exceeded_skips_even_when_tokens_fit(conn):
    # Reserve a tiny cycle so the max-possible-cost of a role call would
    # breach the remaining reservation even though token/latency budgets
    # alone would allow it.
    intent = CycleIntent(
        provider="anthropic", model_name="claude-sonnet-5", max_symbols_per_cycle=1, max_roles_per_symbol=1,
        max_attempts_per_role=1, max_output_tokens_per_cycle=1, max_input_tokens_per_cycle=1,
        max_latency_seconds_per_cycle=60,
    )
    estimate = estimate_cycle_cost(intent, _pricing(), "2026-07-13")
    reservation = reserve_budget(
        conn, "cycle-tiny-cost", intent, estimate, max_actual_cost_per_day_usd=Decimal("1000"),
        max_actual_cost_per_month_usd=Decimal("10000"), clock=_clock_at(BASE_TIME),
    )
    decision = _check(
        conn, reservation, max_possible_output_tokens_for_role=1, max_possible_input_tokens_for_role=1,
        max_possible_latency_seconds_for_role=1,
        estimated_cost_per_output_token=Decimal("999999"), estimated_cost_per_input_token=Decimal("999999"),
    )
    assert decision.decision == DECISION_SKIPPED_BUDGET_EXHAUSTED
    assert "cost" in decision.reason


# --- worst-case, not actual ---------------------------------------------------


def test_uses_maximum_possible_not_actual_cost(conn):
    """A role configured with a large max-output-tokens must be skipped even
    though the real call, if it ran, might use far fewer tokens -- the gate
    only ever sees the worst-case configured maximum."""
    reservation = _reservation(conn, max_output_tokens_per_cycle=50)
    decision = _check(conn, reservation, max_possible_output_tokens_for_role=1000)
    assert decision.decision == DECISION_SKIPPED_BUDGET_EXHAUSTED


# --- distinct status, never conflated with provider failure -------------------


def test_skipped_budget_exhausted_is_a_distinct_type_from_proceed(conn):
    reservation = _reservation(conn, max_output_tokens_per_cycle=10)
    decision = _check(conn, reservation, max_possible_output_tokens_for_role=1000)
    assert isinstance(decision, RoleBudgetDecision)
    assert decision.decision != DECISION_PROCEED
    assert not hasattr(decision, "failure_code")
    assert not hasattr(decision, "provider_failure")


def test_role_budget_decision_rejects_unrecognized_decision_value():
    with pytest.raises(RoleBudgetError):
        RoleBudgetDecision(decision="SOME_OTHER_STATUS", role_name="bull")


# --- no new symbol allowed to start after exhaustion (sequenced roles) -----------


def test_sequenced_role_calls_stop_after_budget_exhausted(conn):
    reservation = _reservation(conn, max_output_tokens_per_cycle=600)
    first = _check(conn, reservation, role_name="bull", role_index=0, max_possible_output_tokens_for_role=500)
    assert first.decision == DECISION_PROCEED

    from trading_research.shadow.budget import record_actual_usage

    record_actual_usage(
        conn, reservation.reservation_id, actual_cost_usd=Decimal("0.001"), actual_input_tokens=100,
        actual_output_tokens=500, actual_latency_seconds=5, provider="anthropic", model_name="claude-sonnet-5",
        clock=_clock_at(BASE_TIME + timedelta(seconds=10)),
    )
    second = _check(conn, reservation, role_name="bear", role_index=1, max_possible_output_tokens_for_role=500)
    assert second.decision == DECISION_SKIPPED_BUDGET_EXHAUSTED
