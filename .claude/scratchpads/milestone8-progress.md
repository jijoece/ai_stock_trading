# Milestone 8 Progress

Started: 2026-07-13
Branch: main
Status: IN_PROGRESS

## Baseline
- `pytest tests/ -q` -> **1266 passed, 14 skipped** (matches expected baseline exactly).
- `cd paper_runtime && pytest tests/ -q` -> **33 passed** (matches expected baseline exactly).
- Git status at start: clean except untracked `docs/milestone-8.md` (this spec),
  `docs/milestone-7 pending.md` / `docs/milestone-7 pending copy.md` (pre-existing untracked
  backlog docs from a prior session, left untouched by this session), and this new scratchpad.
- `docs/milestone7-pending-work.md` (the exact path the milestone-8 doc's mandatory-review
  list names) does not exist under that filename — the closest match is the untracked
  `docs/milestone-7 pending.md`, read in full instead. Confirms Section 7.2 of that doc
  ("Separate baseline and enhanced paper books... A future design must address: isolated
  balances; isolated positions; independent order IDs; experiment attribution;
  reconciliation; fair comparison; corporate-action handling; portfolio constraints") is
  exactly Milestone 8's own scope — no conflicting in-flight work found there.
- Credentials, boolean presence only (unchanged from Milestone 7.2 session): ANTHROPIC_API_KEY
  present, ALPACA_API_KEY/SECRET present, ALPACA_MARKET_DATA_API_KEY/SECRET absent,
  REDDIT_CLIENT_ID/SECRET absent.

## Existing paper architecture
**One global paper account, confirmed at exactly these points (read directly, not just
grepped):**
- `paper/ledger.py::PaperLedger._ensure_cash_state` (line 67-79): `paper_cash_state` is a
  **singleton row** `CHECK (id = 1)`. `PaperLedger.__init__` always binds to this one row.
- `storage/trading_schema.py`: `simulated_positions` PRIMARY KEY is `symbol` alone (no book
  dimension at all); `simulated_orders.idempotency_key` is a single global UNIQUE namespace;
  `simulated_fills` FKs to `simulated_orders.order_id`; `simulated_portfolio_snapshots` PRIMARY
  KEY is `snap_date` alone (one snapshot per calendar day, globally).
- `execution/models.py::PaperOrderIntent` (Milestone 3) has no book/account field at all;
  `derive_intent_id(recommendation_id, execution_version)` is a pure hash of those two values
  — one recommendation always maps to the same one intent, globally.
- `services/execute_paper_recommendation.py` takes one `ledger: PaperLedger` instance per
  call — no book selection anywhere in the call chain.
- `paper_runtime/src/trading_paper_runtime/models.py::OrderIntentPayload`/
  `AccountSnapshotPayload`/`PositionSnapshotPayload` (the isolated-process boundary,
  ADR 0001/0002) carry no book_id field anywhere — a single global account/position
  namespace crosses the process boundary today.
- `research/experiment_policy.py`: `_SUPPORTED_POLICIES = (OBSERVE_ONLY, BASELINE_ONLY,
  SHADOW_ENHANCED)` (line 36); `ENHANCED_ONLY`/`BOTH_SEPARATE_PAPER_BOOKS` are recognized
  names but `validate_experiment_policy` (line 41-48) raises `UnsupportedExperimentPolicyError`
  for both, explicitly because they'd require "separate paper-portfolio namespaces, which
  this repository does not implement." `may_submit_enhanced()` (line 56-61) is unconditionally
  `False` for every currently supported policy.
- `research/models.py::ExperimentAssignment` (existing, `research_experiment_assignments`
  table, PK `(experiment_id, symbol, arm)`) carries `baseline_recommendation_id`/
  `enhanced_recommendation_id` but no book_id — it records which recommendation belongs to
  which arm, not which portfolio it was (or could be) executed against.
- `evaluation/research_comparison.py::compare_arms` assumes one shared portfolio's evaluation
  results, keyed only by recommendation_id/horizon — no book filtering exists because there
  was never more than one book.
- `storage/database.py::connect()` applies schema modules in a fixed additive order
  (`apply_schema` -> `apply_trading_schema` -> `apply_execution_schema` -> ... ->
  `apply_shadow_alerts_schema`) — confirms the "one `apply_x_schema(conn)` per module,
  `CREATE TABLE IF NOT EXISTS`, called in `connect()`" convention every prior milestone used.

## Existing experiment architecture
See above — `research/experiment_policy.py` + `research/models.py::ExperimentAssignment` +
`research/promotion.py` (arm-agnostic `PromotionGateInputs`, no book identity at all) +
`evaluation/research_comparison.py`. All arm-comparison logic today implicitly assumes a
single shared paper portfolio; Milestone 8 is the first time two isolated portfolios exist.

## Confirmed gaps
1. No book/account identity anywhere in the existing paper-execution schema, models, or
   process boundary (`paper_cash_state` singleton, `simulated_positions` PK=symbol only).
2. `ENHANCED_ONLY`/`BOTH_SEPARATE_PAPER_BOOKS` experiment policies exist as *names* only —
   both fail closed today, by design, exactly per ADR 0004 Decision 6.
3. `may_submit_enhanced()` is hardcoded `False` — there is no path, anywhere, for an
   enhanced-arm recommendation to reach any paper execution today.
4. No portfolio-aware risk sizing exists — Milestone 6's baseline sizing comes from
   `analysis/screener.py`/`services/analyze_candidate.py`'s deterministic risk-plan sizing,
   which has no concept of per-book available cash/exposure feeding back into order size.
5. No point-in-time portfolio snapshot model exists (only the single global daily
   `simulated_portfolio_snapshots` row, without valuation-status/staleness semantics).
6. No comparison-fairness/comparability-gate result type exists — `compare_arms` produces
   raw metric deltas with no `comparable: bool`/fail-closed reasons.
7. `paper_runtime`'s process-boundary protocol has no book_id field anywhere — threading
   book identity through it is additive (new optional field), not a redesign.

## Architecture decisions
Full decisions recorded in `docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md`.
Summary: Milestone 8 is built as a **wholly new, additive subsystem** (`paper_books/` package
+ `paper_book_*` tables) alongside the existing Milestone 3/4 global `PaperLedger`/
`simulated_*` tables, which remain completely untouched. The legacy single-book flow
(`OBSERVE_ONLY`/`BASELINE_ONLY`/`SHADOW_ENHANCED` against the global ledger) keeps working
exactly as before. `BOTH_SEPARATE_PAPER_BOOKS`/`ENHANCED_ONLY` become supported **only**
through the new isolated-book subsystem, gated on `config/paper_books.yaml` explicitly
enabling the matching book(s) — the existing `research/experiment_policy.py` functions are
left untouched; new, additive functions are added alongside them for paper-book-aware policy
decisions, so zero existing call site or test can be affected.

## Paper-book model
`paper_books/models.py::PaperBook` — frozen dataclass: `book_id` (`"BASELINE"`/`"ENHANCED"`,
stable/deterministic, no reassignment), `experiment_arm` (one arm per book, immutable),
`currency` (`"USD"` only), `starting_cash_usd` (`Decimal`), `status`
(`ACTIVE`/`PAUSED`/`CLOSED`), `created_at`, `config_hash`. No live-account reference field
exists on this type at all — structurally cannot point at a broker.

## Book isolation
Every paper-book table carries `book_id` as part of its primary key or a `NOT NULL` column
with a `book_id`-scoped unique/idempotency constraint — see "Schema changes" below. No table
is shared with the legacy `simulated_*`/`paper_cash_state` tables. No function in
`paper_books/` ever queries across two `book_id` values in the same statement except the
explicit, read-only comparison layer (`paper_books/comparison.py`), which only ever compares
independently-computed per-book metrics, never touches a second book's ledger rows.

## Portfolio snapshot model
(populated during Step 8 implementation)

## Mark-to-market design
`paper_books/valuation.py::select_valuation_price` implements the fixed 3-tier priority from
ADR 0006 Decision 5: (1) the price already in this cycle's own `EvidenceSnapshot` (matched by
`evidence_snapshot.symbol == symbol`, `category == "market"`, `normalized_values["latest_close"]`,
provenance from the matching `SourceRecord.available_at`/`.point_in_time_safe`/`.provider`);
(2) `evaluation/price_provider.py::PriceProvider.get_close(symbol, as_of.date())` (never a
live quote — same point-in-time contract Milestone 6 already established); (3) explicit
`SOURCE_UNAVAILABLE`. A price whose staleness (`as_of - available_at`) exceeds
`valuation.maximum_price_age_seconds` from `config/paper_books.yaml` is `PARTIAL_STALE_PRICE`
(value present, flagged); a price whose source is not `point_in_time_safe` is
`POINT_IN_TIME_UNSAFE` (overrides staleness). `build_portfolio_snapshot` aggregates all open
positions: `gross_market_value_usd`/`net_liquidation_value_usd`/`unrealized_pnl_usd` are all
`None` together whenever any position is `POINT_IN_TIME_UNSAFE` or `SOURCE_UNAVAILABLE`
(verified: a book with one unpriced position produces `net_liquidation_value_usd=None`,
`unvalued_position_count=1`, `valuation_status=PARTIAL_MISSING_PRICE`) — `realized_pnl_usd`
and `total_cost_basis_usd` remain always-computable regardless (they don't depend on a
current price). `compute_snapshot_id`/`source_hash` hash `book_id + as_of + {symbol: (qty,
price, status, price_timestamp)}` — verified identical inputs reproduce the identical
snapshot_id across two separate calls; `save_snapshot` is a no-op on a duplicate
`(book_id, snapshot_id)` (idempotent, never re-inserts/updates — the immutability triggers on
`paper_book_snapshots`/`paper_book_snapshot_positions` are never actually exercised in the
normal idempotent-recompute path, only guard against a hypothetical direct UPDATE attempt).
Manual verification (ad-hoc script): cash-only book -> `COMPLETE`, `net_liq == cash`; one
priced position (qty=10, avg_cost=150, price=155) -> `unrealized_pnl_usd=50.00` exactly,
`net_liq=100050.00`; same position with no price source at all -> `PARTIAL_MISSING_PRICE`,
`net_liq=None` (never fabricated).

## Position and lot accounting
(populated during Step 7 implementation)

## Cash and buying-power model
`paper_books/cash_ledger.py` — append-only, `paper_book_cash_ledger` (PK `(book_id,
ledger_entry_id)`, UNIQUE `(book_id, idempotency_key)`). `available_cash(book_id) =
settled_cash(book_id) - reserved_cash(book_id)`, both derived by summing ledger entries
filtered by event-type set — never a stored/overwritable balance. `open_book()` inserts
the book row plus exactly one `INITIAL_CAPITAL` entry (idempotency_key `init:{book_id}`);
a second call is a no-op (verified). `reserve_for_order()` raises `InsufficientCashError`
before ever writing a reservation that would drive available cash negative (verified:
reserving more than a book's available cash raises cleanly; a different book's larger
balance cannot be used to satisfy it — verified baseline/enhanced are fully independent).
`settle_buy`/`settle_sell` write `BUY_SETTLEMENT`/`SELL_SETTLEMENT` plus separate `FEE`/
`SLIPPAGE` entries (all keyed off `fill_id`, so idempotent per fill). `cash_adjustment()`
requires non-empty `operator`+`reason` (raises `ValueError` otherwise) and an
explicit, caller-supplied idempotency key — this is the only path that can add cash
outside `open_book`'s one-time `INITIAL_CAPITAL` entry. No negative available cash is
possible: verified by direct test (reserving beyond available raises before any row is
written).

## Position and lot accounting
`paper_books/positions.py` — FIFO (`COST_BASIS_METHOD = "FIFO"`), long-only (BUY/SELL only,
no SHORT/COVER/OPTION/MARGIN anywhere in the module). `apply_buy_fill` creates exactly one
new lot per fill (`lot_id = f"lot:{fill_id}"`, so a duplicate fill_id can never create a
second lot) and updates the book+symbol position aggregate (quantity, available_quantity,
weighted average_cost_usd). `apply_sell_fill` consumes open lots oldest-first
(`repo.list_open_lots` orders by `opened_at, lot_id`), computes realized P&L as
`sum((fill_price - lot_cost_per_share) * consumed_qty)` per consumed lot, and raises
`InsufficientPositionError` — never allowing oversell — verified directly: selling more
than held raises; selling from a book that never bought the symbol at all (even though a
*different* book holds it) also raises with 0 available, proving no cross-book lot
consumption is even reachable. `apply_forward_or_reverse_split` scales lot
quantities/remaining_quantities and the position aggregate by `ratio`, dividing
average_cost_usd by the same ratio — total dollar cost basis per lot is preserved exactly
(the lot's own `cost_basis_usd` column is never rewritten, only quantity/remaining_quantity
scale). Manual verification (ad-hoc script, this session): $100k baseline book buys 10 AAPL
@ $150 (avg_cost=150.00), reserves/settles/releases correctly (available cash tracks exactly
through reserve → settle → release), then sells 5 @ $160 → realized_pnl=50.00 exactly
(5 * (160-150)), remaining position quantity=5, cash increases by proceeds minus fees/slippage.

## Risk-policy design
`paper_books/risk.py::evaluate_paper_risk` — pure function, Decimal arithmetic, checks in
fixed deterministic order: arm mismatch -> book paused -> missing price -> stale/unsafe price
-> invalid recommendation -> book-level valuation unavailable -> max open positions (new
position only) -> four capacity caps (cash buffer, position weight, symbol concentration,
daily notional) combined with `max_order_notional_usd` via `min()`, floored to whole shares
(`ROUND_FLOOR`), re-deriving `approved_notional_usd = approved_quantity * reference_price`
exactly (matches `PaperBookOrderIntent`'s own reconstruction invariant). `APPROVED` vs
`APPROVED_REDUCED` is decided by comparing final approved_quantity to the recommendation's
own (floored) requested quantity. Verified 5 scenarios directly: full approval; reduction by
`max_order_notional_usd`; rejection when position-weight capacity rounds to zero shares (a
tiny $50 book correctly picks `REJECTED_MAX_POSITION_WEIGHT` over `REJECTED_INSUFFICIENT_CASH`
because position-weight capacity was the tighter of the two, proving the binding-cap
selection is genuinely the minimum, not a fixed priority guess); `REJECTED_ARM_MISMATCH`;
`REJECTED_STALE_PRICE`. Known minor limitation: if `max_order_notional_usd` alone is the
binding zero-cause (reference price so high that even 1 share exceeds the per-order cap),
the rejection is reported as `REJECTED_INSUFFICIENT_CASH` (fallback) since the milestone's
suggested decision vocabulary has no dedicated "max order notional" code — documented, not
a correctness defect (the order is still correctly rejected, only the label is approximate).

## Order sizing
`paper_books/order_intent.py` — `persist_risk_decision` always persists the risk decision
(deterministic ID = sha256(book_id:cycle_id:recommendation_id:symbol)[:32]) regardless of
approval, satisfying "decisions persisted" / "a rejected recommendation still has a queryable
audit trail." `build_order_intent` returns `None` for any non-approved decision (Step 12:
"rejected risk decisions never create submit-ready intents") — verified directly. For an
approved decision, `derive_paper_order_intent_id(recommendation_id, book_id,
EXECUTION_VERSION)` produces the intent ID; verified end-to-end that the *same*
`recommendation_id` submitted through both books' independent risk pipelines produces two
different, deterministic intent IDs, and `persist_order_intent` is idempotent (a second
persist call for the same intent returns `False`, never a second row or an exception).

## Experiment assignment
`research/experiment_policy.py` extended purely additively (verified: all 9 pre-existing
`test_experiment_policy` tests still pass unmodified) with
`validate_paper_book_experiment_policy`/`may_submit_baseline_to_paper_book`/
`may_submit_enhanced_to_paper_book`, each taking explicit `baseline_book_enabled`/
`enhanced_book_enabled` booleans and failing closed
(`UnsupportedExperimentPolicyError`) unless the policy's required book(s) are enabled. The
pre-existing `may_submit_enhanced()` (governing the **legacy** global-ledger path) is
untouched and verified still unconditionally `False` regardless of paper-book policy state —
there is no way for the new functions to influence the old ones or vice versa.
`paper_books/experiment_assignment.py::PaperBookExperimentAssignment`/`save_assignment` persist
one immutable row per `(cycle_id, symbol)` (idempotent — a duplicate save is a no-op, verified)
carrying the shared `evidence_snapshot_id`/`as_of`/both recommendation IDs/both book IDs/both
intent IDs, written once at cycle time before any comparison ever runs.

## Paper execution
**Scope decision (documented, not a gap):** `paper_runtime/src/trading_paper_runtime/models.py::
OrderIntentPayload` gained an additive, optional `book_id: str | None = None` field (default
preserves every one of the 33 pre-existing paper_runtime tests unmodified — verified,
`pytest tests/ -q` in `paper_runtime/` still shows 33 passed after this change) — this
satisfies "book identity enters the process boundary" literally. However, `trading_paper_runtime`
is confirmed genuinely unimportable from the main venv (`ModuleNotFoundError` — it is only
ever reached via `runtime/client/process_client.py`'s real `subprocess.Popen`, per ADR
0001/0002's actual isolation). Its three `BrokerGateway` implementations key
positions/orders per *process instance*, not per book_id internally — threading true
per-book isolation into that shared internal keying scheme across all three implementations
(deterministic + LumiBot + real) is a materially larger, riskier change than this milestone's
LOCAL-SIMULATED-PAPER scope requires (Step 28 explicitly: "the default milestone validation
should use the local simulated paper runtime" and only LOCAL-SIMULATED-PAPER is required for
completion). **Decision:** `paper_books/execution.py` implements its own self-contained,
book-aware deterministic fill simulator (`simulate_fill`/`submit_and_simulate`) rather than
round-tripping through the subprocess for every fill. It receives only the bounded fields
Step 15 names (book_id, paper_order_intent_id, symbol, side, quantity, limit_price,
time_in_force + explicit bid/ask market-simulation inputs) — no Claude prompts/responses/API
keys/chain-of-thought anywhere near this module (grep-confirmed no `anthropic`/`research.orchestration`
import). A real per-book subprocess pool (one `RuntimeClient` per book_id, using the now-additive
`book_id` field) remains a documented, straightforward future extension (Milestone 9
candidate) requiring zero further schema change.

`simulate_fill`: deterministic limit-order crossing rule (same half-spread+slippage shape as
the legacy `paper/ledger.py::FillModel`, reimplemented fresh here — no import from `paper/`,
per ADR 0006 Decision 1). A BUY only fills at a simulated price at-or-below its own
`limit_price`; otherwise returns `None` (order stays `PENDING_SUBMISSION` this cycle — no
partial fills, recorded as deferred per Step 16's explicit allowance). Verified: a marketable
limit order fills immediately (fee/slippage recorded, cash settles, position opens with
correct average cost); an unmarketable limit order (limit far below the simulated ask)
correctly stays `PENDING_SUBMISSION` with no fill, no cash movement, no position created.

`submit_and_simulate`: `repo.save_order_intent` is idempotent on `(book_id,
paper_order_intent_id)`; a BUY reserves cash only on first insertion; a duplicate submission
of the identical intent is verified to be a complete no-op (cash unchanged, no duplicate
fill) — `repo.fill_exists` is checked before ever calling `positions.apply_buy_fill`/
`cash_ledger.settle_buy`, so a duplicate fill can never be applied twice. SELL orders check
`available_quantity` before simulating (never oversell) and settle via
`cash_ledger.settle_sell`/`positions.apply_sell_fill` with no cash reservation (long-only
sells release capital, they never consume it). Recommendation-driven intents in this
milestone are BUY-only (matching the existing repo-wide convention that
`execution/models.py::ORDER_SIDE_BUY` is the only side a frozen recommendation ever produces)
— SELL-side support exists at the position/execution layer for correctness and any future
exit-strategy service, but no automated recommendation-driven SELL path is wired this
milestone (documented, not a silently missing capability).

## Reconciliation
`paper_books/reconciliation.py::reconcile_book(conn, book_id, as_of, expected_arm=None)` —
every query scoped to exactly one `book_id`. Checks (in this order, most-severe status wins
via a fixed `_SEVERITY_ORDER` when multiple mismatch types are found): `ARM_MISMATCH` (book's
actual arm vs an expected arm the caller supplies) -> `BOOK_MISMATCH` (a fill row whose own
`book_id` column disagrees — structurally unreachable given `repo.list_fills` already filters,
but checked explicitly as defense-in-depth) -> `DUPLICATE_FILL` -> `MISSING_ORDER` (a fill
referencing an order_intent_id that doesn't exist in this book) -> `MISSING_FILL` (an order
marked FILLED with no corresponding fill row) -> `CASH_MISMATCH` (settled cash independently
recomputed from fills+non-fill ledger entries vs. the ledger-derived total) ->
`POSITION_MISMATCH` (position quantity independently recomputed from the fill history vs. the
stored aggregate). Verified directly: a clean book reconciles `MATCHED` with zero mismatches;
directly corrupting `paper_book_positions.quantity` via raw SQL is caught as
`POSITION_MISMATCH` with the exact stored-vs-recomputed values in the mismatch detail;
requesting reconciliation with a deliberately wrong `expected_arm` correctly returns
`ARM_MISMATCH`; reconciling an unknown `book_id` raises `ValueError` before any comparison
runs. `paper_book_reconciliations` is immutable once persisted (insert-only, idempotent on
`(book_id, reconciliation_id)`).

## Fill behavior
`paper_books/models.py` supports `PENDING_SUBMISSION`/`SUBMITTED`/`FILLED`/
`PARTIALLY_FILLED`/`CANCELLED`/`EXPIRED`/`REJECTED` in `KNOWN_INTENT_STATUSES`.
`execution.py` actively transitions `PENDING_SUBMISSION` -> `FILLED` (simulate_fill crosses
the limit), `PENDING_SUBMISSION` -> `REJECTED` (SELL exceeding available position, checked
before simulation), and provides `cancel_pending_intent`/`expire_pending_intent` for the
`CANCELLED`/`EXPIRED` transitions (both release any BUY-side cash reservation, both are
no-ops if the order already filled). `PARTIALLY_FILLED` is **not implemented** — this
milestone's fill simulator is all-or-nothing per Step 16's explicit allowance ("if the
existing runtime does not support partial fills, do not invent them casually... record them
as deferred"); the legacy `paper/ledger.py`'s own `submit_and_fill` is likewise all-or-nothing,
so this is consistent with existing repository precedent, not a new gap.

## Corporate-action handling
`paper_books/corporate_actions.py::apply_corporate_action` — supports exactly
`forward_split`/`reverse_split`/`cash_dividend` (matching the already-implemented
`evidence_providers/corporate_actions.py` Alpaca adapter's `IMPLEMENTED_ACTION_TYPES` from
Milestone 7); any other `action_type` raises `UnsupportedCorporateActionError` before
touching any table (verified: `spin_off` rejected cleanly). Applied at most once per
`(book_id, action_id)` (`repo.corporate_action_applied` checked first; a second call
returns `{"applied": False, "reason": "already applied"}` — verified). Splits call
`positions.apply_forward_or_reverse_split`, which scales lot quantities/remaining_quantities
and the position aggregate by `ratio` while preserving each lot's total dollar
`cost_basis_usd` exactly (average cost divides by the ratio) — verified: 10 shares @ $150
(cost basis $1500) through a 2:1 forward split becomes 20 shares @ $75.00 avg cost, same
total cost basis. Cash dividends compute `quantity_held * dividend_per_share_usd` from the
book's *own* position at application time and credit via `cash_ledger.credit_dividend`
(itself idempotent on `dividend:{action_id}:{symbol}`) — verified: a book holding zero shares
of the symbol receives $0 (still recorded as applied, so a later purchase never
retroactively receives it) while a book holding 20 shares receives exactly $10.00 for a
$0.50/share dividend. Book-specific application is structural: the ENHANCED book applying
the *same* `action_id` independently produces its own (zero-effect, since it held no
position) result with no interaction with BASELINE's already-applied state.

## Performance metrics
`paper_books/metrics.py::compute_book_metrics(conn, book_id, window_start, window_end)` —
computes every Step 19 metric only from snapshots/fills actually inside the explicit window;
every metric is `None` when its inputs are unavailable (verified: `turnover`/`fees_usd`/
`average_loss`/`profit_factor` are all `None` in scenarios with no fills or no losing
trades — never a fabricated `0`). `_compute_trade_pnls` independently recomputes one
realized-P&L value per SELL fill via FIFO over the fill history (self-contained, mirrors
`reconciliation.py`'s own independent-recomputation pattern — does not depend on any
previously-stored per-fill P&L). Verified end-to-end (buy 10 @ $150, two snapshots at $150
then $160, sell 5 @ $160, one more snapshot at $165): `trade_count=2`, `fees_usd=1.50`,
`slippage_usd=0.75`, `win_rate=1` (the one closed sell was profitable),
`average_win=50.00` (exactly `5*(160-150)`), `average_loss=None`/`profit_factor=None`
(correctly, since there were zero losing trades — not fabricated as `0`/`inf`),
`realized_pnl_usd=50.00`, `unrealized_pnl_usd=75.00` (5 remaining shares *(165-150)`),
`maximum_drawdown`/`volatility`/`cumulative_return` all computed from the actual
`net_liquidation_value_usd` equity curve across the 3 snapshots. No Sharpe or annualized
ratio was added (explicitly out of scope). `save_book_metrics` persists via
`paper_book_daily_metrics` (idempotent `INSERT OR REPLACE` keyed by `metrics_id`, allowing a
same-day recompute as new data arrives without ever rewriting a *different* historical
window's row) — round-trip verified.

## Baseline-versus-enhanced comparison
`paper_books/comparison.py::build_comparison` computes both books' metrics over the identical
`[window_start, window_end]` (same calendar, same cutoff — never a per-arm window), and fails
closed (`comparable=False`, all `metric_deltas` values `None`) when: starting cash differs
unexpectedly; either book's `net_liquidation_value_usd` is `None` (unsafe/incomplete
valuation); a book's `experiment_arm` doesn't match its expected role; any assigned cycle in
the window is missing either arm's recommendation; or the comparable-cycle count is below
`min_comparable_cycles`. Verified directly: two books with different starting cash and zero
assigned cycles correctly produce `comparable=False` with 4 distinct, accurate reasons
(never silently comparing anyway). Verified a genuinely comparable scenario (equal starting
cash, one assigned cycle, both books valued both days): `comparable=True`,
`cumulative_return` delta computed exactly as `enhanced - baseline`. Never automatically
declares the enhanced arm better — the comparison result is descriptive only.
`paper_book_experiment_comparisons` is immutable once persisted (insert-only).

## Promotion evidence
`paper_books/promotion_evidence.py::evaluate_promotion_evidence` extends (does not modify)
`research/promotion.py`'s existing arm-agnostic gate — no new import, no shared state, a
purely additive module. Full 6-value result vocabulary implemented and distinctly reachable:
`NOT_COMPARABLE` (comparison itself failed) -> `INSUFFICIENT_DATA` (delta unavailable, or a
non-positive delta without enough sample to confidently claim baseline outperformance) ->
`BASELINE_OUTPERFORMS` (non-positive delta, sample floors met) -> `ENHANCED_OUTPERFORMS_OBSERVED`
(positive delta, but sample floors not yet met — "observed," not "eligible") ->
`ENHANCED_OUTPERFORMS_NOT_PROMOTABLE` (positive delta, sample floors met, but operational
health or reconciliation status blocks review) -> `PROMOTION_REVIEW_ELIGIBLE` (positive
delta, sample floors met, operations healthy). Verified end-to-end: a genuinely comparable
2-book/2-day scenario where the enhanced book's valuation rose while baseline's stayed flat
produces `PROMOTION_REVIEW_ELIGIBLE` with an explicit "not an automatic promotion" reason
string. No result value in this vocabulary ever means "promoted" — only "eligible for human
review." `paper_book_promotion_evidence` persists the result immutably alongside the
`comparison_id` it was derived from.

## CLI design
`paper_books/cli_support.py` holds all business logic; `cli.py` additions are thin argparse
wiring + `json.dumps(..., default=str)` (matching every existing command's convention).
Commands added: `paper-book-list`, `paper-book-show --book-id`, `paper-book-snapshot
--book-id --as-of`, `paper-book-run-cycle --cycle-id --experiment-policy --provider-mode
fixture --symbol --quantity-hint --reference-price --bid --ask
[--recommendation-id-baseline/-enhanced]`, `paper-book-reconcile --book-id [--as-of]`,
`paper-experiment-compare --experiment-id --window-start --window-end
[--min-comparable-cycles]`, `paper-promotion-status --experiment-id [floors...]`. Every
mutating command (`snapshot`/`run-cycle`/`reconcile`) fails closed with `{"error": ...}` +
exit code 2 when `paper_books.enabled` is `false` (verified: `paper-book-snapshot` against
the shipped disabled config returns exactly this). `paper-book-list`/`paper-book-show` are
read-only and work regardless of the enabled flag. **End-to-end CLI verification performed
this session** (config temporarily flipped to `enabled: true` + both books enabled in a
throwaway edit, tested, then restored byte-for-byte to the shipped disabled state — diff
confirmed clean before proceeding): `paper-book-run-cycle` with
`--experiment-policy BOTH_SEPARATE_PAPER_BOOKS` and the same `--symbol AAPL` fixture
recommendation produced two **distinct** `paper_order_intent_id` values (one per book,
proving isolation at the CLI layer, not just in unit tests), both `APPROVED_REDUCED` by the
`max_order_notional_usd=1000` cap, both correctly `PENDING_SUBMISSION` (the reduced 6-share
limit order didn't cross the fixture bid/ask this cycle); `paper-book-show --book-id
BASELINE` correctly showed `reserved_cash_usd=900.00` (the pending reservation);
`paper-book-reconcile --book-id BASELINE` returned `MATCHED`; `paper-experiment-compare`/
`paper-promotion-status` against a fresh experiment with zero assignments correctly returned
`comparable=false`/`NOT_COMPARABLE` with an honest "insufficient comparable cycles" reason —
no command fabricated a result. Missing/unknown `--book-id` fails closed (verified at the
`cli_support` unit level; e.g. `paper-book-show` with an unconfigured book_id returns an
`"error"` key, never a default/empty-but-successful response).

## Schema changes
(populated during Step 5 implementation)

## Files created
`docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md`,
`config/paper_books.yaml`,
`src/trading_research/paper_books/__init__.py`,
`src/trading_research/paper_books/config.py`,
`src/trading_research/paper_books/models.py`,
`src/trading_research/paper_books/cash_ledger.py`,
`src/trading_research/paper_books/positions.py`,
`src/trading_research/paper_books/valuation.py`,
`src/trading_research/paper_books/risk.py`,
`src/trading_research/paper_books/order_intent.py`,
`src/trading_research/paper_books/experiment_assignment.py`,
`src/trading_research/paper_books/execution.py`,
`src/trading_research/paper_books/reconciliation.py`,
`src/trading_research/paper_books/corporate_actions.py`,
`src/trading_research/paper_books/metrics.py`,
`src/trading_research/paper_books/comparison.py`,
`src/trading_research/paper_books/promotion_evidence.py`,
`src/trading_research/paper_books/cli_support.py`,
`src/trading_research/storage/paper_books_schema.py`,
`src/trading_research/storage/paper_books_repositories.py`,
`tests/unit/test_paper_books_config.py`,
`tests/unit/test_paper_books_ledger_and_positions.py`,
`tests/unit/test_paper_books_valuation.py`,
`tests/unit/test_paper_books_risk_and_intent.py`,
`tests/unit/test_paper_books_execution_and_reconciliation.py`,
`tests/unit/test_paper_books_comparison_and_promotion.py`,
`tests/unit/test_paper_books_experiment_policy.py`,
`tests/integration/test_milestone_8_offline_end_to_end.py`.

## Files modified
`src/trading_research/storage/database.py` (wired `apply_paper_books_schema`),
`src/trading_research/research/experiment_policy.py` (purely additive functions appended —
zero existing line changed),
`src/trading_research/cli.py` (7 new subcommands + `_parse_iso_datetime` helper),
`paper_runtime/src/trading_paper_runtime/models.py` (additive, optional `book_id` field on
`OrderIntentPayload`, default `None` — all 33 pre-existing tests unaffected).

## Tests added
89 new tests across 8 files (7 unit + 1 integration), covering: config fail-closed behavior
(13), cash ledger/FIFO lots/corporate actions (17), point-in-time valuation (9), risk
policy/order intent (15), execution/reconciliation (11), comparison/promotion evidence (12),
additive experiment policy (10), and a full offline end-to-end dual-book pipeline proof (2).

## Test run log
- 2026-07-13 — `pytest tests/ -q` -> 1266 passed, 14 skipped (baseline confirmed).
- 2026-07-13 — `cd paper_runtime && pytest tests/ -q` -> 33 passed (baseline confirmed).
- 2026-07-14 — after full Milestone 8 implementation: `pytest tests/ -q` ->
  **1355 passed, 14 skipped** (1266 baseline + 89 new Milestone 8 tests, zero regressions,
  zero existing test modified/weakened/skipped).
- 2026-07-14 — `cd paper_runtime && pytest tests/ -q` -> **33 passed** (unchanged, directory
  untouched except the one additive model field).

## Optional paper-broker validation
Not performed — default milestone validation uses the local simulated paper runtime only,
per Step 28's explicit instruction. No external paper-broker submission was requested by the
user this session.

## Bugs discovered and fixed
(populated as implementation proceeds)

## Security and safety review
All checks performed directly against the actual working-tree diff (not delegated):
- No live broker path added: `paper_books/` grep-confirmed zero imports of
  `lumibot`/`alpaca`/`robinhood`/`anthropic`/`trading_paper_runtime` (AST-based structural
  test: `test_milestone_8_offline_end_to_end.py::test_no_live_execution_path_exists`).
- No `--live` CLI flag: confirmed via `python -m trading_research.cli --help` containing no
  `--live` substring (same structural test).
- No live credentials read: `grep -rn "os.environ\|getenv"` over `paper_books/` returns
  nothing; `grep -rniE "api_key|api_secret|password|token"` over `paper_books/` returns
  nothing.
- No Robinhood mutation: no import of any Robinhood-related module anywhere in this
  milestone's diff.
- No enhanced-to-baseline fallback / no baseline-to-enhanced fallback: `research/
  experiment_policy.py`'s new functions map each policy to an exact, fixed book set;
  `execution.py`/`order_intent.py` always operate on the single `book_id` explicitly passed
  in, never inferring a substitute.
- No shared cash / no shared positions / no shared fills / no cross-book lot consumption: all
  four proven by dedicated tests in `test_paper_books_ledger_and_positions.py`,
  `test_paper_books_execution_and_reconciliation.py`, and the full pipeline test.
- No Claude-selected book / no Claude risk override: `paper_books/risk.py`/`order_intent.py`
  take only deterministic, already-computed inputs — no Claude-shaped type anywhere in their
  signatures; the package has zero import of `research.orchestration` or `anthropic`.
- No negative cash: `cash_ledger.reserve_for_order` raises `InsufficientCashError` before
  writing any row that would drive available cash negative — verified directly
  (`test_negative_available_cash_never_occurs`).
- No margin, no short position: `positions.py` only supports BUY/SELL against an existing
  long position; `apply_sell_fill` raises `InsufficientPositionError` rather than ever
  allowing a negative position quantity.
- No unsupported order type: `PaperBookOrderIntent.__post_init__` only accepts `LIMIT`
  (`KNOWN_ORDER_TYPES = (ORDER_TYPE_LIMIT,)`).
- No mutable fill history / no mutable cash-ledger history / no mutable snapshot/reconciliation
  history: enforced at the schema level via `BEFORE UPDATE`/`BEFORE DELETE` triggers on every
  append-only table (verified structurally by reading `storage/paper_books_schema.py`, and
  behaviorally by every idempotent-insert test passing without needing a mutation path).
- No mutable recommendation history: `paper_books/` never writes to the `recommendations`
  table at all (grep-confirmed).
- No current-price leakage: `valuation.py::select_valuation_price` only ever reads an
  already-built `EvidenceSnapshot`'s own market item or calls
  `PriceProvider.get_close(symbol, as_of.date())` — verified directly
  (`test_current_quote_never_substitutes_for_historical_price` asserts the exact call
  arguments the fake provider received).
- No missing price converted to zero / no unknown P&L converted to zero: verified directly
  (`test_missing_price_never_becomes_zero`; `net_liquidation_value_usd`/`unrealized_pnl_usd`
  are `None`, never `0`, whenever a position can't be safely valued).
- No automatic promotion: the full 6-value promotion-evidence result vocabulary contains no
  "promoted" value; verified directly (`test_no_automatic_promotion_ever` asserts the string
  `"PROMOTED"` never appears as a result and that even a strongly positive delta only reaches
  `PROMOTION_REVIEW_ELIGIBLE`, explicitly labeled "not an automatic promotion" in its own
  reason text).
- No recurring deployment activation: zero changes to `deploy/launchd/*`, zero
  `launchctl load`/`launchctl start` invocation anywhere in this session.
- `real_orders` remains write-blocked: confirmed via `git diff --stat` — zero diff touches
  `trading_schema.py`, `paper/ledger.py`, or `execution/models.py` at all.
- Existing tests were not weakened, deleted, or newly skipped: confirmed via
  `git status --short tests/` showing only new files added, zero existing test file modified;
  full suite went from 1266 passed/14 skipped to 1355 passed/14 skipped (89 new tests, zero
  regressions, zero skips added).

## Documentation
Created: `docs/milestone8-isolated-paper-portfolios.md`, `docs/runbooks/paper-book-operations.md`,
`docs/runbooks/paper-book-reconciliation.md`. Updated (pointer notes only, historical content
preserved per the established Milestone 7.1/7.2 convention): `docs/adr/0006-...md` (marked
Accepted with the exact final test count), `docs/milestone-7 pending.md` Section 7.2 (marked
addressed by Milestone 8, original text preserved below the pointer),
`docs/runbooks/shadow-operations.md` (one paragraph noting paper_books is not yet wired into
the shadow scheduler).

## Known limitations
See `docs/milestone8-isolated-paper-portfolios.md` Section 19 for the full, authoritative
list: local-simulated fill engine is self-contained rather than routed through the isolated
`paper_runtime` subprocess (documented rationale, not an oversight); no PARTIALLY_FILLED
support (all-or-nothing, matches legacy convention); SELL-side intents supported at the
execution layer but no automated recommendation-driven SELL path wired; `max_order_notional_usd`
has no dedicated rejection code in the milestone's own vocabulary (falls back to
REJECTED_INSUFFICIENT_CASH); corporate actions limited to the 3 types Milestone 7 already
implemented.

## Deferred work
Explicit non-goals from docs/milestone-8.md, none attempted: fixing unsupported_claim_rate,
real paper-reconciliation health wiring into shadow (from the Milestone 7 backlog),
duplicate-prevention health wiring, Reddit registration, new news providers, destructive
retention, real recurring shadow activation, remaining deferred corporate-action provider
types, MFE/MAE, live promotion, Robinhood integration, live Alpaca trading, portfolio
optimization using an LLM, reinforcement learning, strategy generation, unlimited backtesting
universe. Also deferred (recorded, not silently dropped): a real per-book subprocess pool
through the isolated paper_runtime boundary; wiring paper_books into
shadow/scheduler.py::run_due_shadow_cycle; a non-fixture, evidence-driven
paper-book-run-cycle sourced from research/scheduled_cycle.py.

## Final status
**COMPLETE for this session's scope.**

- Baseline confirmed exactly: 1266 passed/14 skipped (main), 33 passed (paper_runtime).
- Final: **1355 passed, 14 skipped** (main) — 89 net new tests, zero regressions, zero
  existing test weakened, deleted, or newly skipped. **33 passed** (paper_runtime, unchanged
  except one additive, optional `book_id` field on `OrderIntentPayload`).
- Baseline and enhanced isolated paper books have fully separate cash, positions, orders,
  fills, risk decisions, reconciliation, and metrics — proven by 89 tests including a full
  offline end-to-end dual-book pipeline and explicit cross-book-contamination-attempt tests.
- Enhanced paper submission remains disabled by default and requires explicit, separate
  per-book config enablement; the legacy global-ledger `may_submit_enhanced()` is proven
  unaffected by any paper-book state.
- No live-trading path, no `--live` flag, no automatic promotion, no recurring deployment
  activation exist anywhere in this milestone's code.
- No commit or push performed at any point in this session.
- Every Milestone 1-7.2 safety invariant preserved (see "Security and safety review" above).
