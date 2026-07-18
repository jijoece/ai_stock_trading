"""Unit tests for `research/codex_failure_classifier.py` (Milestone 12.1
Item 3): one classifier reachable identically from a nonzero exit or a
`turn.failed` message."""
from __future__ import annotations

import pytest

from trading_research.research.codex_failure_classifier import classify_codex_diagnostic
from trading_research.research.errors import ProviderRateLimitError, ProviderTransientError, ProviderUnavailableError


@pytest.mark.parametrize(
    ("diagnostic", "expected_code", "expected_type", "expected_retryable"),
    [
        ("authentication failed", "CODEX_NOT_AUTHENTICATED", ProviderUnavailableError, False),
        ("not authenticated with ChatGPT", "CODEX_NOT_AUTHENTICATED", ProviderUnavailableError, False),
        ("your quota is exhausted", "CODEX_QUOTA_EXHAUSTED", ProviderUnavailableError, False),
        ("rate limit exceeded, please retry", "CODEX_RATE_LIMITED", ProviderRateLimitError, True),
        ("HTTP 429 too many requests", "CODEX_RATE_LIMITED", ProviderRateLimitError, True),
        ("connection reset by peer", "CODEX_NETWORK_FAILURE", ProviderTransientError, True),
        ("dns resolution failed", "CODEX_NETWORK_FAILURE", ProviderTransientError, True),
        ("service temporarily unavailable", "CODEX_TRANSIENT_FAILURE", ProviderTransientError, True),
        ("internal server error 500", "CODEX_TRANSIENT_FAILURE", ProviderTransientError, True),
        ("unsupported model requested", "CODEX_UNSUPPORTED_MODEL", ProviderUnavailableError, False),
        ("invalid configuration supplied", "CODEX_INVALID_CONFIGURATION", ProviderUnavailableError, False),
        ("json schema validation failed", "CODEX_SCHEMA_REJECTED", ProviderUnavailableError, False),
        ("something completely unrecognized happened", "CODEX_PROCESS_EXITED", ProviderUnavailableError, False),
        ("", "CODEX_PROCESS_EXITED", ProviderUnavailableError, False),
    ],
)
def test_classify_codex_diagnostic(diagnostic, expected_code, expected_type, expected_retryable):
    result = classify_codex_diagnostic(diagnostic)
    assert result.code == expected_code
    assert result.error_type is expected_type
    assert result.retryable is expected_retryable


def test_classification_never_echoes_raw_diagnostic_text():
    """The fixed per-category message must never embed the raw input."""
    secret_looking_diagnostic = "authentication failed: token=sk-ant-super-secret-value"
    result = classify_codex_diagnostic(secret_looking_diagnostic)
    assert "sk-ant-super-secret-value" not in result.message


def test_oversized_diagnostic_is_bounded_not_rejected():
    huge = "authentication failed " + ("x" * 100_000)
    result = classify_codex_diagnostic(huge)
    assert result.code == "CODEX_NOT_AUTHENTICATED"
