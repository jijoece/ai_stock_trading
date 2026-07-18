# Milestone 9.1 — Controlled paper-soak activation and readiness closure

**Milestone 9.2 pointer:** the cross-book violation signal this document's Section 4 records as a
permanent, deliberate `MISSING` gap, and the cost-based real-provider-cycle counting described in
Section 9, are both closed — see `docs/milestones/milestone9-2-soak-evidence-integrity.md`. Everything else
below (combined-readiness inputs 1-13 other than those two, the blocking order, the manual
`paper-soak-run`/`paper-soak-readiness` commands, the lifecycle clock fix) is unchanged.

**Status:** Complete for this session's scope.
**Date:** 2026-07-14.
**Applies to:** `src/trading_research/paper_books/controlled_soak_readiness.py`,
`src/trading_research/paper_books/cli_support.py`,
`src/trading_research/paper_books/lifecycle.py` (clock default unchanged — the CLI's own
injection is what changed), `src/trading_research/cli.py`,
`src/trading_research/storage/paper_books_schema.py`,
`src/trading_research/storage/paper_books_repositories.py`,
`src/trading_research/storage/shadow_alerts_schema.py`,
`src/trading_research/storage/shadow_alerts_repositories.py`.

This document records the follow-up Milestone 9/9.1's own deferred list (and Milestone 7.2's
"Recommended next milestone" section) called for: combining the two readiness signals that
already exist in isolation, fixing a real historical-replay clock bug, and adding one bounded
manual operator command. It does **not** activate a recurring schedule, install launchd, add an
external paper broker, or touch live trading anywhere.

## 1. Combined activation-readiness inputs

| Input | Classification | Source |
|---|---|---|
| completed paper cycles | AUTHORITATIVE | `paper_book_lifecycle_runs` (via `evaluate_paper_soak_readiness`) |
| market days covered | AUTHORITATIVE | `paper_book_lifecycle_runs.as_of` distinct dates |
| lifecycle failures | AUTHORITATIVE | `paper_book_lifecycle_runs.failure_reasons_json` |
| book reconciliation | AUTHORITATIVE | `paper_book_reconciliations` (per book, latest up to `as_of`) |
| valuation completeness | DERIVED | `valuation.build_portfolio_snapshot(...).valuation_status` |
| enabled books | AUTHORITATIVE | `paper_books.yaml` config |
| cross-book violations | **MISSING** (documented gap — see Section 4) | none persisted |
| shadow pause state | AUTHORITATIVE | `shadow_pause_state` (current row) |
| shadow kill state | AUTHORITATIVE | `shadow_pause_state.state == KILLED` |
| latest health status | AUTHORITATIVE | `shadow_run_summaries.health_status` |
| unexplained PAUSE_REQUIRED | AUTHORITATIVE | `shadow_run_summaries` + `shadow_run_health_checks` (Milestone 7.2's own detector, reused) |
| unresolved critical alerts | AUTHORITATIVE (Milestone 9.1 additive column — see Section 8) | `shadow_alerts.resolved_at` |
| provider readiness | DERIVED | `shadow.readiness.build_readiness_report`'s provider category |
| pricing readiness | DERIVED | same report's `cost_usd_pricing` health check |
| minimum completed-cycle history | AUTHORITATIVE | `shadow.readiness.ReadinessThresholds.min_completed_cycles_for_ready` |
| minimum real-provider-cycle history | AUTHORITATIVE | same thresholds, `min_real_provider_cycles_for_ready` |

Nothing here is copied or re-derived: `paper_books/controlled_soak_readiness.py` calls Milestone
9's own `evaluate_paper_soak_readiness` (paper-side) and Milestone 7.2's own
`evaluate_activation_readiness` (shadow-side) wholesale, and only adds the two checks neither
already owns.

## 2. `ControlledSoakReadinessResult`

```python
@dataclass(frozen=True)
class ControlledSoakReadinessResult:
    status: str
    reasons: tuple[str, ...]
    paper_soak_status: str              # Milestone 9's own vocabulary, verbatim
    shadow_activation_status: str       # Milestone 7.2's own vocabulary, verbatim
    checks: tuple[ReadinessCheck, ...]   # name/classification/passed/observed/threshold/source/reason
    policy_version: str                 # "controlled-soak-readiness/v1"
```

`status` is always one of: `NOT_READY_PAPER_SOAK`, `NOT_READY_SHADOW_PAUSED`,
`NOT_READY_SHADOW_KILLED`, `NOT_READY_HEALTH_UNEXPLAINED`, `NOT_READY_CRITICAL_ALERTS`,
`NOT_READY_PROVIDER_HISTORY`, `NOT_READY_RECONCILIATION`, `NOT_READY_VALUATION`,
`READY_FOR_MANUAL_SOAK`, `READY_FOR_EXTENDED_MANUAL_SOAK`, `READY_FOR_RECURRING_ACTIVATION_REVIEW`
— enforced by `ControlledSoakReadinessResult.__post_init__` (fails closed on anything else).

## 3. Blocking order (fixed)

1. shadow kill state active → `NOT_READY_SHADOW_KILLED`
2. shadow pause state not ACTIVE → `NOT_READY_SHADOW_PAUSED`
3. unexplained `PAUSE_REQUIRED` exists → `NOT_READY_HEALTH_UNEXPLAINED`
4. unresolved CRITICAL alert(s) exist → `NOT_READY_CRITICAL_ALERTS`
5. paper-book reconciliation not `MATCHED` → `NOT_READY_RECONCILIATION`
6. valuation incomplete/unsafe → `NOT_READY_VALUATION`
7. lifecycle runs contain unresolved failures, or (9/10) minimum completed cycles / market days
   not met → `NOT_READY_PAPER_SOAK` (Milestone 9's own finer-grained result is preserved verbatim
   on `paper_soak_status`)
8. cross-book violation signal — `MISSING`, never blocks 1-7/9-13, only caps the final advisory
   tier (Section 4)
11/12. minimum real-provider-cycle history / provider or pricing readiness insufficient →
   `NOT_READY_PROVIDER_HISTORY`
13. otherwise: `READY_FOR_MANUAL_SOAK` / `READY_FOR_EXTENDED_MANUAL_SOAK` /
   `READY_FOR_RECURRING_ACTIVATION_REVIEW` (Section 4)

One nuance: Milestone 9's own `evaluate_paper_soak_readiness` internally checks
cycle-count/market-days *before* reconciliation/valuation/lifecycle-failures (its own,
already-shipped, unchanged order). This module maps whichever single result it returns onto the
Milestone 9.1 status above — when multiple paper-soak conditions are simultaneously false, the
one Milestone 9's own function reports first wins, not strictly the order listed above. Every
individual condition (tested in isolation, matching this milestone's own test list) surfaces
correctly; only the *co-occurring* case has Milestone-9-inherited precedence. Documented, not
silently different behavior.

`evaluate_activation_readiness`'s `ACTIVATION_NOT_READY_PAUSE_ACTIVE` status is also returned
for the most recent scheduler run's own `paper_reconciliation_mismatch`/
`duplicate_prevention_violation` safety flags (a second, unrelated trigger for the same
Milestone-7.2 status, alongside real pause/kill which this module already checks independently
at steps 1-2). That branch is mapped to `NOT_READY_RECONCILIATION` here — the closest existing
vocabulary entry — rather than inventing a new status for one shared upstream branch.

## 4. Cross-book violation signal — documented gap

No Milestone 8/9 module persists a dedicated cross-book reconciliation/isolation-violation
signal distinct from each book's own `reconcile_book` status (which is already a separate input,
above). `controlled_soak_readiness.py`'s `_CROSS_BOOK_SIGNAL_AVAILABLE = False` records this
honestly instead of fabricating a clear result:

* the check is always reported with `classification=MISSING`, `passed=None`;
* it never blocks manual or extended-manual soak;
* it structurally prevents `READY_FOR_RECURRING_ACTIVATION_REVIEW` — the best attainable result
  today is `READY_FOR_EXTENDED_MANUAL_SOAK` once every other gate clears and
  `market_days_covered >= 2x minimum_market_days`.

A future milestone that adds a real duplicate/cross-book detector (deferred here per this
milestone's own "do not create a large new duplicate-detection subsystem" boundary) should flip
this one constant — no other change to the ordering above is required.

## 5. Lifecycle CLI clock correction

`paper-book-lifecycle-run` previously always passed `clock=_utc_now` into
`run_paper_book_lifecycle`, even for a historical `--as-of`. `run_paper_book_lifecycle` itself
already anchors every timestamp it stamps (order/decision `created_at`) to `as_of` by default —
only the CLI's own override broke that for historical replay: a pending order's `created_at`
becomes real "now," which reads as created in the *future* relative to a later historical
`as_of`, and `market_days_held` raises when that order is reprocessed
(`tests/unit/test_paper_books_lifecycle_cli.py::
test_forced_wallclock_created_at_desyncs_a_later_historical_replay` reproduces this exact
failure by forcing the old behavior).

Fixed: both `paper-book-lifecycle-run` and the new `paper-soak-run` default to
`clock=None` (anchored to `--as-of`) and accept an optional `--audit-time-now` to opt back into a
real wall-clock `created_at` audit stamp — useful when `--as-of` is genuinely "today." Either
way, `--as-of` alone always drives market-day calculations, order eligibility, price selection,
holding-period calculation, snapshot `as_of`, and exit-decision effective date; `--audit-time-now`
only changes what gets written into `created_at`/audit metadata.

## 6. Manual operator command: `paper-soak-run`

```bash
python -m trading_research.cli paper-soak-run \
  --as-of <ISO-8601> \
  [--integrate-cycle-id <id>]... \
  [--audit-time-now]
```

Fixed order: validate `paper_books.enabled`/`paper_books.lifecycle.enabled` → validate shadow
pause/kill state (fails closed if either blocks) → optionally integrate explicitly supplied
cycle IDs → run the lifecycle (which already reconciles every enabled book itself — no second
reconciliation pass) → build the soak report → evaluate combined controlled-soak readiness →
persist the operator-run summary → return sanitized JSON. Cycle IDs are always explicit — an
empty list is a valid "lifecycle-only day"; an unknown cycle ID is recorded in
`failure_reasons`, never silently dropped, and never hides the other book's own processing.

```bash
python -m trading_research.cli paper-soak-readiness --as-of <ISO-8601>
```

Read-only wrapper around `evaluate_controlled_soak_readiness` — no lifecycle run, no persistence.

## 7. Persistent database

Both new commands reuse the existing `cfg.research_database_path` (`RESEARCH_DATABASE_PATH`),
exactly like every other `paper-book-*`/`paper-soak-*` command — no new database flag.

`paper_soak_operator_runs` (additive, immutable — no `UPDATE`/`DELETE` triggers permit
mutation): `operator_run_id` (deterministic hash of `as_of` + sorted requested cycle IDs, mirroring
`paper_book_lifecycle_runs.lifecycle_run_id`'s own convention), `as_of`, `requested_cycle_ids_json`,
`lifecycle_run_id`, `baseline_reconciliation_status`, `enhanced_reconciliation_status`,
`soak_report_status`, `controlled_readiness_status`, `failure_reasons_json`, `policy_version`,
`created_at`. Insert-or-ignore on `operator_run_id`: a replay for the identical `as_of`/cycle-ID
set never creates a duplicate row, but `paper_soak_run_cli` always recomputes every sub-step
fresh (lifecycle/report/readiness are each independently idempotent) — the returned JSON always
reflects current state, matching `paper_book_lifecycle_run_cli`'s own convention.

## 8. Unresolved critical-alert handling

`shadow_alerts` never persisted a resolved/acknowledged concept before this milestone — every
alert was permanently "open," with no way to distinguish a stale historical CRITICAL alert from
one still requiring attention (a genuine, pre-existing gap, not something Milestone 7 or 7.1/7.2
attempted). Added additively (`ALTER TABLE`, mirroring `storage/trading_schema.py`'s own
`_ensure_columns` upgrade pattern): `resolved_at`, `resolved_by`, `resolved_reason` (all nullable
— every alert raised before this column existed reads back as unresolved, never fabricated as
resolved). `shadow_alerts_repositories.py::resolve_alert` is idempotent (a second resolution
attempt on an already-resolved alert is a no-op that never overwrites the first resolution's
audit trail) and `list_alerts(..., unresolved_only=True)` is the query
`controlled_soak_readiness.py` uses. At minimum, an unresolved `CRITICAL` alert blocks
`paper-soak-run`/`paper-soak-readiness` entirely (`NOT_READY_CRITICAL_ALERTS`); a resolved one
never blocks, regardless of age.

## 9. Real-provider history

Reused verbatim from `shadow.readiness.build_readiness_report`: `real_provider_cycle_count` is
`shadow_run_summaries` rows with `cost_usd is not None and cost_usd > 0` — a real accrued cost,
never inferred from provider/model name alone, never conflated with a fixture research cycle
(which never accrues `cost_usd`). No real Claude/evidence-provider call was made in this
session; the count is read from whatever the persistent database already has.

## 10. Documentation and recovery from partial failure

A `paper-soak-run` invocation that fails mid-pipeline (e.g. `paper_books.lifecycle.enabled` is
false, or the shadow system is paused) returns an `"error"` key and persists nothing — there is
no partial operator-run row. A lifecycle-level failure (e.g. one book's own exception) does
*not* abort the command: it is recorded in `failure_reasons` on the returned/persisted summary,
and the other book's processing, the soak report, and the combined readiness evaluation all
still run and are still persisted/returned. Re-running the identical command is always safe
(every sub-step is independently idempotent).

## 11. Advisory-only, no scheduling

`READY_FOR_RECURRING_ACTIVATION_REVIEW` — like Milestone 9's and Milestone 7.2's own
same-shaped statuses — never enables, schedules, or activates anything by itself, and (per
Section 4) is not reachable today pending the cross-book signal. No pause/kill state is ever
automatically cleared. No recurring execution (launchd, cron, or otherwise) is installed or
invoked anywhere in this milestone.

## 12. Deferred (unchanged from Milestone 9's own list, plus this session's own)

Unattended recurring activation, launchd installation, an external paper broker, a per-book
`paper_runtime` subprocess pool, partial fills, trailing stops, live trading, automated
promotion, remaining corporate-action types, dividend record-date entitlement correction,
`unsupported_claim_rate`'s own denominator (Milestone 7.2 known limitation, still open), and a
full cross-book duplicate-prevention detection subsystem (Section 4 — a single documented
constant flip is the future implementation seam, not a redesign).
