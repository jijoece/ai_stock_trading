import sqlite3

import pytest

from trading_research.storage.migrations import apply_schema
from trading_research.storage.trading_schema import apply_trading_schema

REQUIRED_TABLES = {
    "securities", "price_bars", "fundamentals", "corporate_events",
    "earnings_calendar", "sec_filings", "news_items", "reddit_posts",
    "reddit_comments", "reddit_ticker_mentions", "screening_runs",
    "candidate_scores", "recommendations", "recommendation_factors",
    "model_versions", "prompt_versions", "simulated_orders",
    "simulated_fills", "simulated_positions", "simulated_portfolio_snapshots",
    "approvals", "real_orders", "evaluation_results", "benchmark_results",
    "agent_runs", "tool_calls", "errors",
}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_schema_creates_all_required_tables():
    conn = _conn()
    apply_trading_schema(conn)
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert REQUIRED_TABLES <= tables


def test_schema_reapplies_idempotently():
    conn = _conn()
    apply_trading_schema(conn)
    apply_trading_schema(conn)  # must not raise
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert REQUIRED_TABLES <= tables


def test_expected_indexes_present():
    conn = _conn()
    apply_trading_schema(conn)
    indexes = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    expected = {
        "idx_price_bars_ts",
        "idx_recommendations_symbol_ts",
        "idx_recommendations_run",
        "uq_mentions_record_symbol_span",
        "idx_sim_orders_symbol_ts",
        "idx_approvals_rec",
    }
    assert expected <= indexes


def test_foreign_key_enforcement_rejects_orphan_recommendation_factor():
    conn = _conn()
    apply_trading_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO recommendation_factors (rec_id, factor, weight, contribution) "
            "VALUES ('does-not-exist', 'foo', 0.1, 1.0)"
        )


def test_foreign_key_enforcement_allows_valid_reference():
    conn = _conn()
    apply_trading_schema(conn)
    conn.execute(
        "INSERT INTO recommendations (rec_id, symbol, side, ts, status) "
        "VALUES ('rec-1', 'AAPL', 'watch', '2026-07-11T00:00:00+00:00', 'active')"
    )
    conn.execute(
        "INSERT INTO recommendation_factors (rec_id, factor, weight, contribution) "
        "VALUES ('rec-1', 'foo', 0.1, 1.0)"
    )
    conn.commit()
    row = conn.execute("SELECT * FROM recommendation_factors WHERE rec_id = 'rec-1'").fetchone()
    assert row is not None


def test_simulated_orders_idempotency_key_unique():
    conn = _conn()
    apply_trading_schema(conn)
    conn.execute(
        "INSERT INTO simulated_orders (order_id, ts, symbol, side, qty, idempotency_key) "
        "VALUES ('o1', '2026-07-11T00:00:00+00:00', 'AAPL', 'buy', 10, 'idem-1')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO simulated_orders (order_id, ts, symbol, side, qty, idempotency_key) "
            "VALUES ('o2', '2026-07-11T00:00:00+00:00', 'AAPL', 'buy', 5, 'idem-1')"
        )


def test_recommendation_immutable_after_freezing():
    conn = _conn()
    apply_trading_schema(conn)
    conn.execute(
        "INSERT INTO recommendations (rec_id, symbol, side, ts, status, frozen) "
        "VALUES ('rec-1', 'AAPL', 'watch', '2026-07-11T00:00:00+00:00', 'active', 1)"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE recommendations SET score = 99 WHERE rec_id = 'rec-1'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM recommendations WHERE rec_id = 'rec-1'")


def test_real_orders_has_no_write_path():
    """real_orders is reserved: every write is rejected at the DB level."""
    conn = _conn()
    apply_trading_schema(conn)
    conn.execute(
        "INSERT INTO approvals (approval_id, payload_json, payload_hash, created_at, expires_at) "
        "VALUES ('appr-1', '{}', 'hash', '2026-07-11T00:00:00+00:00', '2026-07-12T00:00:00+00:00')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO real_orders (order_id, approval_id, payload_hash, created_at, status) "
            "VALUES ('ro-1', 'appr-1', 'hash', '2026-07-11T00:00:00+00:00', 'pending')"
        )


def test_errors_table_has_severity_and_data_quality_columns():
    conn = _conn()
    apply_trading_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(errors)").fetchall()}
    assert {"severity", "data_quality"} <= cols


def test_recommendations_have_reproducibility_columns():
    conn = _conn()
    apply_trading_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(recommendations)").fetchall()}
    assert {"config_hash", "git_sha", "model_version", "prompt_version"} <= cols


def test_legacy_recommendations_table_renamed_on_upgrade():
    """A pre-rename DB had the research-run shape under the name `recommendations`.

    Verifies the migration detects it by shape and renames it instead of
    colliding with the trading-schema `recommendations` table.
    """
    conn = _conn()
    conn.executescript(
        """
        CREATE TABLE recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workstream_id TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            confidence TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO recommendations (workstream_id, recommendation, confidence) "
        "VALUES ('ws-1', 'buy XYZ', 'high')"
    )
    conn.commit()

    apply_schema(conn)
    apply_trading_schema(conn)

    legacy_row = conn.execute(
        "SELECT * FROM research_recommendations WHERE workstream_id = 'ws-1'"
    ).fetchone()
    assert legacy_row is not None
    assert legacy_row["recommendation"] == "buy XYZ"

    cols = {r[1] for r in conn.execute("PRAGMA table_info(recommendations)").fetchall()}
    assert "rec_id" in cols  # now the trading-schema table


def test_both_schemas_apply_together_without_collision():
    conn = _conn()
    apply_schema(conn)
    apply_trading_schema(conn)
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "research_recommendations" in tables
    assert "recommendations" in tables
    rec_cols = {r[1] for r in conn.execute("PRAGMA table_info(recommendations)").fetchall()}
    assert "rec_id" in rec_cols
