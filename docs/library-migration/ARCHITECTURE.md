# Architecture Map (Library Migration, PR 0)

Snapshot as of 2026-07-26, before any library-migration code changes. Current
behavior is defined by code; this document routes migration planning to it,
it does not restate it. See `../README.md` and `../adr/` for the canonical
description.

## Repository architecture map

```text
current components
  Main trading desk: src/trading_research/
    analysis/            screening, scoring, sentiment, ticker extraction
    risk/                deterministic position sizing
    recommendations/     frozen recommendation construction
    research/            evidence-bound model orchestration, replay
    evidence_providers/  SEC, Alpaca market/news, Reddit, cache, HTTP safety
    evaluation/          forward returns, calibration, turnover, fill metrics
    shadow/              scheduler, lease, health, budget, pause/kill, alerts
    paper_books/         current books, lifecycle, campaigns, recurring/external
    execution/           frozen intents, legacy paper-execution boundary
    runtime/client/      main-process side of paper-runtime.v2 protocol
    runtime/lumibot/     the ONLY package permitted to import lumibot
    storage/             SQLite schemas, repositories, migrations/versioning
    backtesting/         event-driven research backtester (custom engine)
    strategies/          momentum/mean-reversion/event-catalyst definitions
    mcp/                 MCP inventory/classification, read adapters
    services/            application service entry points
    models/              shared immutable/domain models
    paper/               quarantined pre-paper_books legacy ledger
    cli.py                unified CLI registration and handlers

  Isolated paper runtime: paper_runtime/src/trading_paper_runtime/
    lumibot_gateway.py    real credentialed Alpaca paper via LumiBot/alpaca-py
    deterministic_gateway.py, broker_gateway.py, dispatcher.py, protocol.py

custom authorities (everything except paper execution)
  data normalization, indicators, strategy signals, exchange calendars,
  backtesting, fill simulation, cash/position/lot accounting, portfolio
  valuation, risk sizing, order construction, reconciliation, analytics,
  scheduling, retry handling, configuration, persistence, migrations,
  logging, telemetry — all custom, mostly stdlib (verified by PR 0 inventory,
  see DEPENDENCY_MATRIX.md).

external-library integrations already present
  lumibot==4.5.74 (repo pin; DEPENDENCY_MATRIX.md notes 4.5.78 is current) —
    isolated to the optional `paper` extra and to runtime/lumibot/ +
    paper_runtime/, enforced by an AST-walk test
    (test_no_lumibot_import_outside_runtime_package)
  alpaca-py — inside paper_runtime only, via lumibot_gateway.py
  streamlit — dashboard only (src/dashboard/)
  vaderSentiment, pandas, httpx, PyYAML, python-dotenv, jsonschema, mcp,
    anthropic — base dependencies, none touch the responsibilities in scope
    for this migration

entry points
  python -m trading_research.cli  (argparse, no Click/Typer)
  scripts/indicators.py, scripts/score.py, scripts/macro_pillar.py
    (standalone, stdlib-only, deterministic)
  src/dashboard/streamlit_app.py  (read-only Streamlit dashboard)

data flow (core research)
  candidate universe -> screening/deterministic inputs -> point-in-time
  evidence snapshot -> fundamental/technical/bull/bear/manager research
  roles -> schema/output/claim/provenance validation -> conservative
  overlay -> frozen recommendation -> scheduled-cycle persistence ->
  forward evaluation

backtest flow
  backtesting/engine.py::run_backtest — session-by-session event loop over
  HistoricalBar fixtures, custom fill simulation with slippage/fees, single
  entry_price per position (no lots) — structurally distinct from the
  paper_books lot model; see COMPONENT_MATRIX.md "Event-driven backtesting"

paper-execution flow (local, in-process)
  frozen intent -> experiment-arm assignment -> book-scoped risk decision
  (paper_books/risk.py) -> immutable order intent -> in-process deterministic
  fill simulator (paper_books/execution.py, NOT paper_runtime) -> cash/lot/
  position accounting (paper_books/cash_ledger.py, positions.py) -> lifecycle
  exits and valuation (paper_books/valuation.py) -> reconciliation, metrics,
  promotion evidence

external Alpaca paper flow (isolated process boundary)
  eligible frozen intent -> operator account check -> explicit preview ->
  recent-preview + payload/account/config checks -> explicit operator
  submit -> paper_runtime subprocess (JSONL over stdin/stdout) -> LumiBot ->
  Alpaca paper account -> broker lookup/fill events -> reconciliation before
  any ambiguous retry (paper_books/external_broker.py)

persistence boundaries
  storage/database.py::connect() — raw sqlite3, WAL, foreign keys,
  bounded busy timeout, schema-version gate (storage/schema_version.py),
  additive per-subsystem schema (storage/*_schema.py), ordered migrations
  (storage/migrations.py). Trigger-protected: append-only tables and the
  insert/update/delete-rejecting real_orders table.
```

## Migration-relevant boundaries already enforced in code

These existing mechanisms are the pattern this migration should extend, not
replace:

- **LumiBot import boundary** — `test_no_lumibot_import_outside_runtime_package`
  walks the AST of every file under `src/trading_research/` to enforce that
  only `runtime/lumibot/` imports `lumibot`. Any new heavy/conflicting
  library (VectorBT, Riskfolio-Lib) adopted by this migration should get an
  equivalent structural test rather than relying on documentation alone —
  see `DECISIONS.md` D4 and the risks in `MASTER_PLAN.md`.
- **`paper_runtime` as a separately installable distribution** — resolves
  the numpy/pandas version conflict between LumiBot and VectorBT/Riskfolio-
  Lib without any new isolation mechanism (`DEPENDENCY_MATRIX.md` Section 2).
- **`paper_books/` structural isolation by `book_id`** — every table's
  primary key or unique constraint includes `book_id` (ADR 0006 Decision 2).
  This is the reason LumiBot cannot become portfolio-accounting-authoritative
  without an approved superseding ADR (`DECISIONS.md` D1).

See `COMPONENT_MATRIX.md` for the full per-responsibility authority table.
