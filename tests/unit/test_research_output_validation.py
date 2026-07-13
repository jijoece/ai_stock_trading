"""Category D: structured-output tests (docs/milestone-5.md Step 20.D)."""
from __future__ import annotations

import pytest

from trading_research.research.errors import MalformedOutputError, SchemaValidationError
from trading_research.research.output_validation import (
    build_decision,
    build_role_report,
    decision_json_schema,
    parse_structured_json,
    role_report_json_schema,
)

VALID_REPORT = {
    "stance": "BULLISH",
    "summary": "Revenue growth is accelerating.",
    "claims": [
        {"claim_id": "c1", "claim_type": "growth", "statement": "Revenue grew 8% YoY",
         "evidence_ids": ["ev-1"], "numeric_value": "0.08", "unit": "yoy_fraction", "importance": "high"},
    ],
    "catalysts": ["upcoming product launch"],
    "risks": ["regulatory scrutiny"],
    "uncertainties": ["guidance not yet updated"],
    "missing_data_reasons": [],
}

VALID_DECISION = {
    "rating": "OVERWEIGHT",
    "confidence": 0.6,
    "thesis": "Solid growth with manageable risk.",
    "bull_case": "Revenue growth continues.",
    "bear_case": "Regulatory risk could compress multiples.",
    "catalysts": ["earnings beat"],
    "risks": ["regulatory risk"],
    "invalidation_conditions": ["guidance cut below 5% growth"],
    "claims": [],
    "evidence_ids": ["ev-1"],
    "missing_data_reasons": [],
}


def test_valid_role_report_builds():
    report = build_role_report(
        VALID_REPORT, report_id="r1", research_run_id="run-1", role="fundamental", symbol="AAPL",
        snapshot_id="snap-1", model_name="m", prompt_version="v1",
    )
    assert report.stance == "BULLISH"
    assert str(report.claims[0].numeric_value) == "0.08"


def test_malformed_json_raises():
    with pytest.raises(MalformedOutputError):
        parse_structured_json("{not valid json")


def test_trailing_prose_raises():
    with pytest.raises(MalformedOutputError):
        parse_structured_json('{"a": 1}\nHope this helps!')


def test_empty_response_raises():
    with pytest.raises(MalformedOutputError):
        parse_structured_json("   ")


def test_non_object_json_raises():
    with pytest.raises(MalformedOutputError):
        parse_structured_json("[1, 2, 3]")


def test_valid_json_with_no_trailing_content_parses():
    assert parse_structured_json('{"a": 1}') == {"a": 1}


def test_unknown_enum_rejected():
    bad = dict(VALID_REPORT, stance="SUPER_BULLISH")
    with pytest.raises(SchemaValidationError):
        build_role_report(bad, report_id="r1", research_run_id="run-1", role="fundamental", symbol="AAPL", snapshot_id="snap-1", model_name="m", prompt_version="v1")


def test_extra_executable_field_rejected():
    bad = dict(VALID_REPORT, order_type="market")
    with pytest.raises(SchemaValidationError):
        build_role_report(bad, report_id="r1", research_run_id="run-1", role="fundamental", symbol="AAPL", snapshot_id="snap-1", model_name="m", prompt_version="v1")


def test_missing_evidence_ids_on_claim_rejected():
    bad_claims = [dict(VALID_REPORT["claims"][0], evidence_ids=[])]
    bad = dict(VALID_REPORT, claims=bad_claims)
    with pytest.raises(SchemaValidationError):
        build_role_report(bad, report_id="r1", research_run_id="run-1", role="fundamental", symbol="AAPL", snapshot_id="snap-1", model_name="m", prompt_version="v1")


def test_invalid_confidence_rejected():
    bad = dict(VALID_DECISION, confidence=1.5)
    with pytest.raises(SchemaValidationError):
        build_decision(bad, decision_id="d1", research_run_id="run-1", symbol="AAPL", snapshot_id="snap-1", model_name="m", prompt_version="v1")


def test_negative_confidence_rejected():
    bad = dict(VALID_DECISION, confidence=-0.1)
    with pytest.raises(SchemaValidationError):
        build_decision(bad, decision_id="d1", research_run_id="run-1", symbol="AAPL", snapshot_id="snap-1", model_name="m", prompt_version="v1")


def test_oversized_claim_list_rejected():
    many_claims = [dict(VALID_REPORT["claims"][0], claim_id=f"c{i}") for i in range(30)]
    bad = dict(VALID_REPORT, claims=many_claims)
    with pytest.raises(SchemaValidationError):
        build_role_report(
            bad, report_id="r1", research_run_id="run-1", role="fundamental", symbol="AAPL", snapshot_id="snap-1",
            model_name="m", prompt_version="v1", schema=role_report_json_schema(max_claims=20),
        )


def test_missing_bear_case_rejected():
    bad = dict(VALID_DECISION)
    del bad["bear_case"]
    with pytest.raises(SchemaValidationError):
        build_decision(bad, decision_id="d1", research_run_id="run-1", symbol="AAPL", snapshot_id="snap-1", model_name="m", prompt_version="v1")


def test_empty_bear_case_string_rejected_by_domain_model():
    from trading_research.research.errors import EvidenceValidationError

    bad = dict(VALID_DECISION, bear_case="")
    with pytest.raises((SchemaValidationError, EvidenceValidationError)):
        build_decision(bad, decision_id="d1", research_run_id="run-1", symbol="AAPL", snapshot_id="snap-1", model_name="m", prompt_version="v1")


def test_valid_decision_builds():
    decision = build_decision(
        VALID_DECISION, decision_id="d1", research_run_id="run-1", symbol="AAPL", snapshot_id="snap-1",
        model_name="m", prompt_version="v1",
    )
    assert decision.rating == "OVERWEIGHT"
    assert decision.bull_case and decision.bear_case


def test_no_share_quantity_or_dollar_allocation_field_allowed():
    bad = dict(VALID_DECISION, quantity=100)
    with pytest.raises(SchemaValidationError):
        build_decision(bad, decision_id="d1", research_run_id="run-1", symbol="AAPL", snapshot_id="snap-1", model_name="m", prompt_version="v1")

    bad2 = dict(VALID_DECISION, dollar_allocation=1000)
    with pytest.raises(SchemaValidationError):
        build_decision(bad2, decision_id="d1", research_run_id="run-1", symbol="AAPL", snapshot_id="snap-1", model_name="m", prompt_version="v1")
