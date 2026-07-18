"""Deterministic research orchestration (docs/milestone-5.md Step 12).

`analyze_with_research_committee` is the only place that calls a
`ResearchModelProvider`. It never constructs an order, never sizes a
position, and never mutates broker/ledger state — its output is a single
immutable `ResearchDecision` (or an explicit ANALYSIS_INCOMPLETE outcome)
that `overlay.py` and the existing recommendation builder consume
afterward. See docs/adr/0003-claude-research-boundary.md.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from .claim_validation import classify_claim_rejection_reason, validate_decision, validate_role_report
from .configuration import MANAGER_ROLE, ResearchConfiguration
from .errors import (
    EvidenceValidationError,
    MalformedOutputError,
    ManagerNotConfiguredError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUnavailableError,
    SchemaValidationError,
)
from .evidence_validation import validate_snapshot_preconditions
from .failure_taxonomy import (
    CODE_BUDGET_EXHAUSTED,
    CODE_MANAGER_NOT_INVOKED,
    CODE_MISSING_BEAR_CASE,
    CODE_MISSING_REQUIRED_ROLE,
    CODE_RETRY_EXHAUSTED,
    CODE_SCHEMA_REQUIRED_FIELD_MISSING,
    CODE_UNSUPPORTED_MATERIAL_CLAIM,
    STAGE_BUDGET_GATED,
    STAGE_CLAIM_EVIDENCE_VALIDATION,
    STAGE_MANAGER_SKIPPED,
    STAGE_REQUIRED_ROLE_FAILED,
    STAGE_RETRY_EXHAUSTED,
    STAGE_STRUCTURED_SCHEMA,
    ResearchValidationFailure,
    build_retry_feedback,
    new_failure,
)
from .models import EvidenceSnapshot, ResearchDecision, RoleResearchReport, UsageRecord
from .output_validation import build_decision, build_role_report, decision_json_schema, role_report_json_schema
from .prompt_registry import PromptRegistry
from .prompts import build_system_prompt, build_user_prompt
from .provider_protocol import ResearchModelProvider
from .models import ResearchModelRequest

RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_COMPLETED = "COMPLETED"
RUN_STATUS_ANALYSIS_INCOMPLETE = "ANALYSIS_INCOMPLETE"
RUN_STATUS_FAILED = "FAILED"
# Analyst-only/diagnostic terminal status (fix for the pre-existing "manager invoked
# unconditionally" issue): every configured analyst role produced a valid report, no
# manager role was configured, and the caller explicitly opted into this
# (`require_decision=False`) — a legitimate success state, never conflated with
# ANALYSIS_INCOMPLETE (which means something failed) or COMPLETED (which implies a
# decision exists).
RUN_STATUS_ANALYST_REPORTS_COMPLETE_NO_MANAGER = "ANALYST_REPORTS_COMPLETE_NO_MANAGER"

_RETRYABLE_ERRORS = (ProviderTimeoutError, ProviderRateLimitError, ProviderTransientError, MalformedOutputError, SchemaValidationError)
_ROLE_OUTPUT_ERRORS = (SchemaValidationError, EvidenceValidationError)


@dataclass(frozen=True)
class ResearchAttemptRecord:
    attempt_id: str
    research_run_id: str
    role: str
    attempt_number: int
    prompt_name: str
    prompt_version: str
    prompt_hash: str
    system_prompt_hash: str
    schema_version: str
    provider: str
    model_name: str
    success: bool
    failure_reason: str | None
    raw_response_json: dict | None
    validated_payload_json: dict | None
    usage: UsageRecord
    created_at: datetime


@dataclass(frozen=True)
class AttemptControlRequest:
    """Framework-neutral description of one about-to-happen (or just-completed)
    provider attempt (docs/milestone-7.1.md Step 12). Deliberately carries no
    `shadow`/SQLite/budget-specific type — any `ResearchAttemptController`
    implementation (a shadow-budget adapter, a rate limiter, a no-op) reads
    only these framework-neutral fields."""

    research_run_id: str
    symbol: str
    role: str
    attempt_number: int
    model_name: str
    prompt_version: str
    prompt_hash: str
    max_input_tokens: int | None
    max_output_tokens: int
    requested_at: datetime


@dataclass(frozen=True)
class AttemptControlDecision:
    allowed: bool
    code: str
    reason: str | None = None


class ResearchAttemptController(Protocol):
    """Optional, backward-compatible extension point (docs/milestone-7.1.md
    Step 12). `research/orchestration.py` never imports `shadow` — a shadow-
    specific implementation of this Protocol lives in
    `shadow/attempt_controller.py` and is injected by the caller. Default
    behavior (no controller supplied) is unchanged from every prior
    milestone: every attempt proceeds, `after_attempt` is never called."""

    def before_attempt(self, request: AttemptControlRequest) -> AttemptControlDecision: ...

    def after_attempt(self, request: AttemptControlRequest, attempt: "ResearchAttemptRecord") -> None: ...


@dataclass(frozen=True)
class OrchestrationResult:
    research_run_id: str
    snapshot_id: str
    status: str
    decision: ResearchDecision | None
    role_reports: tuple[RoleResearchReport, ...]
    attempts: tuple[ResearchAttemptRecord, ...]
    incomplete_reasons: tuple[str, ...]
    reused_existing_run: bool
    failures: tuple[ResearchValidationFailure, ...] = ()


class ResearchRepository(Protocol):
    """Storage boundary the orchestrator depends on. The concrete SQLite
    implementation lives in `storage/research_repositories.py` and satisfies
    this Protocol structurally — `research/` never imports `storage/`."""

    def get_run_status(self, research_run_id: str) -> str | None: ...
    def get_decision_for_run(self, research_run_id: str) -> ResearchDecision | None: ...
    def get_role_reports_for_run(self, research_run_id: str) -> tuple[RoleResearchReport, ...]: ...
    def save_run_started(self, research_run_id: str, snapshot_id: str, provider: str, model_name: str, roles: tuple[str, ...], run_mode: str, config_hash: str, created_at: datetime) -> None: ...
    def save_attempt(self, attempt: ResearchAttemptRecord) -> None: ...
    def save_attempt_failures(self, failures: "tuple[ResearchValidationFailure, ...]") -> None: ...
    def list_run_failures(self, research_run_id: str) -> "tuple[ResearchValidationFailure, ...]": ...
    def save_role_report(self, report: RoleResearchReport, attempt_id: str, created_at: datetime) -> None: ...
    def save_decision(self, decision: ResearchDecision, created_at: datetime) -> None: ...
    def mark_run_finished(self, research_run_id: str, status: str, completed_at: datetime) -> None: ...


def compute_research_run_id(
    *,
    snapshot_id: str,
    provider_name: str,
    model_name: str,
    roles: tuple[str, ...],
    prompt_registry: PromptRegistry,
    run_mode: str,
    config_hash: str,
) -> str:
    """Deterministic identity: same snapshot + provider + model + prompt
    versions/hashes + roles + run_mode + config always produces the same
    research_run_id, which is exactly what lets the orchestrator reuse an
    existing completed run instead of calling the provider again — and what
    makes a prompt-version bump (different hash) create a *new* run."""
    prompts = {role: {"version": prompt_registry.get(role).version, "hash": prompt_registry.get(role).text_hash} for role in roles}
    payload = {
        "snapshot_id": snapshot_id, "provider": provider_name, "model_name": model_name,
        "roles": list(roles), "run_mode": run_mode, "config_hash": config_hash,
        "prompts": prompts, "system_prompt_hash": prompt_registry.system_prompt_hash(),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"run-{digest[:32]}"


def _schema_for_role(role: str, configuration: ResearchConfiguration) -> dict:
    if role == MANAGER_ROLE:
        return decision_json_schema(max_claims=configuration.max_claims_per_role)
    return role_report_json_schema(max_claims=configuration.max_claims_per_role)


def _attempt_id(research_run_id: str, role: str, attempt_number: int) -> str:
    return f"{research_run_id}-{role}-{attempt_number}"


def _failure_from_exc(
    exc: Exception, *, research_run_id: str, attempt_id: str, role: str, attempt_number: int,
    model_name: str, prompt_version: str, schema_version: str, occurred_at: datetime,
) -> ResearchValidationFailure:
    """Builds one structured failure from any `research/errors.py::ResearchError` — every
    such error now carries `stage`/`code`/`metadata` (Step 7), so this is a direct,
    lossless translation, not a best-effort text classification."""
    return new_failure(
        research_run_id=research_run_id, attempt_id=attempt_id, role=role, attempt_number=attempt_number,
        stage=getattr(exc, "stage", "UNKNOWN"), code=getattr(exc, "code", "UNCLASSIFIED_VALIDATION_FAILURE"),
        message=str(exc), model_name=model_name, prompt_version=prompt_version, schema_version=schema_version,
        occurred_at=occurred_at, field_path=getattr(exc, "field_path", None),
        claim_id=getattr(exc, "claim_id", None), evidence_ids=getattr(exc, "evidence_ids", ()),
        retryable=getattr(exc, "retryable", False), metadata=getattr(exc, "metadata", {}),
    )


def _claim_validation_failures(
    validation, *, research_run_id: str, attempt_id: str, role: str, attempt_number: int,
    model_name: str, prompt_version: str, schema_version: str, occurred_at: datetime,
) -> list[ResearchValidationFailure]:
    """Persists every independently rejected claim (Step 9: "Do not collapse multiple
    claim failures into one generic error") — including a claim whose importance was too
    low to invalidate the whole report, which the pre-Milestone-6.1 code silently dropped
    (see the M6.1 scratchpad's "Historical persistence findings")."""
    failures: list[ResearchValidationFailure] = []
    for claim, reasons in validation.rejected_claims:
        for reason in reasons:
            failures.append(new_failure(
                research_run_id=research_run_id, attempt_id=attempt_id, role=role, attempt_number=attempt_number,
                stage=STAGE_CLAIM_EVIDENCE_VALIDATION, code=classify_claim_rejection_reason(reason),
                message=reason, claim_id=claim.claim_id, evidence_ids=claim.evidence_ids, retryable=True,
                model_name=model_name, prompt_version=prompt_version, schema_version=schema_version,
                occurred_at=occurred_at,
            ))
    for reason in getattr(validation, "consistency_reasons", ()):
        failures.append(new_failure(
            research_run_id=research_run_id, attempt_id=attempt_id, role=role, attempt_number=attempt_number,
            stage=STAGE_CLAIM_EVIDENCE_VALIDATION, code=classify_claim_rejection_reason(reason),
            message=reason, retryable=True, model_name=model_name, prompt_version=prompt_version,
            schema_version=schema_version, occurred_at=occurred_at,
        ))
    if validation.material_claim_unsupported and not failures:
        # The report/decision-level "rating disagrees with unresolved missing_data_reasons"
        # check tripped with no single claim to blame — still a real, queryable failure.
        failures.append(new_failure(
            research_run_id=research_run_id, attempt_id=attempt_id, role=role, attempt_number=attempt_number,
            stage=STAGE_CLAIM_EVIDENCE_VALIDATION, code=CODE_UNSUPPORTED_MATERIAL_CLAIM,
            message="report/decision rating is inconsistent with unresolved missing_data_reasons",
            retryable=True, model_name=model_name, prompt_version=prompt_version, schema_version=schema_version,
            occurred_at=occurred_at,
        ))
    return failures


def _run_role_with_retries(
    *,
    role: str,
    research_run_id: str,
    snapshot: EvidenceSnapshot,
    provider: ResearchModelProvider,
    provider_name: str,
    model_name: str,
    prompt_registry: PromptRegistry,
    configuration: ResearchConfiguration,
    clock: Callable[[], datetime],
    role_reports_for_manager: tuple[RoleResearchReport, ...] = (),
    attempt_controller: "ResearchAttemptController | None" = None,
) -> tuple[RoleResearchReport | ResearchDecision | None, list[ResearchAttemptRecord], list[ResearchValidationFailure]]:
    attempts: list[ResearchAttemptRecord] = []
    all_failures: list[ResearchValidationFailure] = []
    validation_feedback: tuple[str, ...] = ()
    schema = _schema_for_role(role, configuration)
    allowed_evidence_ids = tuple(sorted(snapshot.evidence_ids()))

    for attempt_number in range(1, configuration.max_attempts_per_role + 1):
        prompt_def = prompt_registry.get(role)
        system_prompt = build_system_prompt(prompt_def)
        user_prompt = build_user_prompt(
            snapshot, json_schema=schema, max_input_characters=configuration.max_input_characters,
            validation_feedback=validation_feedback, role_reports=role_reports_for_manager,
        )
        request = ResearchModelRequest(
            role=role, research_run_id=research_run_id, snapshot=snapshot,
            system_prompt=system_prompt, user_prompt=user_prompt, json_schema=schema,
            model_name=model_name, max_output_tokens=configuration.max_output_tokens, temperature=0.0,
            prompt_name=prompt_def.name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
            system_prompt_hash=prompt_registry.system_prompt_hash(), schema_version=prompt_def.schema_version,
            attempt_number=attempt_number, validation_feedback=validation_feedback,
        )
        attempt_id = _attempt_id(research_run_id, role, attempt_number)
        created_at = clock()
        attempt_failures: list[ResearchValidationFailure] = []

        control_request: "AttemptControlRequest | None" = None
        if attempt_controller is not None:
            control_request = AttemptControlRequest(
                research_run_id=research_run_id, symbol=snapshot.symbol, role=role, attempt_number=attempt_number,
                model_name=model_name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
                max_input_tokens=None, max_output_tokens=configuration.max_output_tokens, requested_at=created_at,
            )
            control_decision = attempt_controller.before_attempt(control_request)
            if not control_decision.allowed:
                # No provider call — a budget/role-eligibility denial is
                # structurally distinct from a provider failure (Step 12/13:
                # "denial is not classified as provider failure"). Not
                # retryable within this role invocation: the same denial
                # would recur identically on the next attempt_number.
                gate_failure = new_failure(
                    research_run_id=research_run_id, attempt_id=attempt_id, role=role, attempt_number=attempt_number,
                    stage=STAGE_BUDGET_GATED, code=CODE_BUDGET_EXHAUSTED,
                    message=control_decision.reason or f"attempt gated: {control_decision.code}",
                    retryable=False, model_name=model_name, prompt_version=prompt_def.version,
                    schema_version=prompt_def.schema_version, occurred_at=created_at,
                )
                all_failures.append(gate_failure)
                attempts.append(ResearchAttemptRecord(
                    attempt_id=attempt_id, research_run_id=research_run_id, role=role, attempt_number=attempt_number,
                    prompt_name=prompt_def.name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
                    system_prompt_hash=prompt_registry.system_prompt_hash(), schema_version=prompt_def.schema_version,
                    provider=provider_name, model_name=model_name, success=False,
                    failure_reason=f"attempt gated: {control_decision.reason or control_decision.code}",
                    raw_response_json=None, validated_payload_json=None,
                    usage=_unavailable_usage(provider_name, model_name, role, attempt_number), created_at=created_at,
                ))
                break

        try:
            response = provider.generate_structured(request)
        except ProviderUnavailableError as exc:
            attempt_failures = [_failure_from_exc(
                exc, research_run_id=research_run_id, attempt_id=attempt_id, role=role,
                attempt_number=attempt_number, model_name=model_name, prompt_version=prompt_def.version,
                schema_version=prompt_def.schema_version, occurred_at=created_at,
            )]
            all_failures.extend(attempt_failures)
            attempts.append(ResearchAttemptRecord(
                attempt_id=attempt_id, research_run_id=research_run_id, role=role, attempt_number=attempt_number,
                prompt_name=prompt_def.name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
                system_prompt_hash=prompt_registry.system_prompt_hash(), schema_version=prompt_def.schema_version,
                provider=provider_name, model_name=model_name, success=False, failure_reason=f"provider unavailable: {exc}",
                raw_response_json=None, validated_payload_json=None,
                usage=_unavailable_usage(provider_name, model_name, role, attempt_number, exc), created_at=created_at,
            ))
            if attempt_controller is not None and control_request is not None:
                attempt_controller.after_attempt(control_request, attempts[-1])
            break  # not retryable — provider itself is not usable
        except _RETRYABLE_ERRORS as exc:
            if isinstance(exc, SchemaValidationError) and exc.schema_errors:
                attempt_failures = [
                    new_failure(
                        research_run_id=research_run_id, attempt_id=attempt_id, role=role,
                        attempt_number=attempt_number, stage=se["stage"], code=se["code"], message=se["message"],
                        field_path=se["field_path"], retryable=True, model_name=model_name,
                        prompt_version=prompt_def.version, schema_version=prompt_def.schema_version,
                        occurred_at=created_at,
                    )
                    for se in exc.schema_errors
                ]
            else:
                attempt_failures = [_failure_from_exc(
                    exc, research_run_id=research_run_id, attempt_id=attempt_id, role=role,
                    attempt_number=attempt_number, model_name=model_name, prompt_version=prompt_def.version,
                    schema_version=prompt_def.schema_version, occurred_at=created_at,
                )]
            all_failures.extend(attempt_failures)
            attempts.append(ResearchAttemptRecord(
                attempt_id=attempt_id, research_run_id=research_run_id, role=role, attempt_number=attempt_number,
                prompt_name=prompt_def.name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
                system_prompt_hash=prompt_registry.system_prompt_hash(), schema_version=prompt_def.schema_version,
                provider=provider_name, model_name=model_name, success=False, failure_reason=str(exc),
                raw_response_json=None, validated_payload_json=None,
                usage=_unavailable_usage(provider_name, model_name, role, attempt_number, exc), created_at=created_at,
            ))
            if attempt_controller is not None and control_request is not None:
                attempt_controller.after_attempt(control_request, attempts[-1])
            validation_feedback = build_retry_feedback(tuple(attempt_failures), allowed_evidence_ids=allowed_evidence_ids)
            continue

        try:
            if role == MANAGER_ROLE:
                built = build_decision(
                    dict(response.parsed_json), decision_id=f"{research_run_id}-decision",
                    research_run_id=research_run_id, symbol=snapshot.symbol, snapshot_id=snapshot.snapshot_id,
                    model_name=model_name, prompt_version=prompt_def.version, schema=schema,
                )
                validation = validate_decision(built, snapshot)
            else:
                built = build_role_report(
                    dict(response.parsed_json), report_id=attempt_id, research_run_id=research_run_id,
                    role=role, symbol=snapshot.symbol, snapshot_id=snapshot.snapshot_id,
                    model_name=model_name, prompt_version=prompt_def.version, schema=schema,
                )
                validation = validate_role_report(built, snapshot)
        except _ROLE_OUTPUT_ERRORS as exc:
            # Milestone 6.1 application-bug fix (see the M6.1 scratchpad's "Historical
            # persistence findings" #3-equivalent): `EvidenceValidationError` — e.g. a
            # manager decision whose `bear_case`/`bull_case` is empty, which passes JSON
            # Schema (no `minLength`) but fails the `ResearchDecision` dataclass's own
            # invariant — used to propagate uncaught out of this function and crash the
            # whole committee run instead of being treated as a retryable, classified
            # validation failure like every other structured-output problem.
            if isinstance(exc, SchemaValidationError) and exc.schema_errors:
                attempt_failures = [
                    new_failure(
                        research_run_id=research_run_id, attempt_id=attempt_id, role=role,
                        attempt_number=attempt_number, stage=se["stage"], code=se["code"], message=se["message"],
                        field_path=se["field_path"], retryable=True, model_name=model_name,
                        prompt_version=prompt_def.version, schema_version=prompt_def.schema_version,
                        occurred_at=created_at,
                    )
                    for se in exc.schema_errors
                ]
            elif isinstance(exc, EvidenceValidationError):
                message = str(exc)
                if "bear_case" in message:
                    code, field_path = CODE_MISSING_BEAR_CASE, "bear_case"
                elif "bull_case" in message:
                    code, field_path = CODE_SCHEMA_REQUIRED_FIELD_MISSING, "bull_case"
                else:
                    code, field_path = "UNCLASSIFIED_VALIDATION_FAILURE", None
                attempt_failures = [new_failure(
                    research_run_id=research_run_id, attempt_id=attempt_id, role=role,
                    attempt_number=attempt_number, stage=STAGE_STRUCTURED_SCHEMA, code=code, message=message,
                    field_path=field_path, retryable=True, model_name=model_name, prompt_version=prompt_def.version,
                    schema_version=prompt_def.schema_version, occurred_at=created_at,
                )]
            else:
                attempt_failures = [_failure_from_exc(
                    exc, research_run_id=research_run_id, attempt_id=attempt_id, role=role,
                    attempt_number=attempt_number, model_name=model_name, prompt_version=prompt_def.version,
                    schema_version=prompt_def.schema_version, occurred_at=created_at,
                )]
            all_failures.extend(attempt_failures)
            attempts.append(ResearchAttemptRecord(
                attempt_id=attempt_id, research_run_id=research_run_id, role=role, attempt_number=attempt_number,
                prompt_name=prompt_def.name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
                system_prompt_hash=prompt_registry.system_prompt_hash(), schema_version=prompt_def.schema_version,
                provider=provider_name, model_name=model_name, success=False, failure_reason=str(exc),
                raw_response_json=dict(response.parsed_json), validated_payload_json=None,
                usage=response.usage, created_at=created_at,
            ))
            if attempt_controller is not None and control_request is not None:
                attempt_controller.after_attempt(control_request, attempts[-1])
            validation_feedback = build_retry_feedback(tuple(attempt_failures), allowed_evidence_ids=allowed_evidence_ids)
            continue

        claim_failures = _claim_validation_failures(
            validation, research_run_id=research_run_id, attempt_id=attempt_id, role=role,
            attempt_number=attempt_number, model_name=model_name, prompt_version=prompt_def.version,
            schema_version=prompt_def.schema_version, occurred_at=created_at,
        )
        all_failures.extend(claim_failures)

        if not validation.is_valid:
            reasons = tuple(f.message for f in claim_failures) or ("claim validation failed",)
            attempts.append(ResearchAttemptRecord(
                attempt_id=attempt_id, research_run_id=research_run_id, role=role, attempt_number=attempt_number,
                prompt_name=prompt_def.name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
                system_prompt_hash=prompt_registry.system_prompt_hash(), schema_version=prompt_def.schema_version,
                provider=provider_name, model_name=model_name, success=False,
                failure_reason="claim validation failed: " + "; ".join(reasons),
                raw_response_json=dict(response.parsed_json), validated_payload_json=None,
                usage=response.usage, created_at=created_at,
            ))
            if attempt_controller is not None and control_request is not None:
                attempt_controller.after_attempt(control_request, attempts[-1])
            validation_feedback = build_retry_feedback(tuple(claim_failures), allowed_evidence_ids=allowed_evidence_ids)
            continue

        attempts.append(ResearchAttemptRecord(
            attempt_id=attempt_id, research_run_id=research_run_id, role=role, attempt_number=attempt_number,
            prompt_name=prompt_def.name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
            system_prompt_hash=prompt_registry.system_prompt_hash(), schema_version=prompt_def.schema_version,
            provider=provider_name, model_name=model_name, success=True, failure_reason=None,
            raw_response_json=dict(response.parsed_json), validated_payload_json=dict(response.parsed_json),
            usage=response.usage, created_at=created_at,
        ))
        if attempt_controller is not None and control_request is not None:
            attempt_controller.after_attempt(control_request, attempts[-1])
        return built, attempts, all_failures

    if configuration.max_attempts_per_role >= 1:
        last_attempt_number = attempts[-1].attempt_number if attempts else configuration.max_attempts_per_role
        prompt_def = prompt_registry.get(role)
        all_failures.append(new_failure(
            research_run_id=research_run_id, attempt_id=_attempt_id(research_run_id, role, last_attempt_number),
            role=role, attempt_number=last_attempt_number, stage=STAGE_RETRY_EXHAUSTED, code=CODE_RETRY_EXHAUSTED,
            message=f"role {role!r} exhausted {len(attempts)} attempt(s) without a valid structured report",
            retryable=False, model_name=model_name, prompt_version=prompt_def.version,
            schema_version=prompt_def.schema_version, occurred_at=clock(),
            metadata={"attempts_made": len(attempts)},
        ))
    return None, attempts, all_failures


def _unavailable_usage(provider: str, model_name: str, role: str, attempt_number: int, exc: Exception | None = None) -> UsageRecord:
    """`latency_ms` is recovered from the raising exception's structured metadata when
    present (Step 7) — a provider-boundary failure still measured real wall-clock time
    even though it produced no `ResearchModelResponse`."""
    from .usage import build_usage_record

    latency_ms = getattr(exc, "metadata", {}).get("latency_ms") if exc is not None else None
    return build_usage_record(
        provider=provider, model_name=model_name, role=role, input_tokens=None, output_tokens=None,
        cache_read_tokens=None, cache_write_tokens=None, latency_ms=latency_ms, provider_request_id=None,
        retry_count=attempt_number - 1, success=False,
        cost_estimate_basis="USAGE_NOT_RETURNED" if provider == "claude_code" else None,
    )


def analyze_with_research_committee(
    snapshot: EvidenceSnapshot,
    *,
    provider: ResearchModelProvider,
    provider_name: str,
    model_name: str,
    prompt_registry: PromptRegistry,
    research_repository: ResearchRepository | None,
    configuration: ResearchConfiguration,
    clock: Callable[[], datetime],
    run_mode: str,
    require_decision: bool = True,
    attempt_controller: "ResearchAttemptController | None" = None,
) -> OrchestrationResult:
    """`attempt_controller` (docs/milestone-7.1.md Step 12, default `None` =
    every prior milestone's exact behavior): an optional
    `ResearchAttemptController` consulted before every analyst and manager
    provider attempt, including retries. `research/orchestration.py` never
    imports `shadow` — a shadow-budget-aware implementation is injected by
    the caller (see `shadow/attempt_controller.py`).

    `require_decision` (default `True`, preserving fail-closed production behavior):
    when `True` and `configuration.roles` does not include the manager role, raises
    `ManagerNotConfiguredError` immediately — before any provider call — rather than
    silently returning something that looks like a decision or making analyst-only calls
    that could never produce one. Pass `require_decision=False` for an intentional
    analyst-only/diagnostic run; in that case the manager is never invoked at all, and a
    successful run returns `RUN_STATUS_ANALYST_REPORTS_COMPLETE_NO_MANAGER` with
    `decision=None` rather than a fabricated `ResearchDecision`. When the manager role
    *is* present in `configuration.roles`, `require_decision` has no effect — production
    behavior (manager invoked once every analyst role succeeds) is unchanged."""
    roles = configuration.roles
    analyst_roles = tuple(r for r in roles if r != MANAGER_ROLE)
    manager_configured = MANAGER_ROLE in roles

    if require_decision and not manager_configured:
        raise ManagerNotConfiguredError(
            f"analyze_with_research_committee requires a final decision (require_decision=True, "
            f"the default) but {MANAGER_ROLE!r} is not present in configuration.roles={roles!r}. "
            f"Add 'manager' to roles for a production run, or pass require_decision=False for an "
            f"intentional analyst-only/diagnostic run."
        )

    research_run_id = compute_research_run_id(
        snapshot_id=snapshot.snapshot_id, provider_name=provider_name, model_name=model_name,
        roles=roles, prompt_registry=prompt_registry, run_mode=run_mode, config_hash=configuration.config_hash,
    )

    if research_repository is not None:
        existing_status = research_repository.get_run_status(research_run_id)
        if existing_status == RUN_STATUS_COMPLETED:
            decision = research_repository.get_decision_for_run(research_run_id)
            reports = research_repository.get_role_reports_for_run(research_run_id)
            return OrchestrationResult(
                research_run_id=research_run_id, snapshot_id=snapshot.snapshot_id, status=RUN_STATUS_COMPLETED,
                decision=decision, role_reports=reports, attempts=(), incomplete_reasons=(), reused_existing_run=True,
            )
        if existing_status == RUN_STATUS_ANALYSIS_INCOMPLETE:
            reports = research_repository.get_role_reports_for_run(research_run_id)
            return OrchestrationResult(
                research_run_id=research_run_id, snapshot_id=snapshot.snapshot_id,
                status=RUN_STATUS_ANALYSIS_INCOMPLETE, decision=None, role_reports=reports, attempts=(),
                incomplete_reasons=("reused a previously completed ANALYSIS_INCOMPLETE run",), reused_existing_run=True,
            )
        if existing_status == RUN_STATUS_ANALYST_REPORTS_COMPLETE_NO_MANAGER:
            reports = research_repository.get_role_reports_for_run(research_run_id)
            return OrchestrationResult(
                research_run_id=research_run_id, snapshot_id=snapshot.snapshot_id,
                status=RUN_STATUS_ANALYST_REPORTS_COMPLETE_NO_MANAGER, decision=None, role_reports=reports,
                attempts=(), incomplete_reasons=(), reused_existing_run=True,
            )

    preflight_reasons = validate_snapshot_preconditions(
        snapshot, require_point_in_time_safe=configuration.require_point_in_time_safe,
        fail_on_stale_required_evidence=configuration.fail_on_stale_required_evidence,
    )
    started_at = clock()
    if research_repository is not None and research_repository.get_run_status(research_run_id) is None:
        research_repository.save_run_started(
            research_run_id, snapshot.snapshot_id, provider_name, model_name, roles, run_mode,
            configuration.config_hash, started_at,
        )

    if preflight_reasons:
        if research_repository is not None:
            research_repository.mark_run_finished(research_run_id, RUN_STATUS_ANALYSIS_INCOMPLETE, clock())
        return OrchestrationResult(
            research_run_id=research_run_id, snapshot_id=snapshot.snapshot_id,
            status=RUN_STATUS_ANALYSIS_INCOMPLETE, decision=None, role_reports=(), attempts=(),
            incomplete_reasons=preflight_reasons, reused_existing_run=False,
        )

    all_attempts: list[ResearchAttemptRecord] = []
    all_failures: list[ResearchValidationFailure] = []
    valid_reports: list[RoleResearchReport] = []
    incomplete_reasons: list[str] = []
    failed_required_roles: list[str] = []

    for role in analyst_roles:
        existing_reports = (
            research_repository.get_role_reports_for_run(research_run_id) if research_repository is not None else ()
        )
        already_done = next((r for r in existing_reports if r.role == role), None)
        if already_done is not None:
            valid_reports.append(already_done)
            continue

        report, attempts, failures = _run_role_with_retries(
            role=role, research_run_id=research_run_id, snapshot=snapshot, provider=provider,
            provider_name=provider_name, model_name=model_name, prompt_registry=prompt_registry,
            configuration=configuration, clock=clock, attempt_controller=attempt_controller,
        )
        all_attempts.extend(attempts)
        all_failures.extend(failures)
        for a in attempts:
            if research_repository is not None:
                research_repository.save_attempt(a)
        if research_repository is not None and failures:
            research_repository.save_attempt_failures(tuple(failures))
        if report is None:
            incomplete_reasons.append(f"role {role!r} exhausted retries without a valid report")
            failed_required_roles.append(role)
            prompt_def = prompt_registry.get(role)
            required_role_failure = new_failure(
                research_run_id=research_run_id, attempt_id=f"{research_run_id}-{role}-required-role-failed",
                role=role, attempt_number=configuration.max_attempts_per_role, stage=STAGE_REQUIRED_ROLE_FAILED,
                code=CODE_MISSING_REQUIRED_ROLE,
                message=f"required analyst role {role!r} produced no valid report after retry exhaustion",
                retryable=False, model_name=model_name, prompt_version=prompt_def.version,
                schema_version=prompt_def.schema_version, occurred_at=clock(),
            )
            all_failures.append(required_role_failure)
            if research_repository is not None:
                research_repository.save_attempt_failures((required_role_failure,))
            continue
        valid_reports.append(report)
        if research_repository is not None:
            research_repository.save_role_report(report, attempts[-1].attempt_id, clock())

    if incomplete_reasons:
        # The manager is never invoked when a required analyst role failed — persist why,
        # not just the fact that it happened (Step 10: "The manager-skip record should
        # identify which required role failed, ... why invoking the manager would have
        # violated orchestration policy"). Only persisted when a manager was actually
        # configured: a "manager skipped" record implies one would otherwise have been
        # invoked, which is not true for an analyst-only/diagnostic run that never
        # configured a manager role at all (fabricating this record would be exactly the
        # kind of misleading diagnostic data this fix exists to prevent).
        if manager_configured:
            manager_skip_failure = new_failure(
                research_run_id=research_run_id, attempt_id=f"{research_run_id}-manager-skipped", role=MANAGER_ROLE,
                attempt_number=1, stage=STAGE_MANAGER_SKIPPED, code=CODE_MANAGER_NOT_INVOKED,
                message=(
                    f"manager not invoked: required role(s) {sorted(failed_required_roles)} failed — invoking the "
                    "manager with incomplete required-role input would violate orchestration policy"
                ),
                retryable=False, model_name=model_name, prompt_version="n/a", schema_version="n/a",
                occurred_at=clock(), metadata={"blocking_role_count": len(failed_required_roles)},
            )
            all_failures.append(manager_skip_failure)
            if research_repository is not None:
                research_repository.save_attempt_failures((manager_skip_failure,))
        if research_repository is not None:
            research_repository.mark_run_finished(research_run_id, RUN_STATUS_ANALYSIS_INCOMPLETE, clock())
        return OrchestrationResult(
            research_run_id=research_run_id, snapshot_id=snapshot.snapshot_id,
            status=RUN_STATUS_ANALYSIS_INCOMPLETE, decision=None, role_reports=tuple(valid_reports),
            attempts=tuple(all_attempts), incomplete_reasons=tuple(incomplete_reasons), reused_existing_run=False,
            failures=tuple(all_failures),
        )

    if not manager_configured:
        # `require_decision` was already checked to be False at the top (otherwise this
        # function would have raised before any provider call was ever made) — every
        # configured analyst role produced a valid report, and there is no manager to
        # invoke. This is a legitimate terminal success state, not an error and not
        # ANALYSIS_INCOMPLETE: no manager call is made, no ResearchDecision is
        # constructed or fabricated from the analyst reports.
        if research_repository is not None:
            research_repository.mark_run_finished(research_run_id, RUN_STATUS_ANALYST_REPORTS_COMPLETE_NO_MANAGER, clock())
        return OrchestrationResult(
            research_run_id=research_run_id, snapshot_id=snapshot.snapshot_id,
            status=RUN_STATUS_ANALYST_REPORTS_COMPLETE_NO_MANAGER, decision=None, role_reports=tuple(valid_reports),
            attempts=tuple(all_attempts), incomplete_reasons=(), reused_existing_run=False,
            failures=tuple(all_failures),
        )

    decision, manager_attempts, manager_failures = _run_role_with_retries(
        role=MANAGER_ROLE, research_run_id=research_run_id, snapshot=snapshot, provider=provider,
        provider_name=provider_name, model_name=model_name, prompt_registry=prompt_registry,
        configuration=configuration, clock=clock, role_reports_for_manager=tuple(valid_reports),
        attempt_controller=attempt_controller,
    )
    all_attempts.extend(manager_attempts)
    all_failures.extend(manager_failures)
    for a in manager_attempts:
        if research_repository is not None:
            research_repository.save_attempt(a)
    if research_repository is not None and manager_failures:
        research_repository.save_attempt_failures(tuple(manager_failures))

    if decision is None:
        if research_repository is not None:
            research_repository.mark_run_finished(research_run_id, RUN_STATUS_ANALYSIS_INCOMPLETE, clock())
        return OrchestrationResult(
            research_run_id=research_run_id, snapshot_id=snapshot.snapshot_id,
            status=RUN_STATUS_ANALYSIS_INCOMPLETE, decision=None, role_reports=tuple(valid_reports),
            attempts=tuple(all_attempts), incomplete_reasons=("manager exhausted retries without a valid decision",),
            reused_existing_run=False, failures=tuple(all_failures),
        )

    assert isinstance(decision, ResearchDecision)
    if research_repository is not None:
        research_repository.save_decision(decision, clock())
        final_status = RUN_STATUS_ANALYSIS_INCOMPLETE if decision.rating == "ANALYSIS_INCOMPLETE" else RUN_STATUS_COMPLETED
        research_repository.mark_run_finished(research_run_id, final_status, clock())

    status = RUN_STATUS_ANALYSIS_INCOMPLETE if decision.rating == "ANALYSIS_INCOMPLETE" else RUN_STATUS_COMPLETED
    return OrchestrationResult(
        research_run_id=research_run_id, snapshot_id=snapshot.snapshot_id, status=status, decision=decision,
        role_reports=tuple(valid_reports), attempts=tuple(all_attempts), incomplete_reasons=(), reused_existing_run=False,
        failures=tuple(all_failures),
    )
