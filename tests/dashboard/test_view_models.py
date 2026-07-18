from dataclasses import FrozenInstanceError

import pytest

from dashboard.models.view_models import (
    DashboardOutcome,
    OutcomeMapping,
    map_dashboard_outcome,
)


def test_dashboard_outcome_taxonomy_is_complete():
    assert {outcome.value for outcome in DashboardOutcome} == {
        "BOUGHT_OR_SUBMITTED",
        "BUY_CANDIDATE_NOT_SUBMITTED",
        "REJECTED",
        "SCREENED_OUT",
        "EVIDENCE_INCOMPLETE",
        "RESEARCH_INCOMPLETE",
        "POLICY_BLOCKED",
        "BUDGET_BLOCKED",
        "PROVIDER_FAILURE",
        "DUPLICATE_PREVENTED",
        "PRICE_CONDITION_NOT_MET",
        "NO_ACTION",
        "UNKNOWN",
    }


@pytest.mark.parametrize(
    ("fields", "expected", "reason_code"),
    [
        ({"baseline_paper_submitted": True}, DashboardOutcome.BOUGHT_OR_SUBMITTED, "BASELINE_PAPER_SUBMITTED"),
        ({"paper_order_status": "FILLED"}, DashboardOutcome.BOUGHT_OR_SUBMITTED, "FILLED"),
        ({"recommendation_side": "buy_candidate"}, DashboardOutcome.BUY_CANDIDATE_NOT_SUBMITTED, "BUY_CANDIDATE"),
        ({"risk_decision": "REJECTED_MAX_OPEN_POSITIONS"}, DashboardOutcome.REJECTED, "REJECTED_MAX_OPEN_POSITIONS"),
        ({"recommendation_side": "screened_out"}, DashboardOutcome.SCREENED_OUT, "SCREENED_OUT"),
        (
            {"evidence_screening_completeness": "MISSING_CRITICAL_FUNDAMENTALS"},
            DashboardOutcome.EVIDENCE_INCOMPLETE,
            "MISSING_CRITICAL_FUNDAMENTALS",
        ),
        ({"research_status": "ANALYSIS_INCOMPLETE"}, DashboardOutcome.RESEARCH_INCOMPLETE, "ANALYSIS_INCOMPLETE"),
        ({"risk_decision": "REJECTED_BOOK_PAUSED"}, DashboardOutcome.POLICY_BLOCKED, "REJECTED_BOOK_PAUSED"),
        ({"scheduler_status": "BUDGET_REJECTED"}, DashboardOutcome.BUDGET_BLOCKED, "BUDGET_REJECTED"),
        ({"provider_failure_code": "SEC_TIMEOUT"}, DashboardOutcome.PROVIDER_FAILURE, "SEC_TIMEOUT"),
        ({"recommendation_side": "no_action"}, DashboardOutcome.NO_ACTION, "NO_ACTION"),
    ],
)
def test_maps_stable_persisted_codes(fields, expected, reason_code):
    result = map_dashboard_outcome(**fields)

    assert result.outcome is expected
    assert result.primary_reason_code == reason_code


def test_execution_evidence_has_priority_over_earlier_blocker_codes():
    result = map_dashboard_outcome(
        paper_order_status="FILLED",
        evidence_screening_completeness="MISSING_CRITICAL_FUNDAMENTALS",
        risk_decision="REJECTED_MAX_OPEN_POSITIONS",
    )

    assert result.outcome is DashboardOutcome.BOUGHT_OR_SUBMITTED


@pytest.mark.parametrize("field", [
    {"lifecycle_outcome": "STILL_PENDING"},
    {"lifecycle_outcome": "DUPLICATE_FILL"},
])
def test_ambiguous_persisted_codes_remain_unknown(field):
    result = map_dashboard_outcome(**field)

    assert result.outcome is DashboardOutcome.UNKNOWN


def test_unknown_code_is_not_inferred_and_explanation_is_sanitized_and_bounded():
    result = map_dashboard_outcome(
        recommendation_side="legacy_custom_side",
        unknown_explanation="legacy\nvalue\t" + "x" * 400,
    )

    assert result.outcome is DashboardOutcome.UNKNOWN
    assert result.primary_reason_code == "UNKNOWN"
    assert "\n" not in result.friendly_reason
    assert "\t" not in result.friendly_reason
    assert len(result.friendly_reason) <= 240


def test_outcome_mapping_is_immutable():
    result = OutcomeMapping(DashboardOutcome.NO_ACTION, "NO_ACTION", "No action")

    with pytest.raises(FrozenInstanceError):
        result.primary_reason_code = "CHANGED"
