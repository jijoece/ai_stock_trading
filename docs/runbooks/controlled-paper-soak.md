# Runbook: Controlled manual paper soak (Milestone 9.1)

Operator-facing procedure for the single, manual, end-to-end `paper-soak-run` command. See
`docs/milestones/milestone9-1-controlled-soak-readiness.md` for the full architecture record,
`docs/runbooks/manual-paper-trading-soak.md` for the Milestone 9 per-step commands this builds
on, and `docs/runbooks/paper-book-operations.md` for book-level basics.

**Before you start:** this ships fully disabled — `paper_books.enabled: false` and
`paper_books.lifecycle.enabled: false` in `config/paper_books.yaml`. Nothing here is automated:
every invocation below is a command you type and run yourself, once, on your own schedule. There
is no launchd job, no cron, no recurring scheduler anywhere in this milestone.

## 1. Daily manual workflow

```bash
python -m trading_research.cli paper-soak-run --as-of "2026-07-14T20:00:00Z"
```

This one command: validates config → validates the shadow pause/kill state (fails closed if
either is active) → runs the lifecycle for that date (pending orders, exits, snapshots,
reconciliation, metrics — reuses Milestone 9's own engine unchanged) → builds the soak report →
evaluates combined controlled-soak readiness → persists a bounded operator-run summary → prints
sanitized JSON.

To also integrate one or more real, already-persisted scheduled research cycles on that date:

```bash
python -m trading_research.cli paper-soak-run \
  --as-of "2026-07-14T20:00:00Z" \
  --integrate-cycle-id cycle-2026-07-14-001
```

An unrecognized cycle ID is recorded in the response's `failure_reasons` — it never silently
disappears, and it never prevents the rest of that day's lifecycle processing.

Omit `--integrate-cycle-id` entirely for a lifecycle-only day (no new entries — just pending-order
resolution, exits, snapshots, reconciliation, and reporting for existing positions).

## 2. Historical replay

For a historical `--as-of`, the command defaults to anchoring every timestamp it writes
(order/decision `created_at`) to that `--as-of` — never real wall-clock time. This is what makes
replaying a past date safe: a pending order created "today" during a replay of a months-old date
no longer reads as created in the future relative to a later replay date.

Add `--audit-time-now` only when you want a genuine "an operator actually ran this at such-and-such
real time" audit stamp (typically when `--as-of` is close to today anyway):

```bash
python -m trading_research.cli paper-soak-run \
  --as-of "2026-07-14T20:00:00Z" --audit-time-now
```

`--audit-time-now` never changes market-day calculations, order eligibility, price selection,
holding-period calculation, snapshot `as_of`, or exit-decision effective date — those are always
keyed to `--as-of`.

## 3. Checking readiness without running anything

```bash
python -m trading_research.cli paper-soak-readiness --as-of "2026-07-14T20:00:00Z"
```

Read-only — no lifecycle run, no database write. Inspect the `checks` array for the
observed/threshold/source of every input, and `paper_soak_status`/`shadow_activation_status` for
the two underlying sub-verdicts this combines.

## 4. Reading the result

`controlled_readiness.status` is always advisory:

* `NOT_READY_SHADOW_KILLED` / `NOT_READY_SHADOW_PAUSED` — resolve via the existing
  `shadow-resume`/`shadow-force-clear-kill` commands (never automatic).
* `NOT_READY_HEALTH_UNEXPLAINED` — investigate with `shadow-health-explain`.
* `NOT_READY_CRITICAL_ALERTS` — investigate via `shadow-alerts`; a resolved alert never blocks.
* `NOT_READY_PAPER_SOAK` / `NOT_READY_RECONCILIATION` / `NOT_READY_VALUATION` — investigate with
  `paper-book-soak-report`/`paper-book-reconcile`.
* `NOT_READY_PROVIDER_HISTORY` — accumulate more real shadow-cycle history, or check pricing via
  `shadow-readiness`.
* `READY_FOR_MANUAL_SOAK` / `READY_FOR_EXTENDED_MANUAL_SOAK` — keep running this command daily.
* `READY_FOR_RECURRING_ACTIVATION_REVIEW` — a human may now review recurring activation. This
  status never activates anything by itself, and is not reachable until a future milestone adds
  the cross-book violation signal this milestone deliberately leaves `MISSING` (see
  `docs/milestones/milestone9-1-controlled-soak-readiness.md` Section 4).

## 5. Recovering from a partial failure

If `paper-soak-run` returns an `"error"` key, nothing was persisted — just fix the underlying
issue (disabled config, active pause/kill) and re-run the identical command. If the command
succeeds but `failure_reasons` is non-empty (e.g. one book's own processing failed, or a
requested cycle ID was unknown), the operator-run summary still persists with those reasons
recorded verbatim — re-running the same `--as-of` is always safe (every sub-step is
independently idempotent) and will retry whatever didn't complete.

## 6. What this never does

* Never activates or installs a recurring/launchd schedule.
* Never calls a real external paper broker or Robinhood.
* Never clears a pause or kill state automatically.
* Never promotes the enhanced arm.
* Never runs scheduled research or calls Claude — cycle integration is always explicit and
  operates only on already-persisted, already-frozen cycles.
