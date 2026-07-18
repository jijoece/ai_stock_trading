"""Point-in-time mark-to-market valuation for isolated paper books
(docs/milestone-8.md Steps 8-9).

Price-selection priority (fixed, documented, deterministic):
1. The price already captured in this cycle's point-in-time `EvidenceSnapshot`
   for this exact symbol (reused, never re-fetched).
2. The most recent persisted market bar available strictly at-or-before
   `as_of`, via the existing `evaluation/price_provider.py::PriceProvider`
   contract (never a live quote call).
3. An explicit `SOURCE_UNAVAILABLE` result.

A missing price is never converted to zero. A stale price is never silently
treated as current. Nothing here ever calls a live quote endpoint for a
historical `as_of`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..hashing import hash_config
from ..storage import paper_books_repositories as repo
from ..utc import TimestampError, canonical_utc
from .cash_ledger import SETTLEMENT_POLICY_VERSION
from .models import (
    VALUATION_COMPLETE,
    VALUATION_PARTIAL_MISSING_PRICE,
    VALUATION_PARTIAL_STALE_PRICE,
    VALUATION_POINT_IN_TIME_UNSAFE,
    VALUATION_SOURCE_UNAVAILABLE,
    PaperPortfolioSnapshot,
    compute_snapshot_id,
)

SNAPSHOT_METHODOLOGY_VERSION = "paper-books-valuation-v1"


@dataclass(frozen=True)
class PriceSelection:
    symbol: str
    price: Decimal | None
    provider: str | None
    price_timestamp: datetime | None
    available_at: datetime | None
    point_in_time_safe: bool | None
    source_record_id: str | None
    staleness_seconds: int | None
    status: str


def select_valuation_price(
    symbol: str, as_of: datetime, *, evidence_snapshot=None, price_provider=None,
    maximum_price_age_seconds: int,
) -> PriceSelection:
    # Priority 1: this cycle's own point-in-time EvidenceSnapshot.
    if evidence_snapshot is not None and getattr(evidence_snapshot, "symbol", None) == symbol:
        market_item = next(
            (item for item in evidence_snapshot.evidence_items if item.category == "market"
             and "latest_close" in item.normalized_values),
            None,
        )
        if market_item is not None:
            source = next(
                (s for s in evidence_snapshot.source_records if s.source_id == market_item.source_id), None
            )
            price = Decimal(str(market_item.normalized_values["latest_close"]))
            available_at = source.available_at if source else market_item.as_of
            try:
                valuation_at = canonical_utc(as_of)
                canonical_available = canonical_utc(available_at) if available_at else None
            except TimestampError:
                canonical_available = None
                valuation_at = canonical_utc(as_of)
            declared_safe = source.point_in_time_safe if source else True
            point_in_time_safe = bool(
                declared_safe and canonical_available is not None and canonical_available <= valuation_at
            )
            staleness_seconds = (
                int((valuation_at - canonical_available).total_seconds()) if point_in_time_safe else None
            )
            if not point_in_time_safe:
                status = VALUATION_POINT_IN_TIME_UNSAFE
            elif staleness_seconds is not None and staleness_seconds > maximum_price_age_seconds:
                status = VALUATION_PARTIAL_STALE_PRICE
            else:
                status = VALUATION_COMPLETE
            return PriceSelection(
                symbol=symbol, price=price, provider=source.provider if source else "evidence_snapshot",
                price_timestamp=market_item.as_of, available_at=available_at,
                point_in_time_safe=point_in_time_safe, source_record_id=source.source_id if source else None,
                staleness_seconds=staleness_seconds, status=status,
            )

    # Priority 2: most recent persisted market bar available by as_of.
    if price_provider is not None:
        point = price_provider.get_close(symbol, as_of.date())
        if point is not None:
            try:
                valuation_at = canonical_utc(as_of)
                # Providers implementing the 9.3.1 seam supply authoritative
                # availability. A legacy provider without that field retains
                # the prior contract (available at the requested valuation
                # instant) for compatibility; campaign manifests separately
                # reject normal pre-close dates.
                available_at = canonical_utc(point.available_at) if point.available_at else valuation_at
            except (TimestampError, ValueError):
                available_at = None
                valuation_at = canonical_utc(as_of)
            if available_at is None or available_at > valuation_at:
                return PriceSelection(
                    symbol=symbol, price=Decimal(str(point.close)), provider=point.source,
                    price_timestamp=available_at, available_at=available_at, point_in_time_safe=False,
                    source_record_id=None, staleness_seconds=None, status=VALUATION_POINT_IN_TIME_UNSAFE,
                )
            staleness_seconds = int((valuation_at - available_at).total_seconds())
            status = (
                VALUATION_PARTIAL_STALE_PRICE
                if staleness_seconds > maximum_price_age_seconds else VALUATION_COMPLETE
            )
            return PriceSelection(
                symbol=symbol, price=Decimal(str(point.close)), provider=point.source,
                price_timestamp=available_at, available_at=available_at, point_in_time_safe=True,
                source_record_id=None, staleness_seconds=staleness_seconds, status=status,
            )

    # Priority 3: explicit unavailability — never fabricate a zero.
    return PriceSelection(
        symbol=symbol, price=None, provider=None, price_timestamp=None, available_at=None,
        point_in_time_safe=None, source_record_id=None, staleness_seconds=None,
        status=VALUATION_SOURCE_UNAVAILABLE,
    )


def build_portfolio_snapshot(
    conn, book_id: str, as_of: datetime, *, evidence_snapshots_by_symbol: dict | None = None,
    price_provider=None, maximum_price_age_seconds: int, persist: bool = True,
) -> PaperPortfolioSnapshot:
    from . import cash_ledger

    evidence_snapshots_by_symbol = evidence_snapshots_by_symbol or {}
    cash_available = cash_ledger.available_cash(conn, book_id)
    cash_reserved = cash_ledger.reserved_cash(conn, book_id)
    positions = repo.list_positions(conn, book_id, open_only=True)

    position_inputs: dict[str, dict] = {}
    position_rows: list[dict] = []
    gross_market_value = Decimal("0")
    total_cost_basis = Decimal("0")
    unrealized_pnl = Decimal("0")
    realized_pnl_total = Decimal("0")
    unvalued_count = 0
    stale_count = 0
    unsafe_present = False
    missing_present = False

    for pos in positions:
        symbol = pos["symbol"]
        quantity = Decimal(pos["quantity"])
        cost_basis = quantity * Decimal(pos["average_cost_usd"])
        total_cost_basis += cost_basis
        realized_pnl_total += Decimal(pos["realized_pnl_usd"])

        selection = select_valuation_price(
            symbol, as_of, evidence_snapshot=evidence_snapshots_by_symbol.get(symbol),
            price_provider=price_provider, maximum_price_age_seconds=maximum_price_age_seconds,
        )
        position_inputs[symbol] = {
            "quantity": str(quantity), "price": str(selection.price) if selection.price is not None else None,
            "status": selection.status, "price_timestamp": selection.price_timestamp.isoformat()
            if selection.price_timestamp else None,
        }

        market_value = None
        pos_unrealized = None
        if selection.status == VALUATION_POINT_IN_TIME_UNSAFE:
            unsafe_present = True
            unvalued_count += 1
        elif selection.status == VALUATION_SOURCE_UNAVAILABLE:
            missing_present = True
            unvalued_count += 1
        else:
            market_value = quantity * selection.price
            pos_unrealized = market_value - cost_basis
            gross_market_value += market_value
            unrealized_pnl += pos_unrealized
            if selection.status == VALUATION_PARTIAL_STALE_PRICE:
                stale_count += 1

        position_rows.append({
            "symbol": symbol, "quantity": quantity, "cost_basis_usd": cost_basis,
            "price": selection.price, "price_provider": selection.provider,
            "price_timestamp": selection.price_timestamp.isoformat() if selection.price_timestamp else None,
            "price_available_at": selection.available_at.isoformat() if selection.available_at else None,
            "point_in_time_safe": selection.point_in_time_safe, "source_record_id": selection.source_record_id,
            "staleness_seconds": selection.staleness_seconds, "market_value_usd": market_value,
            "unrealized_pnl_usd": pos_unrealized, "valuation_status": selection.status,
        })

    if not positions:
        overall_status = VALUATION_COMPLETE
    elif unsafe_present:
        overall_status = VALUATION_POINT_IN_TIME_UNSAFE
    elif missing_present:
        overall_status = VALUATION_PARTIAL_MISSING_PRICE
    elif stale_count > 0:
        overall_status = VALUATION_PARTIAL_STALE_PRICE
    else:
        overall_status = VALUATION_COMPLETE

    can_value_fully = not unsafe_present and not missing_present
    gross_mv = gross_market_value if can_value_fully else None
    net_liq = (cash_available + cash_reserved + gross_mv) if gross_mv is not None else None
    unrealized = unrealized_pnl if gross_mv is not None else None

    snapshot_id = compute_snapshot_id(book_id, as_of, position_inputs)
    source_hash = hash_config({
        "book_id": book_id, "as_of": as_of.isoformat(), "positions": position_inputs,
        "settlement_policy_version": SETTLEMENT_POLICY_VERSION,
    })

    snapshot = PaperPortfolioSnapshot(
        snapshot_id=snapshot_id, book_id=book_id, as_of=as_of, cash_available_usd=cash_available,
        cash_reserved_usd=cash_reserved, gross_market_value_usd=gross_mv, net_liquidation_value_usd=net_liq,
        total_cost_basis_usd=total_cost_basis, unrealized_pnl_usd=unrealized, realized_pnl_usd=realized_pnl_total,
        position_count=len(positions), unvalued_position_count=unvalued_count, stale_position_count=stale_count,
        valuation_status=overall_status, source_hash=source_hash,
    )
    if persist:
        repo.save_snapshot(conn, snapshot, position_rows)
    return snapshot
