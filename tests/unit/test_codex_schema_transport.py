from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft7Validator

from trading_research.research.codex_schema_transport import (
    build_codex_transport_schema,
    transport_schema_byte_size,
)
from trading_research.research.errors import ProviderUnavailableError
from trading_research.research.output_validation import (
    decision_json_schema,
    role_report_json_schema,
    validate_against_schema,
)

ROLE_SCHEMAS = {
    "fundamental": role_report_json_schema,
    "technical": role_report_json_schema,
    "bull": role_report_json_schema,
    "bear": role_report_json_schema,
    "manager": decision_json_schema,
}


# --- Immutability ------------------------------------------------------------


def test_input_schema_is_not_mutated():
    canonical = role_report_json_schema()
    frozen = copy.deepcopy(canonical)
    build_codex_transport_schema(canonical)
    assert canonical == frozen


def test_output_is_a_deep_copy_not_shared_state():
    canonical = role_report_json_schema()
    transport = build_codex_transport_schema(canonical)
    transport["properties"]["stance"]["enum"].append("MUTATED")
    assert "MUTATED" not in canonical["properties"]["stance"]["enum"]


def test_repeated_normalization_is_deterministic():
    canonical = role_report_json_schema()
    first = build_codex_transport_schema(canonical)
    second = build_codex_transport_schema(canonical)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --- Safe transforms -----------------------------------------------------------


def test_bound_keywords_are_stripped_but_canonical_still_enforces_them():
    canonical = role_report_json_schema()
    transport = build_codex_transport_schema(canonical)
    assert "maxLength" not in json.dumps(transport)
    assert "maxItems" not in json.dumps(transport)
    # canonical validation must still catch what the transport schema no longer bounds
    oversized = {
        "stance": "BULLISH",
        "summary": "x" * 5000,
        "claims": [], "catalysts": [], "risks": [], "uncertainties": [], "missing_data_reasons": [],
    }
    Draft7Validator(transport).validate(oversized)  # permitted by the broader transport schema
    with pytest.raises(Exception):
        validate_against_schema(oversized, canonical)


def test_object_nodes_become_fully_required_and_closed():
    canonical = role_report_json_schema()
    transport = build_codex_transport_schema(canonical)
    claim_schema = transport["properties"]["claims"]["items"]
    assert sorted(claim_schema["required"]) == sorted(claim_schema["properties"].keys())
    assert claim_schema["additionalProperties"] is False
    assert transport["additionalProperties"] is False


def test_enum_values_preserved():
    canonical = role_report_json_schema()
    transport = build_codex_transport_schema(canonical)
    assert transport["properties"]["stance"]["enum"] == canonical["properties"]["stance"]["enum"]


def test_array_item_schema_preserved():
    canonical = role_report_json_schema()
    transport = build_codex_transport_schema(canonical)
    assert transport["properties"]["catalysts"]["items"]["type"] == "string"


def test_local_defs_ref_is_inlined():
    canonical = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "$defs": {"Item": {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "string"}}}},
        "properties": {"item": {"$ref": "#/$defs/Item"}},
    }
    transport = build_codex_transport_schema(canonical)
    assert "$ref" not in json.dumps(transport)
    assert transport["properties"]["item"]["properties"]["a"]["type"] == "string"
    assert transport["properties"]["item"]["required"] == ["a"]


def test_metadata_keywords_dropped():
    canonical = role_report_json_schema()
    transport = build_codex_transport_schema(canonical)
    assert "$schema" not in transport
    assert "title" not in transport


# --- Fail-closed behavior -------------------------------------------------------


def _assert_fails_closed(schema: dict) -> None:
    with pytest.raises(ProviderUnavailableError) as exc_info:
        build_codex_transport_schema(schema)
    assert exc_info.value.code == "CODEX_TRANSPORT_SCHEMA_UNSUPPORTED"


def test_recursive_ref_fails_closed():
    _assert_fails_closed({
        "$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
        "$ref": "#/$defs/Node",
    })


def test_remote_ref_fails_closed():
    _assert_fails_closed({"$ref": "https://example.com/schema.json#/Thing"})


@pytest.mark.parametrize("construct", ["oneOf", "anyOf", "allOf", "not"])
def test_conditional_constructs_fail_closed(construct):
    if construct == "not":
        _assert_fails_closed({"not": {"type": "string"}})
    else:
        _assert_fails_closed({construct: [{"type": "string"}]})


def test_pattern_properties_fails_closed():
    _assert_fails_closed({"type": "object", "patternProperties": {"^x": {"type": "string"}}})


def test_schema_valued_additional_properties_fails_closed():
    _assert_fails_closed({
        "type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": {"type": "string"},
    })


def test_non_string_enum_fails_closed():
    _assert_fails_closed({"type": "number", "enum": [1, 2, 3]})


def test_invalid_canonical_schema_fails_closed():
    with pytest.raises(ProviderUnavailableError):
        build_codex_transport_schema("not-a-schema-object")  # type: ignore[arg-type]


def test_excessive_nesting_fails_closed():
    node: dict = {"type": "string"}
    for _ in range(60):
        node = {"type": "object", "properties": {"n": node}}
    _assert_fails_closed(node)


def test_oversize_normalized_schema_is_detectable_via_byte_size_helper():
    canonical = role_report_json_schema()
    transport = build_codex_transport_schema(canonical)
    assert transport_schema_byte_size(transport) < 4096
    assert transport_schema_byte_size(transport) > 0


# --- Two-stage validation --------------------------------------------------------


def test_response_passing_transport_but_failing_canonical_is_rejected():
    canonical = role_report_json_schema()
    transport = build_codex_transport_schema(canonical)
    response = {
        "stance": "BULLISH",
        "summary": "x" * 5000,  # violates canonical maxLength, not bounded in transport
        "claims": [], "catalysts": [], "risks": [], "uncertainties": [], "missing_data_reasons": [],
    }
    Draft7Validator(transport).validate(response)  # passes the broader transport schema
    with pytest.raises(Exception):
        validate_against_schema(response, canonical)  # must still be rejected


# --- Role coverage -----------------------------------------------------------


@pytest.mark.parametrize("role", sorted(ROLE_SCHEMAS))
def test_role_schema_normalizes_and_stays_enforceable(role):
    canonical = ROLE_SCHEMAS[role](max_claims=20)
    Draft7Validator.check_schema(canonical)
    transport = build_codex_transport_schema(canonical)
    Draft7Validator.check_schema(transport)
    assert transport_schema_byte_size(transport) < 8192
    # canonical required fields remain a subset of transport's fully-required set
    assert set(canonical["required"]).issubset(set(transport["required"]))
