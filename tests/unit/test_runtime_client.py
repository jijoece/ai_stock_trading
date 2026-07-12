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
