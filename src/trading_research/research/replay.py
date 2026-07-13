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

from .claim_validation import validate_decision, validate_role_report
from .configuration import ResearchConfiguration
from .evidence import canonical_snapshot_payload, compute_snapshot_id
from .models import EvidenceSnapshot, ResearchDecision, ResearchOverlayDecision, RoleResearchReport
from .orchestration import ResearchRepository, compute_research_run_id
from .overlay import apply_research_overlay
from .prompt_registry import PromptRegistry


@dataclass(frozen=True)
class ReplayResult:
    research_run_id: str
    matches: bool
    mismatches: tuple[str, ...]
    reconstructed_decision: ResearchDecision | None
    reconstructed_role_reports: tuple[RoleResearchReport, ...]
    reconstructed_overlay: ResearchOverlayDecision | None


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

    return ReplayResult(
        research_run_id=research_run_id, matches=not mismatches, mismatches=tuple(mismatches),
        reconstructed_decision=stored_decision, reconstructed_role_reports=role_reports,
        reconstructed_overlay=reconstructed_overlay,
    )
