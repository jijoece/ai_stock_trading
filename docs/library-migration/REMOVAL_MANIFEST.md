# Removal Manifest

Nothing is removed by PR 0, 1, or 2. Every entry below requires a **passing
fixture-parity test** in its assigned PR before deletion happens in the
corresponding later PR. Until then, the current custom implementation
remains authoritative.

Do not add `paper_books/*`, `backtesting/engine.py`, `external_broker.py`,
`paper_books/config.py`, or `storage/*` to this manifest — see
`PRESERVATION_MANIFEST.md` and `DECISIONS.md` D1/D3 for why.

| Component | File(s) | Eligible for removal after | Not eligible until | Removal PR |
|---|---|---|---|---|
| Custom market calendar | `src/trading_research/evaluation/market_calendar.py` | `exchange_calendars` fixture parity | All session/holiday/weekend/DST/pre-market/intraday/after-hours/previous-and-next-session/session-count fixture cases pass | PR 17 (parity proven in PR 3) |
| Custom indicators | `scripts/indicators.py` | TA-Lib fixture parity | Warm-up, null, and rounding semantics documented and matched for EMA/RSI/MACD/TRIX/Bollinger | PR 17 (parity proven in PR 4) |
| Custom analytics formulas | `src/trading_research/evaluation/metrics.py` — `sharpe_ratio`, `sortino_ratio`, `max_drawdown`, `calmar_ratio`, `cumulative_return` only | quantstats-lumi / empyrical-reloaded fixture parity | Annualization convention and insufficient-data status semantics matched | PR 17 (parity proven in PR 11) |
| Custom logging formatter | `src/trading_research/logging_config.py` — `RedactingFormatter`, `JsonRedactingFormatter` | Structlog processor parity | Redaction and secret-registration behavior matched exactly (no secret ever logged during the transition) | PR 17 (parity proven in PR 15) |

## Conditionally eligible — gated on a decision PR, not a parity PR

| Component | File(s) | Gate | Outcome if not approved |
|---|---|---|---|
| Custom event-driven backtest engine | `src/trading_research/backtesting/engine.py`, `models.py` | PR 8 decision, based on PR 7 parity report | Remains authoritative indefinitely; LumiBot backtest adapter becomes an additional, non-replacing option |

## Explicitly excluded from this manifest (any version)

```text
paper_books/cash_ledger.py
paper_books/positions.py
paper_books/valuation.py
paper_books/reconciliation.py
paper_books/config.py (hard safety limits)
paper_books/external_broker.py (safety gateway, ambiguous-retry recovery)
paper/ledger.py (quarantined legacy ledger — not migrated either direction)
storage/database.py, storage/*_schema.py, storage/*_repositories.py,
  storage/migrations.py, storage/schema_version.py
  (Category B — evaluate only in PR 13/14, no removal decision until then)
research/* (AI evidence and research orchestration)
```

Adding any of these to this manifest requires the full ADR-supersession
process in `DECISIONS.md`'s governing principle — identify the exact ADR
decision, prove repository evidence invalidates it, prove replacement
parity, add parity/safety/migration/failure-recovery tests, add a
superseding ADR, get approval. None of that process has occurred as of PR 0.
