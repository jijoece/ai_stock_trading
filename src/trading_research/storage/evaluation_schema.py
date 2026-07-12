"""SQLite schema for Milestone 4 forward-performance evaluation
(docs/milestone-4.md Step 11). Separate from `execution_schema.py` — an
independently evolving concern, applied idempotently alongside the other
schemas from `storage/database.py::connect`.
"""
from __future__ import annotations

EVALUATION_SCHEMA_VERSION = 1

EVALUATION_DDL = """
CREATE TABLE IF NOT EXISTS recommendation_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id TEXT NOT NULL,
    horizon_trading_days INTEGER NOT NULL,
    status TEXT NOT NULL,
    evaluation_date TEXT NOT NULL,
    benchmark_symbol TEXT NOT NULL,
    recommendation_price TEXT,
    execution_price TEXT,
    benchmark_price_at_execution TEXT,
    ending_symbol_price TEXT,
    ending_benchmark_price TEXT,
    gross_return TEXT,
    net_return TEXT,
    benchmark_return TEXT,
    excess_return TEXT,
    slippage TEXT,
    fees TEXT NOT NULL,
    max_favorable_excursion TEXT,
    max_adverse_excursion TEXT,
    missing_data_reasons_json TEXT NOT NULL,
    model_version TEXT,
    prompt_version TEXT,
    config_hash TEXT,
    market_regime TEXT,
    price_source_as_of TEXT,
    evaluated_at TEXT NOT NULL,
    UNIQUE (recommendation_id, horizon_trading_days)
);
"""

EVALUATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_recommendation_evaluations_rec ON recommendation_evaluations(recommendation_id);
CREATE INDEX IF NOT EXISTS idx_recommendation_evaluations_status ON recommendation_evaluations(status);
"""


def apply_evaluation_schema(conn) -> None:
    conn.executescript(EVALUATION_DDL)
    conn.executescript(EVALUATION_INDEXES)
    conn.commit()
