"""Main-process runtime client (docs/milestone-4.md Step 6).

`RuntimeClient` never imports LumiBot and never imports anything from the
`paper_runtime` package — it only knows the wire-level JSON contract
(`protocol.py`). It is programmed against a small `Transport` Protocol so
tests can inject a fake, in-memory transport instead of spawning a real
subprocess (docs/milestone-4.md Step 16.B); `SubprocessTransport` is the
only implementation that actually launches a child process.
"""
from __future__ import annotations

import queue
import subprocess
import threading
from typing import Protocol

from . import PROTOCOL_VERSION
from .errors import (
    RuntimeCapabilityError,
    RuntimeOperationError,
    RuntimeRequestTimeoutError,
    RuntimeStartupTimeoutError,
    RuntimeUnavailableError,
)
from .protocol import build_request_line, parse_response_line

# Operations for which a request timeout is safe to treat as retryable by
# the caller — never true for submit_order (docs/milestone-4.md Step 6/8:
# "avoid blind retries for order submission").
_RETRYABLE_ON_TIMEOUT = frozenset(
    {
        "health", "capabilities", "get_order", "list_open_orders", "list_recent_orders", "get_account",
        "list_positions", "ACCOUNT_CHECK", "PREVIEW_LIMIT_ORDER", "GET_ORDER_BY_CLIENT_ID", "GET_ORDER",
        "LIST_ORDER_FILLS", "GET_POSITIONS", "GET_ACCOUNT_SNAPSHOT",
    }
)


class Transport(Protocol):
    def write_line(self, line: str) -> None: ...

    def read_line(self, timeout: float) -> str | None:
        """Return the next stdout line, or None on EOF (process exited).
        Raise `queue.Empty` on timeout (no line available yet)."""
        ...

    def read_stderr(self) -> list[str]:
        """Drain and return any buffered stderr lines (diagnostics only —
        never protocol data)."""
        ...

    def is_alive(self) -> bool: ...

    def terminate(self, timeout: float) -> None: ...


class SubprocessTransport:
    """Spawns the isolated runtime as a child process and speaks JSON Lines
    over its stdin/stdout. stderr is captured on a separate thread/queue so
    it can never be mistaken for protocol output."""

    def __init__(self, command: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self._process = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._stdout_queue: queue.Queue[str | None] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()
        self._stdout_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _pump_stdout(self) -> None:
        try:
            for line in self._process.stdout:
                self._stdout_queue.put(line.rstrip("\n"))
        finally:
            self._stdout_queue.put(None)  # sentinel: EOF / process exited

    def _pump_stderr(self) -> None:
        for line in self._process.stderr:
            with self._stderr_lock:
                self._stderr_lines.append(line.rstrip("\n"))

    def write_line(self, line: str) -> None:
        self._process.stdin.write(line + "\n")
        self._process.stdin.flush()

    def read_line(self, timeout: float) -> str | None:
        return self._stdout_queue.get(timeout=timeout)

    def read_stderr(self) -> list[str]:
        with self._stderr_lock:
            lines, self._stderr_lines = self._stderr_lines, []
        return lines

    def is_alive(self) -> bool:
        return self._process.poll() is None

    def terminate(self, timeout: float) -> None:
        if not self.is_alive():
            return
        try:
            self._process.stdin.close()
        except Exception:
            pass
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._process.kill()


class RuntimeClient:
    """Connects to, health-checks, and speaks paper-runtime.v2 to an
    isolated runtime process. `start()` must succeed (protocol compatible,
    paper mode proven, real-money capability false) before any other
    operation is attempted — there is no degraded/partial mode."""

    def __init__(
        self,
        command: list[str],
        *,
        transport_factory=SubprocessTransport,
        startup_timeout_seconds: float = 15.0,
        request_timeout_seconds: float = 30.0,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._command = command
        self._transport_factory = transport_factory
        self._startup_timeout_seconds = startup_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._cwd = cwd
        self._env = env
        self._transport: Transport | None = None
        self._runtime_version: str | None = None
        self.last_health: dict | None = None
        self.last_capabilities: dict | None = None

    def start(self) -> None:
        self._transport = self._transport_factory(self._command, cwd=self._cwd, env=self._env)
        try:
            health = self._request("health", {}, timeout=self._startup_timeout_seconds)
        except RuntimeRequestTimeoutError as exc:
            raise RuntimeStartupTimeoutError(
                f"runtime did not answer a health check within {self._startup_timeout_seconds}s"
            ) from exc
        self.last_health = health
        if health.get("protocol_version") != PROTOCOL_VERSION:
            raise RuntimeCapabilityError("runtime health protocol version does not match paper-runtime.v2")
        if not health.get("paper_endpoint_verified"):
            raise RuntimeCapabilityError(
                "runtime did not verify a paper-trading endpoint — refusing to use it "
                f"(health={health!r})"
            )
        if health.get("broker_mode") != "paper":
            raise RuntimeCapabilityError(f"runtime broker_mode is not 'paper': {health.get('broker_mode')!r}")
        if not health.get("real_money_disabled", False):
            raise RuntimeCapabilityError("runtime does not report real_money_disabled=true — refusing to use it")

        capabilities = self._request("capabilities", {}, timeout=self._startup_timeout_seconds)
        self.last_capabilities = capabilities
        if capabilities.get("real_money"):
            raise RuntimeCapabilityError("runtime capabilities report real_money=true — refusing to use it")
        if capabilities.get("short_selling") or capabilities.get("options") or capabilities.get("margin"):
            raise RuntimeCapabilityError(
                f"runtime capabilities exceed this milestone's allowed scope: {capabilities!r}"
            )
        if capabilities.get("supported_order_types") != ["LIMIT"]:
            raise RuntimeCapabilityError("runtime must expose LIMIT as its only order type")
        if capabilities.get("supported_sides") != ["BUY", "SELL"]:
            raise RuntimeCapabilityError("runtime must expose only BUY and closing SELL sides")

    def shutdown(self, *, timeout: float = 5.0) -> None:
        if self._transport is not None:
            self._transport.terminate(timeout)

    def diagnostics(self) -> list[str]:
        """Drain stderr without crossing third-party text into the main process.

        LumiBot/SDK diagnostics are not part of the protocol and could contain
        provider-generated request context, so only a bounded count is exposed.
        """
        if self._transport is None:
            return []
        lines = self._transport.read_stderr()
        return [f"{len(lines)} isolated-runtime stderr line(s) suppressed"] if lines else []

    # -- low-level request/response ------------------------------------

    def _request(self, operation: str, payload: dict, *, timeout: float | None = None) -> dict:
        if self._transport is None:
            raise RuntimeUnavailableError("runtime client has not been started")
        timeout = timeout if timeout is not None else self._request_timeout_seconds
        request_id, line = build_request_line(operation, payload)
        if not self._transport.is_alive():
            raise RuntimeUnavailableError(
                f"runtime process is not running — cannot send {operation!r} "
                f"(stderr: {self.diagnostics()!r})"
            )
        self._transport.write_line(line)
        try:
            raw = self._transport.read_line(timeout)
        except queue.Empty as exc:
            retryable = operation in _RETRYABLE_ON_TIMEOUT
            raise RuntimeRequestTimeoutError(
                f"no response to {operation!r} (request_id={request_id}) within {timeout}s", retryable=retryable,
            ) from exc

        if raw is None:
            # The process exited mid-request. This must never be interpreted
            # as a fill, rejection, or any other order outcome.
            raise RuntimeUnavailableError(
                f"runtime process exited while awaiting a response to {operation!r} "
                f"(request_id={request_id}); this is not an order outcome — query get_order once the "
                f"runtime is available again (stderr: {self.diagnostics()!r})"
            )

        response = parse_response_line(raw, expected_request_id=request_id, expected_operation=operation)
        if not response["success"]:
            error = response["error"]
            raise RuntimeOperationError(error["code"], error["message"], retryable=response["retryable"])
        return response["payload"]

    # -- typed operations -------------------------------------------------

    def health(self) -> dict:
        return self._request("health", {})

    def capabilities(self) -> dict:
        return self._request("capabilities", {})

    def submit_order(self, intent_payload: dict) -> dict:
        """Never retried internally. A caller that gets
        `RuntimeRequestTimeoutError`/`RuntimeUnavailableError` from this call
        must call `get_order` with the same idempotency key before deciding
        whether to submit again — never resubmit blindly."""
        return self._request("submit_order", intent_payload)

    def get_order(self, client_order_id: str) -> dict | None:
        try:
            return self._request("get_order", {"client_order_id": client_order_id})
        except RuntimeOperationError as exc:
            if exc.code == "UNKNOWN_ORDER":
                return None
            raise

    def list_open_orders(self) -> list[dict]:
        return self._request("list_open_orders", {})["orders"]

    def list_recent_orders(self, limit: int = 50) -> list[dict]:
        return self._request("list_recent_orders", {"limit": limit})["orders"]

    def get_account(self) -> dict:
        return self._request("get_account", {})

    def list_positions(self) -> list[dict]:
        return self._request("list_positions", {})["positions"]

    def cancel_paper_order(self, client_order_id: str) -> dict:
        return self._request("cancel_paper_order", {"client_order_id": client_order_id})

    # -- Milestone 11 normalized external-paper operations ----------------

    def account_check(self, book_id: str) -> dict:
        return self._request("ACCOUNT_CHECK", {"book_id": book_id})

    def preview_limit_order(self, payload: dict) -> dict:
        return self._request("PREVIEW_LIMIT_ORDER", payload)

    def submit_limit_order(self, payload: dict) -> dict:
        """Mutation is never retried by this client."""
        return self._request("SUBMIT_LIMIT_ORDER", payload)

    def get_order_by_client_order_id(self, book_id: str, client_order_id: str) -> dict | None:
        result = self._request(
            "GET_ORDER_BY_CLIENT_ID", {"book_id": book_id, "client_order_id": client_order_id},
        )
        return result.get("order") if result.get("found") else None

    def get_order_by_broker_order_id(self, book_id: str, broker_order_id: str) -> dict | None:
        result = self._request("GET_ORDER", {"book_id": book_id, "broker_order_id": broker_order_id})
        return result.get("order") if result.get("found") else None

    def cancel_external_order(self, book_id: str, client_order_id: str, account_fingerprint: str) -> dict:
        return self._request(
            "CANCEL_ORDER",
            {
                "book_id": book_id, "client_order_id": client_order_id,
                "account_fingerprint": account_fingerprint,
            },
        )

    def list_order_fills(self, book_id: str, client_order_id: str) -> list[dict]:
        return self._request(
            "LIST_ORDER_FILLS", {"book_id": book_id, "client_order_id": client_order_id},
        )["fills"]

    def get_external_positions(self, book_id: str) -> dict:
        return self._request("GET_POSITIONS", {"book_id": book_id})

    def get_external_account_snapshot(self, book_id: str) -> dict:
        return self._request("GET_ACCOUNT_SNAPSHOT", {"book_id": book_id})
