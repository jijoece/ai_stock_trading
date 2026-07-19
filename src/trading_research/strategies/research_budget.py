"""Token-budgeted research handoff decisions (Milestone 23, B6).

Deterministic, zero-LLM pre-filter that runs *before* a shortlisted
candidate is ever handed to `research/orchestration.py`. It does not
duplicate that module's own fresh-run reuse logic
(`compute_research_run_id` / `reused_existing_run`) — it only decides
whether a candidate is even eligible to be sent today. `REUSE_RESEARCH` vs
`RUN_FULL_RESEARCH` is recorded by the caller after invoking the existing
orchestrator, based on its `reused_existing_run` flag.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Callable

from ..storage.shadow_operations_repositories import (
    list_budget_reservations,
    load_budget_reservation,
    load_budget_reservation_by_idempotency_key,
)
from ..storage.transactions import transaction
from .config import ShortlistConfig
from .selector import ShortlistEntry


class ResearchDecision(str, Enum):
    SEND_TO_RESEARCH = "SEND_TO_RESEARCH"
    REUSE_RESEARCH = "REUSE_RESEARCH"
    RUN_FULL_RESEARCH = "RUN_FULL_RESEARCH"
    RUN_REDUCED_CONTEXT = "RUN_REDUCED_CONTEXT"  # defined for forward-compatibility; no trigger path yet
    SKIP_LOW_SIGNAL = "SKIP_LOW_SIGNAL"
    SKIP_DAILY_CANDIDATE_CAP = "SKIP_DAILY_CANDIDATE_CAP"
    SKIP_DAILY_TOKEN_CAP = "SKIP_DAILY_TOKEN_CAP"


@dataclass(frozen=True)
class DailyResearchBudgetState:
    """Caller-supplied daily counters; not persisted by this module."""

    symbols_sent_today: int
    fresh_cycles_today: int
    daily_token_budget: int | None = None
    tokens_spent_today: int = 0

    def token_budget_remaining(self) -> bool:
        if self.daily_token_budget is None:
            return True
        return self.tokens_spent_today < self.daily_token_budget


@dataclass(frozen=True)
class ResearchBudgetDecision:
    symbol: str
    decision: ResearchDecision
    reason: str


def decide_research_action(
    entry: ShortlistEntry,
    state: DailyResearchBudgetState,
    config: ShortlistConfig,
) -> ResearchBudgetDecision:
    if entry.best_signal_strength < config.minimum_signal_strength_for_research:
        return ResearchBudgetDecision(
            entry.symbol, ResearchDecision.SKIP_LOW_SIGNAL,
            f"signal_strength {entry.best_signal_strength} below "
            f"minimum_signal_strength_for_research {config.minimum_signal_strength_for_research}",
        )
    if state.symbols_sent_today >= config.maximum_symbols_to_research_per_day:
        return ResearchBudgetDecision(
            entry.symbol, ResearchDecision.SKIP_DAILY_CANDIDATE_CAP,
            f"symbols_sent_today {state.symbols_sent_today} >= "
            f"maximum_symbols_to_research_per_day {config.maximum_symbols_to_research_per_day}",
        )
    if state.fresh_cycles_today >= config.maximum_fresh_research_cycles_per_day:
        return ResearchBudgetDecision(
            entry.symbol, ResearchDecision.SKIP_DAILY_CANDIDATE_CAP,
            f"fresh_cycles_today {state.fresh_cycles_today} >= "
            f"maximum_fresh_research_cycles_per_day {config.maximum_fresh_research_cycles_per_day}",
        )
    if not state.token_budget_remaining():
        return ResearchBudgetDecision(
            entry.symbol, ResearchDecision.SKIP_DAILY_TOKEN_CAP,
            f"tokens_spent_today {state.tokens_spent_today} >= daily_token_budget {state.daily_token_budget}",
        )
    return ResearchBudgetDecision(entry.symbol, ResearchDecision.SEND_TO_RESEARCH, "within daily research budget")


def record_research_outcome(symbol: str, reused_existing_run: bool) -> ResearchBudgetDecision:
    """Called after invoking `research.orchestration.analyze_with_research_committee`
    for a `SEND_TO_RESEARCH` candidate, to persist the final decision label."""
    decision = ResearchDecision.REUSE_RESEARCH if reused_existing_run else ResearchDecision.RUN_FULL_RESEARCH
    reason = "orchestrator reused an existing research run" if reused_existing_run else "orchestrator ran a fresh research cycle"
    return ResearchBudgetDecision(symbol, decision, reason)


# --- Milestone 24 Part C5: persistent, atomic daily token-budget reservations ---
#
# Reuses the existing `shadow_budget_reservations` table (already persisted,
# already has `reserved_input_tokens`/`reserved_output_tokens` columns) rather
# than building a second reservation store — `shadow/budget.py` reserves
# against a dollar-cost cap on that same table; this reserves against a raw
# token cap, distinguished by an idempotency-key prefix. No schema migration
# is needed: the table's `status` column is free text with no CHECK
# constraint, so the extra RELEASED/AMBIGUOUS states this module needs are
# just additional string values.

RESEARCH_TOKEN_RESERVATION_PREFIX = "research_token_budget"

TOKEN_RESERVATION_RESERVED = "RESERVED"
TOKEN_RESERVATION_SETTLED = "SETTLED"
TOKEN_RESERVATION_RELEASED = "RELEASED"
TOKEN_RESERVATION_AMBIGUOUS = "AMBIGUOUS"

_LIVE_TOKEN_STATUSES = (TOKEN_RESERVATION_RESERVED, TOKEN_RESERVATION_SETTLED, TOKEN_RESERVATION_AMBIGUOUS)

Clock = Callable[[], datetime]


class TokenBudgetError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResearchTokenReservation:
    reservation_id: str
    idempotency_key: str
    reserved_input_tokens: int
    reserved_output_tokens: int
    status: str


@dataclass(frozen=True)
class TokenBudgetRejected:
    idempotency_key: str
    reason: str
    remaining_tokens: int
    requested_tokens: int


def _research_token_idempotency_key(
    *, research_run_id: str, symbol: str, provider: str, model_name: str | None, utc_date: date,
) -> str:
    """Milestone 24 Part C5's stable reservation key: research run ID +
    symbol + provider + model + UTC date. Reserving twice with the same
    inputs returns the existing reservation rather than double-reserving."""
    return (
        f"{RESEARCH_TOKEN_RESERVATION_PREFIX}:{research_run_id}:{symbol}:{provider}:"
        f"{model_name or 'none'}:{utc_date.isoformat()}"
    )


def _live_tokens_reserved_for_date(conn, utc_date: date) -> int:
    total = 0
    suffix = utc_date.isoformat()
    for row in list_budget_reservations(conn):
        key = row["idempotency_key"]
        if key.startswith(f"{RESEARCH_TOKEN_RESERVATION_PREFIX}:") and key.endswith(suffix):
            if row["status"] in _LIVE_TOKEN_STATUSES:
                total += row["reserved_input_tokens"] + row["reserved_output_tokens"]
    return total


def reserve_research_tokens(
    conn,
    *,
    research_run_id: str,
    symbol: str,
    provider: str,
    model_name: str | None,
    utc_date: date,
    estimated_input_tokens: int,
    maximum_output_tokens: int,
    maximum_reasoning_tokens: int,
    daily_token_cap: int,
    clock: Clock,
) -> ResearchTokenReservation | TokenBudgetRejected:
    """Atomically reserves a conservative worst-case token estimate for one
    research cycle against `daily_token_cap`, counting already-settled and
    still-live reservations for the same UTC date (never just settled
    usage) so two concurrent cycles cannot both see headroom. Idempotent:
    a duplicate call with the same `(research_run_id, symbol, provider,
    model_name, utc_date)` reuses the existing reservation instead of
    reserving twice."""
    key = _research_token_idempotency_key(
        research_run_id=research_run_id, symbol=symbol, provider=provider, model_name=model_name, utc_date=utc_date,
    )
    requested_output = maximum_output_tokens + maximum_reasoning_tokens
    requested_total = estimated_input_tokens + requested_output
    with transaction(conn):
        existing = load_budget_reservation_by_idempotency_key(conn, key)
        if existing is not None:
            return ResearchTokenReservation(
                reservation_id=existing["reservation_id"], idempotency_key=key,
                reserved_input_tokens=existing["reserved_input_tokens"],
                reserved_output_tokens=existing["reserved_output_tokens"], status=existing["status"],
            )
        live_reserved = _live_tokens_reserved_for_date(conn, utc_date)
        remaining = daily_token_cap - live_reserved
        if requested_total > remaining:
            return TokenBudgetRejected(
                idempotency_key=key, reason="daily token cap would be exceeded",
                remaining_tokens=remaining, requested_tokens=requested_total,
            )
        now = clock()
        reservation_id = f"resv-tokens-{uuid.uuid4().hex}"
        conn.execute(
            "INSERT INTO shadow_budget_reservations "
            "(reservation_id, idempotency_key, cycle_intent, reserved_estimated_cost_usd, reserved_input_tokens, "
            "reserved_output_tokens, reserved_latency_seconds, status, consumed_cost_usd, consumed_input_tokens, "
            "consumed_output_tokens, consumed_latency_seconds, emergency_margin_breached, created_at, settled_at) "
            "VALUES (?, ?, ?, '0', ?, ?, 0, ?, '0', 0, 0, 0, 0, ?, NULL)",
            (reservation_id, key, provider, estimated_input_tokens, requested_output,
             TOKEN_RESERVATION_RESERVED, now.isoformat()),
        )
        return ResearchTokenReservation(
            reservation_id=reservation_id, idempotency_key=key,
            reserved_input_tokens=estimated_input_tokens, reserved_output_tokens=requested_output,
            status=TOKEN_RESERVATION_RESERVED,
        )


def _transition_reservation(conn, reservation_id: str, *, from_status: str, to_status: str, extra_sql: str, extra_params: tuple) -> bool:
    reservation = load_budget_reservation(conn, reservation_id)
    if reservation is None:
        raise TokenBudgetError(f"no such token reservation {reservation_id!r}")
    if reservation["status"] != from_status:
        return False  # already transitioned — idempotent no-op, never a double-charge or double-release
    with transaction(conn):
        conn.execute(
            f"UPDATE shadow_budget_reservations SET status = ?{extra_sql} WHERE reservation_id = ?",
            (to_status, *extra_params, reservation_id),
        )
    return True


def settle_research_tokens(
    conn, reservation_id: str, *, actual_input_tokens: int, actual_output_tokens: int, clock: Clock,
) -> bool:
    """Settles a reservation to its actual usage, releasing the unused
    reserved portion — `reserved_input_tokens`/`reserved_output_tokens` are
    overwritten with the actual counts so future budget checks only count
    what was really consumed."""
    now = clock()
    return _transition_reservation(
        conn, reservation_id, from_status=TOKEN_RESERVATION_RESERVED, to_status=TOKEN_RESERVATION_SETTLED,
        extra_sql=", reserved_input_tokens = ?, reserved_output_tokens = ?, consumed_input_tokens = ?, "
                  "consumed_output_tokens = ?, settled_at = ?",
        extra_params=(actual_input_tokens, actual_output_tokens, actual_input_tokens, actual_output_tokens, now.isoformat()),
    )


def release_research_tokens(conn, reservation_id: str, clock: Clock) -> bool:
    """Releases a reservation that was never used (e.g. the cycle failed
    before any call was made)."""
    now = clock()
    return _transition_reservation(
        conn, reservation_id, from_status=TOKEN_RESERVATION_RESERVED, to_status=TOKEN_RESERVATION_RELEASED,
        extra_sql=", settled_at = ?", extra_params=(now.isoformat(),),
    )


def mark_research_tokens_ambiguous(conn, reservation_id: str) -> bool:
    """Marks a reservation AMBIGUOUS when the provider outcome is unclear —
    it stays counted against the daily cap (never blindly released or
    retried) until an operator reconciles it."""
    return _transition_reservation(
        conn, reservation_id, from_status=TOKEN_RESERVATION_RESERVED, to_status=TOKEN_RESERVATION_AMBIGUOUS,
        extra_sql="", extra_params=(),
    )
