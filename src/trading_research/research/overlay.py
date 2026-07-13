"""Deterministic research overlay (docs/milestone-5.md Step 13).

Claude never directly constructs the final recommendation. This module is
ordinary, versioned Python: given a `ResearchDecision` (or none, on
incomplete analysis), it decides one of four actions
(`ALLOW_BASELINE`, `DOWNGRADE_TO_WATCH`, `FORCE_NO_ACTION`,
`ANALYSIS_INCOMPLETE`) and — separately — how that action folds into the
deterministic baseline's `side`. The deterministic baseline remains
authoritative: this policy can only hold a candidate back or downgrade it,
never promote a screened-out candidate, never raise position size, and never
use `confidence` as a sizing multiplier (see `resolve_side_after_overlay`).
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from ..recommendations.builder import SIDE_ANALYSIS_INCOMPLETE, SIDE_BUY_CANDIDATE, SIDE_SCREENED_OUT
from .configuration import ResearchConfiguration
from .errors import UnknownOverlayActionError
from .models import OVERLAY_ACTIONS, ResearchDecision, ResearchOverlayDecision

ACTION_ALLOW_BASELINE = "ALLOW_BASELINE"
ACTION_DOWNGRADE_TO_WATCH = "DOWNGRADE_TO_WATCH"
ACTION_FORCE_NO_ACTION = "FORCE_NO_ACTION"
ACTION_ANALYSIS_INCOMPLETE = "ANALYSIS_INCOMPLETE"

_SUPPORTIVE_RATINGS = ("BUY", "OVERWEIGHT")
_CRITICAL_RATINGS = ("SELL", "UNDERWEIGHT")


def decide_overlay_action(
    decision: ResearchDecision | None,
    orchestration_status: str,
    *,
    configuration: ResearchConfiguration,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Returns (action, reasons, critical_risks). Pure function of its
    inputs — same decision + status + config always produces the same
    action (docs/milestone-5.md Step 20 category H: "deterministic output
    for identical inputs")."""
    if orchestration_status != "COMPLETED" or decision is None:
        return configuration.overlay_incomplete_action, ("research orchestration did not complete",), ()

    if decision.rating == "ANALYSIS_INCOMPLETE":
        return configuration.overlay_incomplete_action, ("research decision rating is ANALYSIS_INCOMPLETE",), ()

    if decision.rating in _CRITICAL_RATINGS:
        return (
            configuration.overlay_critical_risk_action,
            (f"research rating is {decision.rating} — treated as a critical, unresolved risk",),
            decision.risks,
        )

    if decision.rating == "HOLD":
        return ACTION_DOWNGRADE_TO_WATCH, ("research rating is HOLD — not a fresh buy signal",), ()

    if decision.rating in _SUPPORTIVE_RATINGS:
        return ACTION_ALLOW_BASELINE, (f"research rating is {decision.rating} — supportive of the deterministic baseline",), ()

    raise UnknownOverlayActionError(f"unhandled research rating {decision.rating!r} — fails closed")


def derive_overlay_id(research_decision_id: str, baseline_score: Decimal | None, policy_version: str) -> str:
    payload = json.dumps(
        {"decision_id": research_decision_id, "baseline_score": str(baseline_score), "policy_version": policy_version},
        sort_keys=True,
    )
    return "overlay-" + hashlib.sha256(payload.encode()).hexdigest()[:32]


def apply_research_overlay(
    decision: ResearchDecision | None,
    *,
    orchestration_status: str,
    baseline_score: Decimal | None,
    configuration: ResearchConfiguration,
) -> ResearchOverlayDecision:
    action, reasons, critical_risks = decide_overlay_action(decision, orchestration_status, configuration=configuration)
    if action not in OVERLAY_ACTIONS:
        raise UnknownOverlayActionError(f"overlay action {action!r} is not one of {OVERLAY_ACTIONS} — fails closed")

    decision_id = decision.decision_id if decision is not None else "none"
    overlay_id = derive_overlay_id(decision_id, baseline_score, configuration.overlay_policy_version)
    return ResearchOverlayDecision(
        overlay_id=overlay_id, research_decision_id=decision_id, baseline_score=baseline_score,
        action=action, reasons=reasons, critical_risks=critical_risks, policy_version=configuration.overlay_policy_version,
    )


def resolve_side_after_overlay(baseline_side: str, overlay: ResearchOverlayDecision) -> str:
    """The only place a research overlay is allowed to change a `side` value.

    Hard boundaries (docs/milestone-5.md "Safety requirements"):
    * a screened-out or already-incomplete baseline is never changed by the
      overlay in either direction — the overlay cannot bypass a screen
      rejection and cannot promote a screened-out candidate;
    * `ALLOW_BASELINE` is always a no-op — the baseline is authoritative;
    * `DOWNGRADE_TO_WATCH` / `FORCE_NO_ACTION` can only ever hold a
      `buy_candidate` back, never raise a lesser side up to `buy_candidate`
      (there is no code path here that assigns `SIDE_BUY_CANDIDATE`);
    * `ANALYSIS_INCOMPLETE` always forces `analysis_incomplete`, which the
      existing recommendation schema already requires to carry no risk_plan.
    """
    if baseline_side in (SIDE_SCREENED_OUT, SIDE_ANALYSIS_INCOMPLETE):
        return baseline_side

    if overlay.action == ACTION_ALLOW_BASELINE:
        return baseline_side
    if overlay.action == ACTION_DOWNGRADE_TO_WATCH:
        return "watch" if baseline_side == SIDE_BUY_CANDIDATE else baseline_side
    if overlay.action == ACTION_FORCE_NO_ACTION:
        return "no_action" if baseline_side == SIDE_BUY_CANDIDATE else baseline_side
    if overlay.action == ACTION_ANALYSIS_INCOMPLETE:
        return SIDE_ANALYSIS_INCOMPLETE
    raise UnknownOverlayActionError(f"overlay action {overlay.action!r} is not one of {OVERLAY_ACTIONS} — fails closed")
