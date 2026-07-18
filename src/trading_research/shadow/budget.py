"""Budget reservation and settlement (docs/milestone-7.md Step 16, ADR 0005
Decision 5). Flow: estimate worst-case cost -> reserve against remaining
daily/monthly caps (counting concurrent live reservations, not just settled
usage) -> run bounded work -> record actual usage from real provider
response metadata -> settle, releasing the unused portion.

Reuses `research/usage.py`'s pricing lookup (`load_pricing_config`,
`select_pricing`) rather than building a second pricing table. When pricing
is unavailable AND the configured research provider is `anthropic` (real
Claude), `estimate_cycle_cost` raises `PricingRequiredError` so the caller
blocks before any Claude call; `deterministic`/`scripted` providers are
exempt, matching Milestone 6's existing cost-tracking scope
(`research/cost_tracking.py`/`research/usage.py::build_usage_record`
already only prices real Claude usage).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from ..research.usage import PricingEntry, select_pricing
from ..storage.shadow_operations_repositories import (
    list_budget_reservations,
    list_budget_usage,
    load_budget_reservation,
    load_budget_reservation_by_idempotency_key,
    load_budget_usage_attempt,
    save_budget_usage_attempt,
)

RESERVATION_STATUS_RESERVED = "RESERVED"
RESERVATION_STATUS_SETTLED = "SETTLED"
RESERVATION_STATUS_EXPIRED = "EXPIRED"

REAL_CLAUDE_PROVIDER = "anthropic"  # backwards-compatible singular alias
REAL_CLAUDE_PROVIDERS = ("anthropic", "claude_code")
PRICING_EXEMPT_PROVIDERS = ("deterministic", "scripted")

Clock = Callable[[], datetime]


class BudgetConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class CycleIntent:
    """The about-to-run cycle's shape, used to bound the worst-case cost
    estimate. `provider` is the configured research provider
    (`deterministic` | `scripted` | `anthropic`) — mirrors
    `research/configuration.py::KNOWN_PROVIDERS`."""

    provider: str
    model_name: str | None
    max_symbols_per_cycle: int
    max_roles_per_symbol: int
    max_attempts_per_role: int
    max_output_tokens_per_cycle: int
    max_input_tokens_per_cycle: int
    max_latency_seconds_per_cycle: int
    max_calls_per_cycle: int | None = None


@dataclass(frozen=True)
class CostEstimate:
    provider: str
    max_input_tokens: int
    max_output_tokens: int
    max_latency_seconds: int
    estimated_cost_usd: Decimal | None
    pricing_version: str | None
    pricing_required: bool
    pricing_available: bool


@dataclass(frozen=True)
class ReservationHandle:
    reservation_id: str
    idempotency_key: str
    reserved_estimated_cost_usd: Decimal
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_latency_seconds: int
    status: str
    created_at: datetime


@dataclass(frozen=True)
class BudgetRejected:
    idempotency_key: str
    reason: str
    cap_name: str
    remaining: Decimal | int
    requested: Decimal | int


@dataclass(frozen=True)
class BudgetBreachReport:
    reservation_id: str
    breached: bool
    actual_over_estimate_fraction: Decimal | None


def estimate_cycle_cost(intent: CycleIntent, pricing_entries: tuple[PricingEntry, ...], as_of_date: str) -> CostEstimate:
    """Worst-case cost for one cycle, bounded by
    `max_symbols_per_cycle * max_roles_per_symbol * max_attempts_per_role *
    max_output_tokens_per_cycle` (docs/milestone-7.md Step 16 / ADR 0005
    Decision 5). Raises `BudgetConfigError` when pricing is required
    (real-Claude provider) but not configured — this is the structural block
    that prevents any scheduled real-Claude call from starting without
    versioned pricing."""
    max_calls = intent.max_calls_per_cycle or (
        intent.max_symbols_per_cycle * intent.max_roles_per_symbol * intent.max_attempts_per_role
    )
    if intent.max_calls_per_cycle is not None:
        max_output_tokens = intent.max_output_tokens_per_cycle
        max_input_tokens = intent.max_input_tokens_per_cycle
        max_latency_seconds = intent.max_latency_seconds_per_cycle
    else:
        max_output_tokens = max_calls * intent.max_output_tokens_per_cycle
        max_input_tokens = max_calls * intent.max_input_tokens_per_cycle
        max_latency_seconds = max_calls * intent.max_latency_seconds_per_cycle

    pricing_required = intent.provider in REAL_CLAUDE_PROVIDERS
    if not pricing_required:
        return CostEstimate(
            provider=intent.provider, max_input_tokens=max_input_tokens, max_output_tokens=max_output_tokens,
            max_latency_seconds=max_latency_seconds, estimated_cost_usd=None, pricing_version=None,
            pricing_required=False, pricing_available=True,
        )

    pricing = select_pricing(pricing_entries, intent.provider, intent.model_name or "", as_of_date)
    if pricing is None:
        raise BudgetConfigError(
            f"no pricing configured for provider={intent.provider!r} model={intent.model_name!r} "
            f"as_of={as_of_date!r} — recurring real-Claude operation is blocked (fails closed)"
        )

    estimated_cost = (Decimal(max_input_tokens) / Decimal(1_000_000)) * pricing.input_price_per_million + (
        Decimal(max_output_tokens) / Decimal(1_000_000)
    ) * pricing.output_price_per_million
    return CostEstimate(
        provider=intent.provider, max_input_tokens=max_input_tokens, max_output_tokens=max_output_tokens,
        max_latency_seconds=max_latency_seconds, estimated_cost_usd=estimated_cost,
        pricing_version=pricing.pricing_version, pricing_required=True, pricing_available=True,
    )


def _sum_live_reservations(conn) -> Decimal:
    """Concurrent (still-RESERVED) reservations count toward remaining
    budget, not just settled usage — prevents the race where two concurrent
    cycles both see "budget available" (docs/milestone-7.md Step 16)."""
    total = Decimal("0")
    for row in list_budget_reservations(conn, status=RESERVATION_STATUS_RESERVED):
        total += Decimal(row["reserved_estimated_cost_usd"])
    return total


def _sum_settled_usage(conn, date_prefix: str) -> Decimal:
    total = Decimal("0")
    for row in list_budget_usage(conn, usage_date_prefix=date_prefix):
        total += Decimal(row["actual_cost_usd"])
    return total


def reserve_budget(
    conn,
    idempotency_key: str,
    intent: CycleIntent,
    estimate: CostEstimate,
    *,
    max_actual_cost_per_day_usd: Decimal,
    max_actual_cost_per_month_usd: Decimal,
    clock: Clock,
) -> ReservationHandle | BudgetRejected:
    """Reserves `estimate`'s worst-case cost against remaining daily/monthly
    caps. Idempotent: reserving twice with the same `idempotency_key`
    returns the existing reservation rather than double-reserving."""
    existing = load_budget_reservation_by_idempotency_key(conn, idempotency_key)
    if existing is not None:
        return ReservationHandle(
            reservation_id=existing["reservation_id"], idempotency_key=idempotency_key,
            reserved_estimated_cost_usd=Decimal(existing["reserved_estimated_cost_usd"]),
            reserved_input_tokens=existing["reserved_input_tokens"],
            reserved_output_tokens=existing["reserved_output_tokens"],
            reserved_latency_seconds=existing["reserved_latency_seconds"], status=existing["status"],
            created_at=datetime.fromisoformat(existing["created_at"]),
        )

    now = clock()
    requested_cost = estimate.estimated_cost_usd if estimate.estimated_cost_usd is not None else Decimal("0")

    if estimate.pricing_required:
        today_prefix = now.date().isoformat()
        month_prefix = now.strftime("%Y-%m")

        live_reserved = _sum_live_reservations(conn)
        settled_today = _sum_settled_usage(conn, today_prefix)
        settled_month = _sum_settled_usage(conn, month_prefix)

        remaining_daily = max_actual_cost_per_day_usd - settled_today - live_reserved
        if requested_cost > remaining_daily:
            return BudgetRejected(
                idempotency_key=idempotency_key, reason="daily cost cap would be exceeded", cap_name="daily_cost",
                remaining=remaining_daily, requested=requested_cost,
            )

        remaining_monthly = max_actual_cost_per_month_usd - settled_month - live_reserved
        if requested_cost > remaining_monthly:
            return BudgetRejected(
                idempotency_key=idempotency_key, reason="monthly cost cap would be exceeded", cap_name="monthly_cost",
                remaining=remaining_monthly, requested=requested_cost,
            )

    reservation_id = f"resv-{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO shadow_budget_reservations "
        "(reservation_id, idempotency_key, cycle_intent, reserved_estimated_cost_usd, reserved_input_tokens, "
        "reserved_output_tokens, reserved_latency_seconds, status, consumed_cost_usd, consumed_input_tokens, "
        "consumed_output_tokens, consumed_latency_seconds, emergency_margin_breached, created_at, settled_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '0', 0, 0, 0, 0, ?, NULL)",
        (
            reservation_id, idempotency_key, intent.provider, str(requested_cost), estimate.max_input_tokens,
            estimate.max_output_tokens, estimate.max_latency_seconds, RESERVATION_STATUS_RESERVED, now.isoformat(),
        ),
    )
    conn.commit()
    return ReservationHandle(
        reservation_id=reservation_id, idempotency_key=idempotency_key, reserved_estimated_cost_usd=requested_cost,
        reserved_input_tokens=estimate.max_input_tokens, reserved_output_tokens=estimate.max_output_tokens,
        reserved_latency_seconds=estimate.max_latency_seconds, status=RESERVATION_STATUS_RESERVED, created_at=now,
    )


def record_actual_usage(
    conn,
    reservation_id: str,
    *,
    actual_cost_usd: Decimal,
    actual_input_tokens: int,
    actual_output_tokens: int,
    actual_latency_seconds: int,
    provider: str,
    model_name: str | None,
    clock: Clock,
) -> None:
    """Records actual usage supplied by the caller from real provider
    response metadata — never fabricated here (docs/milestone-7.md Step 16 /
    ADR 0005 Decision 5)."""
    reservation = load_budget_reservation(conn, reservation_id)
    if reservation is None:
        raise ValueError(f"no such reservation {reservation_id!r}")
    now = clock()
    usage_id = f"usage-{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO shadow_budget_usage "
        "(usage_id, reservation_id, usage_date, actual_cost_usd, actual_input_tokens, actual_output_tokens, "
        "actual_latency_seconds, provider, model_name, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            usage_id, reservation_id, now.date().isoformat(), str(actual_cost_usd), actual_input_tokens,
            actual_output_tokens, actual_latency_seconds, provider, model_name, now.isoformat(),
        ),
    )
    new_consumed_cost = Decimal(reservation["consumed_cost_usd"]) + actual_cost_usd
    conn.execute(
        "UPDATE shadow_budget_reservations SET consumed_cost_usd = ?, consumed_input_tokens = consumed_input_tokens + ?, "
        "consumed_output_tokens = consumed_output_tokens + ?, consumed_latency_seconds = consumed_latency_seconds + ? "
        "WHERE reservation_id = ?",
        (str(new_consumed_cost), actual_input_tokens, actual_output_tokens, actual_latency_seconds, reservation_id),
    )
    conn.commit()


def record_actual_usage_for_attempt(
    conn,
    reservation_id: str,
    attempt_id: str,
    *,
    actual_cost_usd: Decimal,
    actual_input_tokens: int,
    actual_output_tokens: int,
    actual_latency_seconds: int,
    provider: str,
    model_name: str | None,
    clock: Clock,
) -> bool:
    """Idempotent-on-`attempt_id` wrapper around `record_actual_usage`
    (docs/milestone-7.1.md Step 15). Checks
    `shadow_budget_usage_attempts` first — a second call for an
    already-charged `attempt_id` (a resumed cycle re-processing the same
    completed attempt, or a duplicate `after_attempt` hook invocation) is a
    pure no-op, never a double-charge against the reservation. Returns
    `True` when usage was newly recorded, `False` when it was already
    recorded (so callers can distinguish "charged now" from "already
    charged" without a second query)."""
    if load_budget_usage_attempt(conn, attempt_id) is not None:
        return False
    record_actual_usage(
        conn, reservation_id, actual_cost_usd=actual_cost_usd, actual_input_tokens=actual_input_tokens,
        actual_output_tokens=actual_output_tokens, actual_latency_seconds=actual_latency_seconds,
        provider=provider, model_name=model_name, clock=clock,
    )
    # `record_actual_usage` already inserted the `shadow_budget_usage` row and
    # committed; the newest row for this reservation is the one just inserted
    # (usage_id is a fresh uuid per call, not derivable from attempt_id alone,
    # so it is looked up rather than regenerated here).
    latest_usage = list_budget_usage(conn, reservation_id=reservation_id)[-1]
    now = clock()
    save_budget_usage_attempt(
        conn,
        {
            "attempt_id": attempt_id, "usage_id": latest_usage["usage_id"], "reservation_id": reservation_id,
            "recorded_at": now.isoformat(),
        },
    )
    return True


def check_emergency_margin_breach(conn, reservation_id: str, emergency_margin_fraction: Decimal) -> BudgetBreachReport:
    """Reports (does not act on) whether consumed cost has exceeded the
    reserved estimate by more than `emergency_margin_fraction`. The caller
    (later work) decides whether to trigger a pause — this module only
    reports the breach fact, per ADR 0005 Decision 7's "detect vs act"
    separation."""
    reservation = load_budget_reservation(conn, reservation_id)
    if reservation is None:
        raise ValueError(f"no such reservation {reservation_id!r}")
    reserved = Decimal(reservation["reserved_estimated_cost_usd"])
    consumed = Decimal(reservation["consumed_cost_usd"])
    if reserved <= 0:
        breached = consumed > 0
        fraction = None
    else:
        allowed = reserved * (Decimal("1") + emergency_margin_fraction)
        breached = consumed > allowed
        fraction = (consumed - reserved) / reserved
    if breached:
        conn.execute(
            "UPDATE shadow_budget_reservations SET emergency_margin_breached = 1 WHERE reservation_id = ?",
            (reservation_id,),
        )
        conn.commit()
    return BudgetBreachReport(reservation_id=reservation_id, breached=breached, actual_over_estimate_fraction=fraction)


def settle_reservation(conn, reservation_id: str, clock: Clock) -> None:
    """Releases the unused portion of a reservation — marks it SETTLED.
    Idempotent: settling an already-settled reservation is a no-op."""
    reservation = load_budget_reservation(conn, reservation_id)
    if reservation is None:
        raise ValueError(f"no such reservation {reservation_id!r}")
    if reservation["status"] == RESERVATION_STATUS_SETTLED:
        return
    now = clock()
    conn.execute(
        "UPDATE shadow_budget_reservations SET status = ?, settled_at = ? WHERE reservation_id = ?",
        (RESERVATION_STATUS_SETTLED, now.isoformat(), reservation_id),
    )
    conn.commit()


def expire_abandoned_reservations(conn, clock: Clock, max_age_seconds: int) -> list[str]:
    """Sweep for reservations that were never settled (crash recovery).
    Returns the list of reservation_ids that were expired."""
    now = clock()
    expired_ids: list[str] = []
    for row in list_budget_reservations(conn, status=RESERVATION_STATUS_RESERVED):
        created_at = datetime.fromisoformat(row["created_at"])
        age_seconds = (now - created_at).total_seconds()
        if age_seconds > max_age_seconds:
            conn.execute(
                "UPDATE shadow_budget_reservations SET status = ?, settled_at = ? WHERE reservation_id = ?",
                (RESERVATION_STATUS_EXPIRED, now.isoformat(), row["reservation_id"]),
            )
            expired_ids.append(row["reservation_id"])
    if expired_ids:
        conn.commit()
    return expired_ids


def remaining_reservation_budget(conn, reservation_id: str) -> dict:
    """Convenience accessor for `shadow/role_budget.py`: remaining
    input/output tokens, latency, and cost against a live reservation."""
    reservation = load_budget_reservation(conn, reservation_id)
    if reservation is None:
        raise ValueError(f"no such reservation {reservation_id!r}")
    return {
        "remaining_input_tokens": reservation["reserved_input_tokens"] - reservation["consumed_input_tokens"],
        "remaining_output_tokens": reservation["reserved_output_tokens"] - reservation["consumed_output_tokens"],
        "remaining_latency_seconds": reservation["reserved_latency_seconds"] - reservation["consumed_latency_seconds"],
        "remaining_cost_usd": Decimal(reservation["reserved_estimated_cost_usd"]) - Decimal(reservation["consumed_cost_usd"]),
    }
