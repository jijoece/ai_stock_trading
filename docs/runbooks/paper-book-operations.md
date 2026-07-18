# Runbook: Paper-book operations

Operator-facing procedures for the Milestone 8 isolated paper-book subsystem. See
`docs/milestones/milestone8-isolated-paper-portfolios.md` for the full architecture and
`docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md` for why each boundary
exists.

**Before you start:** paper books ship fully disabled. `config/paper_books.yaml` has
`paper_books.enabled: false` and the enhanced book's own `enabled: false` out of the box.
This is a **LOCAL-SIMULATED-PAPER** system only — there is no live-trading path, no external
paper-broker submission by default, and no way to reach one from this configuration file.

## Enable paper books

1. Open `config/paper_books.yaml`. The shipped defaults:

   ```yaml
   paper_books:
     enabled: false
     books:
       baseline:
         enabled: true
         book_id: BASELINE
         starting_cash_usd: "100000.00"
       enhanced:
         enabled: false
         book_id: ENHANCED
         starting_cash_usd: "100000.00"
   ```

2. Set `paper_books.enabled: true` to allow any paper-book command to mutate state.
3. To enable the enhanced (Claude-informed) arm's own isolated book, set
   `books.enhanced.enabled: true`. This does **not** allow the enhanced arm to reach any live
   destination or the baseline book — it only unlocks its own isolated paper book.
4. Review `risk.*` (position/order/concentration/cash-buffer limits) and `valuation.*`
   (staleness tolerance) before relying on the sizing output — the shipped defaults are
   reasonable starting points, not validated production values.
5. `execution.allow_live_broker` cannot be set to `true` — the config loader raises
   `PaperBooksConfigError` immediately if you try.

Configuration is loaded fresh on every CLI invocation — there is no running process to
restart.

## List and inspect books

```bash
python -m trading_research.cli paper-book-list
python -m trading_research.cli paper-book-show --book-id BASELINE
python -m trading_research.cli paper-book-show --book-id ENHANCED
```

`paper-book-list`/`paper-book-show` are read-only and work regardless of the `enabled` flag.
An unconfigured or unopened `book_id` returns `{"error": ...}`, never a fabricated empty
success.

## Build a mark-to-market snapshot

```bash
python -m trading_research.cli paper-book-snapshot \
  --book-id BASELINE \
  --as-of 2026-07-13T20:00:00Z
```

Requires `paper_books.enabled: true` and the target book to already be opened (via a prior
`paper-book-run-cycle` call) and enabled in config. The response's `valuation_status` field
tells you whether the snapshot is `COMPLETE`, `PARTIAL_STALE_PRICE`, `PARTIAL_MISSING_PRICE`,
`POINT_IN_TIME_UNSAFE`, or `SOURCE_UNAVAILABLE` — `net_liquidation_value_usd` is `null`
whenever any open position couldn't be safely valued. This is intentional; do not interpret a
`null` net liquidation value as a bug.

## Run a fixture-mode cycle (local-simulated paper execution)

```bash
python -m trading_research.cli paper-book-run-cycle \
  --cycle-id cycle-2026-07-13-001 \
  --experiment-policy BOTH_SEPARATE_PAPER_BOOKS \
  --provider-mode fixture \
  --symbol AAPL \
  --quantity-hint 10 \
  --reference-price 150.00 \
  --bid 149.90 \
  --ask 150.10 \
  --recommendation-id-baseline rec-baseline-001 \
  --recommendation-id-enhanced rec-enhanced-001
```

This is the offline/fixture path only — it never calls Claude or fetches real evidence. It
opens each requested book if not already open, builds a fresh snapshot, evaluates
deterministic risk, builds and persists a book-aware order intent, and simulates a fill
against the explicit `--bid`/`--ask` market-simulation inputs. `--experiment-policy` gates
which book(s) actually receive an order:

* `BASELINE_ONLY`/`SHADOW_ENHANCED` — only `--recommendation-id-baseline` is used.
* `ENHANCED_ONLY` — only `--recommendation-id-enhanced` is used, and **only** if the
  enhanced book is enabled in config (otherwise this fails closed with
  `UnsupportedExperimentPolicyError`).
* `BOTH_SEPARATE_PAPER_BOOKS` — both, each into its own isolated book, **only** if both
  books are enabled.
* `OBSERVE_ONLY` — submits nothing to either book.

The same `recommendation_id` submitted to both books always produces two different
`paper_order_intent_id` values — this is expected and proves book isolation, not a bug.

## Reconcile a book

```bash
python -m trading_research.cli paper-book-reconcile --book-id BASELINE
python -m trading_research.cli paper-book-reconcile --book-id ENHANCED --as-of 2026-07-13T20:00:00Z
```

See `docs/runbooks/paper-book-reconciliation.md` for the full status vocabulary and
incident-response guidance.

## Compare baseline vs enhanced

```bash
python -m trading_research.cli paper-experiment-compare \
  --experiment-id exp-2026-07 \
  --window-start 2026-07-01T00:00:00Z \
  --window-end 2026-07-31T00:00:00Z
```

Returns `comparable: false` with explicit reasons whenever starting cash differs
unexpectedly, either book's valuation is unsafe/incomplete, a cycle is missing either arm's
recommendation, or too few comparable cycles exist. Do not interpret `comparable: false` as
an error — it is the system correctly refusing to draw a conclusion from unfair data.

## Check promotion evidence

```bash
python -m trading_research.cli paper-promotion-status --experiment-id exp-2026-07
```

Requires a prior `paper-experiment-compare` call for the same `experiment_id` (it reads the
most recent persisted comparison). Returns one of six results:
`INSUFFICIENT_DATA`/`NOT_COMPARABLE`/`BASELINE_OUTPERFORMS`/`ENHANCED_OUTPERFORMS_OBSERVED`/
`ENHANCED_OUTPERFORMS_NOT_PROMOTABLE`/`PROMOTION_REVIEW_ELIGIBLE`. **No result ever means
"promoted."** `PROMOTION_REVIEW_ELIGIBLE` means a human should review the evidence — it is
never an automatic authorization to change anything about which arm executes live (there is
no live-execution path in this system at all).

## FAQ

**Q: Can the enhanced arm ever reach a live broker through paper books?**
No. `config/paper_books.yaml`'s `execution.allow_live_broker` cannot be set to `true` — the
config loader raises an error immediately. There is no `--live` flag anywhere in the CLI, and
`paper_books/` imports nothing broker-related (verified structurally in
`tests/integration/test_milestone_8_offline_end_to_end.py::test_no_live_execution_path_exists`).

**Q: I ran `paper-book-run-cycle` for the same cycle/symbol/book twice. Did it submit twice?**
No. The order intent ID is deterministic (`recommendation_id + book_id + execution_version`)
and both the order-intent insert and the resulting fill are idempotent — a retried
invocation is a safe no-op.

**Q: Why did my baseline and enhanced books get different position sizes for the same
symbol?**
This is expected whenever the two books' portfolio states differ (different current
positions, different available cash, different existing exposure) — the reason is always a
deterministic, persisted `PaperRiskDecision.reasons` value, queryable via
`paper_book_risk_decisions`. It is never randomness and never cross-book contamination.
