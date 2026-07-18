"""Local-simulated paper execution for isolated paper books (docs/milestone-8.md
Steps 15-16). LOCAL-SIMULATED-PAPER only — never EXTERNAL-PAPER-BROKER, never
LIVE-BROKER, in this milestone.

This module receives only the bounded data Step 15 names: the order intent's
own fields (book_id, paper_order_intent_id, symbol, side, quantity,
limit_price, time_in_force) plus explicit market-simulation inputs (bid/ask).
It never receives Claude prompts, Claude responses, API keys, live-broker
credentials, or research chain-of-thought.

Deliberately does not round-trip through the Milestone 3/4 isolated
`paper_runtime` subprocess boundary for every fill — that boundary's
`BrokerGateway` implementations key positions/orders per *process*, so true
per-book isolation there is achieved by running one subprocess per book,
which remains possible (an additive, optional `book_id` field was added to
`OrderIntentPayload` in this same milestone for that future wiring) but is
not required for this milestone's LOCAL-SIMULATED-PAPER validation path
(docs/milestone-8.md Step 28). All 33 pre-existing paper_runtime tests pass
unmodified — see `.claude/scratchpads/milestone8-progress.md` "Paper
execution" for the full rationale.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..storage import paper_books_repositories as repo
from ..storage.database import begin_immediate, transaction
from ..storage.paper_books_repositories import (
    EXECUTION_NAMESPACE_LOCAL, ExecutionNamespaceConflictError,
)
from . import cash_ledger, positions
from .models import (
    INTENT_STATUS_FILLED,
    INTENT_STATUS_PENDING_SUBMISSION,
    INTENT_STATUS_REJECTED,
    ORDER_SIDE_BUY,
    ORDER_SIDE_SELL,
    PaperBookOrderIntent,
)

SIMULATION_RULE_VERSION = "paper-books-fill-simulation-v1"
DEFAULT_SLIPPAGE_BPS = Decimal("10")
DEFAULT_FEE_BPS = Decimal("0")


@dataclass(frozen=True)
class MarketSimulationInput:
    """Bounded market-simulation inputs — the only price data this simulator
    ever sees (never a live quote endpoint call)."""

    bid: Decimal
    ask: Decimal
    slippage_bps: Decimal = DEFAULT_SLIPPAGE_BPS
    fee_bps: Decimal = DEFAULT_FEE_BPS


class FillSimulationError(RuntimeError):
    pass


def simulate_fill(intent: PaperBookOrderIntent, market: MarketSimulationInput, now: datetime) -> dict | None:
    """Deterministic limit-order fill rule: an order fills only at a price
    at-or-within its own limit_price. No partial fills in this milestone —
    an order either fills for its full quantity or stays PENDING_SUBMISSION
    this cycle (Step 16: "do not invent [partial fills] casually" — recorded
    as deferred)."""
    if market.bid <= 0 or market.ask <= 0 or market.ask < market.bid:
        raise FillSimulationError(f"invalid market simulation input bid={market.bid} ask={market.ask}")
    mid = (market.bid + market.ask) / 2
    half_spread = (market.ask - market.bid) / 2
    slippage = mid * market.slippage_bps / Decimal("10000")

    if intent.side == ORDER_SIDE_BUY:
        simulated_market_price = mid + half_spread + slippage
        would_cross = simulated_market_price <= intent.limit_price
        fill_price = min(simulated_market_price, intent.limit_price)
    else:
        simulated_market_price = mid - half_spread - slippage
        would_cross = simulated_market_price >= intent.limit_price
        fill_price = max(simulated_market_price, intent.limit_price)

    if not would_cross:
        return None

    fees = fill_price * intent.quantity * market.fee_bps / Decimal("10000")
    slippage_cost = slippage * intent.quantity

    return {
        "book_id": intent.book_id, "fill_id": f"pb-fill-{intent.paper_order_intent_id}",
        "paper_order_intent_id": intent.paper_order_intent_id, "symbol": intent.symbol, "side": intent.side,
        "simulated_market_price": simulated_market_price, "limit_price": intent.limit_price,
        "fill_quantity": intent.quantity, "fill_price": fill_price, "fees_usd": fees,
        "slippage_usd": slippage_cost, "fill_timestamp": now, "simulation_rule_version": SIMULATION_RULE_VERSION,
    }


def submit_and_simulate(conn, intent: PaperBookOrderIntent, market: MarketSimulationInput, now: datetime) -> dict:
    """Submits the order intent (book-aware idempotency: a duplicate
    `(book_id, paper_order_intent_id)` submission is a no-op), reserves cash
    for BUY orders, simulates a fill, and applies it (positions + cash
    settlement) exactly once. Never applies the same fill twice.

    Milestone 11.3.1 Item 6 Part A: for a brand-new intent, the
    LOCAL_SIMULATED namespace claim + intent insert + BUY reservation are
    one atomic transaction -- a crash between them used to be able to leave
    an intent with no reservation, and a later replay would skip creating
    one because the intent was no longer new. Item 6 Part B: the durable
    execution-namespace claim (not just `has_external_execution_evidence`'s
    after-the-fact scan of external-side tables) is now the source of truth
    for local/external exclusivity -- an intent claimed EXTERNAL_PAPER can
    never receive a local fill, even if no external evidence rows exist yet
    (e.g. a concurrent external path that claimed but has not yet written
    its preview).
    """
    existing = repo.load_order_intent(conn, intent.book_id, intent.paper_order_intent_id)
    if existing is None:
        with transaction(conn):
            try:
                repo.claim_execution_namespace(
                    conn, intent.book_id, intent.paper_order_intent_id, EXECUTION_NAMESPACE_LOCAL,
                    now, "local_simulator", commit=False,
                )
            except ExecutionNamespaceConflictError as exc:
                raise FillSimulationError(
                    "intent is externally scoped and cannot receive a local simulated fill"
                ) from exc
            inserted = repo.save_order_intent(conn, intent, commit=False)
            if inserted and intent.side == ORDER_SIDE_BUY:
                cash_ledger.reserve_for_order(
                    conn, intent.book_id, intent.paper_order_intent_id, intent.notional_usd, now, commit=False,
                )
    else:
        # Existing pending intent: the namespace claim and (for BUY) the
        # reservation must already be consistent -- fail closed rather than
        # fabricate either one for a replay.
        claim = repo.load_execution_namespace_claim(conn, intent.book_id, intent.paper_order_intent_id)
        if claim is None:
            # Legacy row from before Item 6 (or an intent a caller inserted
            # directly, bypassing this function's own claim step): lazily
            # claim it now rather than fail closed -- but only once
            # confirmed no external evidence exists, mirroring the
            # pre-Item-6 `has_external_execution_evidence` bootstrap check
            # this replaces as the ongoing source of truth.
            if repo.has_external_execution_evidence(conn, intent.book_id, intent.paper_order_intent_id):
                raise FillSimulationError(
                    "intent is externally scoped and cannot receive a local simulated fill"
                )
            repo.claim_execution_namespace(
                conn, intent.book_id, intent.paper_order_intent_id, EXECUTION_NAMESPACE_LOCAL,
                now, "local_simulator",
            )
        elif claim["execution_namespace"] != EXECUTION_NAMESPACE_LOCAL:
            raise FillSimulationError(
                "intent is externally scoped and cannot receive a local simulated fill"
            )
        if (
            existing["side"] == ORDER_SIDE_BUY
            and existing["status"] == INTENT_STATUS_PENDING_SUBMISSION
            and cash_ledger.remaining_buy_reservation(
                conn, intent.book_id, intent.paper_order_intent_id
            ) != intent.notional_usd
        ):
            raise FillSimulationError(
                "existing reservation does not match the frozen intent notional -- refusing to proceed "
                "rather than fabricate a release or adjustment"
            )

    if intent.side == ORDER_SIDE_SELL:
        available = repo.load_position(conn, intent.book_id, intent.symbol)
        available_qty = Decimal(available["available_quantity"]) if available else Decimal("0")
        if intent.quantity > available_qty:
            repo.update_order_status(conn, intent.book_id, intent.paper_order_intent_id, INTENT_STATUS_REJECTED)
            return {"status": INTENT_STATUS_REJECTED, "fill": None, "reason": "insufficient available position"}

    fill = simulate_fill(intent, market, now)
    if fill is None:
        return {"status": INTENT_STATUS_PENDING_SUBMISSION, "fill": None}

    if repo.fill_exists(conn, intent.book_id, fill["fill_id"]):
        return {"status": INTENT_STATUS_FILLED, "fill": None}  # already applied — idempotent no-op

    cost_or_proceeds = fill["fill_price"] * fill["fill_quantity"]

    # Milestone 11.2 Part 6: fill + lot/position + cash settlement + fee/
    # slippage + reservation release + order status must commit atomically
    # (all-or-nothing), mirroring the external fill path's `begin_immediate`
    # pattern in external_broker.py. Previously each repo call defaulted to
    # commit=True and committed independently, so a crash between any two
    # steps left a partially-applied fill (e.g. persisted fill row with no
    # position/cash effect) that a later idempotency check (`fill_exists`)
    # would treat as already complete.
    try:
        begin_immediate(conn)
        if repo.fill_exists(conn, intent.book_id, fill["fill_id"]):
            conn.rollback()
            return {"status": INTENT_STATUS_FILLED, "fill": None}
        saved = repo.save_fill(conn, fill, commit=False)
        if not saved:
            conn.rollback()
            return {"status": INTENT_STATUS_FILLED, "fill": None}

        if intent.side == ORDER_SIDE_BUY:
            positions.apply_buy_fill(
                conn, intent.book_id, intent.symbol, fill["fill_id"], fill["fill_quantity"], fill["fill_price"],
                now, commit=False,
            )
            cash_ledger.settle_buy(
                conn, intent.book_id, fill["fill_id"], cost_or_proceeds, fill["fees_usd"], fill["slippage_usd"],
                now, commit=False,
            )
            cash_ledger.release_reservation(
                conn, intent.book_id, intent.paper_order_intent_id, intent.notional_usd, now,
                reason="filled", commit=False,
            )
        else:
            positions.apply_sell_fill(
                conn, intent.book_id, intent.symbol, fill["fill_id"], fill["fill_quantity"], fill["fill_price"],
                now, commit=False,
            )
            cash_ledger.settle_sell(
                conn, intent.book_id, fill["fill_id"], cost_or_proceeds, fill["fees_usd"], fill["slippage_usd"],
                now, commit=False,
            )

        repo.update_order_status(conn, intent.book_id, intent.paper_order_intent_id, INTENT_STATUS_FILLED, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"status": INTENT_STATUS_FILLED, "fill": fill}


def cancel_pending_intent(conn, intent: PaperBookOrderIntent, now: datetime) -> dict:
    """Cancels an order that never crossed (still `PENDING_SUBMISSION`),
    releasing any BUY-side cash reservation. A no-op if the order already
    filled — never cancels a completed fill."""
    if repo.has_external_execution_evidence(conn, intent.book_id, intent.paper_order_intent_id):
        raise FillSimulationError("externally scoped intent requires explicit external cancellation")
    existing = repo.load_order_intent(conn, intent.book_id, intent.paper_order_intent_id)
    if existing is None or existing["status"] != INTENT_STATUS_PENDING_SUBMISSION:
        return {"status": existing["status"] if existing else None, "cancelled": False}
    if intent.side == ORDER_SIDE_BUY:
        cash_ledger.release_reservation(conn, intent.book_id, intent.paper_order_intent_id, intent.notional_usd, now, reason="cancelled")
    repo.update_order_status(conn, intent.book_id, intent.paper_order_intent_id, "CANCELLED")
    return {"status": "CANCELLED", "cancelled": True}


def expire_pending_intent(conn, intent: PaperBookOrderIntent, now: datetime) -> dict:
    """Expires an order whose `time_in_force` window has elapsed without a
    fill, releasing any BUY-side cash reservation. A no-op if already
    filled/cancelled."""
    if repo.has_external_execution_evidence(conn, intent.book_id, intent.paper_order_intent_id):
        raise FillSimulationError("externally scoped intent cannot be expired by local lifecycle")
    existing = repo.load_order_intent(conn, intent.book_id, intent.paper_order_intent_id)
    if existing is None or existing["status"] != INTENT_STATUS_PENDING_SUBMISSION:
        return {"status": existing["status"] if existing else None, "expired": False}
    if intent.side == ORDER_SIDE_BUY:
        cash_ledger.release_reservation(conn, intent.book_id, intent.paper_order_intent_id, intent.notional_usd, now, reason="expired")
    repo.update_order_status(conn, intent.book_id, intent.paper_order_intent_id, "EXPIRED")
    return {"status": "EXPIRED", "expired": True}
