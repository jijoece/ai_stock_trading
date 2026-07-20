"""Operational token-budget wrapper for the research provider boundary."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..storage.shadow_operations_repositories import load_budget_reservation
from ..strategies.research_budget import (
    TOKEN_ACCOUNTING_REASONING_SEPARATE,
    TOKEN_RESERVATION_AMBIGUOUS,
    TOKEN_RESERVATION_RESERVED,
    Clock,
    TokenBudgetError,
    TokenBudgetRejected,
    mark_research_tokens_ambiguous,
    release_research_tokens,
    reserve_research_tokens,
    settle_research_tokens,
)
from .errors import ProviderUnavailableError


@dataclass
class PersistentResearchTokenBudgetController:
    """Reserve, invoke, and resolve one provider attempt as one operation."""

    conn: Any
    daily_token_cap: int
    maximum_reasoning_tokens: int
    token_accounting_policy: str
    clock: Clock

    @staticmethod
    def _conservative_input_allowance(request: Any) -> int:
        # UTF-8 bytes are a conservative upper bound for token count across
        # supported providers without invoking a model-specific tokenizer.
        return len(request.system_prompt.encode()) + len(request.user_prompt.encode()) + len(
            str(request.json_schema).encode()
        )

    def invoke(self, provider: Any, request: Any, *, provider_name: str | None = None) -> Any:
        try:
            reservation = reserve_research_tokens(
                self.conn, research_run_id=request.research_run_id, symbol=request.snapshot.symbol,
                provider=provider_name or getattr(provider, "provider_name", provider.__class__.__name__),
                model_name=request.model_name,
                estimated_input_tokens=self._conservative_input_allowance(request),
                maximum_output_tokens=request.max_output_tokens,
                maximum_reasoning_tokens=self.maximum_reasoning_tokens,
                daily_token_cap=self.daily_token_cap, clock=self.clock,
                token_accounting_policy=self.token_accounting_policy,
                research_attempt_identity=f"{request.role}:{request.attempt_number}",
            )
        except TokenBudgetError as exc:
            # A reused idempotency key with incompatible immutable metadata
            # (payload mismatch, accounting-policy mismatch) must fail closed
            # without ever reaching the provider (docs/milestones/26.md A5) —
            # never let this propagate as an uncaught crash.
            raise ProviderUnavailableError(str(exc), stage="BUDGET_GATED", code=exc.code, retryable=False) from exc
        if isinstance(reservation, TokenBudgetRejected):
            raise ProviderUnavailableError(
                reservation.reason, stage="BUDGET_GATED", code=reservation.code, retryable=False,
            )
        if reservation.status == TOKEN_RESERVATION_AMBIGUOUS:
            raise ProviderUnavailableError(
                "ambiguous token reservation requires operator reconciliation",
                stage="BUDGET_GATED", code="TOKEN_RESERVATION_RECONCILIATION_REQUIRED", retryable=False,
            )
        if reservation.status != TOKEN_RESERVATION_RESERVED:
            raise ProviderUnavailableError(
                f"token reservation is already {reservation.status}", stage="BUDGET_GATED",
                code="TOKEN_RESERVATION_STATE_CONFLICT", retryable=False,
            )
        try:
            response = provider.generate_structured(request)
        except ProviderUnavailableError:
            # Provider configuration/adapter validation failed before a
            # request could be transmitted.
            release_research_tokens(self.conn, reservation.reservation_id, self.clock)
            raise
        except BaseException:
            # Timeouts, malformed responses, and process interruption can all
            # occur after transmission. Preserve the full hold for explicit
            # operator reconciliation.
            mark_research_tokens_ambiguous(self.conn, reservation.reservation_id)
            raise

        usage = response.usage
        if usage.input_tokens is None or usage.output_tokens is None:
            mark_research_tokens_ambiguous(self.conn, reservation.reservation_id)
            raise ProviderUnavailableError(
                "provider returned no authoritative token usage",
                code="TOKEN_USAGE_EVIDENCE_REQUIRED", retryable=False,
            )
        if usage.token_accounting_policy == TOKEN_ACCOUNTING_REASONING_SEPARATE and usage.reasoning_output_tokens is None:
            # A missing separate reasoning count must never become a
            # fabricated zero under REASONING_SEPARATE accounting
            # (docs/milestones/26.md A3) — the reservation stays counted
            # against the cap pending operator reconciliation.
            mark_research_tokens_ambiguous(self.conn, reservation.reservation_id)
            raise ProviderUnavailableError(
                "REASONING_SEPARATE accounting requires authoritative reasoning-token usage",
                code="TOKEN_USAGE_EVIDENCE_REQUIRED", retryable=False,
            )
        try:
            settle_research_tokens(
                self.conn, reservation.reservation_id,
                actual_input_tokens=usage.input_tokens, actual_output_tokens=usage.output_tokens,
                actual_reasoning_tokens=usage.reasoning_output_tokens or 0,
                token_accounting_policy=usage.token_accounting_policy,
                provider_request_id=usage.provider_request_id, clock=self.clock,
            )
        except TokenBudgetError:
            # A settlement conflict can mean another process already settled
            # this reservation (terminal SETTLED state) rather than a
            # transient persistence failure. Reload and only ever attempt
            # RESERVED -> AMBIGUOUS; never SETTLED -> AMBIGUOUS
            # (docs/milestones/26.md A7).
            reloaded = load_budget_reservation(self.conn, reservation.reservation_id)
            if reloaded is not None and reloaded["status"] == TOKEN_RESERVATION_RESERVED:
                mark_research_tokens_ambiguous(self.conn, reservation.reservation_id)
            raise
        return response
