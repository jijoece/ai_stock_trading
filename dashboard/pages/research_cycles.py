"""Research-cycle history, persisted funnel, and ticker drill-down."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.pages.decisions import render_decision_detail
from dashboard.services.cycle_service import CycleService
from dashboard.services.database import DashboardDatabaseError, configured_database_path
from dashboard.services.decision_service import DecisionService


@st.cache_data(ttl=30, show_spinner=False)
def _load_cycles(database_path: str, limit: int):
    return CycleService(database_path).list_cycles(limit=limit)


@st.cache_data(ttl=30, show_spinner=False)
def _load_detail(database_path: str, cycle_id: str):
    return CycleService(database_path).get_cycle_detail(cycle_id)


def _available(value: object | None) -> str:
    return "Not available" if value is None else str(value)


def render() -> None:
    st.title("Research Cycles")
    st.caption("Persisted cycle history and decision funnels. This view is read-only.")
    if st.button("Refresh persisted data", key="refresh-research-cycles"):
        _load_cycles.clear()
        _load_detail.clear()
    limit = st.select_slider("Maximum cycles", options=(25, 50, 100, 200), value=50)
    try:
        database_path = str(configured_database_path())
        cycles = _load_cycles(database_path, limit)
    except (DashboardDatabaseError, ValueError) as exc:
        st.error(str(exc))
        return
    if not cycles:
        st.info("No persisted research cycles are available.")
        return

    st.dataframe(pd.DataFrame([{
        "Cycle ID": cycle.cycle_id,
        "Scheduler-run ID": _available(cycle.scheduler_run_id),
        "Start": cycle.started_at,
        "Completion": _available(cycle.completed_at),
        "Status": cycle.status,
        "Attempted": cycle.symbols_total,
        "Completed": cycle.symbols_completed,
        "Skipped": cycle.symbols_skipped,
        "Failed": cycle.symbols_failed,
        "Evidence-provider mode": cycle.provider_mode,
        "Research provider / model / mode": "; ".join(cycle.research_provider_partitions) or "Not available",
    } for cycle in cycles]), hide_index=True, use_container_width=True)

    selected_id = st.selectbox("Inspect cycle", [cycle.cycle_id for cycle in cycles])
    try:
        detail = _load_detail(database_path, selected_id)
    except (DashboardDatabaseError, ValueError) as exc:
        st.error(str(exc))
        return
    if detail is None:
        st.info("The selected cycle is no longer available.")
        return

    st.subheader(f"Cycle funnel · {selected_id}")
    funnel = detail.funnel
    st.dataframe(pd.DataFrame([{
        "Selected": funnel.selected,
        "Screened out": funnel.screened_out,
        "Evidence incomplete": funnel.evidence_incomplete,
        "Research incomplete": funnel.research_incomplete,
        "Policy rejected": funnel.policy_rejected,
        "Buy candidates": funnel.buy_candidates,
        "Paper submitted": funnel.paper_submitted,
        "Filled": funnel.filled,
        "Not filled": funnel.not_filled,
    }]), hide_index=True, use_container_width=True)

    if not detail.decisions:
        st.info("No ticker decisions are persisted for this cycle.")
        return
    labels = {
        f"{decision.symbol} · {decision.final_outcome.value}": decision
        for decision in detail.decisions
    }
    ticker_label = st.selectbox("Inspect ticker decision", labels)
    decision = labels[ticker_label]
    try:
        decision_detail = DecisionService(database_path).get_decision_detail(selected_id, decision.symbol)
    except (DashboardDatabaseError, ValueError) as exc:
        st.error(str(exc))
        return
    if decision_detail:
        render_decision_detail(decision_detail)
