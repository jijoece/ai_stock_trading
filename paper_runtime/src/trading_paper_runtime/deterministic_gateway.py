"""In-memory, deterministic `BrokerGateway` for offline isolated-runtime
tests (docs/milestone-4.md Step 16.C). No network, no credentials, no
randomness — every fill is either scripted via `.script_fill(...)` or left
at the default post-submit state (`ACCEPTED`, unfilled) until scripted.

This is *not* the same object as the main repo's
`trading_research.runtime.deterministic_adapter.DeterministicPaperAdapter`
— that one lives across the process boundary in the main project and is
never imported here (docs/milestone-4.md: "the isolated runtime must not
... access the main database" / no cross-boundary imports at all).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from .errors import ErrorCode, RuntimeOperationError
from .models import AccountSnapshotPayload, OrderIntentPayload, OrderSnapshotPayload, PositionSnapshotPayload


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _ScriptedOutcome:
    status: str
    filled_quantity: int
    average_fill_price: str | None
    raw_broker_status: str


@dataclass
class DeterministicBrokerGateway:
    broker_provider: str = "alpaca"
    lumibot_version: str | None = "deterministic-fake"
    starting_cash: str = "100000"

    _orders: dict[str, OrderSnapshotPayload] = field(default_factory=dict)
    _order_symbol: dict[str, str] = field(default_factory=dict)
    _intent_fingerprint: dict[str, tuple] = field(default_factory=dict)
    _scripts: dict[str, _ScriptedOutcome] = field(default_factory=dict)
    _positions: dict[str, PositionSnapshotPayload] = field(default_factory=dict)
    _applied_fill_orders: set = field(default_factory=set)
    submit_calls: list = field(default_factory=list)

    def is_paper_mode_verified(self) -> bool:
        return True

    def script_fill(
        self, client_order_id: str, *, status: str, filled_quantity: int,
        average_fill_price: str | None, raw_broker_status: str,
    ) -> None:
        """Pre-register the outcome `get_order`/`list_open_orders` will
        report the next time this order is looked up — the deterministic
        stand-in for a real broker's asynchronous fill callback."""
        self._scripts[client_order_id] = _ScriptedOutcome(
            status=status, filled_quantity=filled_quantity,
            average_fill_price=average_fill_price, raw_broker_status=raw_broker_status,
        )

    def submit_order(self, intent: OrderIntentPayload) -> OrderSnapshotPayload:
        self.submit_calls.append(intent.idempotency_key)
        fingerprint = (intent.symbol, intent.side, intent.quantity, intent.order_type, intent.limit_price)
        if intent.idempotency_key in self._orders:
            existing_fingerprint = self._intent_fingerprint[intent.idempotency_key]
            if existing_fingerprint != fingerprint:
                raise RuntimeOperationError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    f"idempotency_key {intent.idempotency_key!r} was already used for a different order",
                )
            return self._orders[intent.idempotency_key]

        now = _now_iso()
        snapshot = OrderSnapshotPayload(
            intent_id=intent.intent_id, client_order_id=intent.idempotency_key,
            broker_order_id=f"det-broker-{intent.idempotency_key}", status="ACCEPTED",
            raw_broker_status="new", quantity=intent.quantity, filled_quantity=0,
            average_fill_price=None, submitted_at=now, updated_at=now,
        )
        self._orders[intent.idempotency_key] = snapshot
        self._order_symbol[intent.idempotency_key] = intent.symbol
        self._intent_fingerprint[intent.idempotency_key] = fingerprint
        return snapshot

    def get_order(self, client_order_id: str) -> OrderSnapshotPayload | None:
        existing = self._orders.get(client_order_id)
        if existing is None:
            return None
        script = self._scripts.get(client_order_id)
        if script is None:
            return existing
        updated = OrderSnapshotPayload(
            intent_id=existing.intent_id, client_order_id=existing.client_order_id,
            broker_order_id=existing.broker_order_id, status=script.status,
            raw_broker_status=script.raw_broker_status, quantity=existing.quantity,
            filled_quantity=script.filled_quantity, average_fill_price=script.average_fill_price,
            submitted_at=existing.submitted_at, updated_at=_now_iso(),
        )
        already_applied = client_order_id in self._applied_fill_orders
        self._orders[client_order_id] = updated
        if updated.filled_quantity > 0 and updated.average_fill_price and not already_applied:
            symbol = self._order_symbol[client_order_id]
            self._apply_position(symbol, updated.filled_quantity, updated.average_fill_price)
            self._applied_fill_orders.add(client_order_id)
        return updated

    def _apply_position(self, symbol: str, qty: int, price: str) -> None:
        # Minimal long-only average-cost position model, for reconciliation
        # tests only — not a general ledger.
        existing = self._positions.get(symbol)
        if existing is None:
            self._positions[symbol] = PositionSnapshotPayload(
                symbol=symbol, quantity=str(qty), average_entry_price=price,
                market_value=str(Decimal(price) * qty), as_of=_now_iso(),
            )
            return
        old_qty = Decimal(existing.quantity)
        old_avg = Decimal(existing.average_entry_price)
        new_qty = old_qty + qty
        new_avg = (old_qty * old_avg + qty * Decimal(price)) / new_qty
        self._positions[symbol] = PositionSnapshotPayload(
            symbol=symbol, quantity=str(new_qty), average_entry_price=str(new_avg),
            market_value=str(new_qty * new_avg), as_of=_now_iso(),
        )

    def list_open_orders(self) -> list[OrderSnapshotPayload]:
        return [
            self.get_order(coid) for coid, o in list(self._orders.items())
            if o.status not in ("FILLED", "CANCELLED", "REJECTED", "ERROR")
        ]

    def list_recent_orders(self, limit: int) -> list[OrderSnapshotPayload]:
        ordered = sorted(self._orders.values(), key=lambda o: o.submitted_at, reverse=True)
        return [self.get_order(o.client_order_id) for o in ordered[:limit]]

    def get_account(self) -> AccountSnapshotPayload:
        cash = Decimal(self.starting_cash)
        return AccountSnapshotPayload(
            cash=str(cash), equity=str(cash), buying_power=str(cash), currency="USD", as_of=_now_iso(),
        )

    def list_positions(self) -> list[PositionSnapshotPayload]:
        return list(self._positions.values())

    def cancel_order(self, client_order_id: str) -> OrderSnapshotPayload:
        existing = self._orders.get(client_order_id)
        if existing is None:
            raise RuntimeOperationError(ErrorCode.UNKNOWN_ORDER, f"no known order for {client_order_id!r}")
        if existing.status in ("FILLED", "CANCELLED", "REJECTED", "ERROR"):
            return existing
        updated = OrderSnapshotPayload(
            intent_id=existing.intent_id, client_order_id=existing.client_order_id,
            broker_order_id=existing.broker_order_id, status="CANCELLED",
            raw_broker_status="canceled", quantity=existing.quantity,
            filled_quantity=existing.filled_quantity, average_fill_price=existing.average_fill_price,
            submitted_at=existing.submitted_at, updated_at=_now_iso(),
        )
        self._orders[client_order_id] = updated
        self._scripts.pop(client_order_id, None)
        return updated
