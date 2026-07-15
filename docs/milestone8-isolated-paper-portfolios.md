# Milestone 8 — Isolated paper portfolios and portfolio-aware experiment evaluation

**Status:** Complete for this session's scope.
**Date:** 2026-07-14
**Applies to:** `src/trading_research/paper_books/`, `config/paper_books.yaml`,
`docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md`.

See `.claude/scratchpads/milestone8-progress.md` for the full session log (architecture
inventory, per-step verification narrative, exact commands run). This document is the
durable architecture record.

**Milestone 8.1 pointer:** the real scheduled-research-cycle-to-isolated-paper-book
integration this document's Section 20/21 recorded as deferred is now implemented — see
`docs/milestone8-1-scheduled-paper-book-integration.md`. Section 5's table count below is
corrected to 14 (was previously misstated as "ten" in an earlier draft of this document).

---

## 1. What this milestone built

Two fully isolated paper portfolios — `BASELINE` and `ENHANCED` — so the baseline
deterministic arm and the Claude-informed enhanced arm can each accumulate their own paper
trading history without ever sharing cash, positions, orders, fills, or reconciliation state.
This is a **wholly new, additive subsystem** (`paper_books/` package + `paper_book_*` tables)
living beside the existing Milestone 3/4 global `paper/ledger.py::PaperLedger` — that legacy
system is completely untouched and keeps working exactly as before for
`OBSERVE_ONLY`/`BASELINE_ONLY`/`SHADOW_ENHANCED`.

This milestone remains strictly **LOCAL-SIMULATED-PAPER**. No live trading, no external
paper-broker submission, no margin, no short selling, no options, and no automatic promotion
exist anywhere in this code.

## 2. Architecture

```
Frozen recommendation (baseline or enhanced, Milestone 5/6, unchanged)
        |
paper_books/experiment_assignment.py   (shared evidence_snapshot_id / as_of, one row per cycle+symbol)
        |
paper_books/valuation.py               (point-in-time portfolio snapshot, per book)
        |
paper_books/risk.py                    (deterministic per-book risk decision)
        |
paper_books/order_intent.py            (book-aware immutable order intent, persisted risk decision)
        |
paper_books/execution.py               (local-simulated fill engine, book-scoped idempotency)
        |
paper_books/cash_ledger.py + positions.py   (append-only cash ledger, FIFO lot accounting)
        |
paper_books/reconciliation.py          (independent per-book reconciliation)
        |
paper_books/metrics.py                 (per-book performance metrics)
        |
paper_books/comparison.py              (fail-closed baseline-vs-enhanced comparison)
        |
paper_books/promotion_evidence.py      (evidence-only promotion status, never automatic)
```

Every module above takes an explicit `book_id` parameter and every SQL statement in
`storage/paper_books_repositories.py` filters by it — there is no code path that can read or
write two books' rows in one query.

## 3. Isolation guarantees (all independently tested)

* Separate cash: `paper_book_cash_ledger`, append-only, `UNIQUE(book_id, idempotency_key)`.
* Separate positions/lots: `paper_book_positions`/`paper_book_position_lots`, PK includes
  `book_id`. FIFO consumption is scoped to `WHERE book_id = ?`.
* Separate orders/fills: PK `(book_id, paper_order_intent_id)` / `(book_id, fill_id)`. The
  same `recommendation_id` submitted to two books always produces two different
  `paper_order_intent_id` values (`derive_paper_order_intent_id` hashes `book_id` in).
* Separate risk decisions: `paper_book_risk_decisions`, one row per `(book_id, cycle_id,
  recommendation_id, symbol)`.
* Separate reconciliation: `reconcile_book(book_id, ...)` never queries a second book; a
  mismatch in one book is invisible to the other (tested directly).
* No fallback: `research/experiment_policy.py`'s new `may_submit_baseline_to_paper_book`/
  `may_submit_enhanced_to_paper_book` map each policy to an exact, fixed set of books — never
  inferred, never a silent substitution.

## 4. Configuration

`config/paper_books.yaml` — shipped with `paper_books.enabled: false` and the enhanced
book's own `enabled: false`. `execution.allow_live_broker` is structurally impossible to be
`true` (config loading raises `PaperBooksConfigError` immediately). Decimal-safe (`Decimal`
string values), unknown top-level/nested keys fail closed, duplicate/invalid `book_id` fails
closed. `.env` cannot enable a book or set its cash — there are no credentials in this file
at all.

## 5. Schema

Fourteen new tables in `storage/paper_books_schema.py` (`paper_books`, `paper_book_cash_ledger`,
`paper_book_risk_decisions`, `paper_book_orders`, `paper_book_fills`, `paper_book_positions`,
`paper_book_position_lots`, `paper_book_snapshots`, `paper_book_snapshot_positions`,
`paper_book_reconciliations`, `paper_book_daily_metrics`,
`paper_book_corporate_actions_applied`, `paper_book_experiment_assignments`,
`paper_book_experiment_comparisons`, `paper_book_promotion_evidence` — 14 total). All
additive; zero existing table modified. Immutability enforced via `BEFORE UPDATE`/`BEFORE
DELETE` triggers on every append-only table (cash ledger, fills, snapshots, reconciliations,
experiment assignments/comparisons); `paper_book_orders`/`paper_book_position_lots` allow
only their designated mutable columns (`status`; `remaining_quantity`/`closed_at`) to change,
enforced by a conditional trigger that aborts on any other column changing.

## 6. Cash ledger

Append-only. Event types: `INITIAL_CAPITAL`, `BUY_RESERVATION`, `BUY_SETTLEMENT`,
`SELL_SETTLEMENT`, `FEE`, `SLIPPAGE`, `DIVIDEND`, `CASH_ADJUSTMENT`, `ORDER_RELEASE`.
`available_cash = settled_cash - reserved_cash`, both always derived from ledger entries —
never a stored, overwritable balance. `reserve_for_order` raises `InsufficientCashError`
before ever writing a reservation that would drive available cash negative.
`cash_adjustment` requires a non-empty `operator` + `reason` and an explicit idempotency key.

## 7. Position and lot accounting

FIFO, long-only (BUY/SELL only — no SHORT/COVER/OPTION/MARGIN). `apply_sell_fill` raises
`InsufficientPositionError` rather than ever allowing an oversell, and this is enforced
per-book — a book that never bought a symbol cannot sell it even if a *different* book holds
it. Realized P&L is fully recomputable from the fill history (verified independently in both
`metrics.py` and `reconciliation.py`, using two separately-written recomputation functions
that agree).

## 8. Mark-to-market / point-in-time valuation

`valuation.py::select_valuation_price` — fixed 3-tier priority: (1) this cycle's own
`EvidenceSnapshot` market evidence item; (2) `evaluation/price_provider.py::PriceProvider
.get_close(symbol, as_of.date())` (never a live quote); (3) explicit `SOURCE_UNAVAILABLE`.
Missing price never becomes zero; stale price is explicit
(`PARTIAL_STALE_PRICE`); an unsafe source (`available_at > as_of`) makes the whole snapshot
`POINT_IN_TIME_UNSAFE`, and `net_liquidation_value_usd`/`gross_market_value_usd`/
`unrealized_pnl_usd` are all `None` together whenever any position can't be safely valued.
`compute_snapshot_id` is a content hash over `book_id + as_of + {symbol: (qty, price,
status, price_timestamp)}` — identical inputs always reproduce the identical ID.

## 9. Deterministic risk policy

`risk.py::evaluate_paper_risk` — pure function, Decimal arithmetic, fixed check order (arm
mismatch → book paused → missing/stale/unsafe price → invalid recommendation → unsafe
portfolio valuation → max open positions → four capacity caps combined via `min()`, floored
to whole shares). Full decision vocabulary: `APPROVED`, `APPROVED_REDUCED`,
`REJECTED_INSUFFICIENT_CASH`, `REJECTED_MAX_POSITION_WEIGHT`,
`REJECTED_MAX_SYMBOL_CONCENTRATION`, `REJECTED_MAX_OPEN_POSITIONS`,
`REJECTED_DAILY_NOTIONAL_LIMIT`, `REJECTED_STALE_PRICE`, `REJECTED_MISSING_PRICE`,
`REJECTED_INVALID_RECOMMENDATION`, `REJECTED_BOOK_PAUSED`, `REJECTED_ARM_MISMATCH`. The same
recommendation can produce different approved sizes across books only for a deterministic,
persisted portfolio reason (`PaperRiskDecision.reasons`) — proven directly in the offline
end-to-end test using a book with a large pre-existing position in a *different* symbol.

## 10. Experiment policy and enhanced-only isolation

`research/experiment_policy.py` gained purely additive functions:
`validate_paper_book_experiment_policy`, `may_submit_baseline_to_paper_book`,
`may_submit_enhanced_to_paper_book`. Every pre-existing function/test in that module is
unchanged. `ENHANCED_ONLY`/`BOTH_SEPARATE_PAPER_BOOKS` become selectable only when the
matching isolated book(s) are explicitly enabled in `config/paper_books.yaml`; unsupported
combinations raise `UnsupportedExperimentPolicyError`. The legacy `may_submit_enhanced()`
(gating the **global** ledger) remains unconditionally `False` and is provably unaffected by
any paper-book state.

## 11. Local-simulated execution

`execution.py` implements its own bounded, deterministic fill simulator rather than
round-tripping through the Milestone 3/4 isolated `paper_runtime` subprocess for every fill
(see "Known limitations" below for the full rationale). It receives only book_id,
paper_order_intent_id, symbol, side, quantity, limit_price, time_in_force, and explicit
bid/ask market-simulation inputs — never Claude prompts, responses, API keys, or
chain-of-thought. A BUY/SELL only fills at a price at-or-within its own limit; no partial
fills (all-or-nothing, matching the legacy ledger's own convention). Idempotent: a duplicate
submission or a duplicate fill can never double-apply.

`paper_runtime/src/trading_paper_runtime/models.py::OrderIntentPayload` gained an additive,
optional `book_id` field so a future real subprocess-per-book deployment remains possible
without further schema change — all 33 pre-existing paper_runtime tests pass unmodified.

## 12. Reconciliation

`reconciliation.py::reconcile_book` — book-scoped, deterministic. Status vocabulary:
`MATCHED`, `MISSING_ORDER`, `MISSING_FILL`, `DUPLICATE_FILL`, `CASH_MISMATCH`,
`POSITION_MISMATCH`, `LOT_MISMATCH`, `BOOK_MISMATCH`, `ARM_MISMATCH`,
`PENDING_NOT_APPLICABLE`. Cash and position totals are independently recomputed from the raw
fill history and cross-checked against the ledger/position-aggregate — a direct SQL tamper
of a stored position is caught as `POSITION_MISMATCH` with the exact stored-vs-recomputed
values recorded.

## 13. Corporate actions

`forward_split`, `reverse_split`, `cash_dividend` only (matching Milestone 7's already-shipped
`evidence_providers/corporate_actions.py` Alpaca adapter). Applied at most once per
`(book_id, action_id)`. Splits preserve each lot's total dollar cost basis exactly. Dividends
compute `quantity_held * dividend_per_share_usd` from the book's own position at application
time. Every other action type raises `UnsupportedCorporateActionError` before touching any
table.

## 14. Performance metrics

`metrics.py::compute_book_metrics` — every metric (starting/ending cash, net liquidation
value, realized/unrealized P&L, total/daily/cumulative return, maximum drawdown, volatility,
turnover, trade count, win rate, average win/loss, profit factor, fees, slippage, cash
utilization, average gross exposure, maximum position concentration, unvalued-position rate,
stale-valuation rate) is computed only from data actually available in the explicit sample
window — missing data stays `None`, never a fabricated zero. No Sharpe or other annualized
ratio was added.

## 15. Comparison and promotion evidence

`comparison.py::build_comparison` fails closed (`comparable=False`) on: differing starting
cash, unsafe valuation in either book, a missing per-arm recommendation for an assigned
cycle, or insufficient comparable cycles. `promotion_evidence.py::evaluate_promotion_evidence`
implements the full 6-value result vocabulary (`INSUFFICIENT_DATA`, `NOT_COMPARABLE`,
`BASELINE_OUTPERFORMS`, `ENHANCED_OUTPERFORMS_OBSERVED`,
`ENHANCED_OUTPERFORMS_NOT_PROMOTABLE`, `PROMOTION_REVIEW_ELIGIBLE`) — no value in this
vocabulary ever means "promoted," only "eligible for human review." `research/promotion.py`
is not modified.

## 16. CLI commands

`paper-book-list`, `paper-book-show --book-id`, `paper-book-snapshot --book-id --as-of`,
`paper-book-run-cycle --cycle-id --experiment-policy --provider-mode fixture --symbol
--quantity-hint --reference-price --bid --ask [--recommendation-id-baseline/-enhanced]`,
`paper-book-reconcile --book-id [--as-of]`, `paper-experiment-compare --experiment-id
--window-start --window-end`, `paper-promotion-status --experiment-id`. Every mutating
command fails closed with `{"error": ...}` + exit code 2 when `paper_books.enabled` is
`false`.

## 17. Tests

89 new tests (7 unit files + 1 integration file), covering config fail-closed behavior, cash
ledger/FIFO lots/corporate actions, point-in-time valuation, risk policy/order intent,
execution/reconciliation, comparison/promotion evidence, additive experiment policy, and a
full offline end-to-end dual-book pipeline with explicit no-cross-contamination and
no-live-execution proofs.

```
Main suite:        1355 passed, 14 skipped   (1266 baseline + 89 new — zero regressions)
Paper runtime:      33 passed                (unchanged)
```

## 18. Requirement → implementation → verifying test

| Requirement | Implementation | Verifying test |
|---|---|---|
| Separate cash per book | `paper_books/cash_ledger.py` | `test_paper_books_ledger_and_positions.py` |
| Separate positions/lots | `paper_books/positions.py` | `test_paper_books_ledger_and_positions.py` |
| Book-aware intent IDs | `paper_books/models.py::derive_paper_order_intent_id` | `test_paper_books_risk_and_intent.py::test_same_recommendation_creates_different_book_aware_intent_ids` |
| No enhanced→baseline fallback | `research/experiment_policy.py` additive functions | `test_paper_books_experiment_policy.py` |
| Point-in-time valuation | `paper_books/valuation.py` | `test_paper_books_valuation.py` |
| Deterministic risk sizing | `paper_books/risk.py` | `test_paper_books_risk_and_intent.py` |
| Local-simulated fills, idempotent | `paper_books/execution.py` | `test_paper_books_execution_and_reconciliation.py` |
| Book-scoped reconciliation | `paper_books/reconciliation.py` | `test_paper_books_execution_and_reconciliation.py` |
| Corporate actions, book-specific | `paper_books/corporate_actions.py` | `test_paper_books_ledger_and_positions.py` |
| Fail-closed comparison | `paper_books/comparison.py` | `test_paper_books_comparison_and_promotion.py` |
| Evidence-only promotion | `paper_books/promotion_evidence.py` | `test_paper_books_comparison_and_promotion.py` |
| Full dual-book pipeline, no contamination | all of the above | `test_milestone_8_offline_end_to_end.py` |

## 19. Known limitations

* **Local-simulated fill engine is self-contained, not routed through the isolated
  `paper_runtime` subprocess.** `trading_paper_runtime` is confirmed genuinely unimportable
  from the main venv (subprocess-only boundary, ADR 0001/0002). Its three `BrokerGateway`
  implementations key state per *process instance*, not per `book_id` internally — threading
  true per-book isolation into that shared keying scheme is materially larger than this
  milestone's LOCAL-SIMULATED-PAPER scope requires. A real per-book subprocess pool (one
  `RuntimeClient` per `book_id`, using the now-additive `book_id` field) is a documented,
  low-risk Milestone 9 candidate.
* No `PARTIALLY_FILLED` support — all-or-nothing fills only, matching the legacy ledger's own
  convention and Step 16's explicit allowance to defer this.
* SELL-side order intents are supported at the position/execution layer but no automated
  recommendation-driven SELL path is wired (matches the existing repo-wide convention that a
  frozen recommendation is always a BUY-side entry signal).
* `max_order_notional_usd` has no dedicated rejection code in the milestone's own suggested
  vocabulary; when it alone is the binding zero-cause, the rejection is reported as
  `REJECTED_INSUFFICIENT_CASH` (a documented approximation, not a correctness defect — the
  order is still correctly rejected).
* Corporate actions limited to `forward_split`/`reverse_split`/`cash_dividend`, matching
  Milestone 7's already-implemented provider scope; all other Alpaca-documented action types
  remain explicitly unsupported.

## 20. Deferred / explicitly out of scope

Matches `docs/milestone-8.md`'s own non-goals: `unsupported_claim_rate` fix, real
paper-reconciliation health wiring into shadow, duplicate-prevention health wiring, Reddit
registration, new news providers, destructive retention, real recurring shadow activation,
remaining corporate-action types, MFE/MAE, live promotion, Robinhood/live Alpaca trading,
LLM-based portfolio optimization, reinforcement learning, strategy generation, unlimited
backtesting universe. None of these were attempted.

## 21. Recommended Milestone 9 scope

1. Real per-book subprocess pool through the isolated `paper_runtime` boundary (using the
   additive `book_id` field already shipped this milestone).
2. Wire `paper_books/` into `shadow/scheduler.py::run_due_shadow_cycle` for an actual
   scheduled dual-book cycle (this milestone's CLI `paper-book-run-cycle` is fixture-mode
   only, single-symbol).
3. Real evidence-driven (non-fixture) `paper-book-run-cycle`, sourcing recommendations from
   `research/scheduled_cycle.py`'s existing baseline/enhanced pipeline instead of CLI flags.
4. Persistent multi-day soak of both books to accumulate genuine comparison/promotion-evidence
   history (mirrors the Milestone 7 backlog's own "accumulate persistent shadow history" item).
