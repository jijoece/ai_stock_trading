"""Read-only Streamlit dashboard entry point."""
from __future__ import annotations

import streamlit as st

from dashboard.pages.decisions import render as render_decisions
from dashboard.pages.overview import render as render_overview
from dashboard.pages.portfolio import render as render_portfolio
from dashboard.pages.research_cycles import render as render_research_cycles
from dashboard.pages.system_health import render as render_system_health


st.set_page_config(page_title="Agentic Trading Desk", page_icon="📊", layout="wide")

navigation = st.navigation([
    st.Page(render_overview, title="Overview", icon="📊", url_path="overview", default=True),
    st.Page(render_decisions, title="Decisions", icon="🔎", url_path="decisions"),
    st.Page(render_research_cycles, title="Research Cycles", icon="🧪", url_path="research-cycles"),
    st.Page(render_portfolio, title="Portfolio", icon="💼", url_path="portfolio"),
    st.Page(render_system_health, title="System Health", icon="🩺", url_path="system-health"),
])
navigation.run()
