from __future__ import annotations

import io
import json

from trading_paper_runtime.deterministic_gateway import DeterministicBrokerGateway
from trading_paper_runtime.main import run


def _request_line(operation: str, payload: dict, request_id: str = "req-1") -> str:
    return json.dumps(
        {
            "protocol_version": "paper-runtime.v2",
            "request_id": request_id,
            "operation": operation,
            "sent_at": "2026-07-12T18:00:00Z",
            "payload": payload,
        }
    )


def test_stdio_loop_answers_health_request():
    stdin = io.StringIO(_request_line("health", {}) + "\n")
    stdout = io.StringIO()

    exit_code = run(stdin=stdin, stdout=stdout, gateway_factory=lambda config: DeterministicBrokerGateway())

    assert exit_code == 0
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    response = json.loads(lines[0])
    assert response["success"] is True
    assert response["operation"] == "health"
    assert response["request_id"] == "req-1"


def test_stdio_loop_never_crashes_on_malformed_line():
    stdin = io.StringIO("{not valid json\n" + _request_line("health", {}, "req-2") + "\n")
    stdout = io.StringIO()

    run(stdin=stdin, stdout=stdout, gateway_factory=lambda config: DeterministicBrokerGateway())

    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["success"] is False
    assert lines[0]["error"]["code"] == "MALFORMED_REQUEST"
    assert lines[1]["success"] is True
    assert lines[1]["request_id"] == "req-2"


def test_stdout_never_contains_a_non_json_line():
    stdin = io.StringIO(_request_line("capabilities", {}) + "\n")
    stdout = io.StringIO()

    run(stdin=stdin, stdout=stdout, gateway_factory=lambda config: DeterministicBrokerGateway())

    for line in stdout.getvalue().splitlines():
        if line.strip():
                json.loads(line)  # raises if not valid JSON


def test_unexpected_runtime_errors_never_return_secret_or_raw_exception(monkeypatch):
    class ExplodingGateway(DeterministicBrokerGateway):
        def list_positions(self):
            raise RuntimeError("sdk failure containing super-secret-value")

    monkeypatch.setenv("ALPACA_API_KEY", "super-secret-value")
    stdin = io.StringIO(_request_line("GET_POSITIONS", {"book_id": "BASELINE"}) + "\n")
    stdout = io.StringIO()
    run(stdin=stdin, stdout=stdout, gateway_factory=lambda config: ExplodingGateway())

    raw = stdout.getvalue()
    response = json.loads(raw)
    assert response["success"] is False
    assert response["error"]["message"] == "unexpected isolated runtime error"
    assert "super-secret-value" not in raw
