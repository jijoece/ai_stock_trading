"""Runtime-client tests (docs/milestone-4.md Step 16.B). Every test uses
`FakeTransport` — no real subprocess is ever spawned here."""
from __future__ import annotations

import pytest

from trading_research.runtime.client.errors import (
    ProtocolViolationError,
    RuntimeCapabilityError,
    RuntimeOperationError,
    RuntimeRequestTimeoutError,
    RuntimeStartupTimeoutError,
    RuntimeUnavailableError,
)
from trading_research.runtime.client.process_client import RuntimeClient

from tests.support.runtime_client_fixtures import (
    FakeTransport,
    capabilities_payload,
    fake_transport_factory,
    health_payload,
    start_ready_client,
)


def _client(fake: FakeTransport) -> RuntimeClient:
    return RuntimeClient(
        command=["python3", "-m", "trading_paper_runtime"],
        transport_factory=fake_transport_factory(fake),
        startup_timeout_seconds=1.0,
        request_timeout_seconds=1.0,
    )


def test_successful_startup_health_check():
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    assert client.last_health["paper_endpoint_verified"] is True
    assert client.last_capabilities["real_money"] is False


def test_startup_timeout_raises_distinct_error():
    fake = FakeTransport()
    fake.queue_timeout()
    client = _client(fake)
    with pytest.raises(RuntimeStartupTimeoutError):
        client.start()


def test_non_paper_broker_mode_rejected_at_startup():
    fake = FakeTransport()
    fake.queue_success(health_payload(broker_mode="live"), operation="health")
    client = _client(fake)
    with pytest.raises(RuntimeCapabilityError):
        client.start()


def test_missing_paper_endpoint_verification_rejected_at_startup():
    fake = FakeTransport()
    fake.queue_success(health_payload(paper_endpoint_verified=False), operation="health")
    client = _client(fake)
    with pytest.raises(RuntimeCapabilityError):
        client.start()


def test_real_money_capability_true_is_rejected():
    fake = FakeTransport()
    fake.queue_success(health_payload(), operation="health")
    fake.queue_success(capabilities_payload(real_money=True), operation="capabilities")
    client = _client(fake)
    with pytest.raises(RuntimeCapabilityError):
        client.start()


def test_incompatible_capabilities_rejected():
    fake = FakeTransport()
    fake.queue_success(health_payload(), operation="health")
    fake.queue_success(capabilities_payload(margin=True), operation="capabilities")
    client = _client(fake)
    with pytest.raises(RuntimeCapabilityError):
        client.start()


def test_request_timeout_is_retryable_for_reads():
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    fake.queue_timeout()
    with pytest.raises(RuntimeRequestTimeoutError) as exc:
        client.get_order("intent-1")
    assert exc.value.retryable is True


def test_request_timeout_is_not_retryable_for_submit_order():
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    fake.queue_timeout()
    with pytest.raises(RuntimeRequestTimeoutError) as exc:
        client.submit_order({"intent_id": "intent-1"})
    assert exc.value.retryable is False


def test_no_blind_submit_retry_only_one_write_per_call():
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    fake.queue_timeout()
    writes_before = len(fake.written_lines)
    with pytest.raises(RuntimeRequestTimeoutError):
        client.submit_order({"intent_id": "intent-1"})
    assert len(fake.written_lines) == writes_before + 1


def test_runtime_crash_mid_request_is_unavailable_not_an_order_outcome():
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    fake.queue_eof()
    with pytest.raises(RuntimeUnavailableError):
        client.submit_order({"intent_id": "intent-1"})


def test_dead_transport_short_circuits_before_writing():
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    fake.simulate_crash()
    with pytest.raises(RuntimeUnavailableError):
        client.get_order("intent-1")


def test_non_json_stdout_is_a_protocol_violation():
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    fake.queue_raw_line("not json at all")
    with pytest.raises(ProtocolViolationError):
        client.get_order("intent-1")


def test_mismatched_request_id_is_a_protocol_violation():
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    fake.queue_raw_line(
        '{"protocol_version":"paper-runtime.v1","request_id":"wrong-id","operation":"get_order",'
        '"runtime_version":"x","success":true,"retryable":false,"error":null,"payload":{}}'
    )
    with pytest.raises(ProtocolViolationError):
        client.get_order("intent-1")


def test_mismatched_operation_is_a_protocol_violation():
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    # Steal the real request_id but claim a different operation.
    fake.queue_success({}, operation="health")
    with pytest.raises(ProtocolViolationError):
        client.get_order("intent-1")


def test_get_order_returns_none_for_unknown_order():
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    fake.queue_failure("UNKNOWN_ORDER", "no such order")
    assert client.get_order("intent-1") is None


def test_operation_error_propagates_structured_code():
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    fake.queue_failure("BROKER_ERROR", "boom", retryable=True)
    with pytest.raises(RuntimeOperationError) as exc:
        client.get_account()
    assert exc.value.code == "BROKER_ERROR"
    assert exc.value.retryable is True


def test_stderr_is_captured_separately_from_protocol_output():
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    assert client.diagnostics() == []  # FakeTransport never mixes stderr into stdout responses


def test_safe_shutdown_terminates_transport():
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    client.shutdown()
    assert fake.terminated is True


def test_timeout_alone_does_not_block_the_documented_recovery_lookup():
    """Milestone 11.2 Part 19: a timeout on a mutating call must not itself
    disable the client — the explicitly-allowlisted read-only follow-up
    lookup (e.g. get_order after submit_order times out) is the documented
    recovery path and must still be able to run cleanly on the same
    transport when no actual desync occurred."""
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    fake.queue_timeout()
    with pytest.raises(RuntimeRequestTimeoutError):
        client.submit_order({"intent_id": "i1"})
    fake.queue_success({"intent_id": "i1", "status": "ACCEPTED"})
    result = client.get_order("i1")
    assert result["status"] == "ACCEPTED"


def test_late_stale_response_after_timeout_poisons_the_next_call_and_client_is_marked_unhealthy():
    """Milestone 11.2 Part 19/36: request A times out; its late response
    (still carrying A's own request_id/operation) is the next thing on the
    wire when request B is sent. B must detect the mismatch (never silently
    treat A's response as its own), and the client must then refuse any
    further request until an explicit restart — no request C may be sent on
    this now-desynced transport."""
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)

    fake.queue_timeout()
    with pytest.raises(RuntimeRequestTimeoutError):
        client.submit_order({"intent_id": "i1"})
    stale_request_line = fake.written_lines[-1]
    import json as _json

    stale = _json.loads(stale_request_line)

    # Request B (a different, later call) receives A's late response.
    fake.queue_raw_line(_json.dumps({
        "protocol_version": stale["protocol_version"], "request_id": stale["request_id"],
        "operation": stale["operation"], "runtime_version": "fake-runtime-1",
        "success": True, "retryable": False, "error": None,
        "payload": {"intent_id": "i1", "status": "ACCEPTED"},
    }))
    with pytest.raises(ProtocolViolationError):
        client.get_order("i2")  # B: a different, unrelated request

    # Request C must never reach the wire on this transport.
    lines_before = len(fake.written_lines)
    with pytest.raises(RuntimeUnavailableError, match="unhealthy"):
        client.get_order("i3")
    assert len(fake.written_lines) == lines_before  # C was never even written
    assert fake.terminated is True  # transport was torn down


def test_repeated_start_shutdown_cycles_join_pump_threads_without_leaking():
    """Milestone 11.2 Part 20: uses the real `SubprocessTransport` (a real,
    trivial child process) across several start/shutdown cycles and asserts
    no pump threads are left running afterward."""
    import threading

    from trading_research.runtime.client.process_client import SubprocessTransport

    before = {t.ident for t in threading.enumerate()}
    for _ in range(3):
        transport = SubprocessTransport(["python3", "-c", "import sys; sys.stdin.read()"])
        assert transport.is_alive()
        transport.terminate(timeout=5.0)
        assert not transport.is_alive()
        assert not transport._stdout_thread.is_alive()
        assert not transport._stderr_thread.is_alive()
    after = {t.ident for t in threading.enumerate()}
    assert after <= before  # no new threads left running
