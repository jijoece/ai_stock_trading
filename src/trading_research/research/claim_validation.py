"""Deterministic claim-to-evidence validation (docs/milestone-5.md Step 9).

Every material research claim must reference one or more evidence IDs from
the *exact* snapshot used in the run. This module never trusts the model's
own claim of completeness — it independently re-derives support for every
claim from the snapshot's `normalized_values`, and it is this module (not
the model) that decides whether an unsupported claim is material enough to
force `ANALYSIS_INCOMPLETE`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .models import EvidenceSnapshot, ResearchClaim, ResearchDecision, RoleResearchReport

# Rounding-only tolerance for numeric claims — never a loophole for a model
# to introduce a materially different financial value (Step 9: "use
# documented tolerances only for legitimate rounding").
NUMERIC_TOLERANCE_RELATIVE = Decimal("0.02")


@dataclass(frozen=True)
class ClaimValidationResult:
    claim: ResearchClaim
    valid: bool
    reasons: tuple[str, ...]


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _validate_numeric(claim: ResearchClaim, snapshot: EvidenceSnapshot) -> list[str]:
    assert claim.numeric_value is not None
    candidates: list[Decimal] = []
    for eid in claim.evidence_ids:
        item = snapshot.evidence_by_id(eid)
        if item is None:
            continue
        for raw in item.normalized_values.values():
            decimal_value = _as_decimal(raw)
            if decimal_value is not None:
                candidates.append(decimal_value)

    for evidence_value in candidates:
        tolerance = abs(evidence_value) * NUMERIC_TOLERANCE_RELATIVE
        if abs(evidence_value - claim.numeric_value) <= tolerance:
            return []

    if not candidates:
        return [
            f"numeric claim {claim.numeric_value} has no comparable normalized_values in its cited evidence"
        ]
    return [
        f"numeric claim {claim.numeric_value} does not match any cited evidence's normalized_values "
        f"within {NUMERIC_TOLERANCE_RELATIVE:%} tolerance (closest available: {candidates})"
    ]


def validate_claim(claim: ResearchClaim, snapshot: EvidenceSnapshot) -> ClaimValidationResult:
    reasons: list[str] = []

    if not claim.evidence_ids:
        reasons.append(f"claim {claim.claim_id!r} cites no evidence")

    for eid in claim.evidence_ids:
        item = snapshot.evidence_by_id(eid)
        if item is None:
            reasons.append(
                f"claim {claim.claim_id!r} cites unknown evidence_id {eid!r} "
                f"(not present in snapshot {snapshot.snapshot_id}) — fabricated or cross-snapshot citation"
            )
            continue
        if item.stale:
            reasons.append(f"claim {claim.claim_id!r} relies on stale evidence_id {eid!r}")
        source = next((s for s in snapshot.source_records if s.source_id == item.source_id), None)
        if source is not None and not source.point_in_time_safe:
            reasons.append(
                f"claim {claim.claim_id!r} relies on point-in-time-unsafe evidence_id {eid!r}"
            )

    if claim.numeric_value is not None:
        reasons.extend(_validate_numeric(claim, snapshot))

    return ClaimValidationResult(claim=claim, valid=not reasons, reasons=tuple(reasons))


@dataclass(frozen=True)
class RoleReportValidationResult:
    report: RoleResearchReport
    valid_claims: tuple[ResearchClaim, ...]
    rejected_claims: tuple[tuple[ResearchClaim, tuple[str, ...]], ...]
    material_claim_unsupported: bool

    @property
    def is_valid(self) -> bool:
        return not self.material_claim_unsupported


def validate_role_report(report: RoleResearchReport, snapshot: EvidenceSnapshot) -> RoleReportValidationResult:
    if report.snapshot_id != snapshot.snapshot_id:
        raise ValueError(
            f"report {report.report_id!r} was produced for snapshot {report.snapshot_id!r}, "
            f"cannot validate against snapshot {snapshot.snapshot_id!r}"
        )
    if report.symbol != snapshot.symbol:
        raise ValueError(
            f"report {report.report_id!r} symbol {report.symbol!r} does not match snapshot symbol {snapshot.symbol!r}"
        )

    valid: list[ResearchClaim] = []
    rejected: list[tuple[ResearchClaim, tuple[str, ...]]] = []
    material_unsupported = False
    for claim in report.claims:
        result = validate_claim(claim, snapshot)
        if result.valid:
            valid.append(claim)
        else:
            rejected.append((claim, result.reasons))
            if claim.importance == "high":
                material_unsupported = True

    if report.stance != "ANALYSIS_INCOMPLETE" and not report.missing_data_reasons and snapshot.missing_data_reasons:
        material_unsupported = True

    return RoleReportValidationResult(
        report=report, valid_claims=tuple(valid), rejected_claims=tuple(rejected),
        material_claim_unsupported=material_unsupported,
    )


@dataclass(frozen=True)
class DecisionValidationResult:
    decision: ResearchDecision
    valid_claims: tuple[ResearchClaim, ...]
    rejected_claims: tuple[tuple[ResearchClaim, tuple[str, ...]], ...]
    material_claim_unsupported: bool
    consistency_reasons: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.material_claim_unsupported and not self.consistency_reasons


def validate_decision(decision: ResearchDecision, snapshot: EvidenceSnapshot) -> DecisionValidationResult:
    if decision.snapshot_id != snapshot.snapshot_id:
        raise ValueError(
            f"decision {decision.decision_id!r} was produced for snapshot {decision.snapshot_id!r}, "
            f"cannot validate against snapshot {snapshot.snapshot_id!r}"
        )
    if decision.symbol != snapshot.symbol:
        raise ValueError(
            f"decision {decision.decision_id!r} symbol {decision.symbol!r} does not match snapshot symbol {snapshot.symbol!r}"
        )

    valid: list[ResearchClaim] = []
    rejected: list[tuple[ResearchClaim, tuple[str, ...]]] = []
    material_unsupported = False
    for claim in decision.claims:
        result = validate_claim(claim, snapshot)
        if result.valid:
            valid.append(claim)
        else:
            rejected.append((claim, result.reasons))
            if claim.importance == "high":
                material_unsupported = True

    consistency_reasons: list[str] = []
    for eid in decision.evidence_ids:
        if snapshot.evidence_by_id(eid) is None:
            consistency_reasons.append(f"decision cites unknown/fabricated evidence_id {eid!r}")

    if decision.rating != "ANALYSIS_INCOMPLETE" and decision.missing_data_reasons:
        consistency_reasons.append(
            "decision reports missing_data_reasons but rating is not ANALYSIS_INCOMPLETE — "
            "a complete analysis cannot coexist with missing required evidence"
        )
    if decision.rating != "ANALYSIS_INCOMPLETE" and snapshot.missing_data_reasons:
        consistency_reasons.append(
            "decision claims a rating other than ANALYSIS_INCOMPLETE but the underlying "
            "evidence snapshot itself has unresolved missing_data_reasons"
        )

    return DecisionValidationResult(
        decision=decision, valid_claims=tuple(valid), rejected_claims=tuple(rejected),
        material_claim_unsupported=material_unsupported, consistency_reasons=tuple(consistency_reasons),
    )
