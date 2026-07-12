"""Main-process side of the paper-runtime.v1 envelope contract
(docs/milestone-4.md Step 2/6).

Independently implemented from `paper_runtime.protocol` — see that module's
docstring and this package's `errors.py` for why no code is shared across
the process boundary. Both implementations agree only on the JSON shape.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from . import PROTOCOL_VERSION
from .errors import ProtocolViolationError

_RESPONSE_REQUIRED_FIELDS = frozenset(
    {"protocol_version", "request_id", "operation", "runtime_version", "success", "retryable", "error", "payload"}
)


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def build_request_line(operation: str, payload: dict[str, Any], *, request_id: str | None = None) -> tuple[str, str]:
    """Returns (request_id, json_line)."""
    request_id = request_id or new_request_id()
    envelope = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "operation": operation,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    return request_id, json.dumps(envelope)


def parse_response_line(raw_line: str, *, expected_request_id: str, expected_operation: str) -> dict[str, Any]:
    """Strictly parse and validate one response line. Raises
    `ProtocolViolationError` on anything that doesn't match the contract —
    non-JSON output, a missing/extra field, an unknown protocol_version, or
    a request_id/operation that doesn't match what was sent (docs/milestone-
    4.md: "reject ... responses with mismatched request IDs" / "responses
    that do not match the requested operation")."""
    try:
        data = json.loads(raw_line)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProtocolViolationError(f"runtime produced non-JSON stdout: {raw_line!r}") from exc

    if not isinstance(data, dict):
        raise ProtocolViolationError("response must be a JSON object")

    missing = _RESPONSE_REQUIRED_FIELDS - set(data.keys())
    if missing:
        raise ProtocolViolationError(f"response missing required fields: {sorted(missing)}")
    extra = set(data.keys()) - _RESPONSE_REQUIRED_FIELDS
    if extra:
        raise ProtocolViolationError(f"response has unexpected fields: {sorted(extra)}")

    if data["protocol_version"] != PROTOCOL_VERSION:
        raise ProtocolViolationError(
            f"unsupported protocol_version {data['protocol_version']!r} — this client speaks {PROTOCOL_VERSION!r}"
        )
    if data["request_id"] != expected_request_id:
        raise ProtocolViolationError(
            f"response request_id {data['request_id']!r} does not match sent request_id {expected_request_id!r}"
        )
    if data["operation"] != expected_operation:
        raise ProtocolViolationError(
            f"response operation {data['operation']!r} does not match requested operation {expected_operation!r}"
        )
    if not isinstance(data["success"], bool):
        raise ProtocolViolationError("response.success must be a boolean")
    if not isinstance(data["retryable"], bool):
        raise ProtocolViolationError("response.retryable must be a boolean")
    if not isinstance(data["payload"], dict):
        raise ProtocolViolationError("response.payload must be a JSON object")
    if not data["success"]:
        error = data["error"]
        if not isinstance(error, dict) or "code" not in error or "message" not in error:
            raise ProtocolViolationError("a failed response must carry error.code and error.message")

    return data
