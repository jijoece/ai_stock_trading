"""Operational token-budget wrapper for the research provider boundary."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..strategies.research_budget import (
    TOKEN_ACCOUNTING_REASONING_SEPARATE,
    TOKEN_RESERVATION_AMBIGUOUS,
    TOKEN_RESERVATION_IN_FLIGHT,
    TOKEN_RESERVATION_RESERVED,
    Clock,
    TokenBudgetError,
    TokenBudgetRejected,
    claim_research_token_attempt,
    mark_research_tokens_ambiguous,
    release_research_tokens,
    reserve_research_tokens,
    settle_research_tokens,
)
from .errors import ProviderUnavailableError

# Milestone 27 A1: bounded lease duration for a provider-attempt claim.
# Deliberately generous relative to any single provider call so a slow
# response never expires mid-flight under normal operation; a claim that
# does expire is never auto-retried (see `research_budget.recover_expired_token_claims`).
DEFAULT_TOKEN_CLAIM_LEASE_SECONDS = 600


@dataclass
class PersistentResearchTokenBudgetController:
    """Reserve, claim, invoke, and resolve one provider attempt as one operation."""

    conn: Any
    daily_token_cap: int
    maximum_reasoning_tokens: int
    token_accounting_policy: str
    clock: Clock
    claim_lease_seconds: int = DEFAULT_TOKEN_CLAIM_LEASE_SECONDS

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
        if reservation.status == TOKEN_RESERVATION_IN_FLIGHT:
            # A reused idempotency key can land here when another attempt is
            # already mid-flight for the exact same reservation. Fails the
            # same way the claim race below does — never invoke the provider
            # on a reservation someone else already owns.
            raise ProviderUnavailableError(
                "a provider attempt is already in flight for this reservation", stage="BUDGET_GATED",
                code="TOKEN_ATTEMPT_ALREADY_IN_FLIGHT", retryable=False,
            )
        if reservation.status != TOKEN_RESERVATION_RESERVED:
            raise ProviderUnavailableError(
                f"token reservation is already {reservation.status}", stage="BUDGET_GATED",
                code="TOKEN_RESERVATION_STATE_CONFLICT", retryable=False,
            )

        # Milestone 27 A1: reservation reuse alone must never authorize a
        # provider call. Only the worker that flips this fenced
        # RESERVED -> IN_FLIGHT claim may invoke the provider; a concurrent
        # worker racing the same reservation gets TOKEN_ATTEMPT_ALREADY_IN_FLIGHT
        # here instead of both reaching `provider.generate_structured`.
        claim_owner = f"attempt-{uuid.uuid4().hex}"
        now = self.clock()
        claim = claim_research_token_attempt(
            self.conn, reservation.reservation_id, claim_owner, now,
            now + timedelta(seconds=self.claim_lease_seconds),
        )
        if claim.status == "ALREADY_IN_FLIGHT":
            raise ProviderUnavailableError(
                "a provider attempt is already in flight for this reservation", stage="BUDGET_GATED",
                code="TOKEN_ATTEMPT_ALREADY_IN_FLIGHT", retryable=False,
            )
        if claim.status != "CLAIMED":
            raise ProviderUnavailableError(
                f"token reservation is already {reservation.status}", stage="BUDGET_GATED",
                code="TOKEN_RESERVATION_STATE_CONFLICT", retryable=False,
            )
        claim_generation = claim.claim_generation

        try:
            response = provider.generate_structured(request)
        except ProviderUnavailableError:
            # Provider configuration/adapter validation failed before a
            # request could be transmitted.
            release_research_tokens(
                self.conn, reservation.reservation_id, self.clock,
                claim_owner=claim_owner, claim_generation=claim_generation,
            )
            raise
        except BaseException:
            # Timeouts, malformed responses, and process interruption can all
            # occur after transmission. Preserve the full hold for explicit
            # operator reconciliation.
            mark_research_tokens_ambiguous(
                self.conn, reservation.reservation_id,
                claim_owner=claim_owner, claim_generation=claim_generation,
            )
            raise

        usage = response.usage
        if usage.input_tokens is None or usage.output_tokens is None:
            mark_research_tokens_ambiguous(
                self.conn, reservation.reservation_id,
                claim_owner=claim_owner, claim_generation=claim_generation,
            )
            raise ProviderUnavailableError(
                "provider returned no authoritative token usage",
                code="TOKEN_USAGE_EVIDENCE_REQUIRED", retryable=False,
            )
        if usage.token_accounting_policy == TOKEN_ACCOUNTING_REASONING_SEPARATE and usage.reasoning_output_tokens is None:
            # A missing separate reasoning count must never become a
            # fabricated zero under REASONING_SEPARATE accounting
            # (docs/milestones/26.md A3) — the reservation stays counted
            # against the cap pending operator reconciliation.
            mark_research_tokens_ambiguous(
                self.conn, reservation.reservation_id,
                claim_owner=claim_owner, claim_generation=claim_generation,
            )
            raise ProviderUnavailableError(
                "REASONING_SEPARATE accounting requires authoritative reasoning-token usage",
                code="TOKEN_USAGE_EVIDENCE_REQUIRED", retryable=False,
            )
        # Fenced on claim_owner/claim_generation (Milestone 27 A1): a stale
        # worker whose lease already expired and was recovered to AMBIGUOUS
        # by another owner is rejected here rather than clobbering the
        # recovered state. Unlike the pre-claim design, no fallback mutation
        # is attempted on failure — the fenced UPDATE itself is the single
        # source of truth for who is allowed to resolve this reservation.
        settle_research_tokens(
            self.conn, reservation.reservation_id,
            actual_input_tokens=usage.input_tokens, actual_output_tokens=usage.output_tokens,
            actual_reasoning_tokens=usage.reasoning_output_tokens or 0,
            token_accounting_policy=usage.token_accounting_policy,
            provider_request_id=usage.provider_request_id, clock=self.clock,
            claim_owner=claim_owner, claim_generation=claim_generation,
        )
        return response
