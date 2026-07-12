"""Framework-neutral payload models for the isolated runtime side of the
paper-runtime.v1 protocol (docs/milestone-4.md Step 3).

These are plain dataclasses over JSON-safe primitives (str/int/bool/None) —
Decimals and datetimes cross the wire as strings, never as Python objects.
Nothing here imports LumiBot; `lumibot_gateway.py` is the only module in
this package that translates to/from real LumiBot/Alpaca types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .errors import ErrorCode, RuntimeOperationError

ORDER_TYPES = ("MARKET", "LIMIT")
SIDES = ("BUY",)
ASSET_TYPES = ("equity",)

# Runtime-side submission/order states (docs/milestone-4.md Step 8).
SUBMISSION_STATES = (
    "PENDING_SUBMISSION",
    "SUBMISSION_UNKNOWN",
    "SUBMITTED",
    "ACCEPTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELLED",
    "REJECTED",
    "ERROR",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeOperationError(ErrorCode.VALIDATION_FAILED, message)


def _parse_decimal(value: str, name: str) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise RuntimeOperationError(ErrorCode.MALFORMED_PAYLOAD, f"{name} is not a valid decimal string: {value!r}") from exc


def _parse_dt(value: str, name: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeOperationError(ErrorCode.MALFORMED_PAYLOAD, f"{name} is not a valid ISO8601 timestamp: {value!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class OrderIntentPayload:
    intent_id: str
    recommendation_id: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: str | None
    reference_price: str
    expires_at: str
    idempotency_key: str
    asset_type: str = "equity"

    @classmethod
    def from_dict(cls, data: dict) -> "OrderIntentPayload":
        required = {
            "intent_id", "recommendation_id", "symbol", "side", "quantity", "order_type",
            "limit_price", "reference_price", "expires_at", "idempotency_key",
        }
        missing = required - set(data.keys())
        if missing:
            raise RuntimeOperationError(ErrorCode.MALFORMED_PAYLOAD, f"submit_order payload missing: {sorted(missing)}")
        return cls(
            intent_id=data["intent_id"], recommendation_id=data["recommendation_id"], symbol=data["symbol"],
            side=data["side"], quantity=data["quantity"], order_type=data["order_type"],
            limit_price=data.get("limit_price"), reference_price=data["reference_price"],
            expires_at=data["expires_at"], idempotency_key=data["idempotency_key"],
            asset_type=data.get("asset_type", "equity"),
        )

    def validate(self, *, now: datetime) -> None:
        """Re-validate independently of whatever the main process already
        checked (docs/milestone-4.md Step 3: "The runtime must not trust
        validation from the main process alone")."""
        _require(bool(self.intent_id), "intent_id is required")
        _require(bool(self.recommendation_id), "recommendation_id is required")
        _require(bool(self.idempotency_key), "idempotency_key is required")
        _require(self.asset_type in ASSET_TYPES, f"asset_type must be one of {ASSET_TYPES} — got {self.asset_type!r}")
        _require(self.side in SIDES, f"side must be one of {SIDES} (long-only) — got {self.side!r}")
        _require(isinstance(self.quantity, int) and not isinstance(self.quantity, bool), "quantity must be an int")
        _require(self.quantity > 0, "quantity must be a positive whole number (no fractional shares)")
        _require(self.order_type in ORDER_TYPES, f"order_type must be one of {ORDER_TYPES} — got {self.order_type!r}")
        reference_price = _parse_decimal(self.reference_price, "reference_price")
        _require(reference_price > 0, "reference_price must be positive")
        if self.order_type == "LIMIT":
            _require(self.limit_price is not None, "LIMIT orders require limit_price")
            limit_price = _parse_decimal(self.limit_price, "limit_price")
            _require(limit_price > 0, "limit_price must be positive")
        else:
            _require(self.limit_price is None, "MARKET orders must not carry a limit_price")
        expires_at = _parse_dt(self.expires_at, "expires_at")
        _require(now < expires_at, f"intent expired at {expires_at.isoformat()} (now={now.isoformat()})")


@dataclass(frozen=True)
class OrderSnapshotPayload:
    intent_id: str
    client_order_id: str
    broker_order_id: str | None
    status: str
    raw_broker_status: str | None
    quantity: int
    filled_quantity: int
    average_fill_price: str | None
    submitted_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return {
            "intent_id": self.intent_id, "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id, "status": self.status,
            "raw_broker_status": self.raw_broker_status, "quantity": self.quantity,
            "filled_quantity": self.filled_quantity, "average_fill_price": self.average_fill_price,
            "submitted_at": self.submitted_at, "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class AccountSnapshotPayload:
    cash: str
    equity: str
    buying_power: str | None
    currency: str
    as_of: str

    def to_dict(self) -> dict:
        return {
            "cash": self.cash, "equity": self.equity, "buying_power": self.buying_power,
            "currency": self.currency, "as_of": self.as_of,
        }


@dataclass(frozen=True)
class PositionSnapshotPayload:
    symbol: str
    quantity: str
    average_entry_price: str
    market_value: str | None
    as_of: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "quantity": self.quantity,
            "average_entry_price": self.average_entry_price, "market_value": self.market_value,
            "as_of": self.as_of,
        }


@dataclass(frozen=True)
class HealthPayload:
    available: bool
    protocol_version: str
    runtime_version: str
    lumibot_version: str | None
    broker_provider: str
    broker_mode: str
    has_api_key: bool
    has_api_secret: bool
    paper_endpoint_verified: bool
    network_submission_enabled: bool
    real_money_disabled: bool = True

    def to_dict(self) -> dict:
        return {
            "available": self.available, "protocol_version": self.protocol_version,
            "runtime_version": self.runtime_version, "lumibot_version": self.lumibot_version,
            "broker_provider": self.broker_provider, "broker_mode": self.broker_mode,
            "has_api_key": self.has_api_key, "has_api_secret": self.has_api_secret,
            "paper_endpoint_verified": self.paper_endpoint_verified,
            "network_submission_enabled": self.network_submission_enabled,
            "real_money_disabled": self.real_money_disabled,
        }


@dataclass(frozen=True)
class CapabilitiesPayload:
    supported_operations: tuple = ()
    supported_asset_types: tuple = ASSET_TYPES
    supported_sides: tuple = SIDES
    supported_order_types: tuple = ORDER_TYPES
    fractional_shares: bool = False
    short_selling: bool = False
    options: bool = False
    margin: bool = False
    real_money: bool = False

    def to_dict(self) -> dict:
        return {
            "supported_operations": list(self.supported_operations),
            "supported_asset_types": list(self.supported_asset_types),
            "supported_sides": list(self.supported_sides),
            "supported_order_types": list(self.supported_order_types),
            "fractional_shares": self.fractional_shares,
            "short_selling": self.short_selling,
            "options": self.options,
            "margin": self.margin,
            "real_money": self.real_money,
        }
