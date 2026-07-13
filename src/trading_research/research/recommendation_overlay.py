"""Applies a deterministic research overlay to an already-frozen baseline
recommendation, producing the Arm B ("Claude-enhanced") recommendation
(docs/milestone-5.md Steps 13/16).

Deliberately does not import or duplicate `recommendations/builder.py`'s
scoring/eligibility logic, and never modifies that module. It only takes an
*already frozen, schema-validated* baseline payload and applies one of the
overlay's allowed transformations. Structurally, this file can only ever
null out `risk_plan` or leave it unchanged — there is no code path that
assigns one, so it cannot raise position size, cannot add an order that
didn't already exist on the baseline, and cannot turn a screened-out or
already-incomplete baseline into anything executable
(`overlay.resolve_side_after_overlay` enforces that boundary).
"""
from __future__ import annotations

import copy
import json

from jsonschema import Draft7Validator

from ..config import REPO_ROOT
from ..recommendations.builder import (
    RecommendationBuildError,
    SIDE_ANALYSIS_INCOMPLETE,
    SIDE_BUY_CANDIDATE,
    SIDE_NO_ACTION,
    SIDE_SCREENED_OUT,
    STATUS_ANALYSIS_INCOMPLETE,
    FrozenRecommendation,
    derive_rec_id,
)
from .models import ResearchOverlayDecision
from .overlay import resolve_side_after_overlay

_SCHEMA_PATH = REPO_ROOT / "schemas" / "recommendation.schema.json"


def _load_validator() -> Draft7Validator:
    return Draft7Validator(json.loads(_SCHEMA_PATH.read_text()))


def apply_overlay_to_recommendation(
    baseline: FrozenRecommendation,
    overlay: ResearchOverlayDecision,
) -> FrozenRecommendation:
    baseline_payload = baseline.payload
    new_side = resolve_side_after_overlay(baseline_payload["side"], overlay)

    idempotency_key = f"{baseline_payload['rec_id']}:overlay:{overlay.overlay_id}"
    payload = copy.deepcopy(baseline_payload)
    payload["rec_id"] = derive_rec_id(idempotency_key)
    payload["side"] = new_side
    payload["warnings"] = list(payload.get("warnings", [])) + [f"research overlay: {r}" for r in overlay.reasons]
    payload["rationale_text"] = (
        payload.get("rationale_text", "")
        + f" | Research overlay ({overlay.policy_version}): {overlay.action} — " + "; ".join(overlay.reasons)
    )

    if new_side != SIDE_BUY_CANDIDATE:
        payload["risk_plan"] = None
    if new_side == SIDE_ANALYSIS_INCOMPLETE:
        payload["status"] = STATUS_ANALYSIS_INCOMPLETE
        reasons = list(payload.get("missing_data_reasons", []))
        if not reasons:
            reasons = ["research analysis incomplete"]
        payload["missing_data_reasons"] = reasons

    if payload["side"] in (SIDE_NO_ACTION, SIDE_ANALYSIS_INCOMPLETE, SIDE_SCREENED_OUT) and payload["risk_plan"] is not None:
        raise RecommendationBuildError(f"overlay produced side={payload['side']!r} with a non-null risk_plan")

    validator = _load_validator()
    errors = sorted(validator.iter_errors(payload), key=lambda e: e.json_path)
    if errors:
        raise RecommendationBuildError(
            "overlay-adjusted recommendation failed schema validation: "
            + "; ".join(f"{e.json_path}: {e.message}" for e in errors)
        )
    return FrozenRecommendation(payload=payload)
