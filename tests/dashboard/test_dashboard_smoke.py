from importlib import import_module
from pathlib import Path

from streamlit.testing.v1 import AppTest

def test_page_imports_do_not_touch_database(dashboard_database: Path, monkeypatch):
    monkeypatch.setenv("AI_STOCK_TRADING_DB_PATH", str(dashboard_database))
    before = dashboard_database.read_bytes()

    for module in (
        "dashboard.pages.overview",
        "dashboard.pages.decisions",
        "dashboard.pages.research_cycles",
        "dashboard.pages.portfolio",
        "dashboard.pages.system_health",
    ):
        import_module(module)

    assert dashboard_database.read_bytes() == before


def test_app_entry_point_renders_persisted_overview(dashboard_database: Path, monkeypatch):
    monkeypatch.setenv("AI_STOCK_TRADING_DB_PATH", str(dashboard_database))

    app = AppTest.from_file("../../src/dashboard/streamlit_app.py", default_timeout=10).run()

    assert not app.exception
    assert app.title[0].value == "Overview"
    metric_values = {metric.label: metric.value for metric in app.metric}
    assert metric_values["Portfolio value"] == "$9,995.00"
    assert metric_values["Bought or submitted"] == "1"


def test_decisions_page_renders_filtered_results_and_detail(dashboard_database: Path, monkeypatch):
    monkeypatch.setenv("AI_STOCK_TRADING_DB_PATH", str(dashboard_database))

    app = AppTest.from_string(
        "from dashboard.pages.decisions import render\nrender()",
        default_timeout=10,
    ).run()

    assert not app.exception
    assert app.title[0].value == "Decisions"
    assert list(app.dataframe[0].value["Symbol"]) == ["ABC", "XYZ"]
    assert app.subheader[0].value == "Decision detail · ABC"
    assert "Structured bull case for ABC" in tuple(item.value for item in app.markdown)


def test_research_cycles_page_renders_funnel_and_ticker_detail(dashboard_database: Path, monkeypatch):
    monkeypatch.setenv("AI_STOCK_TRADING_DB_PATH", str(dashboard_database))

    app = AppTest.from_string(
        "from dashboard.pages.research_cycles import render\nrender()",
        default_timeout=10,
    ).run()

    assert not app.exception
    assert app.title[0].value == "Research Cycles"
    assert app.subheader[0].value == "Cycle funnel · cycle-1"
    assert app.subheader[1].value == "Decision detail · ABC"


def test_portfolio_page_renders_positions_orders_and_fills(dashboard_database: Path, monkeypatch):
    monkeypatch.setenv("AI_STOCK_TRADING_DB_PATH", str(dashboard_database))

    app = AppTest.from_string(
        "from dashboard.pages.portfolio import render\nrender()",
        default_timeout=10,
    ).run()

    assert not app.exception
    assert app.title[0].value == "Portfolio"
    assert app.subheader[0].value == "book-1 · BASELINE · ACTIVE"
    assert len(app.dataframe) == 3


def test_system_health_page_keeps_provider_types_separate(dashboard_database: Path, monkeypatch):
    monkeypatch.setenv("AI_STOCK_TRADING_DB_PATH", str(dashboard_database))

    app = AppTest.from_string(
        "from dashboard.pages.system_health import render\nrender()",
        default_timeout=10,
    ).run()

    assert not app.exception
    assert app.title[0].value == "System Health"
    assert [heading.value for heading in app.subheader] == [
        "Evidence-provider health", "Model-provider health",
    ]
    assert app.dataframe[0].value.iloc[0]["Provider"] == "sec_edgar"
    assert set(app.dataframe[1].value["Provider"]) == {"codex", "fixture"}
