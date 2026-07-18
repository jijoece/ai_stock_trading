# Runbook: Manual paper-trading soak

Operator-facing procedures for the Milestone 9 manual daily lifecycle. See
`docs/milestones/milestone9-manual-paper-soak-and-lifecycle.md` for the full architecture record and
`docs/runbooks/paper-book-operations.md` for the Milestone 8 book-level runbook this one builds
on (enable/open books, cash, positions, reconciliation).

**Before you start:** lifecycle processing ships fully disabled. `config/paper_books.yaml` has
`paper_books.lifecycle.enabled: false` and `paper_books.lifecycle.exits.enabled: false` out of
the box, on top of `paper_books.enabled: false`. Nothing in this runbook is automated — every
step below is a manual CLI invocation you run yourself, on your own schedule. There is no
launchd job, no cron, no recurring scheduler wired to any of this.

## 1. Enable lifecycle processing

Edit `config/paper_books.yaml`:

```yaml
paper_books:
  enabled: true
  lifecycle:
    enabled: true
    pending_orders:
      expire_after_market_days: 1
    exits:
      enabled: true
      stop_loss_percent: "0.08"
      profit_target_percent: "0.15"
      maximum_holding_market_days: 20
      exit_on_recommendation_reversal: true
    soak:
      minimum_completed_cycles: 10
      minimum_market_days: 5
```

Review the exit thresholds before relying on them — the shipped values are conservative
defaults, not validated production values. `exits.enabled: false` lets you run pending-order
processing, snapshots, and reconciliation without ever evaluating an exit — a safe way to soak
the entry side alone first.

## 2. Daily sequence

For each market day you want to soak, in order:

```bash
# 1. (Optional) integrate any real scheduled-research cycles from today.
python -m trading_research.cli paper-book-lifecycle-run \
  --as-of 2026-07-14T20:00:00+00:00 \
  --integrate-cycle-id <cycle-id-from-today>

# 2. Review what happened.
python -m trading_research.cli paper-book-soak-report --as-of 2026-07-14T20:00:00+00:00
```

`--integrate-cycle-id` may be repeated, or omitted entirely on a day with no new cycle — the
lifecycle run still processes pending orders, evaluates exits, snapshots, and reconciles both
books. Cycle integration is always explicit: nothing is integrated automatically.

`--as-of` should be a consistent time-of-day (market close is a reasonable convention) across
your soak session — the lifecycle's own point-in-time valuation and market-day-holding math
both depend on it.

## 3. Requesting a manual exit

To close a position outside the automatic rules (e.g. an operator-judgment risk-off decision):

```bash
python -m trading_research.cli paper-book-exit-request \
  --book-id BASELINE --symbol AAPL --operator jijo --reason "manual risk-off, earnings tomorrow"
```

This only records an audited request — it does not submit an order by itself. The request is
picked up (and "consumed," never re-triggered) the next time you run
`paper-book-lifecycle-run` for a date on or after `requested_at`. `--operator` and `--reason`
are both required; the command fails closed on an unknown `--book-id`.

## 4. Checking readiness

```bash
python -m trading_research.cli paper-book-soak-readiness --as-of 2026-07-14T20:00:00+00:00
```

Returns one of `NOT_READY_INSUFFICIENT_CYCLES`, `NOT_READY_INSUFFICIENT_MARKET_DAYS`,
`NOT_READY_RECONCILIATION`, `NOT_READY_VALUATION`, `NOT_READY_LIFECYCLE_FAILURES`,
`READY_FOR_MORE_MANUAL_SOAK`, or `READY_FOR_RECURRING_ACTIVATION_REVIEW`. **None of these
results activates anything.** `READY_FOR_RECURRING_ACTIVATION_REVIEW` means "a human may now
review whether to build recurring activation as a separate, future, explicitly-scoped task" —
it is advisory text, not a switch.

## 5. Recovering from a failed lifecycle run

A lifecycle run never partially corrupts one book because of the other: check
`failure_reasons` in the `paper-book-lifecycle-run` JSON output (or the persisted
`paper_book_lifecycle_runs` row) to see which book, if any, failed and why.

* Every sub-operation inside a lifecycle run is independently idempotent (order submission,
  fill application, cash reservation/release, exit-decision persistence). **Simply re-running
  `paper-book-lifecycle-run` with the same `--as-of` is always safe** — it will not double-fill,
  double-reserve, or duplicate an exit decision, whether or not the prior run partially failed.
* If a failure recurs on retry, inspect the reason string (it is a plain Python exception
  message, not sanitized/truncated) and the book's own state via `paper-book-show` /
  `paper-book-reconcile` (Milestone 8 commands) before retrying again.
* Do not hand-edit any `paper_book_*` table to "fix" a failure — every table in this subsystem
  is append-only or immutable-after-insert by DB trigger; a direct mutation will either be
  rejected or will desynchronize the reconciliation. If historical state genuinely needs
  correcting, use the explicit, audited `paper-book-exit-request` path or a Milestone 8
  `CASH_ADJUSTMENT` (operator + reason required), never a raw `UPDATE`.

## 6. Safety boundaries

* No live trading, no Alpaca/Robinhood order mutation, no external paper broker, no `--live`
  flag exists anywhere in this CLI.
* No margin, shorting, or options — long-only, full-position exits only.
* No environment variable can enable lifecycle processing or exits — only
  `config/paper_books.yaml` can, and both default `false`.
* Readiness and the soak report are advisory-only; neither ever activates recurring processing,
  and the soak report never declares a baseline-vs-enhanced "winner."
* Cross-book state is structurally isolated (every table/query is `book_id`-scoped) — a
  manual exit request or an exit decision for one book can never touch the other's cash,
  positions, or orders.

## 7. Deferred (not built this session)

Unattended recurring activation, launchd installation, an external paper broker, a per-book
`paper_runtime` subprocess pool, partial fills, trailing stops, non-FIFO tax-lot selection,
live trading, and automated promotion all remain out of scope — see
`docs/milestones/milestone9-manual-paper-soak-and-lifecycle.md` Section 13 for the full list.
