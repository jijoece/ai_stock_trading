"""Unit tests for `shadow/attempt_controller.py::ShadowResearchAttemptController`
(docs/milestone-7.1.md Steps 13-15): role-budget check persistence, usage
recording idempotency, and the "never fabricate unavailable usage" boundary.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.research.models import UsageRecord
from trading_research.research.orchestration import AttemptControlRequest, ResearchAttemptRecord
from trading_research.research.usage import PricingEntry
from trading_research.shadow import budget as budget_mod
from trading_research.shadow.attempt_controller import ShadowResearchAttemptController
from trading_research.storage.database import connect
from trading_research.storage.shadow_operations_repositories import list_role_budget_checks

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "test.db")
        yield c
        c.close()


def _reservation(conn, *, reserved_output_tokens=10_000, reserved_input_tokens=100_000, reserved_latency=900):
    intent = budget_mod.CycleIntent(
        provider="anthropic", model_name="claude-test", max_symbols_per_cycle=1, max_roles_per_symbol=5,
        max_attempts_per_role=2, max_output_tokens_per_cycle=reserved_output_tokens // 5,
        max_input_tokens_per_cycle=reserved_input_tokens // 5, max_latency_seconds_per_cycle=reserved_latency // 5,
    )
    pricing = (PricingEntry(
        provider="anthropic", model="claude-test", effective_date="2026-01-01", currency="USD",
        input_price_per_million=Decimal("3"), output_price_per_million=Decimal("15"), pricing_version="v1",
    ),)
    estimate = budget_mod.estimate_cycle_cost(intent, pricing, NOW.date().isoformat())
    reservation = budget_mod.reserve_budget(
        conn, "test-key", intent, estimate, max_actual_cost_per_day_usd=Decimal("100"),
        max_actual_cost_per_month_usd=Decimal("1000"), clock=lambda: NOW,
    )
    return reservation, pricing[0]


def _controller(conn, reservation, pricing, **overrides):
    defaults = dict(
        conn=conn, reservation=reservation, provider="anthropic", allowed_roles=("fundamental", "manager"),
        max_roles_per_symbol=5, max_attempts_per_role=2, max_output_tokens_per_role=2000,
        max_input_tokens_per_role=20000, max_latency_seconds_per_role=180, pricing=pricing, clock=lambda: NOW,
        scheduler_run_id="sched-1", cycle_id="cycle-1",
    )
    defaults.update(overrides)
    return ShadowResearchAttemptController(**defaults)


def _request(role="fundamental", attempt_number=1):
    return AttemptControlRequest(
        research_run_id="run-1", symbol="AAPL", role=role, attempt_number=attempt_number, model_name="claude-test",
        prompt_version="v1", prompt_hash="hash1", max_input_tokens=None, max_output_tokens=2000, requested_at=NOW,
    )


def _attempt(*, attempt_id="run-1-fundamental-1", success=True, input_tokens=100, output_tokens=50, latency_ms=200, provider="anthropic", model_name="claude-test"):
    usage = UsageRecord(
        provider=provider, model_name=model_name, role="fundamental", input_tokens=input_tokens,
        output_tokens=output_tokens, cache_read_tokens=None, cache_write_tokens=None, latency_ms=latency_ms,
        provider_request_id="req-1", retry_count=0, success=success,
        pricing_version="v1" if input_tokens is not None else None,
        estimated_cost=Decimal("0.001") if input_tokens is not None else None,
        cost_status="CALCULATED" if input_tokens is not None else "USAGE_NOT_RETURNED",
    )
    return ResearchAttemptRecord(
        attempt_id=attempt_id, research_run_id="run-1", role="fundamental", attempt_number=1, prompt_name="p",
        prompt_version="v1", prompt_hash="h1", system_prompt_hash="sph1", schema_version="s1", provider=provider,
        model_name=model_name, success=success, failure_reason=None if success else "rejected",
        raw_response_json={}, validated_payload_json={} if success else None, usage=usage, created_at=NOW,
    )


def test_before_attempt_persists_a_role_budget_check_row(conn):
    reservation, pricing = _reservation(conn)
    controller = _controller(conn, reservation, pricing)
    decision = controller.before_attempt(_request())
    assert decision.allowed is True
    checks = list_role_budget_checks(conn, scheduler_run_id="sched-1")
    assert len(checks) == 1
    assert checks[0]["role"] == "fundamental"
    assert checks[0]["decision"] == "PROCEED"


def test_before_attempt_uses_the_supplied_pricing_entry_not_a_second_lookup(conn):
    reservation, pricing = _reservation(conn)
    controller = _controller(conn, reservation, pricing)
    controller.before_attempt(_request())
    checks = list_role_budget_checks(conn)
    expected_max_cost = (
        Decimal(2000) * (pricing.output_price_per_million / Decimal(1_000_000))
        + Decimal(20000) * (pricing.input_price_per_million / Decimal(1_000_000))
    )
    assert Decimal(checks[0]["maximum_attempt_cost_usd"]) == expected_max_cost


def test_disallowed_role_denied(conn):
    reservation, pricing = _reservation(conn)
    controller = _controller(conn, reservation, pricing, allowed_roles=("manager",))
    decision = controller.before_attempt(_request(role="fundamental"))
    assert decision.allowed is False
    assert decision.code == "SKIPPED_BUDGET_EXHAUSTED"


def test_check_id_deterministic_and_idempotent_on_resume(conn):
    reservation, pricing = _reservation(conn)
    controller = _controller(conn, reservation, pricing)
    controller.before_attempt(_request())
    controller.before_attempt(_request())  # identical request — same check_id, INSERT OR IGNORE
    checks = list_role_budget_checks(conn)
    assert len(checks) == 1


def test_after_attempt_records_actual_usage_for_successful_priced_attempt(conn):
    reservation, pricing = _reservation(conn)
    before = budget_mod.remaining_reservation_budget(conn, reservation.reservation_id)
    controller = _controller(conn, reservation, pricing)
    controller.after_attempt(_request(), _attempt())
    remaining = budget_mod.remaining_reservation_budget(conn, reservation.reservation_id)
    assert remaining["remaining_output_tokens"] == before["remaining_output_tokens"] - 50
    assert remaining["remaining_input_tokens"] == before["remaining_input_tokens"] - 100


def test_after_attempt_idempotent_on_attempt_id(conn):
    reservation, pricing = _reservation(conn)
    before = budget_mod.remaining_reservation_budget(conn, reservation.reservation_id)
    controller = _controller(conn, reservation, pricing)
    attempt = _attempt()
    controller.after_attempt(_request(), attempt)
    controller.after_attempt(_request(), attempt)  # duplicate — must not double-charge
    remaining = budget_mod.remaining_reservation_budget(conn, reservation.reservation_id)
    assert remaining["remaining_output_tokens"] == before["remaining_output_tokens"] - 50


def test_after_attempt_never_fabricates_usage_when_tokens_unavailable(conn):
    reservation, pricing = _reservation(conn)
    before = budget_mod.remaining_reservation_budget(conn, reservation.reservation_id)
    controller = _controller(conn, reservation, pricing)
    unavailable_attempt = _attempt(success=False, input_tokens=None, output_tokens=None, latency_ms=150)
    controller.after_attempt(_request(), unavailable_attempt)
    remaining = budget_mod.remaining_reservation_budget(conn, reservation.reservation_id)
    assert remaining["remaining_output_tokens"] == before["remaining_output_tokens"]  # untouched — no fabricated zero charge
    usage_rows = conn.execute("SELECT COUNT(*) AS c FROM shadow_budget_usage_attempts").fetchone()["c"]
    assert usage_rows == 0


def test_after_attempt_deterministic_provider_charges_zero_cost_when_tokens_present(conn):
    reservation, pricing = _reservation(conn)
    controller = _controller(conn, reservation, pricing)
    usage = UsageRecord(
        provider="deterministic", model_name="deterministic-v1", role="fundamental", input_tokens=0,
        output_tokens=0, cache_read_tokens=None, cache_write_tokens=None, latency_ms=5, provider_request_id=None,
        retry_count=0, success=True, pricing_version=None, estimated_cost=None, cost_status="NOT_APPLICABLE",
    )
    attempt = ResearchAttemptRecord(
        attempt_id="run-1-fundamental-1", research_run_id="run-1", role="fundamental", attempt_number=1,
        prompt_name="p", prompt_version="v1", prompt_hash="h1", system_prompt_hash="sph1", schema_version="s1",
        provider="deterministic", model_name="deterministic-v1", success=True, failure_reason=None,
        raw_response_json={}, validated_payload_json={}, usage=usage, created_at=NOW,
    )
    controller.after_attempt(_request(), attempt)
    usage_rows = conn.execute("SELECT actual_cost_usd FROM shadow_budget_usage_attempts a JOIN shadow_budget_usage u ON a.usage_id = u.usage_id").fetchall()
    assert len(usage_rows) == 1
    assert Decimal(usage_rows[0]["actual_cost_usd"]) == Decimal("0")
