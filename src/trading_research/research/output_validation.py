"""Strict structured-output validation (docs/milestone-5.md Step 8).

No free-form prose is ever parsed with regular expressions. Every role
report and every research decision must be strict, schema-valid JSON with
`additionalProperties: false`, bounded list sizes, enum-constrained fields,
and zero fields that could plausibly be interpreted as executable order
instructions. Malformed output is never a partial success — it is always
either a `SchemaValidationError`/`MalformedOutputError` (caught by the
orchestrator's bounded retry loop) or rejected outright.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from jsonschema import Draft7Validator

from .errors import MalformedOutputError, SchemaValidationError
from .models import (
    CLAIM_IMPORTANCE_LEVELS,
    RATINGS,
    STANCES,
    ResearchClaim,
    ResearchDecision,
    RoleResearchReport,
)

ROLE_REPORT_SCHEMA_VERSION = "role-report.v1"
DECISION_SCHEMA_VERSION = "decision.v1"

# Fields that would let structured output smuggle an executable instruction
# past `additionalProperties: false` some other way (defense in depth on top
# of schema strictness — Step 8's explicit denylist).
FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "shares",
        "quantity",
        "share_quantity",
        "dollar_allocation",
        "position_size",
        "order_type",
        "order",
        "broker_instruction",
        "account_id",
        "limit_price",
        "stop_price",
        "side",
        "execute",
        "submit_order",
        "cancel_order",
        "approve",
    }
)

MAX_CLAIMS_DEFAULT = 20
MAX_LIST_ITEMS_DEFAULT = 15
MAX_STRING_CHARS = 4000


def _claim_schema(max_evidence_refs: int) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["claim_id", "claim_type", "statement", "evidence_ids", "importance"],
        "properties": {
            "claim_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "claim_type": {"type": "string", "minLength": 1, "maxLength": 100},
            "statement": {"type": "string", "minLength": 1, "maxLength": MAX_STRING_CHARS},
            "evidence_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": max_evidence_refs,
            },
            "numeric_value": {"type": ["number", "string", "null"]},
            "unit": {"type": ["string", "null"], "maxLength": 50},
            "importance": {"type": "string", "enum": list(CLAIM_IMPORTANCE_LEVELS)},
        },
    }


def role_report_json_schema(*, max_claims: int = MAX_CLAIMS_DEFAULT, max_list_items: int = MAX_LIST_ITEMS_DEFAULT) -> dict:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": ROLE_REPORT_SCHEMA_VERSION,
        "type": "object",
        "additionalProperties": False,
        "required": ["stance", "summary", "claims", "catalysts", "risks", "uncertainties", "missing_data_reasons"],
        "properties": {
            "stance": {"type": "string", "enum": list(STANCES)},
            "summary": {"type": "string", "minLength": 1, "maxLength": MAX_STRING_CHARS},
            "claims": {"type": "array", "items": _claim_schema(20), "maxItems": max_claims},
            "catalysts": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": max_list_items},
            "risks": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": max_list_items},
            "uncertainties": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": max_list_items},
            "missing_data_reasons": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": max_list_items},
        },
    }


def decision_json_schema(*, max_claims: int = MAX_CLAIMS_DEFAULT, max_list_items: int = MAX_LIST_ITEMS_DEFAULT) -> dict:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": DECISION_SCHEMA_VERSION,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "rating", "confidence", "thesis", "bull_case", "bear_case",
            "catalysts", "risks", "invalidation_conditions", "claims",
            "evidence_ids", "missing_data_reasons",
        ],
        "properties": {
            "rating": {"type": "string", "enum": list(RATINGS)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "thesis": {"type": "string", "maxLength": MAX_STRING_CHARS},
            "bull_case": {"type": "string", "maxLength": MAX_STRING_CHARS},
            "bear_case": {"type": "string", "maxLength": MAX_STRING_CHARS},
            "catalysts": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": max_list_items},
            "risks": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": max_list_items},
            "invalidation_conditions": {
                "type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": max_list_items,
            },
            "claims": {"type": "array", "items": _claim_schema(20), "maxItems": max_claims},
            "evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
            "missing_data_reasons": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": max_list_items},
        },
    }


def parse_structured_json(raw_text: str) -> dict:
    """Strict JSON parsing — no trailing prose, no partial success. Any
    leftover non-whitespace after the JSON value, or content that is not
    valid JSON at all, is a `MalformedOutputError`."""
    text = raw_text.strip()
    if not text:
        raise MalformedOutputError("empty structured-output response")
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(text)
    except json.JSONDecodeError as exc:
        raise MalformedOutputError(f"response is not valid JSON: {exc}") from exc
    if text[end:].strip():
        raise MalformedOutputError("response contained trailing content after the JSON value")
    if not isinstance(value, dict):
        raise MalformedOutputError("response JSON must be an object")
    return value


def _scan_forbidden_fields(data: Mapping[str, Any], *, path: str = "") -> list[str]:
    hits: list[str] = []
    for key, value in data.items():
        full_path = f"{path}.{key}" if path else key
        if key in FORBIDDEN_FIELD_NAMES:
            hits.append(full_path)
        if isinstance(value, Mapping):
            hits.extend(_scan_forbidden_fields(value, path=full_path))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    hits.extend(_scan_forbidden_fields(item, path=full_path))
    return hits


def validate_against_schema(data: dict, schema: dict) -> None:
    hits = _scan_forbidden_fields(data)
    if hits:
        raise SchemaValidationError(f"structured output contains forbidden executable-order field(s): {sorted(hits)}")
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        raise SchemaValidationError(
            "structured output failed schema validation: "
            + "; ".join(f"{'.'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors)
        )


def _to_decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise SchemaValidationError(f"numeric_value {value!r} is not a valid decimal") from exc


def _build_claims(raw_claims: list[dict]) -> tuple[ResearchClaim, ...]:
    claims = []
    for c in raw_claims:
        claims.append(
            ResearchClaim(
                claim_id=c["claim_id"],
                claim_type=c["claim_type"],
                statement=c["statement"],
                evidence_ids=tuple(c["evidence_ids"]),
                numeric_value=_to_decimal_or_none(c.get("numeric_value")),
                unit=c.get("unit"),
                importance=c["importance"],
            )
        )
    return tuple(claims)


def build_role_report(
    data: dict,
    *,
    report_id: str,
    research_run_id: str,
    role: str,
    symbol: str,
    snapshot_id: str,
    model_name: str,
    prompt_version: str,
    schema: dict | None = None,
) -> RoleResearchReport:
    validate_against_schema(data, schema or role_report_json_schema())
    return RoleResearchReport(
        report_id=report_id,
        research_run_id=research_run_id,
        role=role,
        symbol=symbol,
        snapshot_id=snapshot_id,
        stance=data["stance"],
        summary=data["summary"],
        claims=_build_claims(data["claims"]),
        catalysts=tuple(data["catalysts"]),
        risks=tuple(data["risks"]),
        uncertainties=tuple(data["uncertainties"]),
        missing_data_reasons=tuple(data["missing_data_reasons"]),
        model_name=model_name,
        prompt_version=prompt_version,
    )


def build_decision(
    data: dict,
    *,
    decision_id: str,
    research_run_id: str,
    symbol: str,
    snapshot_id: str,
    model_name: str,
    prompt_version: str,
    schema: dict | None = None,
) -> ResearchDecision:
    validate_against_schema(data, schema or decision_json_schema())
    return ResearchDecision(
        decision_id=decision_id,
        research_run_id=research_run_id,
        symbol=symbol,
        snapshot_id=snapshot_id,
        rating=data["rating"],
        confidence=Decimal(str(data["confidence"])),
        thesis=data["thesis"],
        bull_case=data["bull_case"],
        bear_case=data["bear_case"],
        catalysts=tuple(data["catalysts"]),
        risks=tuple(data["risks"]),
        invalidation_conditions=tuple(data["invalidation_conditions"]),
        claims=_build_claims(data["claims"]),
        evidence_ids=tuple(data["evidence_ids"]),
        missing_data_reasons=tuple(data["missing_data_reasons"]),
        model_name=model_name,
        prompt_version=prompt_version,
    )
