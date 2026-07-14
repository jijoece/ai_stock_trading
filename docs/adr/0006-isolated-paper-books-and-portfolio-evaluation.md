# ADR 0006: Isolated paper books are a new, additive subsystem beside the existing global paper ledger — not a retrofit of it

**Status:** Accepted (2026-07-14 — `pytest tests/ -q` shows 1355 passed/14 skipped, zero
regressions against the 1266/14 baseline; the isolation and offline end-to-end tests
described in "Acceptance" below all pass)
**Date:** 2026-07-13 (Milestone 8)

## Context

Milestones 1-7.2 built a deterministic screening/scoring/risk pipeline, a Claude-informed
research committee, and a single global paper-trading ledger (`paper/ledger.py::PaperLedger`)
that both the baseline and (structurally blocked) enhanced arms would have shared had the
enhanced arm ever been allowed to submit. `research/experiment_policy.py` recognizes
`ENHANCED_ONLY` and `BOTH_SEPARATE_PAPER_BOOKS` as policy names but raises
`UnsupportedExperimentPolicyError` for both, "because they require separate paper-portfolio
namespaces this milestone does not implement" (ADR 0004 Decision 6). `docs/milestone-7 pending.md`
Section 7.2 records this explicitly as deferred future work with its own required-work list
(isolated balances, isolated positions, independent order IDs, experiment attribution,
reconciliation, fair comparison, corporate-action handling, portfolio constraints).

Repository inspection before writing any code confirmed exactly where the "one global paper
account" assumption lives (see `.claude/scratchpads/milestone8-progress.md` "Existing paper
architecture" for full file:line detail):

* `paper_cash_state` is a singleton row (`CHECK (id = 1)`).
* `simulated_positions` PRIMARY KEY is `symbol` alone — no book dimension.
* `simulated_orders.idempotency_key` is one global UNIQUE namespace.
* `execution/models.py::PaperOrderIntent` and `derive_intent_id` have no book/account field.
* `paper_runtime`'s process-boundary payloads (`OrderIntentPayload`, `AccountSnapshotPayload`,
  `PositionSnapshotPayload`) carry no book_id anywhere.

`docs/milestone-8.md` requires two fully isolated paper books (their own cash, positions,
orders, fills, realized/unrealized P&L, fees/slippage, risk limits) while explicitly
forbidding a redesign of Milestones 1-7.2 and forbidding any path from the enhanced arm to
live execution, the baseline book, or the legacy global ledger.

## Decision 1: A paper book is a logical namespace enforced by schema-level book_id keys, not a second ledger account or broker-paper account

A `PaperBook` (`paper_books/models.py`) is a typed identity (`book_id`, `experiment_arm`,
`currency`, `starting_cash_usd`, `status`, `created_at`, `config_hash`) plus a family of
`paper_book_*` tables, every one of which carries `book_id` as part of its primary key or as a
`NOT NULL` column inside a `book_id`-scoped uniqueness constraint. It is not a second
`PaperLedger` instance pointed at a different SQLite file (that would fragment the single
source of truth / reconciliation story), and it is not a broker-paper account (no credential,
no network call, no broker order ID — `paper_books/execution.py` only ever talks to the
existing local-simulated `paper_runtime` process boundary, extended additively). This mirrors
ADR 0004 Decision 1's "smallest safe extension point" precedent: the existing
`simulated_*`/`paper_cash_state`/`execution/models.py` tables and types are **not modified at
all** — Milestone 8 adds a wholly new, parallel set of tables and modules. The legacy global
ledger keeps working exactly as it does today for `OBSERVE_ONLY`/`BASELINE_ONLY`/
`SHADOW_ENHANCED`; the new isolated-book subsystem is the only way `BOTH_SEPARATE_PAPER_BOOKS`/
`ENHANCED_ONLY` ever become selectable, and only when `config/paper_books.yaml` explicitly
enables the matching book(s).

## Decision 2: Isolation is structural (separate primary-key space), not policy-enforced at read time

Every paper-book table's primary key or unique constraint includes `book_id` (e.g.
`paper_book_positions` PK `(book_id, symbol)`, `paper_book_cash_ledger` PK
`(book_id, ledger_entry_id)`, `paper_book_orders` PK `(book_id, paper_order_intent_id)`).
There is no code path — not a shared table, not a shared cache, not a shared in-process
object — through which a query for one `book_id` can observe or mutate another book's rows.
`paper_books/positions.py`'s FIFO lot consumption, `paper_books/cash_ledger.py`'s available-cash
derivation, and `paper_books/reconciliation.py`'s reconciliation all take a mandatory `book_id`
parameter and every SQL statement they issue filters on it. This is testable and tested
directly (Step 24 isolation tests) rather than merely documented.

## Decision 3: Starting cash is configured per book in `config/paper_books.yaml`, applied exactly once via an `INITIAL_CAPITAL` ledger entry

`config/paper_books.yaml` (disabled by default, `paper_books.enabled: false`) names each
book's `starting_cash_usd` as a `Decimal`-safe string. `paper_books/cash_ledger.py::open_book`
inserts exactly one `INITIAL_CAPITAL` row per `book_id` (idempotent — a second call is a
no-op, never a second deposit). There is no other way to add cash to a book except through
this one initialization path or an explicitly-audited `CASH_ADJUSTMENT` (operator + reason +
timestamp, per Step 6). `.env` cannot enable a book or set its cash — only the YAML file can,
mirroring `evidence_providers.yaml`/`shadow_operations.yaml`'s existing "credentials never
decide a capability" convention.

## Decision 4: Book identity is a first-class, non-optional field on every downstream identifier

* **Order IDs:** `paper_books/models.py::derive_paper_order_intent_id(recommendation_id,
  book_id, execution_version)` hashes all three — the same recommendation submitted to
  BASELINE and ENHANCED books deterministically produces two different intent IDs (Step 12's
  explicit requirement). This mirrors the legacy `execution/models.py::derive_intent_id`
  pattern but adds `book_id` into the hash input rather than reusing the legacy function.
* **Fill IDs:** `paper_book_fills` PK is `(book_id, fill_id)`; a fill_id collision across two
  books is structurally a different row, never a silent overwrite.
* **Positions:** `paper_book_positions` PK `(book_id, symbol)`; `paper_book_position_lots` PK
  `(book_id, lot_id)`, FIFO consumption is scoped to `WHERE book_id = ?`.
* **Cash entries:** `paper_book_cash_ledger` PK `(book_id, ledger_entry_id)`, append-only.
* **Reconciliation:** `paper_book_reconciliations` PK `(book_id, reconciliation_id)`; a
  cross-book reconciliation attempt (Step 24) is rejected with a distinct `ARM_MISMATCH`/
  `BOOK_MISMATCH` status rather than silently reconciling against the wrong book.
* **Evaluation:** performance metrics (`paper_book_daily_metrics`) and comparisons
  (`paper_book_experiment_comparisons`) are always computed from one book's own rows first,
  compared only at the read-only comparison layer.

## Decision 5: Mark-to-market prices are selected by a fixed, documented priority that never leaks a future or current price into a historical snapshot

`paper_books/valuation.py::select_valuation_price(symbol, as_of, ...)` tries, in order: (1) the
price already captured in that cycle's point-in-time `EvidenceSnapshot` (reused, not
re-fetched); (2) the most recent persisted market bar available strictly at-or-before `as_of`
(mirroring the existing `evaluation/price_provider.py::PriceProvider.get_close` point-in-time
contract, never a live quote call); (3) an explicit `SOURCE_UNAVAILABLE` result. A selected
price whose own `available_at` exceeds the configured staleness window is labeled stale
(`PARTIAL_STALE_PRICE`), never silently treated as current. Every snapshot's price selections
are queryable after the fact (provider, timestamp, price, available-at, point-in-time-safe
flag, source-record ID, staleness seconds) via `paper_book_snapshot_positions`.

## Decision 6: Missing or stale valuation never becomes a fabricated number

A missing price contributes `None` to `gross_market_value_usd`/`net_liquidation_value_usd`
(never `0`), and the snapshot's `valuation_status` becomes `PARTIAL_MISSING_PRICE` or worse
(`POINT_IN_TIME_UNSAFE`, `SOURCE_UNAVAILABLE`). `unrealized_pnl_usd` is likewise `None` when
it cannot be safely computed. This mirrors the repository-wide "never fabricate a zero for
missing data" convention already used by `evidence_providers/normalization.py` and
`shadow/health.py`.

## Decision 7: Deterministic per-book risk controls are pure functions over a typed portfolio context, never touching Claude output

`paper_books/risk.py::evaluate_paper_risk(candidate, context: PaperPortfolioContext, policy) ->
PaperRiskDecision` takes only deterministic inputs (recommendation's frozen quantity/price
proposal, the book's own current cash/positions/exposure, and versioned policy thresholds from
`config/paper_books.yaml`). The same recommendation fed to two different `PaperPortfolioContext`
values (one per book) can legitimately produce two different `approved_quantity` values — this
is expected and is exactly why the reason is always a persisted, deterministic
`PaperRiskDecision.reasons` tuple, never left to be inferred. Claude never sees, produces, or
influences a `PaperPortfolioContext` or `PaperRiskDecision` — those types are constructed only
by `paper_books/` and `services/`-layer deterministic code, matching the `research/`-package's
long-standing "Claude analyzes evidence, deterministic code decides everything else" boundary
(ADR 0003).

## Decision 8: The same recommendation timestamp and evidence snapshot feed both arms' book-aware sizing, for fair comparison

`paper_books/experiment_assignment.py` persists one row per `(cycle_id, symbol)` carrying the
shared `evidence_snapshot_id`, shared `as_of`, both recommendation IDs, and both resulting book
IDs/intent IDs. Nothing in the new subsystem re-fetches evidence per arm or per book — the
already-frozen baseline/enhanced recommendations (Milestone 5/6, unchanged) are the sole input
to book-aware sizing. This directly satisfies Step 14's "no future data, no selective
assignment after seeing results" requirements, because the assignment row is written once, at
cycle time, before any comparison or evaluation ever runs.

## Decision 9: Enhanced paper execution cannot create a path to live execution, structurally

`paper_books/execution.py` never imports `runtime/lumibot/`, never imports a broker credential,
and only ever calls the existing local-simulated `paper_runtime` boundary (fixture-mode or
deterministic-mode, exactly like the Milestone 3/4 legacy path already does) with an
additive, optional `book_id` field on `OrderIntentPayload`. `research/experiment_policy.py`'s
existing `may_submit_enhanced()` (hardcoded `False`, gating the **legacy** global-ledger path)
is left completely untouched; a new, separate function
`may_submit_enhanced_to_paper_book(policy, *, enhanced_book_enabled)` governs only the new
isolated-book path and is `True` only for `ENHANCED_ONLY`/`BOTH_SEPARATE_PAPER_BOOKS` **and**
only when `config/paper_books.yaml`'s enhanced book is explicitly enabled. There is no
`--live` flag, no broker adapter selection, and no config key anywhere in this milestone that
can make either function return a live-execution decision — `config/paper_books.yaml`'s
`execution.allow_live_broker` is hardcoded structurally unreadable-as-true, mirroring
`shadow/config.py::ShadowOperationsSection.__post_init__`'s existing
`allow_enhanced_submission` pattern.

## Decision 10: Promotion remains evidence-only; paper-book results extend, not replace, the existing promotion gate

`paper_books/promotion_evidence.py` adds paper-book-specific inputs (minimum comparable
cycles, minimum trading days, minimum closed trades, return/drawdown/cost deltas, evidence
completeness, reconciliation status) to a new possible-result vocabulary
(`INSUFFICIENT_DATA`/`NOT_COMPARABLE`/`BASELINE_OUTPERFORMS`/`ENHANCED_OUTPERFORMS_OBSERVED`/
`ENHANCED_OUTPERFORMS_NOT_PROMOTABLE`/`PROMOTION_REVIEW_ELIGIBLE`). `research/promotion.py`'s
existing `evaluate_promotion`/`PromotionGateConfig` (with `allow_live_promotion` structurally
`False`, unchanged since Milestone 6) is not modified — the new function is an independent,
additive report a human reviews, never an authorization to execute anything. No result value
in the new vocabulary means "promoted" or "approved" — only "eligible for human review."

## Decision 11: Historical book records are immutable; corrections are compensating events, never in-place mutation

`paper_book_cash_ledger` and `paper_book_fills` are append-only (no `UPDATE`/`DELETE` in any
repository function). A correction is a new row referencing the row it corrects
(`CASH_ADJUSTMENT` with operator+reason, or an explicit reversal entry) — never a mutation of
a previously-persisted amount. `paper_book_snapshots` are immutable once inserted (their
`snapshot_id` is a content hash of `book_id + as_of + position/price inputs`, so identical
inputs always reproduce the same ID and a changed input always produces a different one —
Step 8's explicit "snapshot ID changes when valuation inputs change; snapshot ID remains
stable for identical inputs" requirement).

## Consequences

* Zero existing table, model, or function from Milestones 1-7.2 is modified in a
  behavior-changing way. `research/experiment_policy.py`'s existing three functions keep their
  exact current signatures and behavior; new functions are added beside them.
* The known, accepted scope limitation this ADR records: only `forward_split`,
  `reverse_split`, and `cash_dividend` corporate actions are supported (matching the
  already-implemented `evidence_providers/corporate_actions.py` Alpaca adapter from Milestone
  7) — every other documented Alpaca action type remains explicitly unapplied and logged as
  such, never inferred.
* Live trading, margin, short selling, and options remain entirely unimplemented — no new
  code path in this milestone touches `runtime/lumibot/` or any broker credential.

## Acceptance

This ADR is marked Accepted only once `pytest tests/ -q` shows zero regressions against the
1266/14 baseline, the paper-book isolation tests (Step 24) pass, and the offline end-to-end
test (Step 23) demonstrates the full frozen-cycle → dual-book → reconciled → compared →
promotion-evidence flow without any cross-book contamination. See
`.claude/scratchpads/milestone8-progress.md` for the actual command output.
