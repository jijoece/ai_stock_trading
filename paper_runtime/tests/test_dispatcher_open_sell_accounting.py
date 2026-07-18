"""Milestone 11.2 Part 14/36 regression: the isolated runtime must
independently subtract shares already committed to other active open SELL
orders before approving a new SELL — not just check the raw confirmed
position quantity."""
from __future__ import annotations

from dataclasses import dataclass
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


def _dispatcher():
    gateway = DeterministicBrokerGateway()
    return Dispatcher(gateway=gateway, config=CONFIG), gateway


def _seed_long_position(dispatcher, gateway, quantity=10):
    dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="seed-buy", intent_id="seed-buy", side="BUY", quantity=quantity,
    )))
    gateway.script_fill(
        "seed-buy", status="FILLED", filled_quantity=quantity, average_fill_price="150.00", raw_broker_status="filled",
    )
    dispatcher.handle(_request("get_order", {"client_order_id": "seed-buy"}))


def test_position_10_no_open_sell_allows_sell_10():
    dispatcher, gateway = _dispatcher()
    _seed_long_position(dispatcher, gateway, 10)
    result = dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="sell-a", intent_id="sell-a", side="SELL", quantity=10,
    )))
    assert result["status"] == "ACCEPTED"


def test_position_10_open_sell_6_new_sell_5_rejected():
    dispatcher, gateway = _dispatcher()
    _seed_long_position(dispatcher, gateway, 10)
    dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="sell-a", intent_id="sell-a", side="SELL", quantity=6,
    )))
    with pytest.raises(RuntimeOperationError) as excinfo:
        dispatcher.handle(_request("submit_order", _intent_payload(
            idempotency_key="sell-b", intent_id="sell-b", side="SELL", quantity=5,
        )))
    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED


def test_position_10_open_sell_6_new_sell_4_allowed():
    dispatcher, gateway = _dispatcher()
    _seed_long_position(dispatcher, gateway, 10)
    dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="sell-a", intent_id="sell-a", side="SELL", quantity=6,
    )))
    result = dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="sell-b", intent_id="sell-b", side="SELL", quantity=4,
    )))
    assert result["status"] == "ACCEPTED"


def test_same_client_id_retry_not_double_counted():
    dispatcher, gateway = _dispatcher()
    _seed_long_position(dispatcher, gateway, 10)
    dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="sell-a", intent_id="sell-a", side="SELL", quantity=6,
    )))
    # Identical retry of the same intent — must not be blocked by its own
    # already-open order counting against itself.
    result = dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="sell-a", intent_id="sell-a", side="SELL", quantity=6,
    )))
    assert result["status"] == "ACCEPTED"


def test_buy_orders_are_excluded_from_sell_accounting():
    dispatcher, gateway = _dispatcher()
    _seed_long_position(dispatcher, gateway, 10)
    # An open BUY order must not reduce AAPL SELL headroom.
    dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="buy-open", intent_id="buy-open", side="BUY", quantity=3,
    )))
    result = dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="sell-aapl", intent_id="sell-aapl", side="SELL", quantity=10,
    )))
    assert result["status"] == "ACCEPTED"


def test_other_symbol_open_sell_is_excluded_from_this_symbol_accounting():
    dispatcher, gateway = _dispatcher()
    _seed_long_position(dispatcher, gateway, 10)
    dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="seed-buy-msft", intent_id="seed-buy-msft", side="BUY", quantity=50, symbol="MSFT",
    )))
    gateway.script_fill(
        "seed-buy-msft", status="FILLED", filled_quantity=50, average_fill_price="300.00",
        raw_broker_status="filled",
    )
    dispatcher.handle(_request("get_order", {"client_order_id": "seed-buy-msft"}))
    dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="sell-msft", intent_id="sell-msft", side="SELL", symbol="MSFT", quantity=50,
    )))
    # The fully-committed MSFT SELL must not reduce AAPL SELL headroom.
    result = dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="sell-aapl", intent_id="sell-aapl", side="SELL", quantity=10,
    )))
    assert result["status"] == "ACCEPTED"


def test_terminal_open_sell_orders_do_not_reduce_headroom():
    dispatcher, gateway = _dispatcher()
    _seed_long_position(dispatcher, gateway, 10)
    dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="sell-cancelled", intent_id="sell-cancelled", side="SELL", quantity=6,
    )))
    gateway.script_fill(
        "sell-cancelled", status="CANCELLED", filled_quantity=0, average_fill_price=None,
        raw_broker_status="canceled",
    )
    dispatcher.handle(_request("get_order", {"client_order_id": "sell-cancelled"}))
    # The cancelled 6-share SELL no longer commits any shares.
    result = dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="sell-new", intent_id="sell-new", side="SELL", quantity=10,
    )))
    assert result["status"] == "ACCEPTED"


def test_partially_filled_open_sell_only_commits_its_remaining_quantity():
    dispatcher, gateway = _dispatcher()
    _seed_long_position(dispatcher, gateway, 10)
    dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="sell-partial", intent_id="sell-partial", side="SELL", quantity=6,
    )))
    gateway.script_fill(
        "sell-partial", status="PARTIALLY_FILLED", filled_quantity=4, average_fill_price="150.00",
        raw_broker_status="partially_filled",
    )
    dispatcher.handle(_request("get_order", {"client_order_id": "sell-partial"}))
    # Position after the 4-share fill: 10 - 4 = 6 confirmed. Remaining open
    # commitment on sell-partial: 6 - 4 = 2. Available = 6 - 2 = 4.
    with pytest.raises(RuntimeOperationError):
        dispatcher.handle(_request("submit_order", _intent_payload(
            idempotency_key="sell-new", intent_id="sell-new", side="SELL", quantity=5,
        )))
    result = dispatcher.handle(_request("submit_order", _intent_payload(
        idempotency_key="sell-new", intent_id="sell-new", side="SELL", quantity=4,
    )))
    assert result["status"] == "ACCEPTED"


@dataclass
class _FakePosition:
    symbol: str
    quantity: str


@dataclass
class _FakeOpenOrder:
    symbol: str
    side: str
    status: str
    quantity: object
    filled_quantity: object
    client_order_id: str = "fake-order"


class _FractionalOpenOrderGateway:
    """Minimal fake exposing a malformed (fractional) open-order quantity —
    Part 14: 'fractional open-order quantity -> fail closed'."""

    def is_paper_mode_verified(self):
        return True

    def list_positions(self):
        return [_FakePosition(symbol="AAPL", quantity="10")]

    def list_open_orders(self):
        return [_FakeOpenOrder(symbol="AAPL", side="SELL", status="ACCEPTED", quantity="5.5", filled_quantity=0)]


def test_fractional_open_order_quantity_fails_closed():
    dispatcher = Dispatcher(gateway=_FractionalOpenOrderGateway(), config=CONFIG)
    with pytest.raises(RuntimeOperationError) as excinfo:
        dispatcher.handle(_request("submit_order", _intent_payload(
            idempotency_key="sell-a", intent_id="sell-a", side="SELL", quantity=1,
        )))
    assert excinfo.value.code == ErrorCode.MALFORMED_PAYLOAD
