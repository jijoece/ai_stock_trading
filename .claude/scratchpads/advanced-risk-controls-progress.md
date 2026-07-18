# Advanced Risk Controls Progress

Started: 2026-07-18
Branch: agent/milestone-13-risk-controls
Commit: pending publication
Status: implementation and verification complete

## Baseline
- Main test suite: 2185 passed, 18 skipped, 10 warnings in 47.91s
- Paper runtime suite: 53 passed, 1 skipped in 0.25s
- Git status: clean except the in-scope untracked milestone specification
- Existing failures: none
- Existing skipped tests: 18 main-suite tests and 1 paper-runtime test

## Repository findings
- Existing paper-book entry, lifecycle, local fill, and isolated external broker paths were the authoritative integration points.
- Financial values are persisted as Decimal text; advanced state therefore follows the same exact-arithmetic convention.
- The repository already treats whole-project Pyright as non-blocking technical debt.

## Gap analysis
- No authoritative daily risk state, peak-equity drawdown, persistent safety pause, ATR sizing, monotonic trailing state, staged partial exits, event blackout evidence, or deterministic historical replay existed.
- Existing percentage stops and targets remain available for backwards compatibility when the new lifecycle configuration is disabled.

## Architecture decisions
- New controls are additive, deterministic, Decimal-based, append-only where evidence is involved, and disabled by default.
- Daily risk is derived from complete reconciled snapshots and explicit cash flow; missing, stale, incomplete, or unreconciled evidence fails closed.
- Lifecycle state advances through immutable rows; stop prices never loosen and partial stages advance only after completed fills.
- Backtests use daily bars, next-session entry, whole-share fills, explicit fees/slippage, and conservative stop-first ambiguity handling.

## Workstream 1 — Daily-loss and drawdown breakers
- Added pure daily loss/drawdown formulas, storage-derived authoritative state, exact threshold rejection reasons, append-only persistence, and explicit operator-resume safety events.

## Workstream 2 — ATR, trailing stop, and breakeven
- Added point-in-time Wilder ATR, ATR risk sizing, initial risk levels, high-water tracking, monotonic trailing stops, and breakeven activation.

## Workstream 3 — Partial closes
- Added strict staged configuration, deterministic whole-share sizing, decision persistence, local atomic completion, and external completion after the intended SELL quantity is fully accounted for.

## Workstream 4 — Economic-event blackout
- Added a typed provider boundary, versioned event evidence, inclusive blackout windows, strict freshness/point-in-time checks, and fail-closed decisions. A real provider remains environmentally pending.

## Workstream 5 — Historical backtesting
- Added a minimal deterministic multi-symbol daily-bar engine with next-session entries, ATR sizing, lifecycle exits, daily/drawdown/blackout gates, fees, slippage, metrics, and persisted audit artifacts.

## Schema and migrations
- Added schema migration 10 (renumbered from 6 after integrating Milestone 12.1 from `main`) and additive tables for daily risk, lifecycle states/events, partial stages, economic events/decisions, safety events, and backtest runs/states/orders/fills/metrics.
- Added nullable `paper_book_exit_decisions.partial_stage_id`, indexes, and append-only/audit triggers.

## Files created
- `.claude/scratchpads/advanced-risk-controls-progress.md`
- `docs/adr/0008-advanced-risk-lifecycle-state.md`
- `docs/advanced-risk-controls.md`
- `docs/milestones/milestone-13.md`
- `src/trading_research/analysis/indicators.py`
- `src/trading_research/backtesting/__init__.py`
- `src/trading_research/backtesting/configuration.py`
- `src/trading_research/backtesting/data_provider.py`
- `src/trading_research/backtesting/engine.py`
- `src/trading_research/backtesting/models.py`
- `src/trading_research/backtesting/reports.py`
- `src/trading_research/evidence_providers/economic_calendar.py`
- `src/trading_research/paper_books/daily_risk.py`
- `src/trading_research/paper_books/lifecycle_state.py`
- `src/trading_research/paper_books/safety_pause.py`
- `tests/unit/test_advanced_risk_backtest.py`
- `tests/unit/test_advanced_risk_indicators.py`
- `tests/unit/test_daily_risk_state.py`
- `tests/unit/test_economic_event_blackout.py`

## Files modified
- `README.md`
- `config/paper_books.yaml`
- `docs/INDEX.md`
- `docs/runbooks/paper-book-operations.md`
- `src/trading_research/paper_books/config.py`
- `src/trading_research/paper_books/exit_policy.py`
- `src/trading_research/paper_books/external_broker.py`
- `src/trading_research/paper_books/lifecycle.py`
- `src/trading_research/paper_books/models.py`
- `src/trading_research/paper_books/recurring_scheduler.py`
- `src/trading_research/paper_books/risk.py`
- `src/trading_research/paper_books/scheduled_integration.py`
- `src/trading_research/risk/position_sizing.py`
- `src/trading_research/storage/paper_books_repositories.py`
- `src/trading_research/storage/paper_books_schema.py`
- `src/trading_research/storage/schema_version.py`

## Tests added
- ATR/trailing risk-level calculations and insufficient-history handling.
- Daily risk formulas, storage derivation, cash-flow adjustment, and incomplete-state failure.
- Economic blackout boundaries, stale/missing/unsafe evidence, and deterministic IDs.
- Backtest determinism, next-session fills, stop gaps, ambiguity, partial profit, fees, and risk gates.

## Test run log
- 2026-07-18 pre-change: `.venv/bin/pytest tests/ -q --tb=short` — 2185 passed, 18 skipped, 10 warnings.
- 2026-07-18 pre-change: `../.venv/bin/pytest tests/ -q --tb=short` from `paper_runtime/` — 53 passed, 1 skipped.
- 2026-07-18 post-change targeted integration/migration/config/lifecycle suite — 159 passed, 1 skipped.
- 2026-07-18 post-change full main suite — 2195 passed, 18 skipped, 10 warnings in 46.63s.
- 2026-07-18 post-change paper-runtime suite — 53 passed, 1 skipped in 0.05s.
- 2026-07-18 deterministic scoring driver — all 21 checks passed.
- 2026-07-18 Pyright — 2062 errors, 0 warnings versus 2160 errors, 0 warnings on clean `origin/main`; non-blocking pre-existing debt, no clean claim.
- 2026-07-18 `compileall` and `git diff --check` — passed.

## Bugs discovered
- A partial-stage identifier was initially absent from persisted exit decisions; the additive column and repository mapping were added before final verification.

## Safety verification
- Baseline used offline default suites only; no real provider, model, or broker request was made.
- Post-change verification remained offline. No live trading, external broker/provider/model request, order submission, or scheduler activation occurred.
- Paper books, lifecycle execution, economic blackout, and external execution remain disabled by default.

## Known limitations
- The production economic-calendar provider is intentionally `ENVIRONMENTALLY_PENDING`; enabled blackout enforcement fails closed without safe evidence.
- The minimal backtester is daily-bar only and has no intraday path reconstruction; its conservative stop-first rule is explicit.
- Whole-project Pyright remains non-blocking and is not clean, although the error count is lower than the clean branch baseline.

## Remaining work
- Commit, push, and open a draft pull request.

## Final status
- Implementation complete and verified; awaiting publication.
