import json
import sqlite3

import pytest
from jsonschema import Draft7Validator

from trading_research.cli import DISCLAIMER, analyze
from trading_research.config import REPO_ROOT
from trading_research.storage.trading_schema import apply_trading_schema


@pytest.fixture(scope="module")
def validator():
    schema = json.loads((REPO_ROOT / "schemas" / "recommendation.schema.json").read_text())
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def test_analyze_known_ticker_validates(validator):
    rec = analyze("SOFI")
    assert list(validator.iter_errors(rec)) == []
    assert rec["status"] == "active"
    assert rec["disclaimer"] == DISCLAIMER
    assert rec["frozen"] is True
    # Reddit weight cap enforced structurally.
    assert rec["reddit_component"]["weight"] <= 0.10


def test_analyze_unknown_ticker_fails_closed(validator):
    rec = analyze("ZZZZZ")
    assert list(validator.iter_errors(rec)) == []
    assert rec["side"] == "no_action"
    assert any("not in the verified ticker universe" in w for w in rec["warnings"])


def test_analyze_no_market_data_is_analysis_incomplete(validator):
    rec = analyze("SIRI")  # in universe, no mock quote
    assert list(validator.iter_errors(rec)) == []
    assert rec["status"] == "analysis_incomplete"


def test_schema_rejects_overweight_reddit(validator):
    rec = analyze("SOFI")
    rec["reddit_component"]["weight"] = 0.5
    assert len(list(validator.iter_errors(rec))) > 0


def test_schema_rejects_unfrozen(validator):
    rec = analyze("SOFI")
    rec["frozen"] = False
    assert len(list(validator.iter_errors(rec))) > 0


def test_trading_schema_applies_idempotently():
    conn = sqlite3.connect(":memory:")
    apply_trading_schema(conn)
    apply_trading_schema(conn)  # idempotent
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    required = {
        "securities", "price_bars", "fundamentals", "corporate_events",
        "earnings_calendar", "sec_filings", "news_items", "reddit_posts",
        "reddit_comments", "reddit_ticker_mentions", "screening_runs",
        "candidate_scores", "recommendations", "recommendation_factors",
        "model_versions", "prompt_versions", "simulated_orders",
        "simulated_fills", "simulated_positions", "simulated_portfolio_snapshots",
        "approvals", "real_orders", "evaluation_results", "benchmark_results",
        "agent_runs", "tool_calls",
    }
    assert required <= tables
