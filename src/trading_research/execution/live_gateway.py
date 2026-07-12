"""Live-execution interface and its permanently-disabled implementation
(docs/milestone-3.md Step 9).

No code path in this repository can reach a live broker: `LiveExecutionGateway`
is a Protocol with exactly one shipped implementation,
`DisabledLiveExecutionGateway`, whose every method raises
`LiveTradingDisabledError` unconditionally. There is deliberately no
alternate implementation to construct, inject, or feature-flag into — adding
one is out of scope for every milestone through this one (see docs/milestone-3.md
"Non-goals": "autonomous live execution", "direct LLM-to-order execution").
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


class LiveTradingDisabledError(RuntimeError):
    """Raised by every `LiveExecutionGateway` method. Live execution is
    disabled by policy (config/execution.yaml: trading_mode=paper,
    live_trading_enabled=false) and by construction (no live implementation
    of `LiveExecutionGateway` exists in this codebase)."""

    def __init__(self, action: str) -> None:
        super().__init__(
            f"live trading is disabled by policy and configuration — cannot {action}. "
            "trading_mode=paper and live_trading_enabled=false in config/execution.yaml; "
            "no LiveExecutionGateway implementation other than DisabledLiveExecutionGateway "
            "exists in this codebase."
        )


@dataclass(frozen=True)
class ApprovedOrder:
    """A would-be live order that has cleared human approval — not
    constructible from any code path in this milestone (nothing produces a
    `HumanApproval`), but typed here so `LiveExecutionGateway`'s signature is
    complete and future-proof."""

    rec_id: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: Decimal | None
    approval_id: str


@dataclass(frozen=True)
class HumanApproval:
    approval_id: str
    approved_by: str
    approved_at: datetime
    payload_hash: str


@dataclass(frozen=True)
class OrderReview:
    rec_id: str
    reviewable: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str
    status: str


@dataclass(frozen=True)
class BrokerOrderState:
    broker_order_id: str
    status: str
    filled_quantity: int


class LiveExecutionGateway(Protocol):
    def review_order(self, approved_order: ApprovedOrder) -> OrderReview:
        ...

    def place_order(self, approved_order: ApprovedOrder, human_approval: HumanApproval) -> BrokerOrder:
        ...

    def cancel_order(self, broker_order_id: str) -> BrokerOrder:
        ...

    def reconcile_order(self, broker_order_id: str) -> BrokerOrderState:
        ...


class DisabledLiveExecutionGateway:
    """The only `LiveExecutionGateway` implementation in this codebase.
    Every method raises `LiveTradingDisabledError` — there is no bypass, no
    constructor flag, and no environment variable that changes this."""

    def review_order(self, approved_order: ApprovedOrder) -> OrderReview:
        raise LiveTradingDisabledError("review a live order")

    def place_order(self, approved_order: ApprovedOrder, human_approval: HumanApproval) -> BrokerOrder:
        raise LiveTradingDisabledError("place a live order")

    def cancel_order(self, broker_order_id: str) -> BrokerOrder:
        raise LiveTradingDisabledError("cancel a live order")

    def reconcile_order(self, broker_order_id: str) -> BrokerOrderState:
        raise LiveTradingDisabledError("reconcile a live order")
