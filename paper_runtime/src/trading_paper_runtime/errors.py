"""Structured runtime errors (Milestone 4, docs/milestone-4.md Step 2/3).

Every failure the dispatcher returns to the main process carries a stable
`code` and an explicit `retryable` flag — never a bare exception string with
no machine-readable signal. `retryable=True` is reserved for conditions the
caller can safely retry without risking a duplicate broker submission
(e.g. a transient broker timeout on a *read* operation); it is never set on
an ambiguous `submit_order` outcome, which must be resolved by lookup, not
retry (docs/milestone-4.md Step 8).
"""
from __future__ import annotations


class ErrorCode:
    UNKNOWN_PROTOCOL_VERSION = "UNKNOWN_PROTOCOL_VERSION"
    UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
    MALFORMED_REQUEST = "MALFORMED_REQUEST"
    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_PAPER_MODE = "NOT_PAPER_MODE"
    CREDENTIALS_MISSING = "CREDENTIALS_MISSING"
    CREDENTIALS_INVALID = "CREDENTIALS_INVALID"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    UNKNOWN_BROKER_STATUS = "UNKNOWN_BROKER_STATUS"
    BROKER_ERROR = "BROKER_ERROR"
    BROKER_TIMEOUT = "BROKER_TIMEOUT"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# Error codes that are always safe to retry the *same read-only* request for.
# submit_order ambiguity is deliberately never in this set.
RETRYABLE_CODES = frozenset({ErrorCode.BROKER_TIMEOUT})


class RuntimeOperationError(RuntimeError):
    """Raised by dispatcher/gateway code; carries a structured error code and
    an explicit retryability verdict. `retryable` may be overridden per-call
    (e.g. a broker timeout on submit_order is surfaced as SUBMISSION_UNKNOWN,
    not retryable, even though BROKER_TIMEOUT is normally retryable for reads)."""

    def __init__(self, code: str, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable if retryable is not None else (code in RETRYABLE_CODES)
