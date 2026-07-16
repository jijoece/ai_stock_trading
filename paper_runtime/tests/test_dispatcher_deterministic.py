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
        "order_type": "LIMIT",
        "limit_price": "150.00",
        "reference_price": "150.00",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "idempotency_key": "intent-abc123",
    }
    base.update(overrides)
    return base


def _request(operation: str, payload: dict) -> RequestEnvelope:
    return RequestEnvelope(
        protocol_version="paper-runtime.v2", request_id="req-1", operation=operation,
        sent_at=datetime.now(timezone.utc).isoformat(), payload=payload,
    )


def _external_payload(**overrides) -> dict:
    base = {
        "book_id": "BASELINE", "paper_order_intent_id": "pb-intent-1",
        "client_order_id": "epb-baseline-0123456789abcdef", "symbol": "AAPL", "side": "BUY",
        "quantity": 1, "limit_price": "40.00", "time_in_force": "DAY", "asset_type": "equity",
        "extended_hours": False, "payload_hash": "a" * 64,
        "account_fingerprint": "acct_" + "0" * 32,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }
    base.update(overrides)
    return base


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
    assert payload["supported_sides"] == ["BUY", "SELL"]
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


def test_expired_status_is_preserved_and_only_http_404_is_authoritative_not_found():
    from trading_paper_runtime.lumibot_gateway import _is_authoritative_not_found, _map_status

    class Response:
        status_code = 404

    class ApiError(Exception):
        response = Response()

    assert _map_status("expired") == "EXPIRED"
    assert _is_authoritative_not_found(ApiError("broker response")) is True
    assert _is_authoritative_not_found(Exception("404 not found in an arbitrary message")) is False


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


def test_v2_account_preview_submit_and_lookup_are_paper_scoped():
    gateway = DeterministicBrokerGateway()
    dispatcher = Dispatcher(gateway=gateway, config=CONFIG)
    account = dispatcher.handle(_request("ACCOUNT_CHECK", {"book_id": "BASELINE"}))
    payload = _external_payload(account_fingerprint=account["account_fingerprint"])
    preview = dispatcher.handle(_request("PREVIEW_LIMIT_ORDER", payload))
    assert preview["result"] == "APPROVED"
    submitted = dispatcher.handle(_request("SUBMIT_LIMIT_ORDER", payload))
    assert submitted["book_id"] == "BASELINE"
    found = dispatcher.handle(_request(
        "GET_ORDER_BY_CLIENT_ID", {"book_id": "BASELINE", "client_order_id": payload["client_order_id"]},
    ))
    assert found["found"] is True
    assert gateway.submit_calls == [payload["client_order_id"]]


def test_v2_preview_rejects_unknown_fields_and_market_shape():
    dispatcher = _dispatcher()
    payload = _external_payload(account_fingerprint=dispatcher._gateway.account_fingerprint())
    payload["order_type"] = "MARKET"
    with pytest.raises(RuntimeOperationError) as exc:
        dispatcher.handle(_request("PREVIEW_LIMIT_ORDER", payload))
    assert exc.value.code == ErrorCode.MALFORMED_PAYLOAD
