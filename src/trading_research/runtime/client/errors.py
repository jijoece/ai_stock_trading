"""Runtime-client errors (docs/milestone-4.md Step 6).

Independently implemented from `paper_runtime`'s `errors.py` — deliberately
not a shared installable dependency (docs/milestone-4.md Step 4: "Avoid
creating circular installation requirements"). Both sides agree only on the
wire-level JSON contract (protocol_version, field names), never on Python
types.
"""
from __future__ import annotations


class RuntimeClientError(RuntimeError):
    """Base class for every error this client can raise."""


class RuntimeUnavailableError(RuntimeClientError):
    """The runtime process is not running, exited, or could not be reached.
    Never implies an order outcome — a process exit is not a fill, a
    rejection, or a cancellation."""


class RuntimeStartupTimeoutError(RuntimeClientError):
    """The runtime did not respond to a health check within the configured
    startup timeout."""


class RuntimeRequestTimeoutError(RuntimeClientError):
    """A request did not receive a response within the configured request
    timeout. `retryable` distinguishes safe-to-retry reads from
    `submit_order`, which must never be blindly retried on timeout — the
    caller must look the order up by client-order id instead
    (docs/milestone-4.md Step 8)."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ProtocolViolationError(RuntimeClientError):
    """The runtime's response violated the paper-runtime.v2 contract: non-JSON
    stdout, an unknown protocol_version, a mismatched request_id/operation,
    or a malformed envelope shape. The client must reject these outright
    rather than attempt best-effort recovery."""


class RuntimeCapabilityError(RuntimeClientError):
    """The runtime failed a mandatory capability check at startup: it is not
    provably in paper mode, it reports a real-money capability, or its
    protocol version is incompatible. The client refuses to proceed at all
    when this is raised — there is no partial/degraded mode."""


class RuntimeOperationError(RuntimeClientError):
    """The runtime returned `success: false` for a request. Carries the
    runtime's own structured error code/message/retryable verdict — never
    fabricated by the client."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
