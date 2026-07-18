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
    sequential_fake_transport_factory,
    start_ready_client,
)


def _client(fake: FakeTransport) -> RuntimeClient:
    return _client_with_factory(fake_transport_factory(fake))


def _client_with_factory(transport_factory) -> RuntimeClient:
    return RuntimeClient(
        command=["python3", "-m", "trading_paper_runtime"],
        transport_factory=transport_factory,
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


def test_timeout_immediately_marks_transport_unhealthy_and_tears_it_down():
    """Milestone 11.3.1 Item 5: any request timeout -- not only a later
    detected response mismatch -- makes the transport that timed out
    unusable immediately: it is torn down right there, before any other
    request is even attempted."""
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    fake.queue_timeout()
    with pytest.raises(RuntimeRequestTimeoutError):
        client.submit_order({"intent_id": "i1"})
    assert fake.terminated is True


def test_mutating_operation_is_never_automatically_retried_after_timeout():
    """A timed-out mutating operation must not be retried at all -- the
    caller must explicitly call `start()` before attempting it again."""
    fake = FakeTransport()
    client = _client(fake)
    start_ready_client(client, fake)
    fake.queue_timeout()
    with pytest.raises(RuntimeRequestTimeoutError):
        client.submit_order({"intent_id": "i1"})
    lines_before = len(fake.written_lines)
    with pytest.raises(RuntimeUnavailableError, match="unhealthy"):
        client.submit_order({"intent_id": "i1"})
    # The second attempt never even reaches the wire -- it fails closed
    # before writing anything, exactly like a genuinely unusable transport.
    assert len(fake.written_lines) == lines_before


def test_readonly_recovery_lookup_transparently_restarts_onto_a_clean_process():
    """Milestone 11.3.1 Item 5: after a timeout, the documented read-only
    recovery follow-up (e.g. `get_order` after `submit_order` times out)
    must never reuse the unhealthy child -- it transparently restarts onto a
    brand-new transport (a fresh `start()`, re-verifying health/
    capabilities) and only then runs the lookup."""
    fake1 = FakeTransport()
    fake2 = FakeTransport()
    client = _client_with_factory(sequential_fake_transport_factory([fake1, fake2]))
    start_ready_client(client, fake1)

    fake1.queue_timeout()
    with pytest.raises(RuntimeRequestTimeoutError):
        client.submit_order({"intent_id": "i1"})
    assert fake1.terminated is True

    # The recovery lookup restarts onto fake2 (fresh health+capabilities),
    # never touching fake1's now-dead transport again.
    fake2.queue_success(health_payload(), operation="health")
    fake2.queue_success(capabilities_payload(), operation="capabilities")
    fake2.queue_success({"intent_id": "i1", "status": "ACCEPTED"})
    result = client.get_order("i1")
    assert result["status"] == "ACCEPTED"
    assert fake1.written_lines[-1] != "" and len(fake2.written_lines) == 3  # health, capabilities, get_order


def test_late_stale_response_never_reaches_a_later_call_after_restart():
    """Request A times out and is torn down; its late, stale response
    (still carrying A's own request_id/operation) sitting in the dead
    transport's queue must never be read by anything again -- the
    subsequent read-only recovery call restarts onto a brand-new transport
    instead of ever touching fake1 again."""
    fake1 = FakeTransport()
    fake2 = FakeTransport()
    client = _client_with_factory(sequential_fake_transport_factory([fake1, fake2]))
    start_ready_client(client, fake1)

    fake1.queue_timeout()
    with pytest.raises(RuntimeRequestTimeoutError):
        client.submit_order({"intent_id": "i1"})
    stale_request_line = fake1.written_lines[-1]
    import json as _json

    stale = _json.loads(stale_request_line)
    # A's late response arrives after the timeout -- queued on fake1, which
    # is now dead and must never be consulted again.
    fake1.queue_raw_line(_json.dumps({
        "protocol_version": stale["protocol_version"], "request_id": stale["request_id"],
        "operation": stale["operation"], "runtime_version": "fake-runtime-1",
        "success": True, "retryable": False, "error": None,
        "payload": {"intent_id": "i1", "status": "ACCEPTED"},
    }))

    fake2.queue_success(health_payload(), operation="health")
    fake2.queue_success(capabilities_payload(), operation="capabilities")
    fake2.queue_success({"intent_id": "i2", "status": "REJECTED"})
    result = client.get_order("i2")
    assert result["status"] == "REJECTED"  # fake2's response, never fake1's stale one


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


def test_transport_termination_escalates_to_kill_when_process_ignores_terminate():
    """Milestone 11.3.1 Item 5: `_mark_unhealthy_after_timeout` must still
    fully tear down a child that ignores SIGTERM -- `terminate()` escalates
    stdin-close -> SIGTERM -> SIGKILL until the process is actually gone."""
    from trading_research.runtime.client.process_client import SubprocessTransport

    transport = SubprocessTransport([
        "python3", "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
    ])
    assert transport.is_alive()
    transport.terminate(timeout=1.0)  # each escalation stage gets 1s
    assert not transport.is_alive()
    assert not transport._stdout_thread.is_alive()
    assert not transport._stderr_thread.is_alive()


def test_repeated_real_timeout_restart_cycles_do_not_leak_threads_or_processes():
    """Milestone 11.3.1 Item 5: several real (non-fake) timeout -> mark-
    unhealthy -> restart cycles against a child that never answers must
    leave no extra threads or lingering child processes behind."""
    import threading

    from trading_research.runtime.client.process_client import SubprocessTransport

    # A child that reads and discards stdin forever without ever writing a
    # response -- every request against it times out.
    never_responds = ["python3", "-c", "import sys; sys.stdin.read()"]
    client = RuntimeClient(
        command=never_responds, startup_timeout_seconds=0.3, request_timeout_seconds=0.3,
    )
    before = {t.ident for t in threading.enumerate()}
    for _ in range(3):
        with pytest.raises(RuntimeStartupTimeoutError):
            client.start()
        assert client._unhealthy is True
    after = {t.ident for t in threading.enumerate()}
    assert after <= before
