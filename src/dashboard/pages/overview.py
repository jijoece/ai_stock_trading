"""Overview page with persisted, read-only metrics."""
from __future__ import annotations

from decimal import Decimal

import streamlit as st

from dashboard.services.database import DashboardDatabaseError
from dashboard.services.overview_service import OverviewService


def _display(value: object | None) -> str:
    return "Not available" if value is None else str(value)


def _money(value: Decimal | None) -> str:
    return "Not available" if value is None else f"${value:,.2f}"


def render() -> None:
    st.title("Overview")
    st.caption("Persisted research and paper-book state. This dashboard is read-only.")
    try:
        overview = OverviewService().load()
    except DashboardDatabaseError as exc:
        st.error(str(exc))
        return

    first = st.columns(4)
    first[0].metric("Portfolio value", _money(overview.portfolio_value))
    first[1].metric("Cash", _money(overview.cash))
    first[2].metric("Reserved cash", _money(overview.reserved_cash))
    first[3].metric("Open positions", _display(overview.open_positions))

    second = st.columns(4)
    second[0].metric("Realized P/L", _money(overview.realized_pnl))
    second[1].metric("Unrealized P/L", _money(overview.unrealized_pnl))
    second[2].metric("Candidates considered", _display(overview.candidates_considered))
    second[3].metric("Bought or submitted", _display(overview.bought_or_submitted))

    third = st.columns(3)
    third[0].metric("Rejected", _display(overview.rejected))
    third[1].metric("Incomplete", _display(overview.incomplete))
    third[2].metric("Pause state", _display(overview.pause_state))

    st.subheader("Latest persisted activity")
    st.write({
        "Scheduler run": _display(overview.latest_scheduler_run_id),
        "Research cycle": _display(overview.latest_research_cycle_id),
        "Portfolio as of": _display(overview.as_of),
    })
