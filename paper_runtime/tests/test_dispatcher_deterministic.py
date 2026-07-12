from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_paper_runtime.configuration import RuntimeConfiguration
from trading_paper_runtime.deterministic_gateway import DeterministicBrokerGateway
from trading_paper_runtime.dispatcher import Dispatcher
from trading_paper_runtime.errors import ErrorCode, RuntimeOperationError
from trading_paper_runtime.protocol import RequestEnvelope

CONFIG = RuntimeConfiguration(
    broker_provider="alpaca", alpaca_api_key=None, alpaca_api_secret=None, alpaca_is_paper_flag=False,
)


def _intent_payload(**overrides) -> dict:
    base = {
        "intent_id": "intent-abc123",
        "recommendation_id": "rec-1",
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 10,
        "order_type": "MARKET",
        "limit_price": None,
        "reference_price": "150.00",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "idempotency_key": "intent-abc123",
    }
    base.update(overrides)
    return base


def _request(operation: str, payload: dict) -> RequestEnvelope:
    return RequestEnvelope(
        protocol_version="paper-runtime.v1", request_id="req-1", operation=operation,
        sent_at=datetime.now(timezone.utc).isoformat(), payload=payload,
    )


def _dispatcher() -> Dispatcher:
    return Dispatcher(gateway=DeterministicBrokerGateway(), config=CONFIG)


def test_health_always_answers_even_without_credentials():
    dispatcher = _dispatcher()
    payload = dispatcher.handle(_request("health", {}))
    assert payload["available"] is True
    assert payload["real_money_disabled"] is True
    assert payload["has_api_key"] is False


def test_capabilities_reports_fixed_allowlist():
    dispatcher = _dispatcher()
    payload = dispatcher.handle(_request("capabilities", {}))
    assert payload["real_money"] is False
    assert payload["short_selling"] is False
    assert payload["options"] is False
    assert payload["supported_sides"] == ["BUY"]
    assert "submit_order" in payload["supported_operations"]


def test_submit_then_get_order_roundtrip():
    dispatcher = _dispatcher()
    submitted = dispatcher.handle(_request("submit_order", _intent_payload()))
    assert submitted["status"] == "ACCEPTED"
    assert submitted["broker_order_id"]

    fetched = dispatcher.handle(_request("get_order", {"client_order_id": submitted["client_order_id"]}))
    assert fetched["client_order_id"] == submitted["client_order_id"]


def test_duplicate_submit_returns_existing_order_not_a_new_one():
    dispatcher = _dispatcher()
    first = dispatcher.handle(_request("submit_order", _intent_payload()))
    second = dispatcher.handle(_request("submit_order", _intent_payload()))
    assert first["broker_order_id"] == second["broker_order_id"]


def test_conflicting_idempotency_key_reuse_is_rejected():
    dispatcher = _dispatcher()
    dispatcher.handle(_request("submit_order", _intent_payload()))
    with pytest.raises(RuntimeOperationError) as exc:
        dispatcher.handle(_request("submit_order", _intent_payload(symbol="MSFT")))
    assert exc.value.code == ErrorCode.IDEMPOTENCY_CONFLICT


def test_long_only_enforced():
    dispatcher = _dispatcher()
    with pytest.raises(RuntimeOperationError) as exc:
        dispatcher.handle(_request("submit_order", _intent_payload(side="SELL")))
    assert exc.value.code == ErrorCode.VALIDATION_FAILED


def test_no_fractional_quantity():
    dispatcher = _dispatcher()
    with pytest.raises(RuntimeOperationError) as exc:
        dispatcher.handle(_request("submit_order", _intent_payload(quantity="10.5")))
    assert exc.value.code == ErrorCode.VALIDATION_FAILED


def test_expired_intent_rejected():
    dispatcher = _dispatcher()
    expired = _intent_payload(expires_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    with pytest.raises(RuntimeOperationError) as exc:
        dispatcher.handle(_request("submit_order", expired))
    assert exc.value.code == ErrorCode.VALIDATION_FAILED


def test_limit_order_requires_positive_limit_price():
    dispatcher = _dispatcher()
    with pytest.raises(RuntimeOperationError) as exc:
        dispatcher.handle(_request("submit_order", _intent_payload(order_type="LIMIT", limit_price=None)))
    assert exc.value.code == ErrorCode.VALIDATION_FAILED


def test_unknown_order_lookup_fails_closed():
    dispatcher = _dispatcher()
    with pytest.raises(RuntimeOperationError) as exc:
        dispatcher.handle(_request("get_order", {"client_order_id": "nope"}))
    assert exc.value.code == ErrorCode.UNKNOWN_ORDER


def test_cancel_paper_order():
    dispatcher = _dispatcher()
    submitted = dispatcher.handle(_request("submit_order", _intent_payload()))
    cancelled = dispatcher.handle(
        _request("cancel_paper_order", {"client_order_id": submitted["client_order_id"]})
    )
    assert cancelled["status"] == "CANCELLED"


def test_account_snapshot():
    dispatcher = _dispatcher()
    payload = dispatcher.handle(_request("get_account", {}))
    assert payload["currency"] == "USD"
    assert float(payload["cash"]) > 0


def test_position_snapshot_after_scripted_fill():
    gateway = DeterministicBrokerGateway()
    dispatcher = Dispatcher(gateway=gateway, config=CONFIG)
    submitted = dispatcher.handle(_request("submit_order", _intent_payload()))
    gateway.script_fill(
        submitted["client_order_id"], status="FILLED", filled_quantity=10,
        average_fill_price="151.25", raw_broker_status="fill",
    )
    dispatcher.handle(_request("get_order", {"client_order_id": submitted["client_order_id"]}))
    positions = dispatcher.handle(_request("list_positions", {}))["positions"]
    assert positions[0]["symbol"] == "AAPL"
    assert positions[0]["quantity"] == "10"


def test_unknown_broker_status_fails_closed():
    from trading_paper_runtime.lumibot_gateway import _map_status

    with pytest.raises(RuntimeOperationError) as exc:
        _map_status("cash_settled")
    assert exc.value.code == ErrorCode.UNKNOWN_BROKER_STATUS


def test_not_paper_mode_blocks_submission_when_gateway_unverified():
    class _UnverifiedGateway(DeterministicBrokerGateway):
        def is_paper_mode_verified(self) -> bool:
            return False

    dispatcher = Dispatcher(gateway=_UnverifiedGateway(), config=CONFIG)
    with pytest.raises(RuntimeOperationError) as exc:
        dispatcher.handle(_request("submit_order", _intent_payload()))
    assert exc.value.code == ErrorCode.NOT_PAPER_MODE

    # health/capabilities must still answer even when not paper-verified
    health = dispatcher.handle(_request("health", {}))
    assert health["available"] is True
