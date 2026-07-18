"""Milestone 12.1.1 Item 7: independent model-provider health evidence,
computed from persisted research attempts (not evidence-provider request
rows), and its centralized structural/transient failure-code policy.
"""
from __future__ import annotations

from trading_research.research.model_provider_health_policy import (
    MODEL_PROVIDER_FAILURE_STRUCTURAL,
    MODEL_PROVIDER_FAILURE_TRANSIENT,
    UNCLASSIFIED_STRUCTURAL_FAILURE,
    classify_model_provider_failure,
)
from trading_research.shadow.model_provider_health import (
    evaluate_model_provider_health,
    model_provider_health_scope,
)


def _row(**overrides):
    base = {
        "provider": "codex", "model_name": "gpt-test", "success": True,
        "failure_code": None, "failure_retryable": None,
    }
    base.update(overrides)
    return base


def _evaluate(rows, *, provider="codex", model="gpt-test", config_hash="a" * 64):
    return evaluate_model_provider_health(
        rows, expected_provider=provider, expected_model=model,
        provider_configuration_hash=config_hash,
    )


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
    evidence = _evaluate(rows)
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
    evidence = _evaluate(rows)
    assert evidence.structural_failure is False
    assert evidence.timeout_count == 1
    assert evidence.retryable_failure_count == 1
    assert evidence.non_retryable_failure_count == 0


def test_authentication_failure_sets_structural_failure():
    rows = [_row(success=False, failure_code="CODEX_NOT_AUTHENTICATED", failure_retryable=False)]
    evidence = _evaluate(rows)
    assert evidence.structural_failure is True
    assert evidence.authentication_failure_count == 1
    assert evidence.non_retryable_failure_count == 1
    assert evidence.structural_failure_codes == ("CODEX_NOT_AUTHENTICATED",)


def test_no_attempts_is_empty_not_healthy():
    evidence = _evaluate([])
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
    evidence = _evaluate(rows)
    assert evidence.quota_failure_count == 1
    assert evidence.configuration_failure_count == 1
    assert evidence.protocol_failure_count == 1
    assert evidence.missing_usage_failure_count == 1
    assert evidence.rate_limit_count == 1
    assert evidence.structural_failure is True  # every non-rate-limit one here is structural


def test_code_less_non_retryable_and_unknown_retryability_are_structural():
    evidence = _evaluate([
        _row(success=False, failure_code=None, failure_retryable=False),
        _row(success=False, failure_code=None, failure_retryable=None),
    ])
    assert evidence.structural_failure is True
    assert evidence.structural_failure_count == 2
    assert evidence.unclassified_structural_failure_count == 2
    assert evidence.structural_failure_codes == (UNCLASSIFIED_STRUCTURAL_FAILURE,)


def test_unknown_code_respects_conservative_retryability():
    evidence = _evaluate([
        _row(success=False, failure_code="FUTURE_FAILURE", failure_retryable=False),
        _row(success=False, failure_code="FUTURE_RETRYABLE", failure_retryable=True),
    ])
    assert evidence.structural_failure_count == 1
    assert evidence.transient_failure_count == 1


def test_expected_provider_and_model_filter_excludes_other_attempts():
    evidence = _evaluate([
        _row(provider="codex", model_name="gpt-test", success=False, failure_code="CODEX_PROCESS_TIMEOUT", failure_retryable=True),
        _row(provider="claude_code", model_name="gpt-test", success=True),
        _row(provider="codex", model_name="other-model", success=True),
    ])
    assert evidence.attempt_count == 1
    assert evidence.failure_count == 1
    assert evidence.excluded_attempt_count == 2


def test_each_real_provider_affects_only_its_exact_health_partition():
    rows = [
        _row(provider="codex", model_name="gpt-test", success=False, failure_code="CODEX_PROCESS_TIMEOUT", failure_retryable=True),
        _row(provider="claude_code", model_name="claude-cli", success=True),
        _row(provider="anthropic", model_name="claude-api", success=True),
    ]
    codex = _evaluate(rows)
    claude_code = _evaluate(rows, provider="claude_code", model="claude-cli")
    anthropic = _evaluate(rows, provider="anthropic", model="claude-api")
    assert codex.failure_count == 1 and codex.success_count == 0
    assert claude_code.success_count == 1 and claude_code.failure_count == 0
    assert anthropic.success_count == 1 and anthropic.failure_count == 0
    assert codex.excluded_attempt_count == claude_code.excluded_attempt_count == anthropic.excluded_attempt_count == 2


def test_empty_expected_provider_sample_is_not_healthy():
    evidence = _evaluate([_row(provider="anthropic", model_name="claude-test")])
    assert evidence.attempt_count == 0
    assert evidence.success_rate is None


def test_fixture_provider_health_is_not_applicable():
    evidence = _evaluate(
        [_row(provider="deterministic", model_name="deterministic-v1")],
        provider="deterministic", model="deterministic-v1",
    )
    assert evidence.applicable is False
    assert evidence.attempt_count == 0


def test_provider_model_and_configuration_each_create_separate_scopes():
    codex = model_provider_health_scope(
        expected_provider="codex", expected_model="gpt-a", provider_configuration_hash="a" * 64,
    )
    other_model = model_provider_health_scope(
        expected_provider="codex", expected_model="gpt-b", provider_configuration_hash="a" * 64,
    )
    other_config = model_provider_health_scope(
        expected_provider="codex", expected_model="gpt-a", provider_configuration_hash="b" * 64,
    )
    anthropic = model_provider_health_scope(
        expected_provider="anthropic", expected_model="gpt-a", provider_configuration_hash="a" * 64,
    )
    assert len({codex, other_model, other_config, anthropic}) == 4
    assert "/" not in codex
