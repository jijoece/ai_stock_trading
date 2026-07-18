# Recurring Local Paper Trading

Milestone 10 runs only the repository's local simulated BASELINE and ENHANCED paper books. It does not run research, call Claude or evidence providers, submit to an external paper broker, or expose a live mode.

## Preconditions

Recurring execution has independent controls. `paper_books.enabled`, `paper_books.lifecycle.enabled`, and `paper_books.recurring.enabled` must be true; execution must remain `local_simulated`; and activation must be `ACTIVE`. Configuration never creates an activation event.

The referenced immutable Milestone 9.3 review must reference a *campaign attempt* whose status is `COMPLETED_READY_FOR_REVIEW` (the campaign definition itself stays `DEFINED` forever, per Milestone 9.3.1's immutable-definition/resumable-attempt split — validation checks the attempt, never the definition row), belong to that same campaign, have matching manifest/config hashes, be the latest non-superseded review for its campaign, recommend `READY_FOR_RECURRING_ACTIVATION_REVIEW`, contain no final failed checks, contain a successful fresh cross-book verification, meet the configured successful-real-provider floor, remain within the configured market-day age, and have no newer blocking critical alert, reconciliation, or cross-book evidence. (Milestone 11.1 fixed a bug where this validation checked the immutable campaign-definition row instead of the attempt, which made real activation impossible — see the closure doc §1.)

## Two-step activation

Request activation, then approve that exact request separately:

```bash
python -m trading_research.cli paper-recurring-request-activation \
  --activation-review-id <id> --operator <name> --reason "<reason>"

python -m trading_research.cli paper-recurring-activate \
  --request-event-id <id> --operator <name>
```

The states are `INACTIVE`, `ACTIVATION_REQUESTED`, `ACTIVE`, `PAUSED_BY_SAFETY`, and `DEACTIVATED`. Events are append-only. Approval revalidates the review and requires the requested schedule to still match current configuration. A safety pause or deactivation requires a new request and explicit activation; there is no automatic resume.

Deactivate explicitly:

```bash
python -m trading_research.cli paper-recurring-deactivate \
  --operator <name> --reason "<reason>"
```

## Explicit queue

Only completed persisted cycles named by an operator can enter the queue:

```bash
python -m trading_research.cli paper-recurring-enqueue-cycle \
  --cycle-id <id> --operator <name> --reason "<reason>"

python -m trading_research.cli paper-recurring-cancel-cycle \
  --queue-item-id <id> --operator <name> --reason "<reason>"

python -m trading_research.cli paper-recurring-queue-list --status QUEUED
```

Enqueue records a hash of the completed cycle, symbol results, evidence linkage/provenance, snapshots, and frozen recommendations. A changed cycle fails before claim. Active duplicates are idempotent. A processed item cannot be re-enqueued. A failed or cancelled item may be explicitly re-enqueued with a `retry_of_queue_item_id` audit link. Claims are ordered by enqueue time and queue ID and bounded by `maximum_cycles_per_run`.

An item becomes `PROCESSED` only when the controlled-soak lifecycle reports that cycle in `processed_cycle_ids`. A service exception releases claims to `QUEUED`; an unconfirmed individual integration becomes `FAILED` and remains visible.

## Slot and lease behavior

The scheduler derives one identity for today's configured IANA-timezone slot:

```text
paper-recurring:<local-date>:<hour>:<minute>:<recurring-config-hash>
```

Before the local time it returns `SKIPPED_NOT_DUE`. With `market_days_only`, weekends and deterministic market holidays are not due. A late invocation is eligible only for the same local date; it never catches up older dates or scans for missed cycles. Daylight-saving behavior comes from `zoneinfo`.

One SQLite `BEGIN IMMEDIATE` lease named `paper-recurring-local` protects mutation. It records owner, run, heartbeat, and expiry. Active contention returns `SKIPPED_LEASE_HELD`; expired leases can be reclaimed. Only the current owner can heartbeat or release. Lease TTL must cover the configured maximum runtime, and release occurs in `finally`.

Invoke manually or from an operator-managed scheduler:

```bash
python -m trading_research.cli paper-recurring-run-once \
  --now <timezone-aware-ISO-8601> --owner-id <unique-owner>
```

## Safety-gate order

The deterministic gate order is:

1. recurring configuration enabled;
2. local simulated paper lifecycle enabled;
3. activation state active;
4. activation review current and valid;
5. shadow kill clear;
6. shadow pause runnable;
7. no unexplained `PAUSE_REQUIRED`;
8. no unresolved critical alert;
9. controlled readiness has no hard block;
10. successful-provider history sufficient;
11. latest cross-book verification not failed;
12. latest cross-book verification fresh;
13. SQLite available;
14. current slot due;
15. singleton lease acquired.

All failures are returned; the first in this order is primary. Gates are evaluated before and again under the lease. Queue claims and paper lifecycle do not occur on a pre-run failure. With `pause_on_safety_block`, an active scheduler appends `PAUSED_BY_SAFETY` but never clears shadow pause/kill state or resolves an alert.

Post-run cross-book failure, lifecycle failure, reconciliation mismatch, unsafe readiness, or runtime-bound breach persists the evidence and blocks future recurring work. Incident remediation and reactivation are manual.

## Lifecycle-only days and recovery

A due day with no queue still processes pending orders and exits, snapshots, reconciliation, metrics, cross-book verification, controlled readiness, and scheduler evidence. It records `processed_cycle_ids=[]` and `lifecycle_only=true`.

Scheduler run IDs are deterministic per intended slot. Runs begin as recoverable `RUNNING` rows and become immutable at their first terminal status. After a crash, the expired lease can be reclaimed, the same run resumes, abandoned claims are recovered, and the existing lifecycle/order idempotency keys prevent duplicate orders and fills. Replaying a terminal slot returns `SKIPPED_ALREADY_COMPLETED` without lifecycle mutation.

Inspect current state:

```bash
python -m trading_research.cli paper-recurring-status
python -m trading_research.cli paper-recurring-queue-list
```

## Scheduling artifact

`deploy/launchd/com.ai-stock-trading.paper-recurring.example.plist` is an inert example with placeholders. Copying, editing, loading, and operating it are explicitly out of scope. No command or test installs it, and it contains no credential or personal path.
