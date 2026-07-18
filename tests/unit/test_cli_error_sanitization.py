"""Part 17: unexpected CLI failures must never leak raw exception text
(filesystem paths, subprocess detail) — only a stable code and a bounded,
generic message.
"""
from __future__ import annotations

from trading_research.cli import _bounded_message, _sanitized_cli_error
from trading_research.paper_books.external_broker import ExternalPaperError
from trading_research.runtime.client.errors import (
    RuntimeOperationError,
    RuntimeUnavailableError,
)


def test_unexpected_exception_never_leaks_its_own_text():
    secret_path = "/Users/someone/.env: ALPACA_API_KEY=super-secret-value"
    exc = RuntimeError(secret_path)
    result = _sanitized_cli_error(exc)
    assert result["code"] == "EXTERNAL_RUNTIME_ERROR"
    assert secret_path not in result["message"]
    assert "unexpected internal error" in result["message"]


def test_external_paper_error_passes_through_bounded():
    exc = ExternalPaperError("INTENT_NOT_FOUND", "paper intent 'x' was not found in book BASELINE")
    result = _sanitized_cli_error(exc)
    assert result["code"] == "INTENT_NOT_FOUND"
    assert result["message"] == "paper intent 'x' was not found in book BASELINE"


def test_runtime_operation_error_passes_through_bounded():
    exc = RuntimeOperationError("VALIDATION_FAILED", "quantity must be positive", retryable=False)
    result = _sanitized_cli_error(exc)
    assert result["code"] == "VALIDATION_FAILED"
    assert result["message"] == "quantity must be positive"


def test_runtime_unavailable_error_never_leaks_process_detail():
    exc = RuntimeUnavailableError("Popen failed: [Errno 2] No such file or directory: '/opt/secret/venv/bin/python3'")
    result = _sanitized_cli_error(exc)
    assert result["code"] == "RUNTIMEUNAVAILABLEERROR"
    assert "/opt/secret/venv" not in result["message"]
    assert "unavailable" in result["message"]


def test_bounded_message_truncates_oversized_text():
    long_message = "x" * 10_000
    bounded = _bounded_message(long_message)
    assert len(bounded) <= 520
    assert bounded.endswith("...(truncated)")


def test_bounded_message_leaves_short_text_untouched():
    assert _bounded_message("short message") == "short message"
