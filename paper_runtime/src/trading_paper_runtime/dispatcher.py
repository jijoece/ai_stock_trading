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


def _require_str(payload: dict, name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeOperationError(ErrorCode.MALFORMED_PAYLOAD, f"{name} must be a non-empty string")
    return value
