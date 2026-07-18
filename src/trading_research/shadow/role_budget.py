"""Pre-call role/token/attempt/latency/cost budgeting (docs/milestone-7.md
Step 17, ADR 0005 Decision 6). Before each role invocation inside a shadow
cycle, `check_role_budget` computes the *maximum possible* remaining cost of
that call (its configured max output tokens, priced at the reservation's
per-token rate) against the reservation's remaining budget. A call that
could theoretically exceed the remaining budget is skipped with
`SKIPPED_BUDGET_EXHAUSTED` — a status distinct from any provider-failure
code, so it never pollutes provider-failure-rate health metrics or the
existing research-failure taxonomy (docs/milestone-7.md Step 17: "budget
rejection is not classified as provider failure").

This module does not call `research/orchestration.py` or any evidence
provider — it is a pure decision function the scheduler consults before
invoking `analyze_with_research_committee` for a role, matching the existing
`_run_role_with_retries` pattern of deciding role eligibility before
invoking a provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from .budget import ReservationHandle, remaining_reservation_budget

Clock = Callable[[], datetime]

DECISION_PROCEED = "PROCEED"
DECISION_SKIPPED_BUDGET_EXHAUSTED = "SKIPPED_BUDGET_EXHAUSTED"
# Milestone 12.1.1 Item 1: distinct from SKIPPED_BUDGET_EXHAUSTED — the call
# is blocked by the persistent pause/kill system state, not by any budget
# figure, including a pause an earlier attempt in the same role's retry loop
# just triggered (`shadow/attempt_controller.py::after_attempt`).
DECISION_SKIPPED_PAUSED_OR_KILLED = "SKIPPED_PAUSED_OR_KILLED"


class RoleBudgetError(RuntimeError):
    pass


@dataclass(frozen=True)
class RoleBudgetDecision:
    """Distinct, explicit type from any provider-failure taxonomy — a
    caller cannot accidentally conflate `SKIPPED_BUDGET_EXHAUSTED` with a
    provider failure because this dataclass has no `failure_code` field
    shared with that taxonomy at all."""

    decision: str
    role_name: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.decision not in (
            DECISION_PROCEED, DECISION_SKIPPED_BUDGET_EXHAUSTED, DECISION_SKIPPED_PAUSED_OR_KILLED,
        ):
            raise RoleBudgetError(f"unrecognized role-budget decision {self.decision!r} — fails closed")

    @property
    def proceed(self) -> bool:
        return self.decision == DECISION_PROCEED


def check_role_budget(
    conn,
    reservation: ReservationHandle,
    role_name: str,
    role_index: int,
    attempt_number: int,
    *,
    allowed_roles: tuple[str, ...],
    max_roles_per_symbol: int,
    max_attempts_per_role: int,
    max_possible_output_tokens_for_role: int,
    max_possible_input_tokens_for_role: int,
    max_possible_latency_seconds_for_role: int,
    estimated_cost_per_output_token: Decimal,
    estimated_cost_per_input_token: Decimal,
    clock: Clock,
) -> RoleBudgetDecision:
    """Pre-call gate. Every check happens before any provider call is made,
    against the *maximum possible* cost/latency/tokens the about-to-run role
    call could consume — never actual usage, since actual isn't known yet
    (docs/milestone-7.md Step 17)."""
    if role_name not in allowed_roles:
        return RoleBudgetDecision(
            decision=DECISION_SKIPPED_BUDGET_EXHAUSTED, role_name=role_name,
            reason=f"role {role_name!r} is not in the allowed role set {allowed_roles}",
        )
    if role_index >= max_roles_per_symbol:
        return RoleBudgetDecision(
            decision=DECISION_SKIPPED_BUDGET_EXHAUSTED, role_name=role_name,
            reason=f"role_index {role_index} >= max_roles_per_symbol {max_roles_per_symbol}",
        )
    if attempt_number > max_attempts_per_role:
        return RoleBudgetDecision(
            decision=DECISION_SKIPPED_BUDGET_EXHAUSTED, role_name=role_name,
            reason=f"attempt_number {attempt_number} > max_attempts_per_role {max_attempts_per_role}",
        )

    remaining = remaining_reservation_budget(conn, reservation.reservation_id)

    if max_possible_output_tokens_for_role > remaining["remaining_output_tokens"]:
        return RoleBudgetDecision(
            decision=DECISION_SKIPPED_BUDGET_EXHAUSTED, role_name=role_name,
            reason=(
                f"max possible output tokens {max_possible_output_tokens_for_role} exceeds remaining "
                f"output-token budget {remaining['remaining_output_tokens']}"
            ),
        )
    if max_possible_input_tokens_for_role > remaining["remaining_input_tokens"]:
        return RoleBudgetDecision(
            decision=DECISION_SKIPPED_BUDGET_EXHAUSTED, role_name=role_name,
            reason=(
                f"max possible input tokens {max_possible_input_tokens_for_role} exceeds remaining "
                f"input-token budget {remaining['remaining_input_tokens']}"
            ),
        )
    if max_possible_latency_seconds_for_role > remaining["remaining_latency_seconds"]:
        return RoleBudgetDecision(
            decision=DECISION_SKIPPED_BUDGET_EXHAUSTED, role_name=role_name,
            reason=(
                f"max possible latency {max_possible_latency_seconds_for_role}s exceeds remaining "
                f"latency budget {remaining['remaining_latency_seconds']}s"
            ),
        )

    max_possible_cost = (
        Decimal(max_possible_output_tokens_for_role) * estimated_cost_per_output_token
        + Decimal(max_possible_input_tokens_for_role) * estimated_cost_per_input_token
    )
    if max_possible_cost > remaining["remaining_cost_usd"]:
        return RoleBudgetDecision(
            decision=DECISION_SKIPPED_BUDGET_EXHAUSTED, role_name=role_name,
            reason=(
                f"max possible cost {max_possible_cost} exceeds remaining cost budget "
                f"{remaining['remaining_cost_usd']}"
            ),
        )

    return RoleBudgetDecision(decision=DECISION_PROCEED, role_name=role_name, reason=None)
