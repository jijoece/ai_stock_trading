# Deterministic Strategy-Based Candidate Selection

Canonical summary of the Milestone 23 (B) capability: three deterministic
strategy scanners that generate a small, explainable candidate shortlist
ahead of the existing AI research committee. Code and tests are
authoritative for exact behavior; this document is a map, not a
specification.

## Why a separate layer

The existing composite score (fundamentals, technicals, catalysts/risk,
Reddit sentiment) ranks generally promising companies but cannot say
whether a candidate qualifies for mean reversion, momentum breakout, or an
event-driven catalyst. Strategy signals make that explicit, enabling
per-strategy backtests, independent performance measurement, and — because
only shortlisted candidates ever reach the AI research committee — reduced
LLM usage.

## Target flow

```text
verified universe
→ hard liquidity/safety screening      (analysis/screener.py, reused)
→ deterministic strategy scanners      (strategies/momentum_breakout.py,
                                         strategies/mean_reversion.py,
                                         strategies/event_catalyst.py)
→ strategy-specific ranking + shortlist (strategies/selector.py)
→ token-budgeted research handoff      (strategies/research_budget.py)
→ optional AI research committee       (research/orchestration.py)
→ conservative overlay                 (strategies/execution_boundary.py)
→ paper execution                      (paper_books/, gated by activation stage)
```

Zero LLM calls occur before the token-budgeted research handoff step.
`tests/unit/test_strategy_safety_boundaries.py::test_strategies_package_has_no_llm_touching_import`
enforces this at import level for the whole `strategies/` package.

## Module map

| Concern | Module |
|---|---|
| Signal/context contracts | `strategies/contracts.py` |
| Shared safety gates | `strategies/safety_gates.py` |
| Momentum breakout | `strategies/momentum_breakout.py` |
| Mean reversion | `strategies/mean_reversion.py` |
| Event-driven catalyst | `strategies/event_catalyst.py` |
| Deterministic multi-strategy shortlist | `strategies/selector.py` |
| Token-budgeted research handoff | `strategies/research_budget.py` |
| Strategy-specific execution/overlay boundary | `strategies/execution_boundary.py` |
| Per-strategy backtest adapter | `strategies/backtest_adapter.py` |
| Per-strategy backtest metrics | `strategies/strategy_metrics.py` |
| Configuration loader | `strategies/config.py`, `config/strategies.yaml` |

## Execution and overlay boundaries (B7)

`strategies/execution_boundary.py` is the only place a strategy signal's
identity is carried into an order-intent-shaped record
(`StrategyOrderIntentContext`: `strategy_id`, `strategy_signal_id`, entry
condition, invalidation condition, expected holding period, strategy
stop), and the only place an AI research overlay disposition
(`ALLOW_ENTRY` / `REDUCE_CONFIDENCE` / `REDUCE_SIZE` / `NO_ACTION` /
`ANALYSIS_INCOMPLETE`) is folded on top. The overlay function can only
shrink a size multiplier toward zero — it never edits the underlying
context, so it cannot invent a signal, remove a stop, or promote a
non-eligible strategy result.

## Backtesting (B8)

`strategies/backtest_adapter.py` translates `ELIGIBLE` `StrategySignal`s
into `backtesting.models.EntrySignal` and reuses the existing point-in-time
engine (`backtesting/engine.py`) rather than building a second one — the
same no-future-data, next-session-entry, limit-fill, ATR-stop/target, and
transaction-cost rules apply. `strategies/strategy_metrics.py`
reconstructs round-trip trades from the resulting fills and reports win
rate, average/median return, maximum drawdown (from the engine), profit
factor, expectancy, average holding period, turnover, exposure,
time-to-fill, percentage of unfilled signals, and an optional breakdown by
caller-supplied market-regime label. No LLM is used to calculate or
summarize these metrics.

## Activation stages (B9)

```text
Stage 1: offline fixtures
Stage 2: historical backtest
Stage 3: daily read-only candidate list
Stage 4: shadow recommendation tracking
Stage 5: local paper book
Stage 6: supervised Alpaca paper
Stage 7: multi-day paper soak
```

`config/strategies.yaml` → `strategy_candidate_selection.activation_stage`
records the current stage; the loader in `strategies/config.py` refuses to
load a config where `enabled: true` while still at stage 1 or 2. As of this
milestone the repository is at `STAGE_2_HISTORICAL_BACKTEST`:
`strategy_candidate_selection.enabled` stays `false`, and no strategy
signal creates a paper intent.

## Safety invariants

* Strategy scanners cannot bypass `analysis/screener.py`'s hard gates.
* A `StrategySignal` cannot be `ELIGIBLE` without at least one reason code
  (`contracts.py`), and only `ELIGIBLE` signals can seed an order-intent
  context or a backtest entry (`execution_boundary.py`,
  `backtest_adapter.py`) — both fail closed otherwise.
* An AI research overlay can only hold a candidate back, never raise
  signal strength, size, or promote a non-eligible result
  (`execution_boundary.apply_overlay_disposition`).
* Market scanning, ranking, and backtesting run at zero LLM calls; only a
  shortlisted, budget-approved candidate can reach the research committee.
