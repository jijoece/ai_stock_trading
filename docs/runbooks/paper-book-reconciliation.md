# Runbook: Paper-book reconciliation

Investigation playbook for `paper_books/reconciliation.py`'s per-book reconciliation status.
Pair with `docs/runbooks/paper-book-operations.md` for routine commands and
`docs/milestones/milestone8-isolated-paper-portfolios.md` for architecture detail.

## Run a reconciliation

```bash
python -m trading_research.cli paper-book-reconcile --book-id BASELINE
python -m trading_research.cli paper-book-reconcile --book-id ENHANCED --as-of 2026-07-13T20:00:00Z
```

Every reconciliation is scoped to exactly one `book_id` — there is no cross-book
reconciliation, and a mismatch in one book is never hidden or averaged out by the other
book's healthy state. Each run persists a new, immutable row in
`paper_book_reconciliations` (idempotent per `(book_id, reconciliation_id)`).

## Status vocabulary

| Status | Meaning | First response |
|---|---|---|
| `MATCHED` | No mismatch found. | None needed. |
| `ARM_MISMATCH` | The book's own `experiment_arm` doesn't match the `expected_arm` you passed. | Confirm you targeted the correct `book_id` — this usually means an operator/script error, not data corruption. |
| `BOOK_MISMATCH` | A fill row's own `book_id` column disagrees with the book being reconciled. | Structurally unreachable via the shipped repository functions (they always filter by `book_id`) — if you see this, something wrote to `paper_book_fills` outside the normal `execution.py` path. Investigate immediately; do not resume normal operation until the write path is found. |
| `DUPLICATE_FILL` | The same `fill_id` appears more than once for this book. | The `(book_id, fill_id)` primary key should make this impossible via normal writes — treat as a serious integrity signal and stop further submissions to this book until investigated. |
| `MISSING_ORDER` | A fill references a `paper_order_intent_id` that doesn't exist in `paper_book_orders` for this book. | Check whether the order was deleted or inserted into the wrong book by a custom script — `paper_book_orders` has no delete path in the shipped code. |
| `MISSING_FILL` | An order is marked `FILLED` but has no corresponding fill row. | Check for a partial/interrupted `execution.py::submit_and_simulate` call (e.g. process killed between `repo.save_fill` and `repo.update_order_status`, though the update happens last so this ordering makes a genuinely missing fill unlikely from the shipped code path). |
| `CASH_MISMATCH` | Settled cash independently recomputed from the fill history disagrees with the ledger-derived total. | Query `paper_book_cash_ledger` and `paper_book_fills` for this book directly and compare by hand — look for a fill that was applied without a corresponding `settle_buy`/`settle_sell` call (this is the most common way to reach this state if extending the code). |
| `POSITION_MISMATCH` | Position quantity independently recomputed from the fill history disagrees with the stored aggregate, **or** an order's total filled quantity disagrees with its own requested quantity. | Query `paper_book_position_lots` for the symbol and manually walk the FIFO consumption — the mismatch detail includes both the recomputed and stored values. |
| `LOT_MISMATCH` | Reserved for future finer-grained lot-level cross-checks; not currently emitted by the shipped implementation (position-level mismatches are reported as `POSITION_MISMATCH` today). | N/A yet. |
| `PENDING_NOT_APPLICABLE` | Reserved for a future explicit "nothing to reconcile yet" status; not currently emitted (an empty book with no orders reconciles as `MATCHED`, since there is nothing that could mismatch). | N/A yet. |

## Investigating a mismatch

1. Read the `mismatches` list in the reconciliation response — every entry has a `type` and a
   human-readable `detail` string naming the exact stored vs. recomputed values (never just a
   bare status code).
2. Query the underlying tables directly for the affected `book_id`:
   ```sql
   SELECT * FROM paper_book_fills WHERE book_id = 'BASELINE' ORDER BY fill_timestamp;
   SELECT * FROM paper_book_cash_ledger WHERE book_id = 'BASELINE' ORDER BY event_timestamp;
   SELECT * FROM paper_book_positions WHERE book_id = 'BASELINE';
   SELECT * FROM paper_book_position_lots WHERE book_id = 'BASELINE' ORDER BY opened_at;
   ```
3. Reconciliation never rewrites history — `paper_book_reconciliations` only ever records
   what it found. A correction is always a new, explicit compensating event (e.g.
   `cash_ledger.cash_adjustment(..., operator=..., reason=...)`), never a direct `UPDATE` of a
   historical row (the schema's triggers block this anyway for every append-only table).
4. Do not resume normal paper-book operations for a book showing `ARM_MISMATCH`,
   `BOOK_MISMATCH`, or `DUPLICATE_FILL` without first identifying the write path that
   produced it — these three indicate a structural violation of the isolation guarantees this
   milestone exists to provide.

## Cross-book isolation proof

`tests/unit/test_paper_books_execution_and_reconciliation.py::test_one_book_mismatch_does_not_hide_in_the_other`
directly proves that corrupting one book's `paper_book_positions` row produces
`POSITION_MISMATCH` for that book only — the other book independently reconciles `MATCHED`.
This is the property to re-verify (by re-running that test, or an equivalent manual check)
after any change to `reconciliation.py`.
