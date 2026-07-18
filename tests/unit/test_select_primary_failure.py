"""Unit tests for `research/failure_taxonomy.py::select_primary_failure`
(Milestone 12.1.1 Item 2): deterministic, order-independent primary-failure
selection for an attempt that produced several structured failures.
"""
from __future__ import annotations

from datetime import datetime, timezone

from trading_research.research.failure_taxonomy import (
    STAGE_BUDGET_GATED,
    STAGE_CLAIM_EVIDENCE_VALIDATION,
    STAGE_PROVIDER_RESPONSE,
    STAGE_STRUCTURED_SCHEMA,
    STAGE_UNKNOWN,
    new_failure,
    select_primary_failure,
)

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _failure(*, stage, code, retryable, message="msg", **overrides):
    kwargs = dict(
        research_run_id="run-1", attempt_id="run-1-fundamental-1", role="fundamental", attempt_number=1,
        stage=stage, code=code, message=message, model_name="m", prompt_version="v1",
        schema_version="s1", occurred_at=NOW, retryable=retryable,
    )
    kwargs.update(overrides)
    return new_failure(**kwargs)


def test_empty_list_returns_none():
    assert select_primary_failure([]) is None


def test_reversing_failure_order_produces_the_same_primary_code():
    a = _failure(stage=STAGE_PROVIDER_RESPONSE, code="PROVIDER_TIMEOUT", retryable=True)
    b = _failure(stage=STAGE_CLAIM_EVIDENCE_VALIDATION, code="UNKNOWN_EVIDENCE_ID", retryable=True)
    forward = select_primary_failure([a, b])
    backward = select_primary_failure([b, a])
    assert forward.code == backward.code == "PROVIDER_TIMEOUT"


def test_structural_provider_failure_outranks_claim_validation():
    structural = _failure(stage=STAGE_PROVIDER_RESPONSE, code="PROVIDER_UNAVAILABLE", retryable=False)
    claim = _failure(stage=STAGE_CLAIM_EVIDENCE_VALIDATION, code="UNKNOWN_EVIDENCE_ID", retryable=True)
    primary = select_primary_failure([claim, structural])
    assert primary.code == "PROVIDER_UNAVAILABLE"


def test_non_retryable_protocol_failure_outranks_retryable_validation():
    contract = _failure(stage=STAGE_STRUCTURED_SCHEMA, code="CODEX_USAGE_METADATA_MISSING", retryable=False)
    retryable_schema = _failure(stage=STAGE_STRUCTURED_SCHEMA, code="SCHEMA_TYPE_MISMATCH", retryable=True)
    primary = select_primary_failure([retryable_schema, contract])
    assert primary.code == "CODEX_USAGE_METADATA_MISSING"


def test_budget_gate_outranks_retryable_provider_failure():
    budget = _failure(stage=STAGE_BUDGET_GATED, code="BUDGET_EXHAUSTED", retryable=False)
    retryable_provider = _failure(stage=STAGE_PROVIDER_RESPONSE, code="PROVIDER_TIMEOUT", retryable=True)
    primary = select_primary_failure([retryable_provider, budget])
    assert primary.code == "BUDGET_EXHAUSTED"


def test_unknown_stage_falls_to_lowest_priority_tier_without_crashing():
    diagnostic = _failure(stage=STAGE_UNKNOWN, code="UNCLASSIFIED_VALIDATION_FAILURE", retryable=False)
    structural = _failure(stage=STAGE_PROVIDER_RESPONSE, code="PROVIDER_UNAVAILABLE", retryable=False)
    primary = select_primary_failure([diagnostic, structural])
    assert primary.code == "PROVIDER_UNAVAILABLE"
    # And on its own, an unrecognized/diagnostic-tier stage is still selectable.
    assert select_primary_failure([diagnostic]).code == "UNCLASSIFIED_VALIDATION_FAILURE"


def test_raw_messages_do_not_affect_selection():
    a = _failure(stage=STAGE_PROVIDER_RESPONSE, code="PROVIDER_TIMEOUT", retryable=True, message="aaa retry please")
    b = _failure(
        stage=STAGE_PROVIDER_RESPONSE, code="PROVIDER_RATE_LIMITED", retryable=True,
        message="zzz totally different wording", claim_id=None,
    )
    primary_1 = select_primary_failure([a, b])
    primary_2 = select_primary_failure([b, a])
    assert primary_1.code == primary_2.code
