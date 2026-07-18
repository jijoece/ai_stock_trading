"""Read-only paper-book portfolio, position, order, and fill view."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st

from dashboard.services.database import DashboardDatabaseError, configured_database_path
from dashboard.services.portfolio_service import PortfolioFilters, PortfolioService


@st.cache_data(ttl=30, show_spinner=False)
def _load(database_path: str, filters: PortfolioFilters):
    return PortfolioService(database_path).list_portfolios(filters)


def _available(value: object | None) -> str:
    return "Not available" if value is None else str(value)


def _money(value: Decimal | None) -> str:
    return "Not available" if value is None else f"${value:,.2f}"


def render() -> None:
    st.title("Portfolio")
    st.caption("Persisted isolated paper books only. No prices are fetched externally.")
    if st.button("Refresh persisted data", key="refresh-portfolio"):
        _load.clear()
    row = st.columns(3)
    book_id = row[0].text_input("Paper book", max_chars=200)
    symbol = row[1].text_input("Symbol", max_chars=16)
    position_state = row[2].selectbox("Position state", ("ALL", "OPEN", "CLOSED"))
    date_row = st.columns(2)
    start_date = date_row[0].date_input("Activity from", value=None)
    end_date = date_row[1].date_input("Activity through", value=None)
    filters = PortfolioFilters(
        book_id=book_id or None, symbol=symbol or None, position_state=position_state,
        start_date=start_date if isinstance(start_date, date) else None,
        end_date=end_date if isinstance(end_date, date) else None,
    )
    try:
        portfolios = _load(str(configured_database_path()), filters)
    except (DashboardDatabaseError, ValueError) as exc:
        st.error(str(exc))
        return
    if not portfolios:
        st.info("No persisted paper books match these filters.")
        return

    for portfolio in portfolios:
        st.subheader(f"{portfolio.book_id} · {portfolio.experiment_arm} · {portfolio.status}")
        metrics = st.columns(4)
        metrics[0].metric("Cash", _money(portfolio.cash_available))
        metrics[1].metric("Reserved cash", _money(portfolio.cash_reserved))
        metrics[2].metric("Portfolio value", _money(portfolio.net_liquidation_value))
        metrics[3].metric("As of", _available(portfolio.as_of))
        pnl = st.columns(2)
        pnl[0].metric("Realized P/L", _money(portfolio.realized_pnl))
        pnl[1].metric("Unrealized P/L", _money(portfolio.unrealized_pnl))

        st.markdown("#### Positions")
        if portfolio.positions:
            st.dataframe(pd.DataFrame([{
                "Symbol": item.symbol, "Quantity": item.quantity,
                "Available": item.available_quantity, "Reserved": item.reserved_quantity,
                "Average entry price": item.average_cost,
                "Persisted price": item.latest_price,
                "Price source": _available(item.price_source),
                "Price timestamp": _available(item.valued_at),
                "Market value": item.market_value,
                "Realized P/L": item.realized_pnl,
                "Unrealized P/L": item.unrealized_pnl,
                "Allocation %": item.allocation_percentage,
                "Valuation status": _available(item.valuation_status),
            } for item in portfolio.positions]), hide_index=True, use_container_width=True)
        else:
            st.info("No positions match these filters.")

        activity = st.columns(2)
        with activity[0]:
            st.markdown("#### Orders")
            if portfolio.orders:
                st.dataframe(pd.DataFrame([{
                    "Order ID": item.order_id, "Symbol": item.symbol, "Side": item.side,
                    "Quantity": item.quantity, "Limit price": item.limit_price,
                    "Status": item.status, "Cycle ID": item.cycle_id, "Created": item.created_at,
                } for item in portfolio.orders]), hide_index=True, use_container_width=True)
            else:
                st.info("No orders match these filters.")
        with activity[1]:
            st.markdown("#### Fills")
            if portfolio.fills:
                st.dataframe(pd.DataFrame([{
                    "Fill ID": item.fill_id, "Order ID": item.order_id,
                    "Symbol": item.symbol, "Side": item.side, "Quantity": item.quantity,
                    "Fill price": item.fill_price, "Filled": item.filled_at,
                } for item in portfolio.fills]), hide_index=True, use_container_width=True)
            else:
                st.info("No fills match these filters.")
