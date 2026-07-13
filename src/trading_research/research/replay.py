"""Deterministic replay (docs/milestone-5.md Step 16).

`replay_research_run` deliberately has no `ResearchModelProvider` parameter
at all — it is structurally impossible for replay to call a provider or an
execution/broker API. It reconstructs a persisted decision from the
snapshot + persisted role reports, re-runs the same deterministic
validators and overlay policy used at run time, and reports any mismatch
between the recomputed content hashes (snapshot, research_run_id) and the
persisted ones.
"""
from __future__ import annotations

from dataclasses import dataclass

from .claim_validation import classify_claim_rejection_reason, validate_decision, validate_role_report
from .configuration import MANAGER_ROLE, ResearchConfiguration
from .evidence import canonical_snapshot_payload, compute_snapshot_id
from .failure_taxonomy import STAGE_CLAIM_EVIDENCE_VALIDATION, ResearchValidationFailure
from .models import EvidenceSnapshot, ResearchDecision, ResearchOverlayDecision, RoleResearchReport
from .orchestration import ResearchRepository, compute_research_run_id
from .overlay import apply_research_overlay
from .prompt_registry import PromptRegistry

# A failure "signature" — the normalized (role, code, claim_id) triple Step 16 permits as
# an alternative to comparing full failure_ids: replay never calls a provider, so it can
# only re-derive CLAIM_EVIDENCE_VALIDATION-stage failures (by re-running the same
# deterministic claim validators used at run time) — provider/schema-stage failures are
# echoed from persistence, never re-derived, since reconstructing them would require the
# original raw provider response, which replay deliberately never has.
FailureSignature = "tuple[str, str, str | None]"


def _signature(role: str, code: str, claim_id: str | None) -> tuple[str, str, str | None]:
    return (role, code, claim_id)


def _reconstruct_claim_failure_signatures(
    role_reports: tuple[RoleResearchReport, ...], decision: ResearchDecision | None, snapshot: EvidenceSnapshot,
) -> set[tuple[str, str, str | None]]:
    signatures: set[tuple[str, str, str | None]] = set()
    for report in role_reports:
        if report.snapshot_id != snapshot.snapshot_id:
            continue
        validation = validate_role_report(report, snapshot)
        for claim, reasons in validation.rejected_claims:
            for reason in reasons:
                signatures.add(_signature(report.role, classify_claim_rejection_reason(reason), claim.claim_id))
    if decision is not None and decision.snapshot_id == snapshot.snapshot_id:
        validation = validate_decision(decision, snapshot)
        for claim, reasons in validation.rejected_claims:
            for reason in reasons:
                signatures.add(_signature(MANAGER_ROLE, classify_claim_rejection_reason(reason), claim.claim_id))
        for reason in validation.consistency_reasons:
            signatures.add(_signature(MANAGER_ROLE, classify_claim_rejection_reason(reason), None))
    return signatures


@dataclass(frozen=True)
class ReplayResult:
    research_run_id: str
    matches: bool
    mismatches: tuple[str, ...]
    reconstructed_decision: ResearchDecision | None
    reconstructed_role_reports: tuple[RoleResearchReport, ...]
    reconstructed_overlay: ResearchOverlayDecision | None
    persisted_failures: "tuple[ResearchValidationFailure, ...]" = ()
    failure_comparison: "dict[str, list]" = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.failure_comparison is None:
            object.__setattr__(
                self, "failure_comparison",
                {"matched": [], "missing_persisted": [], "unexpected_persisted": [], "not_reconstructible": []},
            )


def _recompute_snapshot_id(snapshot: EvidenceSnapshot) -> str:
    payload = canonical_snapshot_payload(
        symbol=snapshot.symbol, as_of=snapshot.as_of, source_records=snapshot.source_records,
        evidence_items=snapshot.evidence_items, deterministic_factors=snapshot.deterministic_factors,
        sentiment_metrics=snapshot.sentiment_metrics, portfolio_context=snapshot.portfolio_context,
        missing_data_reasons=snapshot.missing_data_reasons, conflict_reasons=snapshot.conflict_reasons,
        point_in_time_safe=snapshot.point_in_time_safe, config_hash=snapshot.config_hash, git_sha=snapshot.git_sha,
    )
    return compute_snapshot_id(payload)


def replay_research_run(
    research_run_id: str,
    *,
    research_repository: ResearchRepository,
    snapshot: EvidenceSnapshot,
    provider_name: str,
    model_name: str,
    prompt_registry: PromptRegistry,
    configuration: ResearchConfiguration,
    run_mode: str,
) -> ReplayResult:
    mismatches: list[str] = []

    recomputed_snapshot_id = _recompute_snapshot_id(snapshot)
    if recomputed_snapshot_id != snapshot.snapshot_id:
        mismatches.append(
            f"snapshot content hash mismatch: recomputed {recomputed_snapshot_id!r} != stored {snapshot.snapshot_id!r}"
        )

    recomputed_run_id = compute_research_run_id(
        snapshot_id=snapshot.snapshot_id, provider_name=provider_name, model_name=model_name,
        roles=configuration.roles, prompt_registry=prompt_registry, run_mode=run_mode,
        config_hash=configuration.config_hash,
    )
    if recomputed_run_id != research_run_id:
        mismatches.append(
            f"research_run_id mismatch: recomputed {recomputed_run_id!r} != requested {research_run_id!r} "
            f"(prompt, model, provider, or configuration drift since the run was created)"
        )

    stored_decision = research_repository.get_decision_for_run(research_run_id)
    role_reports = research_repository.get_role_reports_for_run(research_run_id)
    if stored_decision is None:
        mismatches.append(f"no persisted decision found for research_run_id {research_run_id!r}")

    reconstructed_overlay: ResearchOverlayDecision | None = None
    if stored_decision is not None:
        if stored_decision.snapshot_id != snapshot.snapshot_id:
            mismatches.append("persisted decision references a different snapshot_id than the one supplied for replay")
        else:
            validation = validate_decision(stored_decision, snapshot)
            if not validation.is_valid:
                mismatches.append("persisted decision no longer passes claim/consistency validation on replay")

        for report in role_reports:
            if report.snapshot_id != snapshot.snapshot_id:
                mismatches.append(f"persisted role report {report.role!r} references a different snapshot_id")
                continue
            report_validation = validate_role_report(report, snapshot)
            if not report_validation.is_valid:
                mismatches.append(f"persisted role report {report.role!r} no longer passes claim validation on replay")

        reconstructed_overlay = apply_research_overlay(
            stored_decision, orchestration_status="COMPLETED", baseline_score=None, configuration=configuration,
        )

    # Failure reconstruction (Step 16): re-run the same deterministic claim validators
    # used at run time against the persisted role reports/decision and compare their
    # normalized (role, code, claim_id) signatures against what was actually persisted.
    # This can never call a provider — it only re-derives CLAIM_EVIDENCE_VALIDATION-stage
    # failures from already-persisted, already-schema-valid objects. A role whose attempts
    # were *all* rejected never has a `RoleResearchReport` persisted at all (only the
    # attempt that finally validated is copied to `research_role_reports`) — its claim
    # failures are structurally not re-derivable on replay (there is nothing to
    # re-validate), which is a different, honest condition from "the validator no longer
    # agrees with what it flagged before" and must not be reported as the same thing.
    persisted_failures = research_repository.list_run_failures(research_run_id)
    reconstructible_roles = {r.role for r in role_reports} | ({MANAGER_ROLE} if stored_decision is not None else set())
    reconstructed_signatures = _reconstruct_claim_failure_signatures(role_reports, stored_decision, snapshot)
    persisted_claim_failures = [f for f in persisted_failures if f.stage == STAGE_CLAIM_EVIDENCE_VALIDATION]
    persisted_signatures = {_signature(f.role, f.code, f.claim_id) for f in persisted_claim_failures}
    reconstructible_persisted_signatures = {sig for sig in persisted_signatures if sig[0] in reconstructible_roles}
    not_reconstructible = sorted(persisted_signatures - reconstructible_persisted_signatures)

    matched = sorted(reconstructed_signatures & reconstructible_persisted_signatures)
    missing_persisted = sorted(reconstructed_signatures - reconstructible_persisted_signatures)
    unexpected_persisted = sorted(reconstructible_persisted_signatures - reconstructed_signatures)
    failure_comparison = {
        "matched": matched, "missing_persisted": missing_persisted, "unexpected_persisted": unexpected_persisted,
        "not_reconstructible": not_reconstructible,
    }
    if missing_persisted:
        mismatches.append(
            f"claim-validator now flags {len(missing_persisted)} failure signature(s) not found in persisted "
            f"failures — possible validator-version difference since the run was created"
        )
    if unexpected_persisted:
        mismatches.append(
            f"{len(unexpected_persisted)} persisted failure signature(s) no longer reproduce on replay — possible "
            f"validator-version difference since the run was created"
        )

    return ReplayResult(
        research_run_id=research_run_id, matches=not mismatches, mismatches=tuple(mismatches),
        reconstructed_decision=stored_decision, reconstructed_role_reports=role_reports,
        reconstructed_overlay=reconstructed_overlay, persisted_failures=persisted_failures,
        failure_comparison=failure_comparison,
    )
