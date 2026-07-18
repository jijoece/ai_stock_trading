"""Milestone 12.1.1 Item 7: independent model-provider health evidence,
computed from persisted research attempts (not evidence-provider request
rows), and its centralized structural/transient failure-code policy.
"""
from __future__ import annotations

from trading_research.research.model_provider_health_policy import (
    MODEL_PROVIDER_FAILURE_STRUCTURAL,
    MODEL_PROVIDER_FAILURE_TRANSIENT,
    classify_model_provider_failure,
)
from trading_research.shadow.model_provider_health import evaluate_model_provider_health


def _row(**overrides):
    base = {"success": True, "failure_code": None, "failure_retryable": None}
    base.update(overrides)
    return base


# --- centralized classification policy --------------------------------------


def test_authentication_and_unsupported_model_classify_structural():
    """Required test #1."""
    assert classify_model_provider_failure("CODEX_NOT_AUTHENTICATED", False) == MODEL_PROVIDER_FAILURE_STRUCTURAL
    assert classify_model_provider_failure("CODEX_UNSUPPORTED_MODEL", False) == MODEL_PROVIDER_FAILURE_STRUCTURAL


def test_invalid_configuration_and_usage_contract_classify_structural():
    """Required test #2."""
    assert classify_model_provider_failure("CODEX_INVALID_CONFIGURATION", False) == MODEL_PROVIDER_FAILURE_STRUCTURAL
    assert (
        classify_model_provider_failure("CODEX_USAGE_METADATA_MISSING", False) == MODEL_PROVIDER_FAILURE_STRUCTURAL
    )
    assert (
        classify_model_provider_failure("CODEX_REASONING_TOKENS_INVALID", False) == MODEL_PROVIDER_FAILURE_STRUCTURAL
    )


def test_transient_timeout_classifies_transient():
    assert classify_model_provider_failure("CODEX_PROCESS_TIMEOUT", True) == MODEL_PROVIDER_FAILURE_TRANSIENT


def test_unknown_non_retryable_code_fails_closed_to_structural():
    assert classify_model_provider_failure("SOME_FUTURE_UNKNOWN_CODE", False) == MODEL_PROVIDER_FAILURE_STRUCTURAL
    assert classify_model_provider_failure("SOME_FUTURE_UNKNOWN_CODE", None) == MODEL_PROVIDER_FAILURE_STRUCTURAL


def test_unknown_retryable_code_classifies_transient():
    assert classify_model_provider_failure("SOME_FUTURE_UNKNOWN_CODE", True) == MODEL_PROVIDER_FAILURE_TRANSIENT


# --- evidence aggregation -----------------------------------------------


def test_healthy_attempts_report_full_success_rate():
    """Required test #5 (evidence side)."""
    rows = [_row(success=True) for _ in range(3)]
    evidence = evaluate_model_provider_health(rows)
    assert evidence.attempt_count == 3
    assert evidence.success_count == 3
    assert evidence.success_rate == 1.0
    assert evidence.structural_failure is False


def test_one_transient_timeout_does_not_set_structural_failure():
    """Required test #3."""
    rows = [
        _row(success=True),
        _row(success=False, failure_code="CODEX_PROCESS_TIMEOUT", failure_retryable=True),
    ]
    evidence = evaluate_model_provider_health(rows)
    assert evidence.structural_failure is False
    assert evidence.timeout_count == 1
    assert evidence.retryable_failure_count == 1
    assert evidence.non_retryable_failure_count == 0


def test_authentication_failure_sets_structural_failure():
    rows = [_row(success=False, failure_code="CODEX_NOT_AUTHENTICATED", failure_retryable=False)]
    evidence = evaluate_model_provider_health(rows)
    assert evidence.structural_failure is True
    assert evidence.authentication_failure_count == 1
    assert evidence.non_retryable_failure_count == 1
    assert evidence.structural_failure_codes == ("CODEX_NOT_AUTHENTICATED",)


def test_no_attempts_is_empty_not_healthy():
    evidence = evaluate_model_provider_health([])
    assert evidence.attempt_count == 0
    assert evidence.success_rate is None
    assert evidence.structural_failure is False


def test_quota_configuration_protocol_missing_usage_counts():
    rows = [
        _row(success=False, failure_code="CODEX_QUOTA_EXHAUSTED", failure_retryable=False),
        _row(success=False, failure_code="CODEX_INVALID_CONFIGURATION", failure_retryable=False),
        _row(success=False, failure_code="CODEX_SCHEMA_REJECTED", failure_retryable=False),
        _row(success=False, failure_code="CLAUDE_CODE_USAGE_METADATA_MISSING", failure_retryable=False),
        _row(success=False, failure_code="CODEX_RATE_LIMITED", failure_retryable=True),
    ]
    evidence = evaluate_model_provider_health(rows)
    assert evidence.quota_failure_count == 1
    assert evidence.configuration_failure_count == 1
    assert evidence.protocol_failure_count == 1
    assert evidence.missing_usage_failure_count == 1
    assert evidence.rate_limit_count == 1
    assert evidence.structural_failure is True  # every non-rate-limit one here is structural
