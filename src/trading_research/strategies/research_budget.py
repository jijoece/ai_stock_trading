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
TOKEN_RESERVATION_IN_FLIGHT = "IN_FLIGHT"
TOKEN_RESERVATION_SETTLED = "SETTLED"
TOKEN_RESERVATION_RELEASED = "RELEASED"
TOKEN_RESERVATION_AMBIGUOUS = "AMBIGUOUS"

_LIVE_TOKEN_STATUSES = (
    TOKEN_RESERVATION_RESERVED, TOKEN_RESERVATION_IN_FLIGHT,
    TOKEN_RESERVATION_SETTLED, TOKEN_RESERVATION_AMBIGUOUS,
)

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
            # Same idempotency key does not by itself prove the same request —
            # compare every immutable reservation field, not just kind/policy
            # (docs/milestones/26.md A5): a caller supplying a larger output
            # allowance, a different reasoning allowance, etc. under a key
            # that happens to collide must never silently reuse the original
            # reservation or reach the provider.
            if (
                existing["reserved_input_tokens"] != estimated_input_tokens
                or existing["reserved_output_tokens"] != maximum_output_tokens
                or (existing.get("reserved_reasoning_tokens") or 0) != maximum_reasoning_tokens
            ):
                raise TokenBudgetError(
                    "idempotency key exists with a different immutable reservation payload",
                    code="TOKEN_RESERVATION_PAYLOAD_MISMATCH",
                )
            return _reservation_from_row(existing)
        # Stable provider-attempt identity (research_run_id, symbol, provider,
        # model) — deliberately excludes the UTC budget-date suffix so an
        # unresolved AMBIGUOUS reservation from a prior date still blocks a
        # retry today (docs/milestones/26.md A2: "An unresolved July 19
        # attempt can therefore be retried on July 20 under a new
        # reservation" was the bug). Matches on the run/symbol/provider/model
        # rather than the specific attempt_number: an unresolved ambiguity
        # anywhere in this research run's provider work must block every
        # further attempt for it, not just a retry of the exact same attempt
        # number — this is unchanged, pre-existing blocking scope.
        provider_attempt_key = (
            f"{RESEARCH_TOKEN_RESERVATION_PREFIX}:{research_run_id}:{symbol}:{provider}:"
            f"{model_name or 'none'}:"
        )
        unresolved = next((
            row for row in list_budget_reservations(conn)
            if row.get("reservation_kind") == RESEARCH_TOKEN_RESERVATION_KIND
            and row["status"] == TOKEN_RESERVATION_AMBIGUOUS
            and row["idempotency_key"].startswith(provider_attempt_key)
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
    claim_owner: str | None = None, claim_generation: int | None = None,
) -> bool:
    fencing_sql = ""
    fencing_params: tuple[Any, ...] = ()
    if claim_owner is not None:
        fencing_sql = " AND claim_owner = ? AND claim_generation = ?"
        fencing_params = (claim_owner, claim_generation)
    with transaction(conn):
        cursor = conn.execute(
            f"UPDATE shadow_budget_reservations SET status = ?{extra_sql} "
            f"WHERE reservation_id = ? AND status = ? AND reservation_kind = ?{fencing_sql}",
            (to_status, *extra_params, reservation_id, from_status, RESEARCH_TOKEN_RESERVATION_KIND, *fencing_params),
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


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of `claim_research_token_attempt`. `status` is one of
    `CLAIMED`, `ALREADY_IN_FLIGHT`, `STATE_CONFLICT`."""

    status: str
    reservation_id: str
    claim_owner: str | None = None
    claim_generation: int | None = None


def claim_research_token_attempt(
    conn, reservation_id: str, owner_id: str, now: datetime, expires_at: datetime,
) -> ClaimResult:
    """Atomically claims exclusive provider-invocation rights for one
    reservation (docs/milestones/27.md A1): `RESERVED -> IN_FLIGHT` via a
    single conditional `UPDATE` inside `BEGIN IMMEDIATE`. Only the caller
    that flips exactly one row may invoke the provider. `owner_id` must be a
    bounded, generated identifier (never a machine username, credential, or
    raw process environment value)."""
    if not owner_id:
        raise TokenBudgetError("owner_id must be non-empty")
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE shadow_budget_reservations SET status = ?, claim_owner = ?, "
            "claim_generation = claim_generation + 1, claimed_at = ?, claim_expires_at = ? "
            "WHERE reservation_id = ? AND status = ? AND reservation_kind = ?",
            (
                TOKEN_RESERVATION_IN_FLIGHT, owner_id, now.isoformat(), expires_at.isoformat(),
                reservation_id, TOKEN_RESERVATION_RESERVED, RESEARCH_TOKEN_RESERVATION_KIND,
            ),
        )
        if cursor.rowcount == 1:
            claimed = load_budget_reservation(conn, reservation_id)
            assert claimed is not None
            return ClaimResult(
                status="CLAIMED", reservation_id=reservation_id,
                claim_owner=owner_id, claim_generation=claimed["claim_generation"],
            )
        current = load_budget_reservation(conn, reservation_id)
        if current is None or current.get("reservation_kind") != RESEARCH_TOKEN_RESERVATION_KIND:
            raise TokenBudgetError(f"no such token reservation {reservation_id!r}")
        if current["status"] == TOKEN_RESERVATION_IN_FLIGHT:
            return ClaimResult(status="ALREADY_IN_FLIGHT", reservation_id=reservation_id)
        return ClaimResult(status="STATE_CONFLICT", reservation_id=reservation_id)


def recover_expired_token_claims(conn, *, now: datetime) -> tuple[str, ...]:
    """Maintenance/recovery operation (docs/milestones/27.md A1 "Claim
    expiration"): transitions every `IN_FLIGHT` reservation whose claim
    lease has expired to `AMBIGUOUS`. An expired claim does not prove the
    provider was never called -- transmission may have occurred -- so this
    never resumes or retries the attempt automatically; it only frees the
    reservation for operator reconciliation. Uses the same expected-state
    (`IN_FLIGHT`) and claim-generation check as every other fenced
    transition, so a claim owner that renews/settles concurrently is never
    overwritten."""
    recovered: list[str] = []
    with transaction(conn):
        expired = conn.execute(
            "SELECT reservation_id, claim_owner, claim_generation FROM shadow_budget_reservations "
            "WHERE status = ? AND reservation_kind = ? AND claim_expires_at IS NOT NULL AND claim_expires_at < ?",
            (TOKEN_RESERVATION_IN_FLIGHT, RESEARCH_TOKEN_RESERVATION_KIND, now.isoformat()),
        ).fetchall()
        for row in expired:
            cursor = conn.execute(
                "UPDATE shadow_budget_reservations SET status = ? "
                "WHERE reservation_id = ? AND status = ? AND claim_owner = ? AND claim_generation = ?",
                (
                    TOKEN_RESERVATION_AMBIGUOUS, row["reservation_id"], TOKEN_RESERVATION_IN_FLIGHT,
                    row["claim_owner"], row["claim_generation"],
                ),
            )
            if cursor.rowcount == 1:
                recovered.append(row["reservation_id"])
    return tuple(recovered)


def settle_research_tokens(
    conn, reservation_id: str, *, actual_input_tokens: int, actual_output_tokens: int,
    actual_reasoning_tokens: int = 0,
    token_accounting_policy: str = TOKEN_ACCOUNTING_NOT_APPLICABLE,
    provider_request_id: str | None = None,
    clock: Clock,
    _from_status: str = TOKEN_RESERVATION_RESERVED,
    claim_owner: str | None = None,
    claim_generation: int | None = None,
) -> bool:
    """Settle to authoritative usage while preserving the original estimate.

    When `claim_owner`/`claim_generation` are supplied (the production
    `PersistentResearchTokenBudgetController` path -- docs/milestones/27.md
    A1), the transition is fenced: it requires `IN_FLIGHT` plus an exact
    claim match, so a stale worker whose lease already expired and was
    recovered to `AMBIGUOUS` by another owner can never settle over it.
    Callers that never claim (direct RESERVED -> SETTLED, e.g. cost
    reservations or pre-claim test coverage) keep the original unfenced
    behavior."""
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

    from_status = TOKEN_RESERVATION_IN_FLIGHT if claim_owner is not None else _from_status
    return _transition_reservation(
        conn, reservation_id, from_status=from_status, to_status=TOKEN_RESERVATION_SETTLED,
        extra_sql=", consumed_input_tokens = ?, consumed_output_tokens = ?, consumed_reasoning_tokens = ?, "
                  "token_accounting_policy = ?, provider_request_id = ?, emergency_margin_breached = ?, settled_at = ?",
        extra_params=(actual_input_tokens, actual_output_tokens, actual_reasoning_tokens, policy,
                      provider_request_id, breached, now.isoformat()),
        compatible=_compatible, claim_owner=claim_owner, claim_generation=claim_generation,
    )


def release_research_tokens(
    conn, reservation_id: str, clock: Clock, *,
    claim_owner: str | None = None, claim_generation: int | None = None,
) -> bool:
    """Releases a reservation that was never used (e.g. the cycle failed
    before any call was made). With `claim_owner`/`claim_generation` set,
    releases a fenced `IN_FLIGHT` claim instead -- used only when the
    provider adapter proves the request was never transmitted
    (docs/milestones/27.md A1 "Provider exceptions")."""
    now = clock()
    from_status = TOKEN_RESERVATION_IN_FLIGHT if claim_owner is not None else TOKEN_RESERVATION_RESERVED
    return _transition_reservation(
        conn, reservation_id, from_status=from_status, to_status=TOKEN_RESERVATION_RELEASED,
        extra_sql=", settled_at = ?", extra_params=(now.isoformat(),),
        compatible=lambda row: row["status"] == TOKEN_RESERVATION_RELEASED,
        claim_owner=claim_owner, claim_generation=claim_generation,
    )


def mark_research_tokens_ambiguous(
    conn, reservation_id: str, *, claim_owner: str | None = None, claim_generation: int | None = None,
) -> bool:
    """Marks a reservation AMBIGUOUS when the provider outcome is unclear —
    it stays counted against the daily cap (never blindly released or
    retried) until an operator reconciles it. With `claim_owner`/
    `claim_generation` set, this is a fenced `IN_FLIGHT -> AMBIGUOUS`
    transition (docs/milestones/27.md A1): a stale worker whose claim
    already expired and was recovered by another owner gets rejected here
    instead of clobbering the recovered state."""
    from_status = TOKEN_RESERVATION_IN_FLIGHT if claim_owner is not None else TOKEN_RESERVATION_RESERVED
    return _transition_reservation(
        conn, reservation_id, from_status=from_status, to_status=TOKEN_RESERVATION_AMBIGUOUS,
        compatible=lambda row: row["status"] == TOKEN_RESERVATION_AMBIGUOUS,
        claim_owner=claim_owner, claim_generation=claim_generation,
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
                # Already at the target status — only an idempotent no-op
                # when every authoritative field of this second reconciliation
                # matches what was actually persisted the first time
                # (docs/milestones/26.md A6). Operator/reason text may differ
                # freely; accounting evidence may not.
                if target_status == TOKEN_RESERVATION_SETTLED:
                    evidence_matches = (
                        reloaded["consumed_input_tokens"] == actual_input_tokens
                        and reloaded["consumed_output_tokens"] == actual_output_tokens
                        and (reloaded.get("consumed_reasoning_tokens") or 0) == (actual_reasoning_tokens or 0)
                        and (reloaded.get("token_accounting_policy") or None) == token_accounting_policy
                        and (reloaded.get("provider_request_id") or None) == (provider_request_id or None)
                    )
                else:
                    evidence_matches = True  # RELEASED carries no numeric accounting evidence to conflict on
                if evidence_matches:
                    return False
                raise TokenBudgetError(
                    "repeated reconciliation to the same target carries different accounting evidence "
                    "than the first reconciliation",
                    code="TOKEN_RECONCILIATION_EVIDENCE_CONFLICT",
                )
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


# --- Legacy (pre-PR #35) token reservation migration (docs/milestones/26.md A4) ---
#
# `shadow_operations_schema.py`'s additive migration gave every pre-existing
# `shadow_budget_reservations` row `reservation_kind = RESEARCH_COST` (the
# column default) regardless of whether the row was actually a research-token
# reservation. A row whose idempotency_key carries the stable
# `research_token_budget:` prefix is unambiguously a legacy token
# reservation — this backfills its token-specific columns without ever
# touching a genuine cost reservation.

TOKEN_MIGRATION_LEGACY_ACCOUNTING_POLICY = TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT


@dataclass(frozen=True)
class LegacyTokenMigrationConflict:
    reservation_id: str
    code: str


@dataclass(frozen=True)
class LegacyTokenMigrationResult:
    migrated_reservation_ids: tuple[str, ...]
    already_migrated_reservation_ids: tuple[str, ...]
    conflicts: tuple[LegacyTokenMigrationConflict, ...]


def _legacy_budget_date(idempotency_key: str, created_at_iso: str | None) -> str | None:
    """Preferred order (docs/milestones/26.md A4): parse the legacy key's UTC
    date suffix; otherwise derive from timezone-aware `created_at`; otherwise
    `None` (caller fails closed)."""
    suffix = idempotency_key.rsplit(":", 1)[-1]
    try:
        return date.fromisoformat(suffix).isoformat()
    except ValueError:
        pass
    if not created_at_iso:
        return None
    try:
        parsed = datetime.fromisoformat(created_at_iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).date().isoformat()


class LegacyTokenMigrationError(TokenBudgetError):
    """Raised instead of returning a result when any legacy candidate fails
    validation (docs/milestones/27.md A2): a partial migration that reports
    some rows migrated while others are silently left conflicted would let
    startup continue with unresolved metadata. `conflicts` never carries raw
    row contents -- only the reservation id and the bounded conflict code."""

    def __init__(self, conflicts: tuple[LegacyTokenMigrationConflict, ...]) -> None:
        super().__init__(
            f"{len(conflicts)} legacy token reservation(s) failed migration validation",
            code="TOKEN_MIGRATION_CONFLICT",
        )
        self.conflicts = conflicts


def migrate_legacy_token_reservations_locked(conn) -> LegacyTokenMigrationResult:
    """Body of the legacy migration (docs/milestones/26.md A4,
    docs/milestones/27.md A2). Assumes the caller already holds an open
    write transaction on `conn` (the repository-wide `commit=False`
    convention -- see `storage/transactions.py`) so it can participate in
    `storage/schema_version.py::apply_pending_schema_migrations`'s own
    transaction instead of nesting a second `BEGIN IMMEDIATE`.

    Two-phase and fail-closed: phase one only inspects and validates every
    legacy candidate row; if any conflict exists, it raises
    `LegacyTokenMigrationError` without mutating a single row (docs/milestones/27.md
    A2: "Do not return successful migration results containing unresolved
    conflicts"). Phase two mutates only once every candidate is known-valid.

    Never splits a legacy combined `reserved_output_tokens` value (may have
    folded `maximum_output_tokens + maximum_reasoning_tokens` together
    pre-PR#35) — preserves it exactly and migrates to the conservative
    `REASONING_INCLUDED_IN_OUTPUT` policy with reasoning usage at zero
    rather than fabricating a separate reasoning count. Never alters a row
    whose idempotency_key lacks the `research_token_budget:` prefix (cost
    reservations)."""
    already_migrated: list[str] = []
    conflicts: list[LegacyTokenMigrationConflict] = []
    candidates: list[tuple[str, str]] = []
    for row in list_budget_reservations(conn):
        key = row["idempotency_key"]
        if not key.startswith(f"{RESEARCH_TOKEN_RESERVATION_PREFIX}:"):
            continue
        if row.get("reservation_kind") == RESEARCH_TOKEN_RESERVATION_KIND:
            if row.get("token_accounting_policy") is None or row.get("budget_date") is None:
                conflicts.append(
                    LegacyTokenMigrationConflict(row["reservation_id"], "TOKEN_MIGRATION_INCOMPLETE_METADATA")
                )
            else:
                already_migrated.append(row["reservation_id"])
            continue

        budget_date_str = _legacy_budget_date(key, row.get("created_at"))
        if budget_date_str is None:
            conflicts.append(
                LegacyTokenMigrationConflict(row["reservation_id"], "TOKEN_MIGRATION_BUDGET_DATE_UNRESOLVED")
            )
            continue
        candidates.append((row["reservation_id"], budget_date_str))

    if conflicts:
        raise LegacyTokenMigrationError(tuple(conflicts))

    migrated: list[str] = []
    for reservation_id, budget_date_str in candidates:
        conn.execute(
            "UPDATE shadow_budget_reservations SET reservation_kind = ?, budget_date = ?, "
            "token_accounting_policy = ?, reserved_reasoning_tokens = 0, consumed_reasoning_tokens = 0 "
            "WHERE reservation_id = ?",
            (
                RESEARCH_TOKEN_RESERVATION_KIND, budget_date_str, TOKEN_MIGRATION_LEGACY_ACCOUNTING_POLICY,
                reservation_id,
            ),
        )
        migrated.append(reservation_id)
    return LegacyTokenMigrationResult(
        migrated_reservation_ids=tuple(migrated), already_migrated_reservation_ids=tuple(already_migrated),
        conflicts=(),
    )


def migrate_legacy_token_reservations(conn, clock: Clock) -> LegacyTokenMigrationResult:
    """Standalone entry point for manual/test invocation: owns its own
    transaction around `migrate_legacy_token_reservations_locked`. Database
    startup instead calls the `_locked` body directly from inside
    `storage/schema_version.py`'s own migration transaction."""
    del clock  # reserved for a future recorded migration timestamp; not persisted today
    with transaction(conn):
        return migrate_legacy_token_reservations_locked(conn)
