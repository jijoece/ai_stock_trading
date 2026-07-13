"""Baseline-vs-Claude-enhanced experiment assignment (docs/milestone-5.md Step 14).

Both arms are always constructed and persisted together, whatever the
outcome of either one — this is what prevents survivorship bias ("Do not
create survivorship bias by evaluating only recommendations that
executed"). A screened-out, no-action, watch, or ANALYSIS_INCOMPLETE
recommendation is assigned exactly like an executable one.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from .models import ExperimentAssignment

DEFAULT_ASSIGNMENT_POLICY_VERSION = "research-experiment.v1"


def derive_experiment_id(candidate_run_id: str, policy_version: str) -> str:
    digest = hashlib.sha256(f"{candidate_run_id}:{policy_version}".encode()).hexdigest()
    return f"exp-{digest[:32]}"


def build_experiment_assignments(
    *,
    candidate_run_id: str,
    symbol: str,
    as_of: datetime,
    baseline_recommendation_id: str | None,
    enhanced_recommendation_id: str | None,
    assignment_policy_version: str = DEFAULT_ASSIGNMENT_POLICY_VERSION,
) -> tuple[ExperimentAssignment, ExperimentAssignment]:
    """Deterministic experiment_id from (candidate_run_id, policy_version) —
    re-running the same candidate under the same policy reuses the same
    experiment_id (idempotent experiment construction)."""
    experiment_id = derive_experiment_id(candidate_run_id, assignment_policy_version)
    baseline = ExperimentAssignment(
        experiment_id=experiment_id, candidate_run_id=candidate_run_id, symbol=symbol, as_of=as_of, arm="BASELINE",
        baseline_recommendation_id=baseline_recommendation_id, enhanced_recommendation_id=None,
        assignment_policy_version=assignment_policy_version,
    )
    enhanced = ExperimentAssignment(
        experiment_id=experiment_id, candidate_run_id=candidate_run_id, symbol=symbol, as_of=as_of, arm="ENHANCED",
        baseline_recommendation_id=None, enhanced_recommendation_id=enhanced_recommendation_id,
        assignment_policy_version=assignment_policy_version,
    )
    return baseline, enhanced
