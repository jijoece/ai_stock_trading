"""SQLite schema for the trading-desk data model (architecture §17).

Separate from migrations.py (research-run tables) so the two concerns evolve
independently. Applied idempotently. The `real_orders` table is RESERVED for
a later, explicitly gated phase — no code in this repository writes to it.
"""
from __future__ import annotations

from .migrations import rename_legacy_research_recommendations

TRADING_SCHEMA_VERSION = 2

TRADING_DDL = """
CREATE TABLE IF NOT EXISTS securities (
    symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    exchange TEXT NOT NULL,
    sector TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    is_otc INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT,
    universe_source TEXT NOT NULL DEFAULT 'seed'
);

CREATE TABLE IF NOT EXISTS price_bars (
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER,
    adj_factor REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (symbol, ts, source)
);

CREATE TABLE IF NOT EXISTS fundamentals (
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    revenue REAL, eps REAL, gross_margin REAL, operating_margin REAL,
    free_cash_flow REAL, cash REAL, debt REAL, shares_outstanding REAL,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (symbol, as_of, source)
);

CREATE TABLE IF NOT EXISTS corporate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_date TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS earnings_calendar (
    symbol TEXT NOT NULL,
    report_date TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (symbol, report_date, source)
);

CREATE TABLE IF NOT EXISTS sec_filings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    form_type TEXT NOT NULL,
    filed_at TEXT NOT NULL,
    url TEXT NOT NULL,
    risk_flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS news_items (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    published_at TEXT,
    source TEXT NOT NULL,
    url TEXT,
    headline TEXT NOT NULL,
    injection_risk TEXT NOT NULL DEFAULT 'none',
    retrieved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reddit_posts (
    id TEXT PRIMARY KEY,
    reddit_post_id TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    subreddit TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '[unknown]',
    created_utc REAL NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    num_comments INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    url TEXT,
    injection_risk TEXT NOT NULL DEFAULT 'none',
    retrieved_at TEXT NOT NULL,
    source_endpoint TEXT NOT NULL DEFAULT '',
    fetch_timestamp TEXT NOT NULL DEFAULT '',
    sentiment_compound REAL
);

CREATE TABLE IF NOT EXISTS reddit_comments (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    subreddit TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '[unknown]',
    created_utc REAL NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    injection_risk TEXT NOT NULL DEFAULT 'none',
    retrieved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reddit_ticker_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    is_cashtag INTEGER NOT NULL,
    ambiguous INTEGER NOT NULL,
    context_confirmed INTEGER NOT NULL,
    span_start INTEGER,
    confidence TEXT NOT NULL DEFAULT '',
    rejection_reason TEXT,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS screening_runs (
    run_id TEXT PRIMARY KEY,
    ran_at TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    universe_count INTEGER NOT NULL,
    passed_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_scores (
    run_id TEXT NOT NULL REFERENCES screening_runs(run_id),
    symbol TEXT NOT NULL,
    fundamentals_score REAL,
    technicals_score REAL,
    catalyst_score REAL,
    reddit_component REAL,
    total REAL NOT NULL,
    rank INTEGER,
    PRIMARY KEY (run_id, symbol)
);

-- Frozen at insert: no code path may UPDATE these rows (enforced in
-- repositories; evaluation writes to evaluation_results instead).
CREATE TABLE IF NOT EXISTS recommendations (
    rec_id TEXT PRIMARY KEY,
    run_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    ts TEXT NOT NULL,
    price_at_rec REAL,
    score REAL,
    confidence TEXT,
    status TEXT NOT NULL,
    acted INTEGER NOT NULL DEFAULT 0,
    rationale_text TEXT NOT NULL DEFAULT '',
    model_version TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    config_hash TEXT NOT NULL DEFAULT '',
    git_sha TEXT NOT NULL DEFAULT '',
    frozen INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS recommendation_factors (
    rec_id TEXT NOT NULL REFERENCES recommendations(rec_id),
    factor TEXT NOT NULL,
    raw_value REAL,
    normalized REAL,
    weight REAL,
    contribution REAL,
    PRIMARY KEY (rec_id, factor)
);

CREATE TABLE IF NOT EXISTS model_versions (
    version TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    version TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS simulated_orders (
    order_id TEXT PRIMARY KEY,
    rec_id TEXT REFERENCES recommendations(rec_id),
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty INTEGER NOT NULL CHECK (qty > 0),
    order_type TEXT NOT NULL DEFAULT 'market',
    limit_price REAL,
    stop_price REAL,
    status TEXT NOT NULL DEFAULT 'open',
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS simulated_fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES simulated_orders(order_id),
    ts TEXT NOT NULL,
    price REAL NOT NULL,
    qty INTEGER NOT NULL,
    spread_cost REAL NOT NULL DEFAULT 0,
    slippage_cost REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS simulated_positions (
    symbol TEXT PRIMARY KEY,
    qty INTEGER NOT NULL,
    avg_cost REAL NOT NULL,
    opened_at TEXT NOT NULL,
    stop REAL,
    target REAL,
    status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS simulated_portfolio_snapshots (
    snap_date TEXT PRIMARY KEY,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    settled_cash REAL NOT NULL,
    positions_json TEXT NOT NULL DEFAULT '[]',
    drawdown REAL
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    rec_id TEXT REFERENCES recommendations(rec_id),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'expired', 'invalidated')),
    approved_at TEXT,
    approved_by TEXT
);

-- RESERVED for a later, explicitly gated phase. No writer exists in this repo.
CREATE TABLE IF NOT EXISTS real_orders (
    order_id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL REFERENCES approvals(approval_id),
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_results (
    rec_id TEXT NOT NULL REFERENCES recommendations(rec_id),
    horizon TEXT NOT NULL,
    ret REAL,
    mfe REAL,
    mae REAL,
    stop_hit INTEGER,
    target_hit INTEGER,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (rec_id, horizon)
);

CREATE TABLE IF NOT EXISTS benchmark_results (
    run_id TEXT NOT NULL,
    period TEXT NOT NULL,
    strategy_ret REAL,
    spy_ret REAL,
    equal_weight_ret REAL,
    random_screen_ret_mean REAL,
    random_screen_ret_p05 REAL,
    random_screen_ret_p95 REAL,
    simple_tech_ret REAL,
    PRIMARY KEY (run_id, period)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    config_hash TEXT NOT NULL DEFAULT '',
    git_sha TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    tool TEXT NOT NULL,
    args_hash TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL,
    latency_ms INTEGER,
    error TEXT
);

-- Shared with the research schema (architecture §17: existing table extended
-- with severity + data_quality); created here too so a trading-only
-- connection is self-sufficient.
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    custom_id TEXT,
    occurred_at TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'error',
    data_quality INTEGER NOT NULL DEFAULT 0
);
"""

TRADING_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_price_bars_ts ON price_bars(ts);
CREATE INDEX IF NOT EXISTS idx_corporate_events_symbol ON corporate_events(symbol, event_date);
CREATE INDEX IF NOT EXISTS idx_earnings_report_date ON earnings_calendar(report_date);
CREATE INDEX IF NOT EXISTS idx_sec_filings_symbol ON sec_filings(symbol, filed_at);
CREATE INDEX IF NOT EXISTS idx_news_symbol_published ON news_items(symbol, published_at);
CREATE INDEX IF NOT EXISTS idx_reddit_posts_sub_created ON reddit_posts(subreddit, created_utc);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reddit_posts_symbol_post_id
    ON reddit_posts(symbol, reddit_post_id) WHERE reddit_post_id <> '';
CREATE INDEX IF NOT EXISTS idx_reddit_comments_post ON reddit_comments(post_id);
CREATE INDEX IF NOT EXISTS idx_mentions_symbol_ts ON reddit_ticker_mentions(symbol, ts);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mentions_record_symbol_span
    ON reddit_ticker_mentions(record_type, record_id, symbol, span_start);
CREATE INDEX IF NOT EXISTS idx_candidate_scores_symbol ON candidate_scores(symbol);
CREATE INDEX IF NOT EXISTS idx_recommendations_symbol_ts ON recommendations(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_recommendations_run ON recommendations(run_id);
CREATE INDEX IF NOT EXISTS idx_sim_orders_symbol_ts ON simulated_orders(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_sim_fills_order ON simulated_fills(order_id);
CREATE INDEX IF NOT EXISTS idx_approvals_rec ON approvals(rec_id);
CREATE INDEX IF NOT EXISTS idx_evaluation_computed ON evaluation_results(computed_at);
CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_errors_run ON errors(run_id);
"""

# Fail-closed enforcement at the database level:
# - recommendations are immutable once frozen (evaluation writes elsewhere);
# - real_orders is a RESERVED schema — every write is rejected until a later,
#   explicitly gated phase ships a separate execution profile (arch §12/§21).
TRADING_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_recommendations_frozen_no_update
BEFORE UPDATE ON recommendations
WHEN OLD.frozen = 1
BEGIN
    SELECT RAISE(ABORT, 'recommendations are immutable after freezing');
END;

CREATE TRIGGER IF NOT EXISTS trg_recommendations_frozen_no_delete
BEFORE DELETE ON recommendations
WHEN OLD.frozen = 1
BEGIN
    SELECT RAISE(ABORT, 'recommendations are immutable after freezing');
END;

CREATE TRIGGER IF NOT EXISTS trg_real_orders_reserved_insert
BEFORE INSERT ON real_orders
BEGIN
    SELECT RAISE(ABORT, 'real_orders is reserved — no execution path exists in this phase');
END;

CREATE TRIGGER IF NOT EXISTS trg_real_orders_reserved_update
BEFORE UPDATE ON real_orders
BEGIN
    SELECT RAISE(ABORT, 'real_orders is reserved — no execution path exists in this phase');
END;

CREATE TRIGGER IF NOT EXISTS trg_real_orders_reserved_delete
BEFORE DELETE ON real_orders
BEGIN
    SELECT RAISE(ABORT, 'real_orders is reserved — no execution path exists in this phase');
END;
"""

# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS
# no-ops on existing databases, so these are applied via idempotent ALTERs.
_COLUMN_UPGRADES: dict[str, dict[str, str]] = {
    "recommendations": {
        "config_hash": "TEXT NOT NULL DEFAULT ''",
        "git_sha": "TEXT NOT NULL DEFAULT ''",
        # Full frozen payload (risk_plan, warnings, missing_data_reasons,
        # data_timestamps, reddit_component, disclaimer, ...) — the flat
        # columns above never carried these, so Milestone 3's paper-execution
        # eligibility/intent path (which needs risk_plan) has something to
        # read back verbatim instead of reconstructing it lossily from
        # recommendation_factors. NULL for any row written before this
        # column existed; readers must treat NULL as "unavailable", never
        # reconstruct a guessed payload.
        "payload_json": "TEXT",
    },
    "reddit_ticker_mentions": {
        "span_start": "INTEGER",
        "confidence": "TEXT NOT NULL DEFAULT ''",
        "rejection_reason": "TEXT",
    },
    "reddit_posts": {
        "reddit_post_id": "TEXT NOT NULL DEFAULT ''",
        "symbol": "TEXT NOT NULL DEFAULT ''",
        "body": "TEXT NOT NULL DEFAULT ''",
        "source_endpoint": "TEXT NOT NULL DEFAULT ''",
        "fetch_timestamp": "TEXT NOT NULL DEFAULT ''",
        "sentiment_compound": "REAL",
    },
    "errors": {
        "severity": "TEXT NOT NULL DEFAULT 'error'",
        "data_quality": "INTEGER NOT NULL DEFAULT 0",
    },
}


def _ensure_columns(conn) -> None:
    for table, columns in _COLUMN_UPGRADES.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            continue  # table doesn't exist yet; CREATE handles it in full
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def apply_trading_schema(conn) -> None:
    rename_legacy_research_recommendations(conn)
    conn.executescript(TRADING_DDL)
    _ensure_columns(conn)
    conn.executescript(TRADING_INDEXES)
    conn.executescript(TRADING_TRIGGERS)
    conn.commit()
