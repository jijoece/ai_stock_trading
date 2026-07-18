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

# Locked-down, self-managed CLI providers whose own call caps/health markers
# this controller enforces directly (docs/milestone-12-codex-provider.md) —
# every other real provider (anthropic) relies on the generic role-budget
# check below instead.
_MANAGED_CLI_PROVIDERS = ("claude_code", "codex")

# Milestone 12.1 Item 1: explicit allowlist of stable typed failure codes
# (research/failure_taxonomy.py) that indicate a structural provider problem
# — authentication, unexpected auth method, unsupported CLI version, or
# credit/quota exhaustion — none of which will resolve themselves on the
# next attempt. Deliberately NOT a free-text/substring check: a message that
# happens to contain a word like "retry" (a transient, retryable failure)
# must never trigger this pause, only an attempt whose `failure_code` is
# exactly one of these values.
IMMEDIATE_PROVIDER_PAUSE_CODES = frozenset(
    {
        "CODEX_NOT_AUTHENTICATED",
        "CODEX_UNEXPECTED_AUTH_METHOD",
        "CODEX_VERSION_UNSUPPORTED",
        "CODEX_QUOTA_EXHAUSTED",
        "CODEX_USAGE_METADATA_MISSING",
        "CLAUDE_CODE_NOT_AUTHENTICATED",
        "CLAUDE_CODE_AUTH_STATUS_FAILED",
        "CLAUDE_CODE_UNEXPECTED_AUTH_METHOD",
        "CLAUDE_CODE_OAUTH_TOKEN_MISSING",
        "CLAUDE_CODE_VERSION_UNSUPPORTED",
        "CLAUDE_CODE_CREDIT_EXHAUSTED",
        "CLAUDE_CODE_USAGE_METADATA_MISSING",
    }
)


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
    max_calls_per_cycle: int | None = None
    max_calls_per_day: int | None = None
    max_calls_per_month: int | None = None
    max_api_equivalent_cost_per_call_usd: Decimal | None = None
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
        decision = None
        max_possible_cost = (
            Decimal(self.max_output_tokens_per_role) * cost_per_output_token
            + Decimal(self.max_input_tokens_per_role) * cost_per_input_token
        )
        if self.provider in _MANAGED_CLI_PROVIDERS:
            provider_label = "Claude Code" if self.provider == "claude_code" else "Codex"
            now = self.clock()
            call_caps = (
                ("cycle", self.max_calls_per_cycle, self.conn.execute(
                    "SELECT COUNT(*) FROM shadow_role_budget_checks WHERE reservation_id = ? AND decision = 'PROCEED'",
                    (self.reservation.reservation_id,),
                ).fetchone()[0]),
                ("day", self.max_calls_per_day, self.conn.execute(
                    "SELECT COUNT(*) FROM research_attempts WHERE provider = ? AND created_at LIKE ?",
                    (self.provider, now.date().isoformat() + "%"),
                ).fetchone()[0]),
                ("month", self.max_calls_per_month, self.conn.execute(
                    "SELECT COUNT(*) FROM research_attempts WHERE provider = ? AND created_at LIKE ?",
                    (self.provider, now.strftime("%Y-%m") + "%"),
                ).fetchone()[0]),
            )
            for period, cap, used in call_caps:
                if cap is not None and used >= cap:
                    decision = role_budget_mod.RoleBudgetDecision(
                        decision=role_budget_mod.DECISION_SKIPPED_BUDGET_EXHAUSTED,
                        role_name=request.role,
                        reason=f"{provider_label} {period} call cap {cap} has been reached",
                    )
                    break
            if (
                decision is None and self.max_api_equivalent_cost_per_call_usd is not None
                and max_possible_cost > self.max_api_equivalent_cost_per_call_usd
            ):
                decision = role_budget_mod.RoleBudgetDecision(
                    decision=role_budget_mod.DECISION_SKIPPED_BUDGET_EXHAUSTED,
                    role_name=request.role,
                    reason=f"{provider_label} maximum API-equivalent cost per call would be exceeded",
                )
        if decision is None:
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
        if self.provider in _MANAGED_CLI_PROVIDERS and not attempt.success:
            # Milestone 12.1 Item 1: act only on the stable typed
            # `failure_code` taxonomy, never on `failure_reason` free text —
            # a retryable transient failure (e.g. CODEX_TRANSIENT_FAILURE,
            # CODEX_PROCESS_TIMEOUT) must never trip this pause just because
            # its message happens to contain a word like "retry".
            if attempt.failure_code in IMMEDIATE_PROVIDER_PAUSE_CODES:
                from . import pause as pause_mod

                provider_label = "Claude Code" if self.provider == "claude_code" else "Codex"
                current = pause_mod.current_state(self.conn)
                if current.state == pause_mod.STATE_ACTIVE:
                    pause_mod.request_pause(
                        self.conn,
                        reason=f"{provider_label} provider health failed closed",
                        source=pause_mod.SOURCE_AUTOMATIC_HEALTH_RULE,
                        target_state=pause_mod.STATE_PAUSED_PROVIDER_HEALTH,
                        clock=self.clock,
                    )
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
