"""Bounded, read-only paper-book portfolio, order, and fill queries."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
from typing import Any

from dashboard.models.view_models import (
    PaperFillSummary,
    PaperOrderSummary,
    PortfolioSummary,
    PositionSummary,
)
from dashboard.services.database import DashboardDatabaseError, connect_read_only


MAX_ROWS_PER_BOOK = 200


@dataclass(frozen=True, slots=True)
class PortfolioFilters:
    book_id: str | None = None
    symbol: str | None = None
    position_state: str = "ALL"
    start_date: date | None = None
    end_date: date | None = None


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _required_decimal(value: Any) -> Decimal:
    return _decimal(value) or Decimal("0")


def _validated(filters: PortfolioFilters) -> PortfolioFilters:
    book_id = filters.book_id.strip() if filters.book_id else None
    symbol = filters.symbol.strip().upper() if filters.symbol else None
    if book_id and len(book_id) > 200:
        raise ValueError("paper-book filter is invalid")
    if symbol and (len(symbol) > 16 or not symbol.replace(".", "").replace("-", "").isalnum()):
        raise ValueError("symbol filter is invalid")
    state = filters.position_state.upper()
    if state not in {"ALL", "OPEN", "CLOSED"}:
        raise ValueError("position-state filter is invalid")
    return PortfolioFilters(book_id, symbol, state, filters.start_date, filters.end_date)


class PortfolioService:
    def __init__(self, database_path: str | Path | None = None):
        self._database_path = database_path

    def list_portfolios(self, filters: PortfolioFilters | None = None) -> tuple[PortfolioSummary, ...]:
        filters = _validated(filters or PortfolioFilters())
        book_query = "SELECT book_id, experiment_arm, status FROM paper_books"
        parameters: list[Any] = []
        if filters.book_id:
            book_query += " WHERE book_id = ?"
            parameters.append(filters.book_id)
        book_query += " ORDER BY book_id LIMIT 200"

        try:
            with connect_read_only(self._database_path) as connection:
                books = connection.execute(book_query, parameters).fetchall()
                return tuple(self._load_book(connection, book, filters) for book in books)
        except sqlite3.Error as exc:
            raise DashboardDatabaseError("Dashboard portfolio data is unavailable.") from exc

    def _load_book(
        self, connection: sqlite3.Connection, book: sqlite3.Row, filters: PortfolioFilters
    ) -> PortfolioSummary:
        snapshot = connection.execute(
            "SELECT * FROM paper_book_snapshots WHERE book_id = ? ORDER BY created_at DESC LIMIT 1",
            (book["book_id"],),
        ).fetchone()

        position_clauses = ["p.book_id = ?"]
        position_parameters: list[Any] = [book["book_id"]]
        if filters.symbol:
            position_clauses.append("p.symbol = ?")
            position_parameters.append(filters.symbol)
        if filters.position_state == "OPEN":
            position_clauses.append("CAST(p.quantity AS NUMERIC) > 0")
        elif filters.position_state == "CLOSED":
            position_clauses.append("CAST(p.quantity AS NUMERIC) = 0")
        position_parameters.append(MAX_ROWS_PER_BOOK)
        position_rows = connection.execute(f"""
            SELECT p.*, sp.price AS snapshot_price, sp.price_provider,
                   sp.price_timestamp, sp.market_value_usd, sp.unrealized_pnl_usd AS snapshot_unrealized,
                   sp.valuation_status AS snapshot_valuation_status
            FROM paper_book_positions p
            LEFT JOIN paper_book_snapshot_positions sp
              ON sp.book_id = p.book_id AND sp.symbol = p.symbol
             AND sp.snapshot_id = ?
            WHERE {' AND '.join(position_clauses)}
            ORDER BY p.symbol LIMIT ?
        """, ([snapshot["snapshot_id"] if snapshot else None] + position_parameters)).fetchall()

        gross_value = _decimal(snapshot["gross_market_value_usd"]) if snapshot else None
        positions: list[PositionSummary] = []
        for row in position_rows:
            market_value = _decimal(row["market_value_usd"])
            allocation = None
            if market_value is not None and gross_value not in {None, Decimal("0")}:
                allocation = market_value / gross_value * Decimal("100")
            latest_price = _decimal(row["snapshot_price"])
            price_source = row["price_provider"]
            valued_at = _datetime(row["price_timestamp"])
            if latest_price is None:
                latest_price = _decimal(row["latest_valuation_price"])
                price_source = "paper_book_positions.latest_valuation_price" if latest_price is not None else None
                valued_at = _datetime(row["valuation_timestamp"])
            positions.append(PositionSummary(
                book_id=row["book_id"], symbol=row["symbol"],
                quantity=_required_decimal(row["quantity"]),
                available_quantity=_required_decimal(row["available_quantity"]),
                reserved_quantity=_required_decimal(row["reserved_quantity"]),
                average_cost=_required_decimal(row["average_cost_usd"]),
                latest_price=latest_price, market_value=market_value,
                realized_pnl=_required_decimal(row["realized_pnl_usd"]),
                unrealized_pnl=_decimal(row["snapshot_unrealized"] or row["unrealized_pnl_usd"]),
                valuation_status=row["snapshot_valuation_status"] or row["valuation_status"],
                valued_at=valued_at, price_source=price_source,
                allocation_percentage=allocation,
            ))

        orders = self._orders(connection, book["book_id"], filters)
        fills = self._fills(connection, book["book_id"], filters)
        return PortfolioSummary(
            book_id=book["book_id"], experiment_arm=book["experiment_arm"],
            as_of=_datetime(snapshot["as_of"]) if snapshot else None, status=book["status"],
            cash_available=_decimal(snapshot["cash_available_usd"]) if snapshot else None,
            cash_reserved=_decimal(snapshot["cash_reserved_usd"]) if snapshot else None,
            gross_market_value=gross_value,
            net_liquidation_value=_decimal(snapshot["net_liquidation_value_usd"]) if snapshot else None,
            realized_pnl=_decimal(snapshot["realized_pnl_usd"]) if snapshot else None,
            unrealized_pnl=_decimal(snapshot["unrealized_pnl_usd"]) if snapshot else None,
            valuation_status=snapshot["valuation_status"] if snapshot else None,
            positions=tuple(positions), orders=orders, fills=fills,
        )

    @staticmethod
    def _activity_filters(filters: PortfolioFilters, timestamp_column: str) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if filters.symbol:
            clauses.append("symbol = ?")
            parameters.append(filters.symbol)
        if filters.start_date:
            clauses.append(f"{timestamp_column} >= ?")
            parameters.append(datetime.combine(filters.start_date, time.min).isoformat())
        if filters.end_date:
            clauses.append(f"{timestamp_column} <= ?")
            parameters.append(datetime.combine(filters.end_date, time.max).isoformat())
        return clauses, parameters

    def _orders(
        self, connection: sqlite3.Connection, book_id: str, filters: PortfolioFilters
    ) -> tuple[PaperOrderSummary, ...]:
        clauses, parameters = self._activity_filters(filters, "created_at")
        query = "SELECT * FROM paper_book_orders WHERE book_id = ?"
        if clauses:
            query += " AND " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        rows = connection.execute(query, [book_id, *parameters, MAX_ROWS_PER_BOOK]).fetchall()
        return tuple(PaperOrderSummary(
            book_id=row["book_id"], order_id=row["paper_order_intent_id"], symbol=row["symbol"],
            side=row["side"], quantity=_required_decimal(row["quantity"]),
            limit_price=_required_decimal(row["limit_price"]), status=row["status"],
            cycle_id=row["cycle_id"], created_at=_datetime(row["created_at"]),  # type: ignore[arg-type]
        ) for row in rows)

    def _fills(
        self, connection: sqlite3.Connection, book_id: str, filters: PortfolioFilters
    ) -> tuple[PaperFillSummary, ...]:
        clauses, parameters = self._activity_filters(filters, "fill_timestamp")
        query = "SELECT * FROM paper_book_fills WHERE book_id = ?"
        if clauses:
            query += " AND " + " AND ".join(clauses)
        query += " ORDER BY fill_timestamp DESC LIMIT ?"
        rows = connection.execute(query, [book_id, *parameters, MAX_ROWS_PER_BOOK]).fetchall()
        return tuple(PaperFillSummary(
            book_id=row["book_id"], fill_id=row["fill_id"], order_id=row["paper_order_intent_id"],
            symbol=row["symbol"], side=row["side"], quantity=_required_decimal(row["fill_quantity"]),
            fill_price=_required_decimal(row["fill_price"]),
            filled_at=_datetime(row["fill_timestamp"]),  # type: ignore[arg-type]
        ) for row in rows)
