"""Shadow-specific `ResearchAttemptController` adapter (docs/milestone-7.1.md
Steps 13-15). Connects `research/orchestration.py`'s framework-neutral
attempt-control hooks to `shadow/role_budget.py::check_role_budget` and
`shadow/budget.py::record_actual_usage_for_attempt`.

`research/orchestration.py` never imports this module or any other `shadow`
module — this adapter is injected by the caller (the scheduler's
`cycle_kwargs_builder`, see `shadow/scheduler.py`) via the `attempt_controller`
parameter, and only this module imports both `research.orchestration`'s
Protocol/dataclasses and `shadow.*`.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable

from ..research.orchestration import AttemptControlDecision, AttemptControlRequest, ResearchAttemptRecord
from ..research.usage import PricingEntry
from ..storage.shadow_operations_repositories import save_role_budget_check
from . import role_budget as role_budget_mod
from .budget import PRICING_EXEMPT_PROVIDERS, ReservationHandle, record_actual_usage_for_attempt, remaining_reservation_budget

Clock = Callable[[], datetime]


def _compute_check_id(*, reservation_id: str, research_run_id: str, role: str, attempt_number: int) -> str:
    """Deterministic identity (docs/milestone-7.1.md Step 14: "deterministic/
    idempotent check identity") — the same (reservation, run, role, attempt)
    tuple always produces the same `check_id`, so a resumed cycle's repeated
    pre-attempt check never inserts a duplicate audit row (`save_role_budget_check`
    uses `INSERT OR IGNORE`)."""
    payload = f"{reservation_id}|{research_run_id}|{role}|{attempt_number}"
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"rbcheck-{digest[:32]}"


@dataclass
class ShadowResearchAttemptController:
    """One instance per scheduled-cycle symbol invocation. `provider`/
    `pricing` are the SAME provider name and `PricingEntry` already used for
    the cycle's own budget reservation (docs/milestone-7.1.md Step 13: "use
    the same pricing entry used by the cycle reservation... do not perform a
    second inconsistent pricing lookup") — never re-selected here.
    """

    conn: sqlite3.Connection
    reservation: ReservationHandle
    provider: str
    allowed_roles: tuple[str, ...]
    max_roles_per_symbol: int
    max_attempts_per_role: int
    max_output_tokens_per_role: int
    max_input_tokens_per_role: int
    max_latency_seconds_per_role: int
    pricing: PricingEntry | None
    clock: Clock
    scheduler_run_id: str | None = None
    cycle_id: str | None = None
    _role_index_by_role: dict[str, int] = field(default_factory=dict)
    _next_role_index: int = field(default=0, init=False)

    def _role_index(self, role: str) -> int:
        if role not in self._role_index_by_role:
            self._role_index_by_role[role] = self._next_role_index
            self._next_role_index += 1
        return self._role_index_by_role[role]

    def before_attempt(self, request: AttemptControlRequest) -> AttemptControlDecision:
        role_index = self._role_index(request.role)
        cost_per_output_token = (
            (self.pricing.output_price_per_million / Decimal(1_000_000)) if self.pricing is not None else Decimal("0")
        )
        cost_per_input_token = (
            (self.pricing.input_price_per_million / Decimal(1_000_000)) if self.pricing is not None else Decimal("0")
        )
        decision = role_budget_mod.check_role_budget(
            self.conn, self.reservation, request.role, role_index, request.attempt_number,
            allowed_roles=self.allowed_roles, max_roles_per_symbol=self.max_roles_per_symbol,
            max_attempts_per_role=self.max_attempts_per_role,
            max_possible_output_tokens_for_role=self.max_output_tokens_per_role,
            max_possible_input_tokens_for_role=self.max_input_tokens_per_role,
            max_possible_latency_seconds_for_role=self.max_latency_seconds_per_role,
            estimated_cost_per_output_token=cost_per_output_token,
            estimated_cost_per_input_token=cost_per_input_token,
            clock=self.clock,
        )

        remaining = remaining_reservation_budget(self.conn, self.reservation.reservation_id)
        max_possible_cost = (
            Decimal(self.max_output_tokens_per_role) * cost_per_output_token
            + Decimal(self.max_input_tokens_per_role) * cost_per_input_token
        )
        checked_at = self.clock()
        check_id = _compute_check_id(
            reservation_id=self.reservation.reservation_id, research_run_id=request.research_run_id,
            role=request.role, attempt_number=request.attempt_number,
        )
        save_role_budget_check(
            self.conn,
            {
                "check_id": check_id, "reservation_id": self.reservation.reservation_id,
                "scheduler_run_id": self.scheduler_run_id, "cycle_id": self.cycle_id,
                "research_run_id": request.research_run_id, "symbol": request.symbol, "role": request.role,
                "attempt_number": request.attempt_number, "provider": self.provider, "model_name": request.model_name,
                "decision": decision.decision, "reason": decision.reason,
                "remaining_input_tokens": remaining["remaining_input_tokens"],
                "remaining_output_tokens": remaining["remaining_output_tokens"],
                "remaining_latency_ms": remaining["remaining_latency_seconds"] * 1000,
                "remaining_cost_usd": str(remaining["remaining_cost_usd"]),
                "maximum_attempt_input_tokens": self.max_input_tokens_per_role,
                "maximum_attempt_output_tokens": self.max_output_tokens_per_role,
                "maximum_attempt_latency_ms": self.max_latency_seconds_per_role * 1000,
                "maximum_attempt_cost_usd": str(max_possible_cost),
                "checked_at": checked_at.isoformat(),
            },
        )

        return AttemptControlDecision(allowed=decision.proceed, code=decision.decision, reason=decision.reason)

    def after_attempt(self, request: AttemptControlRequest, attempt: ResearchAttemptRecord) -> None:
        usage = attempt.usage
        if usage.input_tokens is None or usage.output_tokens is None or usage.latency_ms is None:
            # Never fabricate usage the provider did not actually report
            # (docs/milestone-7.1.md hard boundary) — a non-retryable
            # provider error with no token data simply is not charged here;
            # the real, authoritative attempt record (with whatever latency
            # data IS available) is already persisted in `research_attempts`
            # by the orchestrator itself, independent of this hook.
            return
        if usage.estimated_cost is not None:
            cost = usage.estimated_cost
        elif usage.provider in PRICING_EXEMPT_PROVIDERS:
            cost = Decimal("0")
        else:
            # Cost genuinely unknown for a provider whose pricing should
            # have been resolved (the cycle-level reservation preflight
            # already fails closed before this can happen for a real
            # anthropic run) — never fabricate a zero cost here either.
            return
        latency_seconds = int(round(usage.latency_ms / 1000))
        record_actual_usage_for_attempt(
            self.conn, self.reservation.reservation_id, attempt.attempt_id, actual_cost_usd=cost,
            actual_input_tokens=usage.input_tokens, actual_output_tokens=usage.output_tokens,
            actual_latency_seconds=latency_seconds, provider=attempt.provider, model_name=attempt.model_name,
            clock=self.clock,
        )
