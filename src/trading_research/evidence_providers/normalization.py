"""Deterministic evidence-snapshot outcome classification (docs/milestone-6.md
Step 12). Sits on top of the existing `research/evidence.py::build_evidence_snapshot`
and `research/evidence_validation.py::validate_snapshot_preconditions` — it
does not reimplement snapshot construction, conflict retention, or
point-in-time computation (all already correct and tested from Milestone 5).
This module only adds the *outcome status* a scheduled cycle needs to decide
whether a snapshot may enter the Claude-enhanced path.
"""
from __future__ import annotations

from ..research.evidence_validation import validate_snapshot_preconditions
from ..research.models import EvidenceSnapshot

OUTCOME_COMPLETE = "COMPLETE"
OUTCOME_COMPLETE_WITH_CONFLICTS = "COMPLETE_WITH_CONFLICTS"
OUTCOME_INCOMPLETE_REQUIRED_DATA = "INCOMPLETE_REQUIRED_DATA"
OUTCOME_STALE_REQUIRED_DATA = "STALE_REQUIRED_DATA"
OUTCOME_POINT_IN_TIME_UNSAFE = "POINT_IN_TIME_UNSAFE"
OUTCOME_PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

# A snapshot in any of these states must never enter the Claude-enhanced path
# (docs/milestone-6.md: "A snapshot that is unsafe or missing required
# evidence must not enter the enhanced recommendation path").
BLOCKING_OUTCOMES = (
    OUTCOME_INCOMPLETE_REQUIRED_DATA,
    OUTCOME_STALE_REQUIRED_DATA,
    OUTCOME_POINT_IN_TIME_UNSAFE,
    OUTCOME_PROVIDER_UNAVAILABLE,
)

_UNAVAILABLE_MARKERS = ("not configured", "unavailable", "no api key")


def _reason_indicates_provider_unavailable(reason: str) -> bool:
    lowered = reason.lower()
    return any(marker in lowered for marker in _UNAVAILABLE_MARKERS)


def classify_snapshot_outcome(
    snapshot: EvidenceSnapshot,
    *,
    required_categories: tuple[str, ...],
    require_point_in_time_safe: bool = True,
    fail_on_stale_required_evidence: bool = True,
) -> tuple[str, tuple[str, ...]]:
    """Returns `(outcome, reasons)`. Never mutates the snapshot; purely a
    classification over its already-computed fields."""
    if require_point_in_time_safe and not snapshot.point_in_time_safe:
        return OUTCOME_POINT_IN_TIME_UNSAFE, ("snapshot is not point-in-time safe",)

    reasons = validate_snapshot_preconditions(
        snapshot, require_point_in_time_safe=False,  # already checked above with its own status
        fail_on_stale_required_evidence=fail_on_stale_required_evidence,
        required_categories=required_categories,
    )
    if reasons:
        stale_reasons = tuple(r for r in reasons if "stale" in r)
        missing_reasons = tuple(r for r in reasons if r not in stale_reasons)
        if missing_reasons:
            if missing_reasons and all(
                _reason_indicates_provider_unavailable(r) for r in snapshot.missing_data_reasons
            ) and snapshot.missing_data_reasons:
                return OUTCOME_PROVIDER_UNAVAILABLE, reasons
            return OUTCOME_INCOMPLETE_REQUIRED_DATA, reasons
        return OUTCOME_STALE_REQUIRED_DATA, reasons

    if snapshot.conflict_reasons:
        return OUTCOME_COMPLETE_WITH_CONFLICTS, snapshot.conflict_reasons

    return OUTCOME_COMPLETE, ()
