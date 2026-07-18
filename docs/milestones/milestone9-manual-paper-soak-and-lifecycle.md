# Milestone 9 — Manual paper-trading soak and position lifecycle

**Milestone 9.1 pointer:** combined paper-soak + shadow-operational activation readiness, the
single manual `paper-soak-run` operator command, and a lifecycle-CLI clock-anchoring fix are
now implemented — see `docs/milestone9-1-controlled-soak-readiness.md`. This document's own
`paper-book-lifecycle-run`/`paper-book-soak-report`/`paper-book-soak-readiness` commands
described below are unchanged (the lifecycle service's own `as_of`-anchored clock default,
Section 2 below, was always correct — only the CLI's separate wall-clock override was fixed).

**Status:** Complete for this session's scope.
**Date:** 2026-07-14
**Applies to:** `src/trading_research/paper_books/exit_policy.py`,
`src/trading_research/paper_books/lifecycle.py`, `src/trading_research/paper_books/config.py`,
`src/trading_research/paper_books/cli_support.py`, `src/trading_research/storage/
paper_books_schema.py`, `src/trading_research/storage/paper_books_repositories.py`,
`src/trading_research/storage/trading_repositories.py`, `src/trading_research/cli.py`,
`src/trading_research/shadow/scheduler.py`, `config/paper_books.yaml`.

This document is the durable architecture record for the operational lifecycle Milestone 8/8.1
left deferred:

```text
manual scheduled research run
-> optional paper-book integration (Milestone 8.1, unchanged)
-> process pending paper orders
-> evaluate existing positions
-> create deterministic exit intents
-> simulate eligible fills
-> update cash and positions
-> create portfolio snapshots
-> reconcile each book
-> calculate metrics
-> produce a daily soak report
```

## 1. What this milestone built

A single new module, `paper_books/lifecycle.py`, plus a deterministic exit-decision module,
`paper_books/exit_policy.py`, that let an operator run controlled, persistent, multi-day paper
soak sessions against the isolated Milestone 8 books. No Milestone 8/8.1 module was rewritten —
lifecycle processing reuses `valuation`, `execution`, `positions`, `cash_ledger`,
`reconciliation`, and `metrics` exactly as they already exist, and reuses
`scheduled_integration.integrate_scheduled_cycle_into_paper_books` unmodified for entry.

## 2. Entry point

```python
run_paper_book_lifecycle(
    conn, *, as_of, paper_books_config, price_provider=None,
    integrate_cycle_ids=(), experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", clock=None,
) -> PaperBookLifecycleResult
```

Fails closed with `LifecycleError` when `paper_books.enabled` or `paper_books.lifecycle.enabled`
is false, or `as_of` is not timezone-aware. Manually invoked only — never called by launchd or a
recurring scheduler.

Processing order (fixed):

1. Validate lifecycle configuration.
2. Optionally integrate explicitly supplied `integrate_cycle_ids` via the unmodified Milestone
   8.1 entry point.
3. Process existing pending orders (fill / expire / remain pending) per enabled book.
4. Evaluate exits for every open long position (skipped entirely when
   `lifecycle.exits.enabled` is false).
5. Persist exit decisions.
6. Create eligible SELL intents.
7. Simulate eligible fills (reuses `execution.py::submit_and_simulate` — no second fill
   simulator).
8. Create one portfolio snapshot per enabled book.
9. Reconcile each enabled book.
10. Compute and persist metrics.
11. Persist a `paper_book_lifecycle_runs` summary row.

One book's failure is caught and recorded in `failure_reasons`; it never prevents the other
book's own processing, and every already-idempotent sub-operation (order submission, fill
application, cash reservation/release, exit-decision insert) remains safe to retry.

**Default clock is anchored to `as_of`, never wall-clock `now()`.** This module's own
"explicit `as_of`, no implicit current-time lookup" contract applies to every timestamp it
stamps by default (including order/decision `created_at`), not only price selection — a caller
that wants a genuine "actually executed at" audit timestamp may still inject an explicit
`clock`. The CLI (`paper-book-lifecycle-run`) does inject real wall-clock time for `created_at`
audit metadata, since a human operator's `--as-of` is normally close to "today" anyway.

## 3. Deterministic exit policy

`exit_policy.py::evaluate_exit_decision` — pure function, long-only, full-position exits only.
Fixed, documented check order:

1. no open long position -> `SKIPPED_NO_POSITION`
2. missing price -> `SKIPPED_MISSING_PRICE`
3. price not point-in-time safe -> `SKIPPED_POINT_IN_TIME_UNSAFE`
4. stale price -> `SKIPPED_STALE_PRICE`
5. an unconsumed manual exit request exists -> `EXIT_MANUAL_REQUEST` (an explicit, audited
   human instruction outranks every automatic rule)
6. stop loss: `reference_price <= cost_basis_per_share * (1 - stop_loss_percent)`
7. profit target: `reference_price >= cost_basis_per_share * (1 + profit_target_percent)`
8. maximum holding period: market days held (via `evaluation/market_calendar.py`, reused —
   never raw calendar days) `>= maximum_holding_market_days`
9. recommendation reversal (only when `exit_on_recommendation_reversal` is true and a
   qualifying newer recommendation was supplied)
10. otherwise `HOLD`

Missing/stale/unsafe prices are checked *before* any trigger rule, so they can never fabricate
an exit. Every decision (including `HOLD`/`SKIPPED_*`) is persisted immutably in
`paper_book_exit_decisions`, keyed by a deterministic `exit_decision_id` hash of
`(book_id, symbol, as_of, policy_version)` — a retried lifecycle run for the same date never
creates a duplicate decision row.

### Recommendation-reversal definition

A reversal fires only from a newer, frozen, in-window recommendation for the exact same symbol
(`position_opened_at < recommendation.ts <= lifecycle_as_of`) whose `side` is `screened_out` or
`no_action` **and** whose `status` is `active` — i.e. the system's newest view for this symbol
is no longer an actionable `buy_candidate`. A recommendation that is still `watch` or
`analysis_incomplete`, or a symbol with no newer recommendation at all, is never treated as a
sell signal (`is_reversal_recommendation` in `exit_policy.py` documents and implements this
classification).

### Manual exit

`paper_book_manual_exit_requests` (additive, immutable, `UNIQUE(book_id, idempotency_key)`)
carries `book_id`/`symbol`/`operator`/`reason`/`requested_at`/`idempotency_key` — created only
via `paper-book-exit-request`, never through an arbitrary SQL/mutation interface. A request is
"consumed" the moment an exit decision references its `manual_exit_request_id`
(`list_unconsumed_manual_exit_requests` excludes it from then on) — a lifecycle rerun never
re-triggers an already-acted-on manual request.

## 4. SELL intents

Approved exit decisions become full-position `SELL` `PaperBookOrderIntent`s, built directly
(not through `risk.py`/`order_intent.build_order_intent`, which are BUY-sizing only) and
submitted through the unmodified `execution.py::submit_and_simulate` — the same engine
Milestone 8 already uses, so oversell protection, idempotent fill application, and cash
settlement are inherited, not reimplemented.

* Side `SELL`, `order_type=LIMIT` only.
* Quantity = `min(exit_decision.quantity, position.available_quantity)` — never exceeds the
  book's own available long position.
* Stable ID: `derive_paper_order_intent_id(exit_decision_id, book_id, "paper-books-lifecycle-
  execution-v1")` — the existing Milestone 8 hash function, reused with the exit_decision_id in
  the "recommendation" slot (a documented, deliberate field reuse, matching the precedent
  `OrderIntentPayload.book_id` set in Milestone 8.1).
* `limit_price` = the exit decision's own point-in-time-safe `reference_price` — exactly the
  price that triggered the decision, mirroring Milestone 8's BUY-side convention
  (`limit_price = approved_notional / approved_quantity`, also exactly the reference price).
* No recommendation mutation, no cross-book/cross-arm submission (`book.experiment_arm` is
  always the fixed value for that book_id), no live destination.
* An exit whose market-simulation input is unavailable is still created and persisted, staying
  `PENDING_SUBMISSION` — never a fabricated fill (Section 5's "Pending-order lifecycle" then
  re-evaluates it on the next lifecycle date).

Before evaluating a symbol's exit, `lifecycle.py` checks for an already-outstanding
`PENDING_SUBMISSION` SELL for that book/symbol and skips re-evaluation if one exists — this is
what prevents a second exit decision (and a second SELL order for shares the first order hasn't
released yet) while the first is still resolving.

## 5. Pending-order lifecycle

`_process_pending_orders` (any side — BUY entries from Milestone 8.1, or SELL exits from this
milestone) reloads every `PENDING_SUBMISSION` order for an enabled book and, per order:

1. Computes its market-day age from `created_at` to `as_of` (via `exit_policy.market_days_held`
   — market days, not calendar days).
2. Expires it (`execution.expire_pending_intent`, releasing any BUY-side cash reservation
   exactly once) once `lifecycle.pending_orders.expire_after_market_days` is exceeded.
3. Otherwise builds a fresh, point-in-time-safe market-simulation input for the *current*
   `as_of` (same tier-2 synthetic-bid/ask construction `scheduled_integration.py` already uses:
   the selected reference price, converted to a symmetric bid/ask via
   `execution.DEFAULT_SLIPPAGE_BPS`) and re-attempts the fill via the unmodified
   `execution.submit_and_simulate` — idempotent by construction (`save_order_intent`'s
   `inserted` flag prevents a double cash reservation; `fill_exists` prevents a double fill
   application).

An order created with a limit exactly at its own trigger-day reference price will typically
*not* fill immediately (the same, already-documented Milestone 8/8.1 tier-2 known limitation —
see that milestone's docs) and stays pending until a later day's price genuinely crosses the
limit. This is not a defect: it is exactly what "process pending paper orders" on a *later*
lifecycle date is for, and the offline end-to-end test (Section 8 below) demonstrates a real
stop-loss and a real profit-target SELL each resolving one lifecycle day after creation.

## 6. Persistent soak database

No new CLI database flag was added: `cli.py`'s existing `cfg.research_database_path`
(`RESEARCH_DATABASE_PATH` env var, default `data/research.sqlite3`) is already a persistent,
non-temporary, non-hardcoded path shared by every other `paper-book-*` command — the new
Milestone 9 commands reuse it unchanged, satisfying "use a persistent evaluation database"
without a redundant flag.

`paper_book_lifecycle_runs` persists exactly the fields Section 8 of the milestone spec lists
(`lifecycle_run_id`, `as_of`, `processed_cycle_ids`, `books_processed`, `pending_orders_filled`,
`pending_orders_expired`, `exit_decisions`, `exit_orders_created`, `exit_orders_filled`,
`snapshot_ids`, `reconciliation_statuses`, `metrics_ids`, `failure_reasons`), keyed by a
deterministic `lifecycle_run_id` hash of `(as_of, config_hash)` — retrying the same lifecycle
date resolves to the same row (insert-or-ignore); the function's *return value* always reflects
a fresh recompute of current state, since every sub-operation is independently idempotent.

## 7. CLI commands

```bash
python -m trading_research.cli paper-book-lifecycle-run \
  --as-of <ISO-8601> [--integrate-cycle-id <id>]...

python -m trading_research.cli paper-book-exit-request \
  --book-id <BASELINE|ENHANCED> --symbol <symbol> --operator <name> --reason "<reason>"

python -m trading_research.cli paper-book-soak-report --as-of <ISO-8601>

python -m trading_research.cli paper-book-soak-readiness --as-of <ISO-8601>
```

`--operator` is a required flag on `paper-book-exit-request` (the milestone's own Section 3
"Manual exit" requires an operator on every request; the example command block omitted it, so
this is a deliberate, documented addition, not a deviation from the requirement). All four
commands fail closed with an `{"error": ...}` + non-zero exit whenever `paper_books.enabled`
(and, for the lifecycle run, `paper_books.lifecycle.enabled`) is false, or an unknown
book/symbol/date is supplied. Every response is sanitized, deterministic JSON — no raw Claude
prompt/response content anywhere in this path.

## 8. Daily soak report and readiness

`paper-book-soak-report` is read-only: per enabled book it reports cash available/reserved, net
liquidation value, realized/unrealized P&L, open position count, pending order count, orders
filled today, exits triggered today, reconciliation status, valuation status, unvalued position
count, maximum position concentration, and completed experiment cycles (distinct real
`cycle_id`s that produced an order for that book — synthetic `lifecycle:<date>` cycle IDs used
by exit orders are excluded). When both books are enabled it also reports `comparable_cycles`
and metric deltas — but **never** a winner: `promotion_evidence_status` is a pointer to the
existing, authoritative `paper-promotion-status` command (Milestone 8), never recomputed or
duplicated here. Status is one of `NOT_ENOUGH_HISTORY` / `RUNNING` (reserved, currently folded
into the other three) / `ATTENTION_REQUIRED` / `READY_FOR_ACTIVATION_REVIEW` — the latter
**never** activates anything by itself.

`paper-book-soak-readiness` is a deterministic, advisory-only check over the full
`paper_book_lifecycle_runs` history up to `as_of`: both books enabled, minimum completed
cycles, minimum market days, zero unresolved lifecycle-run failures, `MATCHED` reconciliation,
and `COMPLETE` valuation — in that check order, first failing check wins
(`NOT_READY_INSUFFICIENT_CYCLES` / `NOT_READY_INSUFFICIENT_MARKET_DAYS` /
`NOT_READY_LIFECYCLE_FAILURES` / `NOT_READY_RECONCILIATION` / `NOT_READY_VALUATION`), otherwise
`READY_FOR_MORE_MANUAL_SOAK` or (once market days covered is at least double the configured
minimum) `READY_FOR_RECURRING_ACTIVATION_REVIEW`. No result value ever enables recurring
processing — it is a human-readable recommendation only.

## 9. Optional scheduler hook

`shadow/scheduler.py::run_due_shadow_cycle` gained a second optional keyword,
`paper_book_lifecycle_hook: Callable[[datetime], Any] | None = None` (default `None` = zero
behavior change for every existing caller), invoked once after the existing
`paper_book_integrator` hook (if any), in its own try/except. A raised exception is recorded on
two new `ShadowCycleRunResult` fields (`paper_book_lifecycle_status`/`_reason`), never raised,
never folded into `failure_reason` or `paper_book_integration_status`. `shadow/scheduler.py`
still does not import `paper_books` at all — no real `run_paper_book_lifecycle` is wired into
any caller in this session; the hook exists and is tested, matching this milestone's own
"optional hook only, no automatic invocation" requirement.

## 10. Configuration

`config/paper_books.yaml` gained an OPTIONAL `paper_books.lifecycle` section (default
`enabled: false`, shipped disabled at every nesting level — `exits.enabled: false` too), with
`pending_orders.expire_after_market_days`, `exits.{stop_loss_percent, profit_target_percent,
maximum_holding_market_days, exit_on_recommendation_reversal}`, and
`soak.{minimum_completed_cycles, minimum_market_days}`. Every percentage is a Decimal-safe
quoted string validated to `(0, 1)`; every market-day count must be a positive integer; unknown
keys at any nesting level fail closed (`PaperBooksConfigError`). No environment variable can
enable any part of this section — only this file can, matching the existing
`scheduled_integration` gate's own convention.

## 11. Tests

* `tests/unit/test_paper_books_exit_policy.py` — 25 tests: every trigger rule, every skip
  reason, manual-request priority, reversal classification, determinism, market-day counting.
* `tests/unit/test_paper_books_lifecycle.py` — 17 tests: pending-order fill/expire/reservation-
  release, every exit-decision type, duplicate-decision/duplicate-order prevention, missing-
  price safety, book-failure isolation, cross-book isolation, snapshot/reconciliation/metrics
  persistence.
* `tests/unit/test_paper_books_lifecycle_cli.py` — 12 tests: fail-closed (disabled lifecycle,
  disabled paper_books), valid lifecycle run, manual exit request validation/creation, soak
  report/readiness sanitized output and never-a-winner assertion.
* `tests/unit/test_shadow_scheduler.py` — 4 new tests appended: hook not supplied = zero
  behavior change, hook invoked and recorded, hook exception recorded/never raised/never
  conflated with the entry-integration hook, both hooks run independently.
* `tests/integration/test_milestone_9_offline_end_to_end.py` — 2 tests: the full soak pipeline
  (pre-existing positions -> two real fixture scheduled cycles across three market days ->
  explicit cycle integration -> a real stop-loss exit -> a real profit-target exit -> pending-
  order resolution one day later for each -> reconciliation/snapshots/metrics -> soak report ->
  readiness -> idempotent reruns -> no cross-book contamination), plus a structural
  no-live-execution-path proof (AST import scan of `lifecycle.py`/`exit_policy.py` + `--live`
  flag absence).

## 12. Known limitations

* A SELL exit intent's limit price equals its own trigger-day reference price exactly (mirrors
  Milestone 8's BUY convention), so it will typically not fill on the same lifecycle day it is
  created — it resolves on a later day's pending-order reprocessing once the price genuinely
  crosses. This is the same, already-documented tier-2 known limitation from Milestone 8/8.1,
  not a new defect, and is directly exercised (not merely asserted) by the offline e2e test.
* `paper-book-soak-report`'s `promotion_evidence_status` is a pointer, not a recomputation — an
  operator must still run `paper-promotion-status` for the authoritative, evidence-only result.
* Full-position exits only — no partial exits, no trailing stops, no tax-lot selection beyond
  the existing FIFO convention.
* Manual exit requests are picked FIFO (oldest unconsumed first) when more than one exists for
  the same book/symbol; only the milestone's core single-request flow was exercised.

## 13. Deferred / explicitly out of scope

Matches `docs/milestone-9.md`'s own non-goals: unattended recurring activation, launchd
installation, an external Alpaca paper broker, a per-book `paper_runtime` subprocess pool,
partial fills, trailing stops, non-FIFO tax-lot selection, live trading, automated promotion,
remaining corporate-action types, dividend record-date entitlement correction, and the
Milestone 7 health backlog. None of these were attempted.
