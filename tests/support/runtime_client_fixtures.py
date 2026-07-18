"""Fake `Transport` for `runtime.client.process_client.RuntimeClient` tests
(docs/milestone-4.md Step 16.B: "Use a fake subprocess or fake transport").

No real subprocess is ever spawned by these tests — `FakeTransport` is
handed to `RuntimeClient` via its `transport_factory` constructor argument,
matching `SubprocessTransport`'s `(command, cwd=, env=)` call signature but
returning a pre-built, fully scripted fake instead. Responses are queued as
*actions* (not raw JSON up front) because `request_id` is generated fresh
per request — each action is resolved against the most recently written
request line at `read_line` time, echoing its real request_id/operation.
"""
from __future__ import annotations

import json
import queue
from collections import deque


class FakeTransport:
    def __init__(self) -> None:
        self.written_lines: list[str] = []
        self._actions: deque[tuple] = deque()
        self._alive = True
        self.terminated = False

    # -- test-side scripting -----------------------------------------------

    def queue_success(self, payload: dict, *, operation: str | None = None) -> None:
        self._actions.append(("success", payload, operation))

    def queue_failure(self, code: str, message: str, *, retryable: bool = False) -> None:
        self._actions.append(("failure", code, message, retryable))

    def queue_raw_line(self, line: str) -> None:
        self._actions.append(("raw", line))

    def queue_timeout(self) -> None:
        self._actions.append(("timeout",))

    def queue_eof(self) -> None:
        self._actions.append(("eof",))

    def simulate_crash(self) -> None:
        self._alive = False

    # -- Transport protocol --------------------------------------------------

    def write_line(self, line: str) -> None:
        self.written_lines.append(line)

    def read_line(self, timeout: float) -> str | None:
        if not self._actions:
            return None
        action = self._actions.popleft()
        kind = action[0]
        if kind == "timeout":
            raise queue.Empty()
        if kind == "eof":
            return None
        if kind == "raw":
            return action[1]

        sent = json.loads(self.written_lines[-1])
        if kind == "success":
            _, payload, operation = action
            return json.dumps(
                {
                    "protocol_version": sent["protocol_version"], "request_id": sent["request_id"],
                    "operation": operation or sent["operation"], "runtime_version": "fake-runtime-1",
                    "success": True, "retryable": False, "error": None, "payload": payload,
                }
            )
        if kind == "failure":
            _, code, message, retryable = action
            return json.dumps(
                {
                    "protocol_version": sent["protocol_version"], "request_id": sent["request_id"],
                    "operation": sent["operation"], "runtime_version": "fake-runtime-1",
                    "success": False, "retryable": retryable, "error": {"code": code, "message": message},
                    "payload": {},
                }
            )
        raise AssertionError(f"unknown scripted action {action!r}")

    def read_stderr(self) -> list[str]:
        return []

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self, timeout: float) -> None:
        self._alive = False
        self.terminated = True


def fake_transport_factory(fake: FakeTransport):
    def _factory(command, cwd=None, env=None):
        return fake

    return _factory


def sequential_fake_transport_factory(fakes):
    """Milestone 11.3.1 Item 5: returns a new `FakeTransport` from `fakes`
    (in order) on each `transport_factory(...)` call -- matching real
    `SubprocessTransport` semantics, where every `start()` spawns a genuinely
    new child process. `fake_transport_factory` above deliberately returns
    the *same* fake forever, which cannot model a restart onto a clean
    process at all; use this one for any test that exercises `start()` being
    called more than once (an explicit restart, or the client's own
    transparent restart-on-retryable-timeout)."""
    fakes_iter = iter(fakes)

    def _factory(command, cwd=None, env=None):
        return next(fakes_iter)

    return _factory


def health_payload(**overrides) -> dict:
    base = {
        "available": True,
        "protocol_version": "paper-runtime.v2",
        "runtime_version": "fake-runtime-1",
        "lumibot_version": "4.5.74",
        "broker_provider": "alpaca",
        "broker_mode": "paper",
        "has_api_key": True,
        "has_api_secret": True,
        "paper_endpoint_verified": True,
        "network_submission_enabled": True,
        "real_money_disabled": True,
    }
    base.update(overrides)
    return base


def capabilities_payload(**overrides) -> dict:
    base = {
        "supported_operations": [
            "health", "capabilities", "submit_order", "get_order", "list_open_orders",
            "list_recent_orders", "get_account", "list_positions", "cancel_paper_order",
        ],
        "supported_asset_types": ["equity"],
        "supported_sides": ["BUY", "SELL"],
        "supported_order_types": ["LIMIT"],
        "fractional_shares": False,
        "short_selling": False,
        "options": False,
        "margin": False,
        "real_money": False,
    }
    base.update(overrides)
    return base


def start_ready_client(client, fake: FakeTransport, *, health: dict | None = None, capabilities: dict | None = None) -> None:
    """Queue a passing health+capabilities pair and call client.start()."""
    fake.queue_success(health or health_payload(), operation="health")
    fake.queue_success(capabilities or capabilities_payload(), operation="capabilities")
    client.start()
