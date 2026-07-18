"""Read-only scheduler, safety, budget, hysteresis, and provider health."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.services.database import DashboardDatabaseError, configured_database_path
from dashboard.services.health_service import HealthService


@st.cache_data(ttl=30, show_spinner=False)
def _load(database_path: str):
    return HealthService(database_path).load()


def _available(value: object | None) -> str:
    return "Not available" if value is None else str(value)


def render() -> None:
    st.title("System Health")
    st.caption("Persisted operational state only. No scheduler or pause controls are available.")
    if st.button("Refresh persisted data", key="refresh-system-health"):
        _load.clear()
    try:
        view = _load(str(configured_database_path()))
    except DashboardDatabaseError as exc:
        st.error(str(exc))
        return

    status = view.status
    first = st.columns(4)
    first[0].metric("Pause / kill state", _available(status.shadow_pause_state))
    first[1].metric("Recurring activation", _available(status.recurring_activation_state))
    first[2].metric("Shadow scheduler", _available(status.latest_shadow_scheduler_status))
    first[3].metric("Recurring scheduler", _available(status.latest_recurring_scheduler_status))
    second = st.columns(4)
    second[0].metric("Latest successful run", _available(status.latest_successful_run_at))
    second[1].metric("Health", _available(status.health_status))
    second[2].metric("Hysteresis", _available(status.hysteresis_status))
    second[3].metric("Budget", _available(status.budget_status))
    st.write({
        "Active policy version": _available(status.active_policy_version),
        "Active policy hash": _available(status.active_policy_hash),
        "Status as of": _available(status.as_of),
        "Hysteresis reasons": status.hysteresis_reasons or ("Not available",),
        "Active paper-book safety pauses": status.active_safety_pauses or ("None",),
    })

    def provider_frame(kind: str) -> pd.DataFrame:
        return pd.DataFrame([{
            "Provider": provider.provider,
            "Model": _available(provider.model),
            "Mode": _available(provider.mode),
            "Production signal": "Yes" if provider.is_production else "No",
            "Status": provider.status,
            "Requests / attempts": provider.total_requests,
            "Successes": provider.successful_requests,
            "Success rate": provider.success_rate,
            "Failure streak": provider.failure_streak,
            "Recovery streak": provider.recovery_streak,
            "Authentication failures": provider.authentication_failures,
            "Configuration failures": provider.configuration_failures,
            "Timeouts": provider.timeout_failures,
            "Rate limits": provider.rate_limit_failures,
            "Quota failures": provider.quota_failures,
            "Latest error code": _available(provider.latest_error_code),
        } for provider in view.providers if provider.provider_kind == kind])

    st.subheader("Evidence-provider health")
    evidence = provider_frame("EVIDENCE")
    st.dataframe(evidence, hide_index=True, use_container_width=True) if not evidence.empty else st.info(
        "No persisted evidence-provider health is available."
    )
    st.subheader("Model-provider health")
    st.caption("Codex, Claude Code, Anthropic, deterministic, and scripted partitions remain separate.")
    models = provider_frame("MODEL")
    st.dataframe(models, hide_index=True, use_container_width=True) if not models.empty else st.info(
        "No persisted model-provider health is available."
    )
