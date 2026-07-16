"""The broker boundary every gateway implementation satisfies
(docs/milestone-4.md Step 7). `dispatcher.py` programs against this
Protocol only — it never imports LumiBot or a specific broker SDK directly,
mirroring how the main repo's `execution/adapter_protocol.py` keeps
orchestration code framework-neutral.

`DeterministicBrokerGateway` (offline, in-memory) and
`LumiBotAlpacaPaperGateway` (real, credentialed) are the two implementations
in this package.
"""
from __future__ import annotations

from typing import Protocol

from .models import AccountSnapshotPayload, FillPayload, OrderIntentPayload, OrderSnapshotPayload, PositionSnapshotPayload


class BrokerGateway(Protocol):
    broker_provider: str
    lumibot_version: str | None

    def is_paper_mode_verified(self) -> bool:
        """True only if the underlying broker connection has been proven to
        point at a paper-trading endpoint — never inferred from a default."""
        ...

    def submit_order(self, intent: OrderIntentPayload) -> OrderSnapshotPayload:
        """Submit exactly once for a given `intent.idempotency_key`; a
        second call with the same key must return the existing order
        (docs/milestone-4.md Step 8), never submit a second broker order."""
        ...

    def get_order(self, client_order_id: str) -> OrderSnapshotPayload | None:
        ...

    def get_order_by_broker_id(self, broker_order_id: str) -> OrderSnapshotPayload | None:
        ...

    def list_order_fills(self, client_order_id: str) -> list[FillPayload]:
        ...

    def account_fingerprint(self) -> str:
        """Stable one-way identifier; never the raw broker account ID."""
        ...

    def list_open_orders(self) -> list[OrderSnapshotPayload]:
        ...

    def list_recent_orders(self, limit: int) -> list[OrderSnapshotPayload]:
        ...

    def get_account(self) -> AccountSnapshotPayload:
        ...

    def list_positions(self) -> list[PositionSnapshotPayload]:
        ...

    def cancel_order(self, client_order_id: str) -> OrderSnapshotPayload:
        ...
