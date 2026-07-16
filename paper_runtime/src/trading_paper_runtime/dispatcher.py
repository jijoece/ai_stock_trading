"""Routes a validated `RequestEnvelope` to the configured `BrokerGateway`
and builds the response payload (docs/milestone-4.md Step 3).

`health` and `capabilities` never require the gateway to be a real,
credentialed broker connection — they must always answer, even when
credentials are missing, so the main process can observe *why* submission
is blocked. Every other operation requires `gateway.is_paper_mode_verified()`
to be true; if it is not, every mutating/reading-broker-state operation
fails closed with `NOT_PAPER_MODE` rather than silently proceeding.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import PROTOCOL_VERSION, RUNTIME_VERSION
from .broker_gateway import BrokerGateway
from .configuration import RuntimeConfiguration
from .errors import ErrorCode, RuntimeOperationError
from .models import CapabilitiesPayload, HealthPayload, OrderIntentPayload
from .protocol import RequestEnvelope

try:
    import lumibot as _lumibot

    _LUMIBOT_VERSION = getattr(_lumibot, "__version__", "unknown")
except ImportError:  # pragma: no cover — exercised only when lumibot is absent
    _LUMIBOT_VERSION = None


class Dispatcher:
    def __init__(self, gateway: BrokerGateway, config: RuntimeConfiguration) -> None:
        self._gateway = gateway
        self._config = config

    def handle(self, request: RequestEnvelope) -> dict:
        handler = getattr(self, f"_op_{request.operation}", None)
        if handler is None:  # unreachable if protocol.py's allowlist stays in sync, but fail closed anyway
            raise RuntimeOperationError(ErrorCode.UNKNOWN_OPERATION, f"no handler for {request.operation!r}")
        return handler(request.payload)

    def _require_paper_verified(self) -> None:
        if not self._gateway.is_paper_mode_verified():
            raise RuntimeOperationError(
                ErrorCode.NOT_PAPER_MODE,
                "broker connection has not proven paper mode — refusing to operate",
            )

    # -- operations -----------------------------------------------------

    def _op_health(self, _payload: dict) -> dict:
        paper_verified = False
        try:
            paper_verified = self._gateway.is_paper_mode_verified()
        except Exception:
            paper_verified = False
        return HealthPayload(
            available=True,
            protocol_version=PROTOCOL_VERSION,
            runtime_version=RUNTIME_VERSION,
            lumibot_version=_LUMIBOT_VERSION,
            broker_provider=self._config.broker_provider,
            broker_mode="paper",
            has_api_key=self._config.has_api_key,
            has_api_secret=self._config.has_api_secret,
            paper_endpoint_verified=paper_verified,
            network_submission_enabled=paper_verified,
            real_money_disabled=True,
        ).to_dict()

    def _op_capabilities(self, _payload: dict) -> dict:
        from .protocol import SUPPORTED_OPERATIONS

        return CapabilitiesPayload(supported_operations=SUPPORTED_OPERATIONS).to_dict()

    def _op_submit_order(self, payload: dict) -> dict:
        self._require_paper_verified()
        intent = OrderIntentPayload.from_dict(payload)
        intent.validate(now=datetime.now(timezone.utc))
        if intent.side == "SELL":
            self._validate_confirmed_long(intent.symbol, intent.quantity)
        snapshot = self._gateway.submit_order(intent)
        return snapshot.to_dict()

    def _op_get_order(self, payload: dict) -> dict:
        self._require_paper_verified()
        client_order_id = _require_str(payload, "client_order_id")
        snapshot = self._gateway.get_order(client_order_id)
        if snapshot is None:
            raise RuntimeOperationError(ErrorCode.UNKNOWN_ORDER, f"no known order for {client_order_id!r}")
        return snapshot.to_dict()

    def _op_list_open_orders(self, _payload: dict) -> dict:
        self._require_paper_verified()
        return {"orders": [o.to_dict() for o in self._gateway.list_open_orders()]}

    def _op_list_recent_orders(self, payload: dict) -> dict:
        self._require_paper_verified()
        limit = payload.get("limit", 50)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise RuntimeOperationError(ErrorCode.MALFORMED_PAYLOAD, "limit must be a positive int")
        return {"orders": [o.to_dict() for o in self._gateway.list_recent_orders(limit)]}

    def _op_get_account(self, _payload: dict) -> dict:
        self._require_paper_verified()
        return self._gateway.get_account().to_dict()

    def _op_list_positions(self, _payload: dict) -> dict:
        self._require_paper_verified()
        return {"positions": [p.to_dict() for p in self._gateway.list_positions()]}

    def _op_cancel_paper_order(self, payload: dict) -> dict:
        self._require_paper_verified()
        client_order_id = _require_str(payload, "client_order_id")
        snapshot = self._gateway.cancel_order(client_order_id)
        return snapshot.to_dict()

    # -- Milestone 11 paper-runtime.v2 broker-neutral operations --------

    def _op_ACCOUNT_CHECK(self, payload: dict) -> dict:
        self._require_paper_verified()
        _require_exact_fields(payload, {"book_id"})
        book_id = _require_book_id(payload)
        return {
            "provider": "alpaca_paper", "environment": "paper", "book_id": book_id,
            "account_fingerprint": self._gateway.account_fingerprint(),
            "paper_endpoint_verified": True,
        }

    def _op_PREVIEW_LIMIT_ORDER(self, payload: dict) -> dict:
        self._require_paper_verified()
        intent = _external_intent(payload)
        intent.validate(now=datetime.now(timezone.utc))
        fingerprint = self._gateway.account_fingerprint()
        expected = payload["account_fingerprint"]
        if expected != fingerprint:
            raise RuntimeOperationError(ErrorCode.VALIDATION_FAILED, "account fingerprint mismatch")
        if intent.side == "SELL":
            self._validate_confirmed_long(intent.symbol, intent.quantity)
        return {
            "provider": "alpaca_paper", "environment": "paper", "book_id": intent.book_id,
            "client_order_id": intent.idempotency_key, "account_fingerprint": fingerprint,
            "result": "APPROVED", "reasons": [],
        }

    def _op_SUBMIT_LIMIT_ORDER(self, payload: dict) -> dict:
        self._require_paper_verified()
        intent = _external_intent(payload)
        intent.validate(now=datetime.now(timezone.utc))
        fingerprint = self._gateway.account_fingerprint()
        if payload["account_fingerprint"] != fingerprint:
            raise RuntimeOperationError(ErrorCode.VALIDATION_FAILED, "account fingerprint mismatch")
        if intent.side == "SELL":
            self._validate_confirmed_long(intent.symbol, intent.quantity)
        return _external_order_dict(self._gateway.submit_order(intent))

    def _op_GET_ORDER_BY_CLIENT_ID(self, payload: dict) -> dict:
        self._require_paper_verified()
        _require_exact_fields(payload, {"book_id", "client_order_id"})
        book_id = _require_book_id(payload)
        client_order_id = _require_str(payload, "client_order_id")
        snapshot = self._gateway.get_order(client_order_id)
        if snapshot is None:
            return {"found": False, "book_id": book_id, "client_order_id": client_order_id}
        return {"found": True, "order": _external_order_dict(snapshot)}

    def _op_GET_ORDER(self, payload: dict) -> dict:
        self._require_paper_verified()
        _require_exact_fields(payload, {"book_id", "broker_order_id"})
        _require_book_id(payload)
        broker_order_id = _require_str(payload, "broker_order_id")
        snapshot = self._gateway.get_order_by_broker_id(broker_order_id)
        return {"found": snapshot is not None, "order": _external_order_dict(snapshot) if snapshot else None}

    def _op_CANCEL_ORDER(self, payload: dict) -> dict:
        self._require_paper_verified()
        _require_exact_fields(payload, {"book_id", "client_order_id", "account_fingerprint"})
        _require_book_id(payload)
        if _require_str(payload, "account_fingerprint") != self._gateway.account_fingerprint():
            raise RuntimeOperationError(ErrorCode.VALIDATION_FAILED, "account fingerprint mismatch")
        return _external_order_dict(self._gateway.cancel_order(_require_str(payload, "client_order_id")))

    def _op_LIST_ORDER_FILLS(self, payload: dict) -> dict:
        self._require_paper_verified()
        _require_exact_fields(payload, {"book_id", "client_order_id"})
        _require_book_id(payload)
        fills = self._gateway.list_order_fills(_require_str(payload, "client_order_id"))
        return {"fills": [fill.to_dict() for fill in fills]}

    def _op_GET_POSITIONS(self, payload: dict) -> dict:
        self._require_paper_verified()
        _require_exact_fields(payload, {"book_id"})
        book_id = _require_book_id(payload)
        return {
            "book_id": book_id, "account_fingerprint": self._gateway.account_fingerprint(),
            "positions": [position.to_dict() for position in self._gateway.list_positions()],
        }

    def _op_GET_ACCOUNT_SNAPSHOT(self, payload: dict) -> dict:
        self._require_paper_verified()
        _require_exact_fields(payload, {"book_id"})
        book_id = _require_book_id(payload)
        result = self._gateway.get_account().to_dict()
        result.update(
            provider="alpaca_paper", environment="paper", book_id=book_id,
            account_fingerprint=self._gateway.account_fingerprint(),
        )
        return result

    def _validate_confirmed_long(self, symbol: str, quantity: int) -> None:
        position = next((p for p in self._gateway.list_positions() if p.symbol == symbol), None)
        if position is None or int(float(position.quantity)) < quantity:
            raise RuntimeOperationError(ErrorCode.VALIDATION_FAILED, "SELL exceeds confirmed broker long position")


def _require_str(payload: dict, name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeOperationError(ErrorCode.MALFORMED_PAYLOAD, f"{name} must be a non-empty string")
    return value


def _require_exact_fields(payload: dict, expected: set[str]) -> None:
    missing = expected - set(payload)
    extra = set(payload) - expected
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"missing {sorted(missing)}")
        if extra:
            detail.append(f"unexpected {sorted(extra)}")
        raise RuntimeOperationError(ErrorCode.MALFORMED_PAYLOAD, "; ".join(detail))


def _require_book_id(payload: dict) -> str:
    book_id = _require_str(payload, "book_id")
    if book_id not in ("BASELINE", "ENHANCED"):
        raise RuntimeOperationError(ErrorCode.MALFORMED_PAYLOAD, "book_id must be BASELINE or ENHANCED")
    return book_id


def _external_intent(payload: dict) -> OrderIntentPayload:
    required = {
        "book_id", "paper_order_intent_id", "client_order_id", "symbol", "side", "quantity",
        "limit_price", "time_in_force", "asset_type", "extended_hours", "payload_hash",
        "account_fingerprint", "expires_at",
    }
    _require_exact_fields(payload, required)
    book_id = _require_book_id(payload)
    if payload["time_in_force"] != "DAY":
        raise RuntimeOperationError(ErrorCode.VALIDATION_FAILED, "time_in_force must be DAY")
    if payload["asset_type"] != "equity":
        raise RuntimeOperationError(ErrorCode.VALIDATION_FAILED, "asset_type must be equity")
    if payload["extended_hours"] is not False:
        raise RuntimeOperationError(ErrorCode.VALIDATION_FAILED, "extended_hours must be false")
    for name in ("payload_hash", "account_fingerprint"):
        _require_str(payload, name)
    limit_price = _require_str(payload, "limit_price")
    return OrderIntentPayload(
        intent_id=_require_str(payload, "paper_order_intent_id"),
        recommendation_id=_require_str(payload, "paper_order_intent_id"),
        symbol=_require_str(payload, "symbol"), side=_require_str(payload, "side"),
        quantity=payload["quantity"], order_type="LIMIT", limit_price=limit_price,
        reference_price=limit_price, expires_at=_require_str(payload, "expires_at"),
        idempotency_key=_require_str(payload, "client_order_id"), asset_type="equity", book_id=book_id,
    )


def _external_order_dict(snapshot) -> dict:
    return {
        "provider": "alpaca_paper", "environment": "paper",
        "account_fingerprint": snapshot.account_fingerprint,
        "book_id": snapshot.book_id, "client_order_id": snapshot.client_order_id,
        "broker_order_id": snapshot.broker_order_id, "symbol": snapshot.symbol,
        "side": snapshot.side, "quantity": snapshot.quantity, "limit_price": snapshot.limit_price,
        "time_in_force": snapshot.time_in_force, "status": snapshot.status,
        "submitted_at": snapshot.submitted_at, "updated_at": snapshot.updated_at,
        "filled_quantity": snapshot.filled_quantity,
        "average_fill_price": snapshot.average_fill_price,
        "rejection_code": "BROKER_REJECTED" if snapshot.status == "REJECTED" else None,
    }
