"""Main-process-side value objects for the runtime client
(docs/milestone-4.md Step 6).

`intent_to_submit_payload` is the only place a `PaperOrderIntent`
(execution/models.py) gets serialized to the wire — everything downstream
of the client only ever sees plain dicts / these small parsed dataclasses,
never a LumiBot object (there are none on this side of the boundary).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ...execution.models import PaperOrderIntent


def derive_client_order_id(intent: PaperOrderIntent) -> str:
    """Stable, broker-safe client order id derived from `intent_id`
    (docs/milestone-4.md Step 8). `intent_id` is already
    `"intent-" + 32 hex chars` (see execution/models.py::derive_intent_id) —
    well under Alpaca's 128-character client_order_id limit and composed
    only of `[a-z0-9-]`, which every broker's client-order-id charset
    accepts."""
    return intent.intent_id


def intent_to_submit_payload(intent: PaperOrderIntent) -> dict:
    return {
        "intent_id": intent.intent_id,
        "recommendation_id": intent.recommendation_id,
        "symbol": intent.symbol,
        "side": intent.side,
        "quantity": intent.quantity,
        "order_type": intent.order_type,
        "limit_price": str(intent.limit_price) if intent.limit_price is not None else None,
        "reference_price": str(intent.reference_price),
        "expires_at": intent.expires_at.isoformat(),
        "idempotency_key": derive_client_order_id(intent),
    }


@dataclass(frozen=True)
class RuntimeOrderSnapshot:
    intent_id: str
    client_order_id: str
    broker_order_id: str | None
    status: str
    raw_broker_status: str | None
    quantity: int
    filled_quantity: int
    average_fill_price: Decimal | None
    submitted_at: str
    updated_at: str

    @classmethod
    def from_payload(cls, payload: dict) -> "RuntimeOrderSnapshot":
        avg = payload.get("average_fill_price")
        return cls(
            intent_id=payload["intent_id"], client_order_id=payload["client_order_id"],
            broker_order_id=payload.get("broker_order_id"), status=payload["status"],
            raw_broker_status=payload.get("raw_broker_status"), quantity=payload["quantity"],
            filled_quantity=payload["filled_quantity"],
            average_fill_price=Decimal(avg) if avg is not None else None,
            submitted_at=payload["submitted_at"], updated_at=payload["updated_at"],
        )


@dataclass(frozen=True)
class RuntimeAccountSnapshot:
    cash: Decimal
    equity: Decimal
    buying_power: Decimal | None
    currency: str
    as_of: str

    @classmethod
    def from_payload(cls, payload: dict) -> "RuntimeAccountSnapshot":
        bp = payload.get("buying_power")
        return cls(
            cash=Decimal(payload["cash"]), equity=Decimal(payload["equity"]),
            buying_power=Decimal(bp) if bp is not None else None,
            currency=payload["currency"], as_of=payload["as_of"],
        )


@dataclass(frozen=True)
class RuntimePositionSnapshot:
    symbol: str
    quantity: Decimal
    average_entry_price: Decimal
    market_value: Decimal | None
    as_of: str

    @classmethod
    def from_payload(cls, payload: dict) -> "RuntimePositionSnapshot":
        mv = payload.get("market_value")
        return cls(
            symbol=payload["symbol"], quantity=Decimal(payload["quantity"]),
            average_entry_price=Decimal(payload["average_entry_price"]),
            market_value=Decimal(mv) if mv is not None else None, as_of=payload["as_of"],
        )
