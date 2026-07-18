"""Filtered decision explorer and structured decision detail."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st

from dashboard.models.view_models import CandidateDecisionDetail, DashboardOutcome
from dashboard.services.database import DashboardDatabaseError
from dashboard.services.decision_service import DecisionFilters, DecisionService


def _available(value: object | None) -> str:
    return "Not available" if value is None else str(value)


def _money(value: Decimal | None) -> str:
    return "Not available" if value is None else f"${value:,.2f}"


def _bullets(items: tuple[str, ...]) -> None:
    if items:
        for item in items:
            st.markdown(f"- {item}")
    else:
        st.write("Not available")


def _render_path(detail: CandidateDecisionDetail) -> None:
    stages = (
        ("1. Candidate selection", detail.screening_status),
        ("2. Screening", detail.evidence_screening_completeness),
        ("3. Evidence completeness", detail.evidence_research_completeness),
        ("4. Research result", detail.research_status),
        ("5. Deterministic overlay", next((x for x in detail.policy_checks if not x.startswith(("Risk policy:", "Overlay policy:"))), None)),
        ("6. Risk and policy evaluation", detail.risk_decision),
        ("7. Paper-order eligibility", detail.summary.paper_order_status),
        ("8. Order and fill result", detail.fill_status or detail.summary.paper_order_status),
    )
    for label, status in stages:
        st.write(f"**{label}:** {_available(status)}")


def render_decision_detail(detail: CandidateDecisionDetail) -> None:
    st.subheader(f"Decision detail · {detail.summary.symbol}")
    st.write(
        f"**Outcome:** {detail.summary.final_outcome.value}  \n"
        f"**Primary reason:** {detail.summary.primary_reason_code} — {detail.summary.friendly_reason}"
    )
    with st.expander("Decision path", expanded=True):
        _render_path(detail)

    if detail.summary.final_outcome is DashboardOutcome.BOUGHT_OR_SUBMITTED:
        left, right = st.columns(2)
        with left:
            st.markdown("#### Bull thesis")
            st.write(_available(detail.bull_thesis))
            st.markdown("#### Catalysts")
            _bullets(detail.catalysts)
            st.markdown("#### Evidence references")
            _bullets(detail.evidence_references)
        with right:
            st.markdown("#### Bear case")
            st.write(_available(detail.bear_case))
            st.markdown("#### Risks")
            _bullets(detail.risks)
            st.markdown("#### Policy checks")
            _bullets(detail.policy_checks)
        st.write({
            "Reference price": _money(detail.reference_price),
            "Limit price": _money(detail.limit_price),
            "Quantity": _available(detail.quantity),
            "Paper book": _available(detail.paper_book_id),
            "Order status": _available(detail.summary.paper_order_status),
            "Fill status": _available(detail.fill_status),
        })
    else:
        st.write({
            "Stable reason code": detail.summary.primary_reason_code,
            "Friendly explanation": detail.summary.friendly_reason,
            "Failed stage": _available(detail.failed_stage),
            "Observed value": _available(detail.observed_value),
            "Required threshold": _available(detail.required_threshold),
            "Block classification": _available(detail.block_category),
        })


def render() -> None:
    st.title("Decisions")
    st.caption("Bounded, persisted decision records. No order or control actions are available.")
    filters_row = st.columns(4)
    start_date = filters_row[0].date_input("From", value=None)
    end_date = filters_row[1].date_input("Through", value=None)
    symbol = filters_row[2].text_input("Symbol", max_chars=16)
    outcome_value = filters_row[3].selectbox(
        "Outcome", ["All", *(outcome.value for outcome in DashboardOutcome)]
    )
    reason = st.text_input("Primary reason code", max_chars=120)
    limit = st.select_slider("Maximum results", options=(25, 50, 100, 200), value=100)

    filters = DecisionFilters(
        start_date=start_date if isinstance(start_date, date) else None,
        end_date=end_date if isinstance(end_date, date) else None,
        symbol=symbol or None,
        outcome=None if outcome_value == "All" else DashboardOutcome(outcome_value),
        primary_reason=reason or None,
    )
    service = DecisionService()
    try:
        decisions = service.list_decisions(filters, limit=limit)
    except (DashboardDatabaseError, ValueError) as exc:
        st.error(str(exc))
        return

    if not decisions:
        st.info("No persisted decisions match these filters.")
        return

    frame = pd.DataFrame([{
        "Symbol": item.symbol,
        "Timestamp": item.timestamp,
        "Outcome": item.final_outcome.value,
        "Primary reason": item.primary_reason_code,
        "Score / confidence": str(item.score) if item.score is not None else _available(item.confidence),
        "Recommended action": item.enhanced_result or item.baseline_result or "Not available",
        "Paper-order status": _available(item.paper_order_status),
        "Cycle ID": _available(item.research_cycle_id),
        "Scheduler-run ID": _available(item.scheduler_run_id),
    } for item in decisions])
    st.dataframe(frame, hide_index=True, use_container_width=True)

    labels = {
        f"{item.timestamp.isoformat()} · {item.symbol} · {item.final_outcome.value} · {index + 1}": item
        for index, item in enumerate(decisions)
    }
    selected_label = st.selectbox("Inspect decision", labels)
    selected = labels[selected_label]
    if selected.research_cycle_id:
        try:
            detail = service.get_decision_detail(selected.research_cycle_id, selected.symbol)
        except (DashboardDatabaseError, ValueError) as exc:
            st.error(str(exc))
            return
        if detail:
            render_decision_detail(detail)
