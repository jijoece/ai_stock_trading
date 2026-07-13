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

from .claim_validation import validate_decision, validate_role_report
from .configuration import MANAGER_ROLE, ResearchConfiguration
from .errors import (
    MalformedOutputError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUnavailableError,
    SchemaValidationError,
)
from .evidence_validation import validate_snapshot_preconditions
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

_RETRYABLE_ERRORS = (ProviderTimeoutError, ProviderRateLimitError, ProviderTransientError, MalformedOutputError, SchemaValidationError)


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
class OrchestrationResult:
    research_run_id: str
    snapshot_id: str
    status: str
    decision: ResearchDecision | None
    role_reports: tuple[RoleResearchReport, ...]
    attempts: tuple[ResearchAttemptRecord, ...]
    incomplete_reasons: tuple[str, ...]
    reused_existing_run: bool


class ResearchRepository(Protocol):
    """Storage boundary the orchestrator depends on. The concrete SQLite
    implementation lives in `storage/research_repositories.py` and satisfies
    this Protocol structurally — `research/` never imports `storage/`."""

    def get_run_status(self, research_run_id: str) -> str | None: ...
    def get_decision_for_run(self, research_run_id: str) -> ResearchDecision | None: ...
    def get_role_reports_for_run(self, research_run_id: str) -> tuple[RoleResearchReport, ...]: ...
    def save_run_started(self, research_run_id: str, snapshot_id: str, provider: str, model_name: str, roles: tuple[str, ...], run_mode: str, config_hash: str, created_at: datetime) -> None: ...
    def save_attempt(self, attempt: ResearchAttemptRecord) -> None: ...
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
) -> tuple[RoleResearchReport | ResearchDecision | None, list[ResearchAttemptRecord]]:
    attempts: list[ResearchAttemptRecord] = []
    validation_feedback: tuple[str, ...] = ()
    schema = _schema_for_role(role, configuration)

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

        try:
            response = provider.generate_structured(request)
        except ProviderUnavailableError as exc:
            attempts.append(ResearchAttemptRecord(
                attempt_id=attempt_id, research_run_id=research_run_id, role=role, attempt_number=attempt_number,
                prompt_name=prompt_def.name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
                system_prompt_hash=prompt_registry.system_prompt_hash(), schema_version=prompt_def.schema_version,
                provider=provider_name, model_name=model_name, success=False, failure_reason=f"provider unavailable: {exc}",
                raw_response_json=None, validated_payload_json=None,
                usage=_unavailable_usage(provider_name, model_name, role, attempt_number), created_at=created_at,
            ))
            break  # not retryable — provider itself is not usable
        except _RETRYABLE_ERRORS as exc:
            attempts.append(ResearchAttemptRecord(
                attempt_id=attempt_id, research_run_id=research_run_id, role=role, attempt_number=attempt_number,
                prompt_name=prompt_def.name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
                system_prompt_hash=prompt_registry.system_prompt_hash(), schema_version=prompt_def.schema_version,
                provider=provider_name, model_name=model_name, success=False, failure_reason=str(exc),
                raw_response_json=None, validated_payload_json=None,
                usage=_unavailable_usage(provider_name, model_name, role, attempt_number), created_at=created_at,
            ))
            validation_feedback = (str(exc),)
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
        except SchemaValidationError as exc:
            attempts.append(ResearchAttemptRecord(
                attempt_id=attempt_id, research_run_id=research_run_id, role=role, attempt_number=attempt_number,
                prompt_name=prompt_def.name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
                system_prompt_hash=prompt_registry.system_prompt_hash(), schema_version=prompt_def.schema_version,
                provider=provider_name, model_name=model_name, success=False, failure_reason=str(exc),
                raw_response_json=dict(response.parsed_json), validated_payload_json=None,
                usage=response.usage, created_at=created_at,
            ))
            validation_feedback = (str(exc),)
            continue

        if not validation.is_valid:
            reasons = tuple(r for _, rs in validation.rejected_claims for r in rs) + getattr(validation, "consistency_reasons", ())
            attempts.append(ResearchAttemptRecord(
                attempt_id=attempt_id, research_run_id=research_run_id, role=role, attempt_number=attempt_number,
                prompt_name=prompt_def.name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
                system_prompt_hash=prompt_registry.system_prompt_hash(), schema_version=prompt_def.schema_version,
                provider=provider_name, model_name=model_name, success=False,
                failure_reason="claim validation failed: " + "; ".join(reasons),
                raw_response_json=dict(response.parsed_json), validated_payload_json=None,
                usage=response.usage, created_at=created_at,
            ))
            validation_feedback = reasons
            continue

        attempts.append(ResearchAttemptRecord(
            attempt_id=attempt_id, research_run_id=research_run_id, role=role, attempt_number=attempt_number,
            prompt_name=prompt_def.name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
            system_prompt_hash=prompt_registry.system_prompt_hash(), schema_version=prompt_def.schema_version,
            provider=provider_name, model_name=model_name, success=True, failure_reason=None,
            raw_response_json=dict(response.parsed_json), validated_payload_json=dict(response.parsed_json),
            usage=response.usage, created_at=created_at,
        ))
        return built, attempts

    return None, attempts


def _unavailable_usage(provider: str, model_name: str, role: str, attempt_number: int) -> UsageRecord:
    from .usage import build_usage_record

    return build_usage_record(
        provider=provider, model_name=model_name, role=role, input_tokens=None, output_tokens=None,
        cache_read_tokens=None, cache_write_tokens=None, latency_ms=None, provider_request_id=None,
        retry_count=attempt_number - 1, success=False,
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
) -> OrchestrationResult:
    roles = configuration.roles
    analyst_roles = tuple(r for r in roles if r != MANAGER_ROLE)

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
    valid_reports: list[RoleResearchReport] = []
    incomplete_reasons: list[str] = []

    for role in analyst_roles:
        existing_reports = (
            research_repository.get_role_reports_for_run(research_run_id) if research_repository is not None else ()
        )
        already_done = next((r for r in existing_reports if r.role == role), None)
        if already_done is not None:
            valid_reports.append(already_done)
            continue

        report, attempts = _run_role_with_retries(
            role=role, research_run_id=research_run_id, snapshot=snapshot, provider=provider,
            provider_name=provider_name, model_name=model_name, prompt_registry=prompt_registry,
            configuration=configuration, clock=clock,
        )
        all_attempts.extend(attempts)
        for a in attempts:
            if research_repository is not None:
                research_repository.save_attempt(a)
        if report is None:
            incomplete_reasons.append(f"role {role!r} exhausted retries without a valid report")
            continue
        valid_reports.append(report)
        if research_repository is not None:
            research_repository.save_role_report(report, attempts[-1].attempt_id, clock())

    if incomplete_reasons:
        if research_repository is not None:
            research_repository.mark_run_finished(research_run_id, RUN_STATUS_ANALYSIS_INCOMPLETE, clock())
        return OrchestrationResult(
            research_run_id=research_run_id, snapshot_id=snapshot.snapshot_id,
            status=RUN_STATUS_ANALYSIS_INCOMPLETE, decision=None, role_reports=tuple(valid_reports),
            attempts=tuple(all_attempts), incomplete_reasons=tuple(incomplete_reasons), reused_existing_run=False,
        )

    decision, manager_attempts = _run_role_with_retries(
        role=MANAGER_ROLE, research_run_id=research_run_id, snapshot=snapshot, provider=provider,
        provider_name=provider_name, model_name=model_name, prompt_registry=prompt_registry,
        configuration=configuration, clock=clock, role_reports_for_manager=tuple(valid_reports),
    )
    all_attempts.extend(manager_attempts)
    for a in manager_attempts:
        if research_repository is not None:
            research_repository.save_attempt(a)

    if decision is None:
        if research_repository is not None:
            research_repository.mark_run_finished(research_run_id, RUN_STATUS_ANALYSIS_INCOMPLETE, clock())
        return OrchestrationResult(
            research_run_id=research_run_id, snapshot_id=snapshot.snapshot_id,
            status=RUN_STATUS_ANALYSIS_INCOMPLETE, decision=None, role_reports=tuple(valid_reports),
            attempts=tuple(all_attempts), incomplete_reasons=("manager exhausted retries without a valid decision",),
            reused_existing_run=False,
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
    )
