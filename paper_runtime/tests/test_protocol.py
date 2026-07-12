from __future__ import annotations

import json

import pytest

from trading_paper_runtime import PROTOCOL_VERSION
from trading_paper_runtime.errors import ErrorCode, RuntimeOperationError
from trading_paper_runtime.protocol import (
    build_error_response,
    build_success_response,
    parse_request_line,
)


def _valid_request(**overrides) -> dict:
    base = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": "req-1",
        "operation": "health",
        "sent_at": "2026-07-12T18:00:00Z",
        "payload": {},
    }
    base.update(overrides)
    return base


def test_parse_valid_request():
    request = parse_request_line(json.dumps(_valid_request()))
    assert request.protocol_version == PROTOCOL_VERSION
    assert request.operation == "health"
    assert request.request_id == "req-1"


def test_rejects_unknown_protocol_version():
    with pytest.raises(RuntimeOperationError) as exc:
        parse_request_line(json.dumps(_valid_request(protocol_version="paper-runtime.v2")))
    assert exc.value.code == ErrorCode.UNKNOWN_PROTOCOL_VERSION


def test_rejects_unknown_operation():
    with pytest.raises(RuntimeOperationError) as exc:
        parse_request_line(json.dumps(_valid_request(operation="delete_everything")))
    assert exc.value.code == ErrorCode.UNKNOWN_OPERATION


def test_rejects_malformed_json():
    with pytest.raises(RuntimeOperationError) as exc:
        parse_request_line("{not json")
    assert exc.value.code == ErrorCode.MALFORMED_REQUEST


def test_rejects_missing_required_field():
    request = _valid_request()
    del request["sent_at"]
    with pytest.raises(RuntimeOperationError) as exc:
        parse_request_line(json.dumps(request))
    assert exc.value.code == ErrorCode.MALFORMED_REQUEST


def test_rejects_unexpected_extra_field():
    request = _valid_request(extra_field="not allowed")
    with pytest.raises(RuntimeOperationError) as exc:
        parse_request_line(json.dumps(request))
    assert exc.value.code == ErrorCode.MALFORMED_REQUEST


def test_rejects_non_dict_payload():
    request = _valid_request(payload="not a dict")
    with pytest.raises(RuntimeOperationError) as exc:
        parse_request_line(json.dumps(request))
    assert exc.value.code == ErrorCode.MALFORMED_PAYLOAD


def test_success_response_echoes_request_id_and_operation():
    request = parse_request_line(json.dumps(_valid_request()))
    response = build_success_response(request, runtime_version="test-runtime-1", payload={"ok": True})
    assert response.success is True
    assert response.retryable is False
    assert response.error is None
    assert response.request_id == "req-1"
    assert response.operation == "health"
    line = response.to_json_line()
    parsed = json.loads(line)
    assert parsed["payload"] == {"ok": True}


def test_error_response_carries_structured_error():
    err = RuntimeOperationError(ErrorCode.BROKER_ERROR, "boom", retryable=True)
    response = build_error_response("req-2", "get_order", runtime_version="test-runtime-1", error=err)
    assert response.success is False
    assert response.retryable is True
    assert response.error == {"code": ErrorCode.BROKER_ERROR, "message": "boom"}


def test_secret_redaction_in_logging_filter():
    import logging

    from trading_paper_runtime.logging_config import _RedactingFilter

    import os

    os.environ["ALPACA_API_KEY"] = "super-secret-key"
    try:
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="using key super-secret-key to connect", args=(), exc_info=None,
        )
        assert _RedactingFilter().filter(record) is True
        assert "super-secret-key" not in record.getMessage()
        assert "REDACTED" in record.getMessage()
    finally:
        del os.environ["ALPACA_API_KEY"]
