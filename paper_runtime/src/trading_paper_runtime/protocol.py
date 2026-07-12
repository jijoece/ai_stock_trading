"""paper-runtime.v1 envelope parsing/serialization (docs/milestone-4.md Step 2).

JSON Lines over stdin/stdout: exactly one JSON object per line, no pickle,
no Python object serialization. Strict validation on the way in — unknown
protocol versions, unknown operations, malformed payloads, and unexpected
extra top-level fields are all rejected with a structured error rather than
silently ignored or best-effort-coerced.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import PROTOCOL_VERSION
from .errors import ErrorCode, RuntimeOperationError

SUPPORTED_OPERATIONS = (
    "health",
    "capabilities",
    "submit_order",
    "get_order",
    "list_open_orders",
    "list_recent_orders",
    "get_account",
    "list_positions",
    "cancel_paper_order",
)

_REQUEST_REQUIRED_FIELDS = frozenset({"protocol_version", "request_id", "operation", "sent_at", "payload"})


@dataclass(frozen=True)
class RequestEnvelope:
    protocol_version: str
    request_id: str
    operation: str
    sent_at: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResponseEnvelope:
    protocol_version: str
    request_id: str
    operation: str
    runtime_version: str
    success: bool
    retryable: bool
    error: dict[str, Any] | None
    payload: dict[str, Any]

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "protocol_version": self.protocol_version,
                "request_id": self.request_id,
                "operation": self.operation,
                "runtime_version": self.runtime_version,
                "success": self.success,
                "retryable": self.retryable,
                "error": self.error,
                "payload": self.payload,
            },
            sort_keys=True,
        )


def parse_request_line(line: str) -> RequestEnvelope:
    """Parse and strictly validate one request line. Raises
    `RuntimeOperationError` (never a bare exception) on any malformed input —
    the dispatcher is responsible for turning that into an error response
    keyed by whatever request_id/operation could be salvaged, or none."""
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeOperationError(ErrorCode.MALFORMED_REQUEST, f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeOperationError(ErrorCode.MALFORMED_REQUEST, "request must be a JSON object")

    extra = set(data.keys()) - _REQUEST_REQUIRED_FIELDS
    if extra:
        raise RuntimeOperationError(
            ErrorCode.MALFORMED_REQUEST, f"request contains unexpected fields: {sorted(extra)}"
        )
    missing = _REQUEST_REQUIRED_FIELDS - set(data.keys())
    if missing:
        raise RuntimeOperationError(
            ErrorCode.MALFORMED_REQUEST, f"request missing required fields: {sorted(missing)}"
        )

    protocol_version = data["protocol_version"]
    if protocol_version != PROTOCOL_VERSION:
        raise RuntimeOperationError(
            ErrorCode.UNKNOWN_PROTOCOL_VERSION,
            f"unsupported protocol_version {protocol_version!r} — this runtime speaks {PROTOCOL_VERSION!r}",
        )

    operation = data["operation"]
    if operation not in SUPPORTED_OPERATIONS:
        raise RuntimeOperationError(ErrorCode.UNKNOWN_OPERATION, f"unknown operation {operation!r}")

    request_id = data["request_id"]
    if not isinstance(request_id, str) or not request_id:
        raise RuntimeOperationError(ErrorCode.MALFORMED_REQUEST, "request_id must be a non-empty string")

    sent_at = data["sent_at"]
    if not isinstance(sent_at, str) or not sent_at:
        raise RuntimeOperationError(ErrorCode.MALFORMED_REQUEST, "sent_at must be a non-empty ISO8601 string")

    payload = data["payload"]
    if not isinstance(payload, dict):
        raise RuntimeOperationError(ErrorCode.MALFORMED_PAYLOAD, "payload must be a JSON object")

    return RequestEnvelope(
        protocol_version=protocol_version, request_id=request_id, operation=operation,
        sent_at=sent_at, payload=payload,
    )


def build_success_response(
    request: RequestEnvelope, *, runtime_version: str, payload: dict[str, Any]
) -> ResponseEnvelope:
    return ResponseEnvelope(
        protocol_version=PROTOCOL_VERSION, request_id=request.request_id, operation=request.operation,
        runtime_version=runtime_version, success=True, retryable=False, error=None, payload=payload,
    )


def build_error_response(
    request_id: str, operation: str, *, runtime_version: str, error: RuntimeOperationError,
) -> ResponseEnvelope:
    return ResponseEnvelope(
        protocol_version=PROTOCOL_VERSION, request_id=request_id, operation=operation,
        runtime_version=runtime_version, success=False, retryable=error.retryable,
        error={"code": error.code, "message": error.message}, payload={},
    )
