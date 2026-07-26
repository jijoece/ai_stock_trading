# Component Matrix

Every generic-infrastructure responsibility inventoried during PR 0, its
current authority, its classification, and the migration decision. See
`DECISIONS.md` for the reasoning behind any item that diverges from
`docs/milestones/rebuild/plan.md`, and `PRESERVATION_MANIFEST.md` /
`REMOVAL_MANIFEST.md` for the corresponding file-level detail.

Classification legend: **Commodity** (generic, no domain semantics) /
**Evaluate** (plausible library fit, parity or feasibility unproven) /
**Domain-specific** (encodes project safety, accounting, or audit
invariants a library does not provide).

| Component | Current authority | Classification | Target library | Adopt / Evaluate / Defer / Reject | Adapter retained | Custom code removed |
|---|---|---|---|---|---|---|
| Exchange sessions/holidays | `evaluation/market_calendar.py` | Commodity | exchange_calendars | Adopt (PR 3) | Thin wrapper only if callers need a stable return shape | Yes, after fixture parity |
| Technical indicators (`scripts/indicators.py`) | Custom EMA/RSI/MACD/TRIX/Bollinger | Commodity | TA-Lib (pandas-ta-classic fallback) | Adopt (PR 4) | Thin wrapper for warm-up/null/rounding parity | Yes, after fixture parity |
| ATR risk indicator (`analysis/indicators.py`) | Decimal-based ATR | Commodity value / Domain-specific consumer | TA-Lib for the value; `Decimal` conversion stays custom | Adopt (wrapped) | Yes — conversion boundary | Value calc only |
| Strategy signals/scoring | `analysis/scorer.py`, `analysis/screener.py`, `strategies/*` | Domain-specific | None | Preserve | N/A | No |
| Vectorized research/parameter sweeps | None today | New capability | VectorBT | Adopt (PR 5), additive | New adapter, isolated group | No removal — new code |
| Event-driven backtesting | `backtesting/engine.py` | Evaluate (Category B) | LumiBot backtest mode | Evaluate (PR 6/7/8) | Adapter beside existing engine, no deletion yet | Decision gated on PR 7 parity report |
| Alpaca paper execution | `paper_runtime/.../lumibot_gateway.py`, `runtime/lumibot/adapter.py` | Already library-based | LumiBot (unchanged) | No change | Existing `runtime/lumibot/` boundary | No |
| Cash ledger | `paper_books/cash_ledger.py` | **Domain-specific (ADR 0006)** | None | Preserve | N/A | No |
| Position/lot accounting | `paper_books/positions.py` | **Domain-specific (ADR 0006)** | None | Preserve | N/A | No |
| Portfolio valuation | `paper_books/valuation.py` | **Domain-specific (ADR 0006)** | None | Preserve | N/A | No |
| Legacy global ledger | `paper/ledger.py` (quarantined) | Domain-specific, dead-end | None | Preserve as-is | N/A | No — not migrated either direction |
| Performance analytics | `evaluation/metrics.py` | Commodity | quantstats-lumi, empyrical-reloaded | Adopt (PR 11) | `Decimal`→float boundary explicit | Yes, formulas only, after parity |
| Book-level metrics | `paper_books/metrics.py`, `strategies/strategy_metrics.py` | Domain-specific | None | Preserve | N/A | No |
| Benchmark/experiment comparison | `evaluation/research_comparison.py`, `paper_books/comparison.py` | Domain-specific | None | Preserve | N/A | No |
| Configuration (internal domain models) | `config.py`, per-domain config dataclasses | **Domain-specific (ADR 0001)** | None | Preserve | N/A | No |
| Configuration (YAML/env/CLI/JSONL boundary parsing) | Hand-written per-loader validation | Commodity | Pydantic v2 | Evaluate (PR 2, boundary-only) | Explicit boundary→dataclass conversion | Only if PR 2 proves reduction in custom code |
| DataFrame validation | None | New capability | Pandera | Defer | N/A | N/A |
| Historical dataset storage | Fixtures/SQLite only | New capability | PyArrow/Parquet | Defer | N/A | N/A |
| Persistence (repository/DAO layer) | `storage/database.py` (raw `sqlite3`), `storage/*_repositories.py` | Evaluate (Category B) | SQLAlchemy 2.x (Core only for trigger-protected tables) | Evaluate (PR 13) | N/A until approved | No implementation until approved |
| Migrations | `storage/migrations.py`, `storage/schema_version.py` | Evaluate (Category B) | Alembic | Evaluate (PR 13/14) | N/A until approved | No implementation until approved |
| Scheduling (due-time computation) | `shadow/scheduler.py`, `paper_books/recurring_scheduler.py` | Evaluate (Category B) | APScheduler v3 (coexist, not replace) | Evaluate (PR 14) | Lease/generation-fencing stays custom regardless | No |
| Generic transient retries | Per-provider hand-rolled backoff | Evaluate (Category B) | Tenacity | Evaluate (PR 14) | Structurally excluded from ambiguous-broker-retry path | Only generic transport retry code, if approved |
| Ambiguous-order retry/recovery | `paper_books/external_broker.py::recover_stranded_submission` | **Domain-specific** | None | Preserve | N/A | No |
| Structured logging | `logging_config.py` | Commodity | Structlog | Adopt (PR 15) | Custom redaction ported to a structlog processor | Yes, after redaction parity |
| Tracing/metrics (spans) | None (domain telemetry only, not spans) | New capability | OpenTelemetry | Adopt (PR 16), additive | N/A | No — domain telemetry (`cycle_telemetry.py`) stays |
| Portfolio optimization | None | New capability | Riskfolio-Lib | Evaluate (PR 12), advisory-only | Must pass existing hard safety gateway | No — new advisory code only |
| Hard safety limits/gateway | `paper_books/config.py`, `paper_books/external_broker.py` | **Domain-specific** | None | Preserve | N/A | No |
| Account fingerprinting | `paper_books/external_broker.py` | **Domain-specific (ADR 0007)** | None | Preserve | N/A | No |
| AI research/evidence provenance | `research/*` | **Domain-specific (ADR 0003/0004)** | None | Preserve | N/A | No |
| Property-based test coverage | None | New capability | Hypothesis | Adopt (dev group, PR 1) | N/A | No — additive test tooling |
| Deterministic time control in tests | Ad hoc per-test mocking | Commodity | time-machine | Adopt (dev group, PR 1) | N/A | Opportunistic cleanup only where it reduces custom mocking |
