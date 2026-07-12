"""Schema-validation coverage for schemas/recommendation.schema.json using
static fixtures under tests/fixtures/recommendations/ (see 1A.3).
"""
import json

import pytest
from jsonschema import Draft7Validator

from trading_research.config import REPO_ROOT

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "recommendations"


@pytest.fixture(scope="module")
def validator():
    schema = json.loads((REPO_ROOT / "schemas" / "recommendation.schema.json").read_text())
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


VALID_FIXTURES = [
    "valid_active_buy_candidate.json",
    "valid_no_action.json",
    "valid_analysis_incomplete.json",
    "valid_screened_out.json",
]

INVALID_FIXTURES = [
    "invalid_incomplete_with_order_details.json",
    "invalid_incomplete_missing_reasons.json",
    "invalid_no_action_with_risk_plan.json",
    "invalid_overweight_reddit.json",
    "invalid_unfrozen.json",
    "invalid_account_identifier.json",
    "invalid_missing_git_sha.json",
    "invalid_bad_symbol.json",
    "invalid_wrong_disclaimer.json",
    "invalid_screened_out_with_risk_plan.json",
]


@pytest.mark.parametrize("fixture_name", VALID_FIXTURES)
def test_valid_fixture_passes(validator, fixture_name):
    rec = _load(fixture_name)
    errors = list(validator.iter_errors(rec))
    assert errors == [], f"{fixture_name} should validate: {errors}"


@pytest.mark.parametrize("fixture_name", INVALID_FIXTURES)
def test_invalid_fixture_fails(validator, fixture_name):
    rec = _load(fixture_name)
    errors = list(validator.iter_errors(rec))
    assert errors, f"{fixture_name} should NOT validate but did"


def test_invalid_account_identifier_rejected_by_additional_properties(validator):
    """additionalProperties:false must reject an account_id field outright —
    frozen recommendations must never carry broker account identifiers."""
    rec = _load("invalid_account_identifier.json")
    errors = list(validator.iter_errors(rec))
    messages = " ".join(e.message for e in errors)
    assert "account_id" in messages or "Additional properties" in messages


def test_analysis_incomplete_never_has_executable_risk_plan(validator):
    """Direct assertion of the safety property, independent of schema wiring:
    no ANALYSIS_INCOMPLETE fixture in this repo carries a populated risk_plan."""
    for name in VALID_FIXTURES:
        rec = _load(name)
        if rec["status"] == "analysis_incomplete":
            assert rec["risk_plan"] is None


def test_all_fixtures_carry_research_only_disclaimer():
    disclaimer = "Research output only. Not financial advice. Not an instruction to trade."
    for name in VALID_FIXTURES:
        rec = _load(name)
        assert rec["disclaimer"] == disclaimer
