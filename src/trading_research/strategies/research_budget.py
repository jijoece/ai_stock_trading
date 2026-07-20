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
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Callable

from ..storage.shadow_operations_repositories import (
    list_budget_reservations,
    load_budget_reservation,
    load_budget_reservation_by_idempotency_key,
    insert_token_budget_reconciliation,
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
# Reuses the existing `shadow_budget_reservations` table, but uses an explicit
# reservation kind and separate reasoning-token columns.  Cost reservations
# remain unchanged and token-only rows cannot be mistaken for cost holds.

RESEARCH_TOKEN_RESERVATION_PREFIX = "research_token_budget"
RESEARCH_TOKEN_RESERVATION_KIND = "RESEARCH_TOKENS"

# Persisted wire values are repeated here deliberately: strategies/ is a
# deterministic zero-LLM package and may not import the research/provider
# package merely to share string constants.
TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT = "REASONING_INCLUDED_IN_OUTPUT"
TOKEN_ACCOUNTING_REASONING_SEPARATE = "REASONING_SEPARATE"
TOKEN_ACCOUNTING_NOT_APPLICABLE = "NOT_APPLICABLE"
TOKEN_ACCOUNTING_POLICIES = (
    TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT,
    TOKEN_ACCOUNTING_REASONING_SEPARATE,
    TOKEN_ACCOUNTING_NOT_APPLICABLE,
)

TOKEN_RESERVATION_RESERVED = "RESERVED"
TOKEN_RESERVATION_SETTLED = "SETTLED"
TOKEN_RESERVATION_RELEASED = "RELEASED"
TOKEN_RESERVATION_AMBIGUOUS = "AMBIGUOUS"

_LIVE_TOKEN_STATUSES = (TOKEN_RESERVATION_RESERVED, TOKEN_RESERVATION_SETTLED, TOKEN_RESERVATION_AMBIGUOUS)

Clock = Callable[[], datetime]


class TokenBudgetError(RuntimeError):
    def __init__(self, message: str, *, code: str = "TOKEN_BUDGET_ERROR") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class ResearchTokenReservation:
    reservation_id: str
    idempotency_key: str
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_reasoning_tokens: int
    token_accounting_policy: str
    status: str


@dataclass(frozen=True)
class TokenBudgetRejected:
    idempotency_key: str
    reason: str
    remaining_tokens: int
    requested_tokens: int
    code: str = "BUDGET_EXHAUSTED"


def _research_token_idempotency_key(
    *, research_run_id: str, symbol: str, provider: str, model_name: str | None,
    utc_date: date, research_attempt_identity: str,
) -> str:
    """Stable identity for one provider attempt on one UTC budget date."""
    return (
        f"{RESEARCH_TOKEN_RESERVATION_PREFIX}:{research_run_id}:{symbol}:{provider}:"
        f"{model_name or 'none'}:{research_attempt_identity}:{utc_date.isoformat()}"
    )


def _require_nonnegative_int(value: int, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise TokenBudgetError(f"{field_name} must be an integer >= 0")
    return value


def _require_policy(policy: str) -> str:
    if policy not in TOKEN_ACCOUNTING_POLICIES:
        raise TokenBudgetError(
            f"token_accounting_policy {policy!r} is not one of {TOKEN_ACCOUNTING_POLICIES}",
            code="TOKEN_ACCOUNTING_POLICY_MISMATCH",
        )
    return policy


def _counted_tokens(input_tokens: int, output_tokens: int, reasoning_tokens: int, policy: str) -> int:
    return input_tokens + output_tokens + (
        reasoning_tokens if policy == TOKEN_ACCOUNTING_REASONING_SEPARATE else 0
    )


def _reservation_counted_tokens(row: dict) -> int:
    policy = row.get("token_accounting_policy") or TOKEN_ACCOUNTING_NOT_APPLICABLE
    if row["status"] == TOKEN_RESERVATION_SETTLED:
        return _counted_tokens(
            row["consumed_input_tokens"], row["consumed_output_tokens"],
            row.get("consumed_reasoning_tokens") or 0, policy,
        )
    return _counted_tokens(
        row["reserved_input_tokens"], row["reserved_output_tokens"],
        row.get("reserved_reasoning_tokens") or 0, policy,
    )


def _live_tokens_reserved_for_date(conn, utc_date: date) -> int:
    total = 0
    for row in list_budget_reservations(conn):
        if (
            row.get("reservation_kind") == RESEARCH_TOKEN_RESERVATION_KIND
            and row.get("budget_date") == utc_date.isoformat()
            and row["status"] in _LIVE_TOKEN_STATUSES
        ):
            total += _reservation_counted_tokens(row)
    return total


def _date_has_budget_breach(conn, utc_date: date) -> bool:
    return any(
        row.get("reservation_kind") == RESEARCH_TOKEN_RESERVATION_KIND
        and row.get("budget_date") == utc_date.isoformat()
        and bool(row["emergency_margin_breached"])
        for row in list_budget_reservations(conn)
    )


def _reservation_from_row(row: dict) -> ResearchTokenReservation:
    return ResearchTokenReservation(
        reservation_id=row["reservation_id"], idempotency_key=row["idempotency_key"],
        reserved_input_tokens=row["reserved_input_tokens"],
        reserved_output_tokens=row["reserved_output_tokens"],
        reserved_reasoning_tokens=row.get("reserved_reasoning_tokens") or 0,
        token_accounting_policy=row.get("token_accounting_policy") or TOKEN_ACCOUNTING_NOT_APPLICABLE,
        status=row["status"],
    )


def reserve_research_tokens(
    conn,
    *,
    research_run_id: str,
    symbol: str,
    provider: str,
    model_name: str | None,
    estimated_input_tokens: int,
    maximum_output_tokens: int,
    maximum_reasoning_tokens: int,
    daily_token_cap: int,
    clock: Clock,
    token_accounting_policy: str = TOKEN_ACCOUNTING_NOT_APPLICABLE,
    research_attempt_identity: str = "attempt-1",
    utc_date: date | None = None,
) -> ResearchTokenReservation | TokenBudgetRejected:
    """Atomically reserves a conservative worst-case token estimate for one
    research cycle against `daily_token_cap`, counting already-settled and
    still-live reservations for the same UTC date (never just settled
    usage) so two concurrent cycles cannot both see headroom. Idempotent:
    a duplicate call with the same attempt identity reuses the existing reservation instead of
    reserving twice."""
    estimated_input_tokens = _require_nonnegative_int(estimated_input_tokens, "estimated_input_tokens")
    maximum_output_tokens = _require_nonnegative_int(maximum_output_tokens, "maximum_output_tokens")
    maximum_reasoning_tokens = _require_nonnegative_int(maximum_reasoning_tokens, "maximum_reasoning_tokens")
    daily_token_cap = _require_nonnegative_int(daily_token_cap, "daily_token_cap")
    policy = _require_policy(token_accounting_policy)
    if not research_attempt_identity:
        raise TokenBudgetError("research_attempt_identity must be non-empty")
    now = clock()
    if now.tzinfo is None:
        raise TokenBudgetError("clock must return a timezone-aware datetime")
    budget_date = now.astimezone(timezone.utc).date()
    if utc_date is not None and utc_date != budget_date:
        raise TokenBudgetError(
            f"caller-supplied utc_date {utc_date} does not match clock-derived date {budget_date}",
            code="TOKEN_BUDGET_DATE_MISMATCH",
        )
    key = _research_token_idempotency_key(
        research_run_id=research_run_id, symbol=symbol, provider=provider, model_name=model_name,
        utc_date=budget_date, research_attempt_identity=research_attempt_identity,
    )
    requested_total = _counted_tokens(
        estimated_input_tokens, maximum_output_tokens, maximum_reasoning_tokens, policy,
    )
    with transaction(conn):
        existing = load_budget_reservation_by_idempotency_key(conn, key)
        if existing is not None:
            if (
                existing.get("reservation_kind") != RESEARCH_TOKEN_RESERVATION_KIND
                or (existing.get("token_accounting_policy") or TOKEN_ACCOUNTING_NOT_APPLICABLE) != policy
            ):
                raise TokenBudgetError(
                    "idempotency key exists with incompatible reservation metadata",
                    code="TOKEN_ACCOUNTING_POLICY_MISMATCH",
                )
            return _reservation_from_row(existing)
        attempt_prefix = (
            f"{RESEARCH_TOKEN_RESERVATION_PREFIX}:{research_run_id}:{symbol}:{provider}:"
            f"{model_name or 'none'}:"
        )
        unresolved = next((
            row for row in list_budget_reservations(conn)
            if row.get("reservation_kind") == RESEARCH_TOKEN_RESERVATION_KIND
            and row.get("budget_date") == budget_date.isoformat()
            and row["status"] == TOKEN_RESERVATION_AMBIGUOUS
            and row["idempotency_key"].startswith(attempt_prefix)
        ), None)
        if unresolved is not None:
            return TokenBudgetRejected(
                idempotency_key=key,
                reason="ambiguous token reservation requires operator reconciliation",
                remaining_tokens=0, requested_tokens=requested_total,
                code="TOKEN_RESERVATION_RECONCILIATION_REQUIRED",
            )
        if _date_has_budget_breach(conn, budget_date):
            return TokenBudgetRejected(
                idempotency_key=key, reason="daily token budget is blocked by a prior usage breach",
                remaining_tokens=0, requested_tokens=requested_total,
            )
        live_reserved = _live_tokens_reserved_for_date(conn, budget_date)
        remaining = daily_token_cap - live_reserved
        if requested_total > remaining:
            return TokenBudgetRejected(
                idempotency_key=key, reason="daily token cap would be exceeded",
                remaining_tokens=remaining, requested_tokens=requested_total,
            )
        reservation_id = f"resv-tokens-{uuid.uuid4().hex}"
        conn.execute(
            "INSERT INTO shadow_budget_reservations "
            "(reservation_id, idempotency_key, cycle_intent, reserved_estimated_cost_usd, reserved_input_tokens, "
            "reserved_output_tokens, reserved_reasoning_tokens, reserved_latency_seconds, status, "
            "consumed_cost_usd, consumed_input_tokens, consumed_output_tokens, consumed_reasoning_tokens, "
            "consumed_latency_seconds, emergency_margin_breached, reservation_kind, budget_date, "
            "token_accounting_policy, created_at, settled_at) "
            "VALUES (?, ?, ?, '0', ?, ?, ?, 0, ?, '0', 0, 0, 0, 0, 0, ?, ?, ?, ?, NULL)",
            (reservation_id, key, provider, estimated_input_tokens, maximum_output_tokens,
             maximum_reasoning_tokens, TOKEN_RESERVATION_RESERVED, RESEARCH_TOKEN_RESERVATION_KIND,
             budget_date.isoformat(), policy, now.isoformat()),
        )
        return ResearchTokenReservation(
            reservation_id=reservation_id, idempotency_key=key,
            reserved_input_tokens=estimated_input_tokens, reserved_output_tokens=maximum_output_tokens,
            reserved_reasoning_tokens=maximum_reasoning_tokens, token_accounting_policy=policy,
            status=TOKEN_RESERVATION_RESERVED,
        )


def _transition_reservation(
    conn, reservation_id: str, *, from_status: str, to_status: str,
    extra_sql: str = "", extra_params: tuple[Any, ...] = (), compatible: Callable[[dict], bool] | None = None,
) -> bool:
    with transaction(conn):
        cursor = conn.execute(
            f"UPDATE shadow_budget_reservations SET status = ?{extra_sql} "
            "WHERE reservation_id = ? AND status = ? AND reservation_kind = ?",
            (to_status, *extra_params, reservation_id, from_status, RESEARCH_TOKEN_RESERVATION_KIND),
        )
        if cursor.rowcount == 1:
            return True
        current = load_budget_reservation(conn, reservation_id)
        if current is None or current.get("reservation_kind") != RESEARCH_TOKEN_RESERVATION_KIND:
            raise TokenBudgetError(f"no such token reservation {reservation_id!r}")
        if compatible is not None and compatible(current):
            return False
        raise TokenBudgetError(
            f"cannot transition token reservation {reservation_id!r} from {current['status']} to {to_status}",
            code="TOKEN_RESERVATION_STATE_CONFLICT",
        )


def settle_research_tokens(
    conn, reservation_id: str, *, actual_input_tokens: int, actual_output_tokens: int,
    actual_reasoning_tokens: int = 0,
    token_accounting_policy: str = TOKEN_ACCOUNTING_NOT_APPLICABLE,
    provider_request_id: str | None = None,
    clock: Clock,
    _from_status: str = TOKEN_RESERVATION_RESERVED,
) -> bool:
    """Settle to authoritative usage while preserving the original estimate."""
    actual_input_tokens = _require_nonnegative_int(actual_input_tokens, "actual_input_tokens")
    actual_output_tokens = _require_nonnegative_int(actual_output_tokens, "actual_output_tokens")
    actual_reasoning_tokens = _require_nonnegative_int(actual_reasoning_tokens, "actual_reasoning_tokens")
    policy = _require_policy(token_accounting_policy)
    now = clock()
    reservation = load_budget_reservation(conn, reservation_id)
    if reservation is None or reservation.get("reservation_kind") != RESEARCH_TOKEN_RESERVATION_KIND:
        raise TokenBudgetError(f"no such token reservation {reservation_id!r}")
    reserved_policy = reservation.get("token_accounting_policy") or TOKEN_ACCOUNTING_NOT_APPLICABLE
    if policy != reserved_policy:
        raise TokenBudgetError(
            f"settlement policy {policy} does not match reservation policy {reserved_policy}",
            code="TOKEN_ACCOUNTING_POLICY_MISMATCH",
        )
    consumed_total = _counted_tokens(actual_input_tokens, actual_output_tokens, actual_reasoning_tokens, policy)
    reserved_total = _reservation_counted_tokens(reservation)
    breached = int(consumed_total > reserved_total)

    def _compatible(current: dict) -> bool:
        return (
            current["status"] == TOKEN_RESERVATION_SETTLED
            and current["consumed_input_tokens"] == actual_input_tokens
            and current["consumed_output_tokens"] == actual_output_tokens
            and (current.get("consumed_reasoning_tokens") or 0) == actual_reasoning_tokens
            and (current.get("token_accounting_policy") or TOKEN_ACCOUNTING_NOT_APPLICABLE) == policy
            and (current.get("provider_request_id") or None) == (provider_request_id or None)
        )

    return _transition_reservation(
        conn, reservation_id, from_status=_from_status, to_status=TOKEN_RESERVATION_SETTLED,
        extra_sql=", consumed_input_tokens = ?, consumed_output_tokens = ?, consumed_reasoning_tokens = ?, "
                  "token_accounting_policy = ?, provider_request_id = ?, emergency_margin_breached = ?, settled_at = ?",
        extra_params=(actual_input_tokens, actual_output_tokens, actual_reasoning_tokens, policy,
                      provider_request_id, breached, now.isoformat()),
        compatible=_compatible,
    )


def release_research_tokens(conn, reservation_id: str, clock: Clock) -> bool:
    """Releases a reservation that was never used (e.g. the cycle failed
    before any call was made)."""
    now = clock()
    return _transition_reservation(
        conn, reservation_id, from_status=TOKEN_RESERVATION_RESERVED, to_status=TOKEN_RESERVATION_RELEASED,
        extra_sql=", settled_at = ?", extra_params=(now.isoformat(),),
        compatible=lambda row: row["status"] == TOKEN_RESERVATION_RELEASED,
    )


def mark_research_tokens_ambiguous(conn, reservation_id: str) -> bool:
    """Marks a reservation AMBIGUOUS when the provider outcome is unclear —
    it stays counted against the daily cap (never blindly released or
    retried) until an operator reconciles it."""
    return _transition_reservation(
        conn, reservation_id, from_status=TOKEN_RESERVATION_RESERVED, to_status=TOKEN_RESERVATION_AMBIGUOUS,
        compatible=lambda row: row["status"] == TOKEN_RESERVATION_AMBIGUOUS,
    )


def reconcile_ambiguous_research_tokens(
    conn,
    reservation_id: str,
    *,
    target_status: str,
    operator: str,
    reason: str,
    reconciliation_source: str,
    clock: Clock,
    actual_input_tokens: int | None = None,
    actual_output_tokens: int | None = None,
    actual_reasoning_tokens: int | None = None,
    token_accounting_policy: str | None = None,
    provider_request_id: str | None = None,
) -> bool:
    """Operator-only AMBIGUOUS resolution with an append-only audit row."""
    if not operator or not reason or not reconciliation_source:
        raise TokenBudgetError(
            "operator, reason, and reconciliation_source are required",
            code="TOKEN_USAGE_EVIDENCE_REQUIRED",
        )
    if target_status not in (TOKEN_RESERVATION_SETTLED, TOKEN_RESERVATION_RELEASED):
        raise TokenBudgetError(
            "AMBIGUOUS may only be reconciled to SETTLED or RELEASED",
            code="TOKEN_RESERVATION_STATE_CONFLICT",
        )
    now = clock()
    update_sql: str
    update_params: tuple[Any, ...]
    if target_status == TOKEN_RESERVATION_SETTLED:
        if actual_input_tokens is None or actual_output_tokens is None or actual_reasoning_tokens is None:
            raise TokenBudgetError(
                "authoritative input, output, and reasoning usage are required",
                code="TOKEN_USAGE_EVIDENCE_REQUIRED",
            )
        if token_accounting_policy is None:
            raise TokenBudgetError(
                "token_accounting_policy is required", code="TOKEN_USAGE_EVIDENCE_REQUIRED",
            )
        actual_input_tokens = _require_nonnegative_int(actual_input_tokens, "actual_input_tokens")
        actual_output_tokens = _require_nonnegative_int(actual_output_tokens, "actual_output_tokens")
        actual_reasoning_tokens = _require_nonnegative_int(actual_reasoning_tokens, "actual_reasoning_tokens")
        policy = _require_policy(token_accounting_policy)
        update_sql = (
            "UPDATE shadow_budget_reservations SET status = ?, consumed_input_tokens = ?, "
            "consumed_output_tokens = ?, consumed_reasoning_tokens = ?, token_accounting_policy = ?, "
            "provider_request_id = ?, emergency_margin_breached = CASE "
            "WHEN (? + ? + CASE WHEN ? = ? THEN ? ELSE 0 END) > "
            "(reserved_input_tokens + reserved_output_tokens + CASE WHEN token_accounting_policy = ? "
            "THEN COALESCE(reserved_reasoning_tokens, 0) ELSE 0 END) THEN 1 ELSE 0 END, settled_at = ? "
            "WHERE reservation_id = ? AND status = ? AND reservation_kind = ?"
        )
        update_params = (
            TOKEN_RESERVATION_SETTLED, actual_input_tokens, actual_output_tokens, actual_reasoning_tokens,
            policy, provider_request_id, actual_input_tokens, actual_output_tokens, policy,
            TOKEN_ACCOUNTING_REASONING_SEPARATE, actual_reasoning_tokens,
            TOKEN_ACCOUNTING_REASONING_SEPARATE, now.isoformat(), reservation_id,
            TOKEN_RESERVATION_AMBIGUOUS, RESEARCH_TOKEN_RESERVATION_KIND,
        )
    else:
        policy = token_accounting_policy
        update_sql = (
            "UPDATE shadow_budget_reservations SET status = ?, settled_at = ? "
            "WHERE reservation_id = ? AND status = ? AND reservation_kind = ?"
        )
        update_params = (
            TOKEN_RESERVATION_RELEASED, now.isoformat(), reservation_id,
            TOKEN_RESERVATION_AMBIGUOUS, RESEARCH_TOKEN_RESERVATION_KIND,
        )
    with transaction(conn):
        current = load_budget_reservation(conn, reservation_id)
        if current is None or current.get("reservation_kind") != RESEARCH_TOKEN_RESERVATION_KIND:
            raise TokenBudgetError(f"no such token reservation {reservation_id!r}")
        reserved_policy = current.get("token_accounting_policy") or TOKEN_ACCOUNTING_NOT_APPLICABLE
        if target_status == TOKEN_RESERVATION_SETTLED and policy != reserved_policy:
            raise TokenBudgetError(
                f"settlement policy {policy} does not match reservation policy {reserved_policy}",
                code="TOKEN_ACCOUNTING_POLICY_MISMATCH",
            )
        cursor = conn.execute(update_sql, update_params)
        if cursor.rowcount == 0:
            reloaded = load_budget_reservation(conn, reservation_id)
            if reloaded is not None and reloaded["status"] == target_status:
                return False
            raise TokenBudgetError(
                f"cannot reconcile token reservation from {current['status']} to {target_status}",
                code="TOKEN_RESERVATION_STATE_CONFLICT",
            )
        else:
            insert_token_budget_reconciliation(conn, {
                "reconciliation_id": f"token-reconciliation-{uuid.uuid4().hex}",
                "reservation_id": reservation_id, "from_status": TOKEN_RESERVATION_AMBIGUOUS,
                "to_status": target_status, "operator": operator, "reason": reason,
                "reconciliation_source": reconciliation_source,
                "provider_request_id": provider_request_id,
                "actual_input_tokens": actual_input_tokens,
                "actual_output_tokens": actual_output_tokens,
                "actual_reasoning_tokens": actual_reasoning_tokens,
                "token_accounting_policy": token_accounting_policy,
                "reconciled_at": now.isoformat(),
            })
    return True
