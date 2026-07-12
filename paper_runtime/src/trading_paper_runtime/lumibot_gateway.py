"""Real, credentialed Alpaca-paper `BrokerGateway` (docs/milestone-4.md Step 7).

What is genuinely LumiBot here vs. what is not
-----------------------------------------------
This gateway constructs a real `lumibot.brokers.Alpaca` broker instance
(the same class LumiBot's own `Strategy`/`Trader` would use) purely to reuse
LumiBot's own credential wiring and to prove the connection is paper mode
before anything else happens, and it builds a real `lumibot.entities.Asset`
for every order to validate the symbol against LumiBot's own asset model.

LumiBot's `Broker.submit_order`/`get_order`/`get_tracked_positions` API is
designed around a `Strategy` instance tracking its own orders inside a
`Trader` event loop — the exact shape ADR 0001 (Milestone 3) and this
milestone's ADR 0002 deliberately do not adopt (see
"docs/adr/0001-lumibot-paper-runtime.md" Decision 1, reaffirmed by
"docs/adr/0002-isolated-lumibot-runtime.md"). Actual order transmission,
status lookup, cancellation, account, and position reads therefore go
through the same underlying `alpaca-py` `TradingClient` LumiBot's `Alpaca`
broker itself wraps (`broker.api`) — not a second, competing broker
integration. Every credential and endpoint check still runs through the
LumiBot-constructed broker object first.

No credentials are available in this repository's development environment
(confirmed absent from `.env` at implementation time), so this module has
not been exercised against a live paper-broker connection — see
`docs/milestone4-isolated-paper-broker.md` "Known limitations".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from .configuration import RuntimeConfiguration
from .errors import ErrorCode, RuntimeOperationError
from .models import AccountSnapshotPayload, OrderIntentPayload, OrderSnapshotPayload, PositionSnapshotPayload

# Alpaca (alpaca-py) raw order statuses -> internal runtime status
# (docs/milestone-4.md Step 8). Fail closed on anything not explicitly
# mapped, mirroring the main repo's runtime/lumibot/event_mapper.py posture.
_ALPACA_STATUS_MAP: dict[str, str] = {
    "new": "ACCEPTED",
    "accepted": "ACCEPTED",
    "pending_new": "SUBMITTED",
    "accepted_for_bidding": "ACCEPTED",
    "partially_filled": "PARTIALLY_FILLED",
    "filled": "FILLED",
    "done_for_day": "CANCELLED",
    "canceled": "CANCELLED",
    "expired": "CANCELLED",
    "pending_cancel": "ACCEPTED",
    "stopped": "ERROR",
    "rejected": "REJECTED",
    "suspended": "ERROR",
    "calculated": "ACCEPTED",
    "replaced": "ACCEPTED",
    "pending_replace": "ACCEPTED",
    "held": "ACCEPTED",
}


def _map_status(raw: str) -> str:
    key = str(raw).strip().lower()
    if key not in _ALPACA_STATUS_MAP:
        raise RuntimeOperationError(
            ErrorCode.UNKNOWN_BROKER_STATUS, f"unrecognized Alpaca order status {raw!r} — fail closed"
        )
    return _ALPACA_STATUS_MAP[key]


@dataclass
class LumiBotAlpacaPaperGateway:
    """Implements `broker_gateway.BrokerGateway` against a real, credentialed
    Alpaca paper-trading connection, constructed via LumiBot's `Alpaca`
    broker for credential handling and paper-mode verification."""

    config: RuntimeConfiguration
    broker_provider: str = "alpaca"
    lumibot_version: str | None = field(default=None, init=False)

    _broker: object | None = field(default=None, init=False, repr=False)
    _api: object | None = field(default=None, init=False, repr=False)
    _paper_verified: bool = field(default=False, init=False)
    _init_error: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            import lumibot

            self.lumibot_version = getattr(lumibot, "__version__", "unknown")
        except ImportError:
            self.lumibot_version = None
            self._init_error = "lumibot is not installed in this environment"
            return

        if not self.config.has_credentials:
            self._init_error = "ALPACA_API_KEY / ALPACA_API_SECRET are not both set"
            return
        if not self.config.alpaca_is_paper_flag:
            self._init_error = "ALPACA_IS_PAPER is not exactly 'true' — refusing to assume paper mode"
            return

        try:
            from lumibot.brokers import Alpaca

            broker_config = {
                "API_KEY": self.config.alpaca_api_key,
                "API_SECRET": self.config.alpaca_api_secret,
                "OAUTH_TOKEN": None,
                "PAPER": True,
            }
            self._broker = Alpaca(
                broker_config, connect_stream=False, start_orders_thread=False,
            )
            self._api = self._broker.api
        except Exception as exc:  # broker construction/auth failures must not crash health checks
            self._init_error = f"failed to construct Alpaca broker: {exc}"
            self._broker = None
            self._api = None
            return

        self._paper_verified = self._verify_paper_endpoint()
        if not self._paper_verified:
            self._init_error = "Alpaca TradingClient base URL did not verify as the paper endpoint"

    def _verify_paper_endpoint(self) -> bool:
        try:
            from alpaca.common.enums import BaseURL

            base_url = getattr(self._api, "_base_url", None)
            return base_url == BaseURL.TRADING_PAPER or str(base_url).startswith(
                "https://paper-api.alpaca.markets"
            )
        except Exception:
            return False

    def is_paper_mode_verified(self) -> bool:
        return self._paper_verified and self._api is not None

    def _require_verified(self) -> None:
        if not self.is_paper_mode_verified():
            raise RuntimeOperationError(
                ErrorCode.NOT_PAPER_MODE,
                self._init_error or "Alpaca paper broker connection is not verified",
            )

    def _validate_asset(self, symbol: str) -> None:
        # Genuine LumiBot entity construction — validates the symbol against
        # LumiBot's own Asset model before anything is sent to Alpaca.
        from lumibot.entities import Asset

        Asset(symbol=symbol, asset_type=Asset.AssetType.STOCK)

    def submit_order(self, intent: OrderIntentPayload) -> OrderSnapshotPayload:
        self._require_verified()
        self._validate_asset(intent.symbol)

        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        client_order_id = intent.idempotency_key

        existing = self.get_order(client_order_id)
        if existing is not None:
            return existing  # idempotent replay — never resubmit (docs/milestone-4.md Step 8)

        common_kwargs = dict(
            symbol=intent.symbol, qty=intent.quantity, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, client_order_id=client_order_id,
        )
        if intent.order_type == "LIMIT":
            order_request = LimitOrderRequest(limit_price=float(intent.limit_price), **common_kwargs)
        else:
            order_request = MarketOrderRequest(**common_kwargs)

        try:
            order = self._api.submit_order(order_data=order_request)
        except Exception as exc:
            # Ambiguous outcome: the broker may or may not have received the
            # order. Never fabricate acknowledgement and never blind-retry —
            # look the order up by client_order_id before deciding anything.
            recovered = self.get_order(client_order_id)
            if recovered is not None:
                return recovered
            raise RuntimeOperationError(
                ErrorCode.SUBMISSION_UNKNOWN,
                f"submit_order outcome is unknown (broker call raised {exc!r}); "
                "query get_order with the same client_order_id before retrying — do not resubmit",
                retryable=False,
            ) from exc

        return self._order_to_snapshot(order)

    def get_order(self, client_order_id: str) -> OrderSnapshotPayload | None:
        self._require_verified()
        try:
            order = self._api.get_order_by_client_id(client_order_id)
        except Exception as exc:
            message = str(exc).lower()
            if "404" in message or "not found" in message:
                return None
            raise RuntimeOperationError(ErrorCode.BROKER_ERROR, f"get_order failed: {exc}") from exc
        return self._order_to_snapshot(order)

    def list_open_orders(self) -> list[OrderSnapshotPayload]:
        self._require_verified()
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self._api.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
        return [self._order_to_snapshot(o) for o in orders]

    def list_recent_orders(self, limit: int) -> list[OrderSnapshotPayload]:
        self._require_verified()
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self._api.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit, direction="desc")
        )
        return [self._order_to_snapshot(o) for o in orders]

    def get_account(self) -> AccountSnapshotPayload:
        self._require_verified()
        account = self._api.get_account()
        return AccountSnapshotPayload(
            cash=str(account.cash), equity=str(account.equity),
            buying_power=str(getattr(account, "buying_power", None)) if getattr(account, "buying_power", None) is not None else None,
            currency=getattr(account, "currency", "USD") or "USD",
            as_of=datetime.now(timezone.utc).isoformat(),
        )

    def list_positions(self) -> list[PositionSnapshotPayload]:
        self._require_verified()
        positions = self._api.get_all_positions()
        now = datetime.now(timezone.utc).isoformat()
        return [
            PositionSnapshotPayload(
                symbol=p.symbol, quantity=str(p.qty), average_entry_price=str(p.avg_entry_price),
                market_value=str(p.market_value) if getattr(p, "market_value", None) is not None else None,
                as_of=now,
            )
            for p in positions
        ]

    def cancel_order(self, client_order_id: str) -> OrderSnapshotPayload:
        self._require_verified()
        existing = self.get_order(client_order_id)
        if existing is None:
            raise RuntimeOperationError(ErrorCode.UNKNOWN_ORDER, f"no known order for {client_order_id!r}")
        if existing.broker_order_id:
            try:
                self._api.cancel_order_by_id(existing.broker_order_id)
            except Exception as exc:
                raise RuntimeOperationError(ErrorCode.BROKER_ERROR, f"cancel_order failed: {exc}") from exc
        updated = self.get_order(client_order_id)
        return updated if updated is not None else existing

    def _order_to_snapshot(self, order: object) -> OrderSnapshotPayload:
        raw_status = getattr(order.status, "value", order.status)
        status = _map_status(raw_status)
        filled_qty = int(Decimal(str(order.filled_qty or 0)))
        avg_price = str(order.filled_avg_price) if getattr(order, "filled_avg_price", None) else None
        now = datetime.now(timezone.utc).isoformat()
        return OrderSnapshotPayload(
            intent_id=str(order.client_order_id), client_order_id=str(order.client_order_id),
            broker_order_id=str(order.id) if getattr(order, "id", None) else None,
            status=status, raw_broker_status=str(raw_status), quantity=int(Decimal(str(order.qty or 0))),
            filled_quantity=filled_qty, average_fill_price=avg_price,
            submitted_at=str(order.submitted_at) if getattr(order, "submitted_at", None) else now,
            updated_at=str(order.updated_at) if getattr(order, "updated_at", None) else now,
        )
