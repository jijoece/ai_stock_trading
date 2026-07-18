# Milestone 10 — Controlled recurring local paper-trading scheduler

> Implementation status: implemented by `paper_books/recurring_scheduler.py`, the additive `paper_recurring_*` SQLite tables, the `paper-recurring-*` operator commands, and [the recurring local paper runbook](runbooks/recurring-local-paper-trading.md). The shipped configuration and example scheduling artifact remain disabled.

Work directly in the existing `ai_stock_trading` repository.

Milestones through 9.3 should already provide:

* isolated BASELINE and ENHANCED local paper books;
* deterministic paper-book entry and exit processing;
* pending-order lifecycle;
* snapshots, reconciliation, and metrics;
* successful-provider provenance;
* complete readiness diagnostics;
* state-sensitive cross-book verification;
* critical-alert operations;
* controlled multi-day soak campaigns;
* immutable activation-review reports.

Milestone 10 must add controlled recurring execution for the existing **local simulated paper-trading workflow only**.

Do not add an external paper broker or live trading.

---

# Objective

Implement an explicitly activated local recurring scheduler:

```text
human-approved activation review
→ activation request
→ explicit activation
→ scheduled invocation
→ singleton lease
→ safety-gate evaluation
→ bounded explicit cycle queue
→ controlled paper-soak lifecycle
→ cross-book verification
→ readiness evaluation
→ scheduler-run persistence
→ lease release
```

The scheduler must never:

* run research automatically;
* invoke Claude;
* call evidence providers;
* discover arbitrary cycles;
* place external broker orders;
* activate itself.

---

# Working mode

You are a coding agent with direct repository access.

Use repository tools to:

* inspect symbols and references;
* edit files;
* add additive SQLite schema;
* run targeted tests;
* run the final test suites.

Implement the code directly.

Do not return only a hypothetical patch.

Do not commit or push unless explicitly instructed after review.

---

# Token-efficiency requirements

My Codex usage is limited. Work efficiently.

1. Confirm Milestone 9.3 exists before implementing.
2. Use symbol search and references before reading full files.
3. Read only directly relevant files.
4. Do not broadly reread prior milestone documents.
5. Keep the scratchpad concise.
6. Do not produce a long architecture investigation.
7. Reuse existing lifecycle, readiness, lease, calendar, and persistence functions.
8. Run targeted tests during implementation.
9. Run the full main suite only:

   * once for baseline;
   * once at completion.
10. Run the paper-runtime suite only:

* once for baseline;
* once at completion.

11. Use:

```bash
pytest -q --tb=short
```

12. Do not print complete passing-test lists.
13. Do not dump large source files, database tables, or JSON output.
14. Do not make network calls.
15. Avoid broad refactoring.
16. Stop once the acceptance criteria are met.

---

# Prerequisite verification

Confirm the current repository contains the Milestone 9.3 equivalents of:

```text
src/trading_research/paper_books/soak_campaign.py
src/trading_research/paper_books/controlled_soak_readiness.py
src/trading_research/paper_books/cross_book_verification.py
src/trading_research/research/provider_provenance.py
docs/milestones/milestone9-3-evidence-integrity-and-soak-campaign.md
```

Confirm that Milestone 9.3 provides:

* an immutable activation-review record;
* a final recommendation vocabulary including:
  `READY_FOR_RECURRING_ACTIVATION_REVIEW`;
* successful real-provider-cycle counts;
* complete `all_failed_checks`;
* persisted cross-book verification IDs;
* a service-level controlled-soak function reusable outside the CLI.

When names differ, trace and use the actual repository symbols.

When a required prerequisite is genuinely missing, implement only the smallest compatible seam needed for Milestone 10 and document it. Do not recreate Milestone 9.3.

---

# Initial files to inspect

Read only the relevant symbols in:

```text
src/trading_research/paper_books/soak_campaign.py
src/trading_research/paper_books/controlled_soak_readiness.py
src/trading_research/paper_books/cross_book_verification.py
src/trading_research/paper_books/lifecycle.py
src/trading_research/paper_books/cli_support.py
src/trading_research/paper_books/config.py

src/trading_research/shadow/scheduler.py
src/trading_research/shadow/lease.py
src/trading_research/shadow/pause.py
src/trading_research/shadow/readiness.py

src/trading_research/evaluation/market_calendar.py

src/trading_research/storage/paper_books_schema.py
src/trading_research/storage/paper_books_repositories.py
src/trading_research/storage/shadow_operations_repositories.py

src/trading_research/cli.py
config/paper_books.yaml
```

Read:

```text
docs/milestones/milestone9-3-evidence-integrity-and-soak-campaign.md
docs/runbooks/paper-soak-campaign.md
```

Use the current repository as the source of truth.

---

# Scratchpad

Create:

```text
.claude/scratchpads/milestone10-progress.md
```

Use only:

```markdown
# Milestone 10 Progress

## Baseline
## Prerequisite verification
## Activation state
## Queue and lease design
## Safety gates
## Scheduler implementation
## Tests
## Documentation
## Safety review
## Known limitations
## Final status
```

Record only summarized commands, decisions, and test results.

Do not include credentials, raw model output, private reasoning, or large source excerpts.

---

# Baseline

Run:

```bash
pytest tests/ -q --tb=short
```

Record the actual result.

Then:

```bash
cd paper_runtime
pytest tests/ -q --tb=short
```

Record the actual result.

Return to the repository root.

Check Git status and preserve unrelated work.

---

# Hard boundaries

Do not:

* add live trading;
* add an external paper-broker adapter;
* add Alpaca or Robinhood order submission;
* add a `--live` option;
* add margin, shorting, or options;
* call Claude;
* call evidence, market-data, SEC, Reddit, or news providers;
* run scheduled research automatically;
* infer cycles by scanning every unprocessed cycle;
* activate recurring execution from configuration alone;
* automatically approve an activation request;
* automatically resume after a safety pause;
* clear kill or pause state;
* automatically resolve alerts;
* weaken readiness thresholds;
* bypass activation-review evidence;
* automatically promote the enhanced arm;
* modify `real_orders`;
* install launchd, cron, or a system daemon;
* embed credentials in scheduling files;
* allow simultaneous scheduler mutations;
* share state between paper books.

---

# Part 1 — Recurring configuration

Add an optional disabled-by-default section under `paper_books`, conceptually:

```yaml
paper_books:
  recurring:
    enabled: false

    schedule:
      timezone: America/Los_Angeles
      market_days_only: true
      hour: 13
      minute: 30

    maximum_cycles_per_run: 5
    maximum_runtime_seconds: 900
    lease_ttl_seconds: 1200
    activation_review_max_age_market_days: 10
    pause_on_safety_block: true
```

Requirements:

* `enabled: false` by default;
* configuration alone cannot activate execution;
* strict unknown-key rejection;
* validate timezone;
* validate hour and minute;
* positive bounded integer validation;
* no environment variable enables recurring execution;
* include recurring configuration in the scheduler configuration hash;
* existing configurations without `recurring` remain valid and disabled.

---

# Part 2 — Activation state machine

Implement an append-only activation-event model.

Required states:

```text
INACTIVE
ACTIVATION_REQUESTED
ACTIVE
PAUSED_BY_SAFETY
DEACTIVATED
```

Suggested event types:

```text
ACTIVATION_REQUESTED
ACTIVATED
SAFETY_PAUSED
DEACTIVATED
```

Every event must include:

```text
activation_event_id
previous_state
new_state
activation_review_id
campaign_id
operator
reason
requested_schedule_json
created_at
policy_version
```

Requirements:

* append-only and immutable;
* deterministic or safely unique event IDs;
* current state derived from the latest valid event;
* configuration cannot create an activation event;
* first activation audit trail cannot be overwritten;
* deactivation creates a new event;
* safety pause creates a new event;
* reactivation after `PAUSED_BY_SAFETY` requires another explicit operator activation;
* never automatically transition from `PAUSED_BY_SAFETY` to `ACTIVE`.

---

# Part 3 — Two-step activation

Implement:

```text
activation request
→ explicit activation
```

## Activation request

Must require:

```text
activation_review_id
operator
reason
requested schedule
```

Validate that the referenced immutable activation review:

* exists;
* belongs to a completed campaign;
* has final recommendation:
  `READY_FOR_RECURRING_ACTIVATION_REVIEW`;
* has no unresolved blocking checks;
* references a successful or sufficient cross-book verification;
* satisfies successful real-provider history;
* is not older than `activation_review_max_age_market_days`;
* has not already been superseded by newer blocking evidence.

A request alone must not activate execution.

## Activation approval

Must require:

```text
request_event_id
operator
```

Requirements:

* request must exist;
* request must still be valid;
* request must not already be activated or rejected;
* activation review must be revalidated at approval time;
* operator and timestamp are audited;
* activation creates a separate immutable event;
* never infer approval from matching operator names.

A two-person rule is not required, but the implementation may support one without making it mandatory.

---

# Part 4 — Explicit cycle queue

Recurring execution must process only explicitly queued persisted cycles.

Add an append-only or safely stateful queue model such as:

```text
paper_recurring_cycle_queue
```

Suggested fields:

```text
queue_item_id
cycle_id
status
enqueued_by
enqueue_reason
enqueued_at
claimed_by_run_id
claimed_at
processed_operator_run_id
processed_at
failure_reason
created_at
```

Required statuses:

```text
QUEUED
CLAIMED
PROCESSED
FAILED
CANCELLED
```

Requirements:

* cycle must exist;
* cycle must be completed or otherwise explicitly eligible;
* recommendation and evidence state must be frozen;
* duplicate active queue entries for the same cycle rejected or resolved idempotently;
* queue ordering deterministic;
* processing bounded by `maximum_cycles_per_run`;
* no implicit database-wide discovery;
* queue claims must be atomic;
* failed items remain auditable;
* processed items are never processed again automatically;
* cancellation requires operator and reason;
* queue processing remains idempotent after process restart.

---

# Part 5 — Singleton lease

Reuse the existing shadow scheduler lease implementation when its semantics fit.

Otherwise add the smallest paper-recurring-specific lease extension.

Required fields:

```text
lease_name
owner_id
acquired_at
heartbeat_at
expires_at
scheduler_run_id
```

Requirements:

* only one recurring paper scheduler may mutate state at once;
* acquisition is atomic;
* active lease conflict returns a bounded skipped result;
* stale lease can be recovered after expiry;
* owner ID required;
* heartbeat supported for bounded long runs;
* release in `finally`;
* a process must not release another owner’s active lease;
* lease acquisition alone must not mutate paper books.

---

# Part 6 — Due-slot calculation

Use the configured IANA timezone and existing market calendar.

Create deterministic intended schedule identities such as:

```text
paper-recurring:<local-market-date>:<hour>:<minute>:<config-hash>
```

Requirements:

* timezone-aware;
* daylight-saving-safe;
* market-days-only option supported;
* no network calendar lookup;
* use existing deterministic market-day calendar;
* repeated invocation for the same intended slot is idempotent;
* invocation before the due time returns `SKIPPED_NOT_DUE`;
* invocation for an already completed slot returns `SKIPPED_ALREADY_COMPLETED`;
* invocation after a bounded acceptable lateness policy follows the documented behavior;
* do not silently process several missed historical slots in one run.

One invocation processes at most one intended schedule slot.

---

# Part 7 — Pre-run safety gates

Before claiming cycles or mutating paper books, evaluate every gate.

Required gates:

1. recurring configuration enabled;
2. activation state `ACTIVE`;
3. activation review still valid and not stale;
4. shadow kill state not active;
5. shadow pause state runnable;
6. no unexplained `PAUSE_REQUIRED`;
7. no unresolved blocking CRITICAL alert;
8. latest controlled readiness not in a hard-blocking state;
9. latest successful-provider history still sufficient;
10. latest cross-book verification not `FAILED`;
11. cross-book verification not stale relative to newer paper evidence;
12. persistent database available;
13. scheduler slot due;
14. singleton lease acquired.

Return all failed gates while preserving one deterministic primary gate.

No paper lifecycle or queue claim may occur when a hard pre-run gate fails.

When configured:

```yaml
pause_on_safety_block: true
```

a safety failure while currently `ACTIVE` may append a `PAUSED_BY_SAFETY` event.

It must never clear the underlying shadow pause or kill state.

---

# Part 8 — Scheduler service

Create a focused module:

```text
src/trading_research/paper_books/recurring_scheduler.py
```

Conceptual entry point:

```python
run_recurring_paper_scheduler(
    conn,
    *,
    now,
    paper_books_config,
    shadow_config,
    owner_id,
    price_provider=None,
    audit_clock=None,
) -> RecurringPaperSchedulerResult
```

The service must not load CLI configuration internally when typed configuration objects are supplied.

Processing order:

```text
validate recurring configuration
→ derive intended slot
→ load activation state
→ evaluate pre-lease non-mutating gates
→ acquire singleton lease
→ re-evaluate safety gates under lease
→ create scheduler-run record
→ claim bounded queue items
→ call existing controlled-soak service
→ run lifecycle even when queue is empty
→ persist cross-book verification
→ evaluate full controlled readiness
→ finalize queue items
→ persist scheduler result
→ heartbeat where needed
→ release lease
```

Use the existing service-level controlled-soak workflow from Milestone 9.3.

Do not invoke CLI functions from the scheduler service.

---

# Part 9 — Lifecycle-only recurring days

The scheduler must support a due market day with no queued cycle.

On such a day it should still:

* process pending orders;
* evaluate exits;
* resolve eligible simulated fills;
* create snapshots;
* reconcile both books;
* compute metrics;
* run cross-book verification;
* evaluate readiness;
* persist the recurring scheduler result.

This is not an error.

The run should record:

```text
processed_cycle_ids = []
lifecycle_only = true
```

---

# Part 10 — Scheduler persistence

Add additive tables when equivalent storage does not exist:

```text
paper_recurring_activation_events
paper_recurring_cycle_queue
paper_recurring_scheduler_runs
paper_recurring_scheduler_leases
```

A scheduler-run row should include:

```text
scheduler_run_id
intended_schedule_id
intended_at
started_at
ended_at
owner_id
lease_name
activation_event_id
activation_review_id
queue_item_ids
requested_cycle_ids
processed_cycle_ids
operator_run_id
lifecycle_run_id
cross_book_verification_id
cross_book_verification_status
controlled_readiness_status
all_failed_checks
lifecycle_only
status
failure_reasons
config_hash
policy_version
created_at
```

Required statuses:

```text
COMPLETED
COMPLETED_WITH_WARNINGS
SKIPPED_INACTIVE
SKIPPED_NOT_DUE
SKIPPED_ALREADY_COMPLETED
SKIPPED_LEASE_HELD
BLOCKED_SAFETY
FAILED
```

Requirements:

* deterministic scheduler-run ID for the intended slot;
* immutable final evidence;
* retry safe;
* no duplicate completed run for one intended slot;
* no raw model output;
* no credentials;
* bounded failure output.

When a process crashes after creating a run but before completion, a later invocation must recover safely without blindly duplicating lifecycle mutations.

---

# Part 11 — Queue completion semantics

Mark a queue item `PROCESSED` only when the controlled-soak result confirms its cycle was integrated or already idempotently integrated.

Do not mark it processed merely because it was claimed.

When one cycle fails:

* record its failure;
* do not hide successful cycles;
* follow the existing continue-on-failure policy;
* preserve retryability when safe;
* avoid duplicate paper orders.

When the controlled-soak workflow fails before any integration:

* release or fail the queue claims deterministically;
* do not strand items indefinitely in `CLAIMED`.

---

# Part 12 — Safety-pause behavior

When a hard safety gate is triggered during or after a run:

* persist the scheduler result;
* preserve lifecycle and verification evidence already produced;
* append `PAUSED_BY_SAFETY` when configured;
* stop future recurring runs;
* require explicit operator reactivation.

Examples:

```text
cross-book verification FAILED
reconciliation mismatch
unresolved lifecycle failure
new unresolved CRITICAL alert
unsafe valuation
shadow kill or pause
```

Do not automatically resolve the incident.

Do not automatically retry continuously.

---

# Part 13 — Operator CLI

Add:

```bash
python -m trading_research.cli paper-recurring-request-activation \
  --activation-review-id <id> \
  --operator <name> \
  --reason "<reason>"

python -m trading_research.cli paper-recurring-activate \
  --request-event-id <id> \
  --operator <name>

python -m trading_research.cli paper-recurring-deactivate \
  --operator <name> \
  --reason "<reason>"

python -m trading_research.cli paper-recurring-enqueue-cycle \
  --cycle-id <id> \
  --operator <name> \
  --reason "<reason>"

python -m trading_research.cli paper-recurring-cancel-cycle \
  --queue-item-id <id> \
  --operator <name> \
  --reason "<reason>"

python -m trading_research.cli paper-recurring-run-once \
  --now <ISO-8601> \
  --owner-id <id>

python -m trading_research.cli paper-recurring-status

python -m trading_research.cli paper-recurring-queue-list \
  [--status QUEUED]
```

Requirements:

* structured bounded JSON;
* deterministic ordering;
* required operator and reason validation;
* no credentials;
* no network calls;
* no implicit activation;
* no live mode;
* command errors return nonzero according to existing CLI conventions.

---

# Part 14 — Inert scheduling artifact

Create an example scheduling artifact only when useful:

```text
deploy/launchd/com.ai-stock-trading.paper-recurring.example.plist
```

Requirements:

* example only;
* never installed by tests or commands;
* disabled/inert until copied and edited manually;
* invokes only:

```text
paper-recurring-run-once
```

* no API keys;
* no account credentials;
* no broker secrets;
* no hardcoded personal filesystem paths where a placeholder works;
* documentation must clearly state that installation is out of scope.

Do not add an automatic installer.

---

# Part 15 — Tests

Add focused offline tests.

## Configuration

* recurring section absent means disabled;
* recurring section explicitly disabled;
* unknown keys rejected;
* invalid timezone rejected;
* invalid hour/minute rejected;
* invalid bounds rejected.

## Activation

* configuration alone does not activate;
* missing activation review fails;
* non-ready review fails;
* stale review fails;
* activation request succeeds;
* request alone remains inactive;
* explicit activation succeeds;
* duplicate activation is idempotent or rejected safely;
* deactivation succeeds;
* safety pause requires explicit reactivation;
* activation audit history immutable.

## Queue

* completed eligible cycle enqueues;
* unknown cycle rejected;
* duplicate queue entry rejected or idempotent;
* deterministic ordering;
* bounded claim count;
* cancellation audited;
* processed item not reprocessed;
* failed claim recovery.

## Lease

* successful acquisition;
* active conflict;
* stale recovery;
* wrong-owner release rejected;
* lease released after success;
* lease released after exception.

## Scheduler

* disabled configuration;
* inactive activation state;
* not due;
* non-market day;
* already completed slot;
* lease conflict;
* lifecycle-only day;
* explicit queued-cycle processing;
* bounded queue processing;
* idempotent duplicate invocation;
* one intended slot per invocation;
* no research or provider calls;
* no live execution.

## Safety gates

* kill blocks before mutation;
* pause blocks before mutation;
* unresolved CRITICAL alert blocks;
* unexplained health pause blocks;
* stale activation review blocks;
* insufficient successful-provider history blocks;
* failed cross-book verification blocks;
* stale cross-book verification blocks;
* reconciliation mismatch blocks;
* unsafe valuation blocks;
* all simultaneous failed gates returned;
* deterministic primary gate;
* safety block appends `PAUSED_BY_SAFETY`;
* no automatic reactivation.

## Recovery

* crash after queue claim does not strand items permanently;
* crash after lifecycle persistence does not duplicate orders;
* repeated intended slot does not create duplicate completed runs;
* one-book failure remains isolated and visible.

---

# Part 16 — Offline integration test

Add one deterministic integration test:

```text
persistent database
→ completed Milestone 9.3 campaign
→ READY_FOR_RECURRING_ACTIVATION_REVIEW
→ explicit activation request
→ explicit activation
→ enqueue two completed frozen cycles
→ invoke due scheduler slot
→ lease acquired
→ bounded cycles processed
→ paper lifecycle
→ cross-book verification PASSED
→ controlled readiness evaluated
→ scheduler run COMPLETED
→ queue items PROCESSED
→ replay same slot
→ SKIPPED_ALREADY_COMPLETED
→ no duplicate orders, fills, lifecycle runs, or queue processing
→ no network or live execution
```

Add a second integration case:

```text
active recurring state
→ unresolved CRITICAL alert or FAILED cross-book verification
→ no paper mutation
→ scheduler run BLOCKED_SAFETY
→ activation transitions to PAUSED_BY_SAFETY
→ later invocation remains inactive
→ explicit reactivation required
```

Add a third case:

```text
due market day
→ no queued cycles
→ lifecycle-only processing succeeds
→ snapshots/reconciliation/verification/readiness persisted
```

---

# Part 17 — Documentation

Create:

```text
docs/milestones/milestone10-controlled-recurring-local-paper.md
docs/runbooks/recurring-local-paper-trading.md
```

Update Milestone 9.3 documentation with a short pointer only.

Document:

* recurring execution remains local simulated paper only;
* activation state machine;
* two-step activation;
* activation-review requirements;
* queue semantics;
* due-slot calculation;
* market-day behavior;
* singleton lease;
* safety-gate order;
* lifecycle-only days;
* safety-pause behavior;
* manual reactivation;
* crash recovery;
* operator CLI;
* inert launchd example;
* no external or live execution.

Do not rewrite older milestone documents.

---

# Deferred items

Keep out of Milestone 10:

```text
external Alpaca paper broker
Robinhood trading
live broker execution
broker account reconciliation
automatic research/provider invocation
automatic cycle discovery
automatic enhanced-arm promotion
partial fills beyond the local simulator’s current behavior
trailing stops
remaining corporate actions
dividend entitlement correction
automatic launchd installation
distributed scheduler
multi-host lease service
web dashboard
notification delivery redesign
```

---

# Required final tests

During implementation, run targeted tests only.

At completion run:

```bash
pytest tests/ -q --tb=short
```

Then:

```bash
cd paper_runtime
pytest tests/ -q --tb=short
```

Do not run real or network tests.

---

# Acceptance criteria

Milestone 10 is complete when:

1. Existing tests remain passing.
2. Existing paper-runtime tests remain passing.
3. Recurring execution ships disabled.
4. Configuration alone cannot activate it.
5. A ready Milestone 9.3 activation review is required.
6. Activation uses a two-step audited workflow.
7. Activation history is immutable.
8. Safety pause requires explicit reactivation.
9. Only explicitly queued cycles are processed.
10. Queue processing is bounded.
11. Lifecycle-only recurring days work.
12. Market-day and timezone calculation are deterministic.
13. One invocation processes at most one intended slot.
14. Singleton lease prevents concurrent mutation.
15. Stale leases can recover safely.
16. Kill and pause states block before paper mutation.
17. Unresolved critical alerts block.
18. Failed or stale cross-book verification blocks.
19. Insufficient successful-provider history blocks.
20. Readiness returns all failed gates.
21. Safety blocks can append `PAUSED_BY_SAFETY`.
22. Scheduler-run evidence is persisted.
23. Queue items are not falsely marked processed.
24. Crash recovery does not duplicate orders or fills.
25. Replaying a completed slot is idempotent.
26. No research or provider call occurs.
27. No external broker call occurs.
28. No live-trading path exists.
29. No scheduler artifact is automatically installed.
30. Documentation matches implementation.
31. No commit or push occurs unless explicitly requested.

---

## Milestone 11 external-paper boundary

Milestone 10 remains a local-simulation scheduler. When Milestone 11 marks a
book as externally enabled, scheduled integration persists its approved intent
in `paper_external_submission_queue` with
`AWAITING_OPERATOR_EXTERNAL_SUBMISSION`. The scheduler and lifecycle never
invoke `SUBMIT_LIMIT_ORDER` or `CANCEL_ORDER`. A human must later run the
separate external account check, preview, and submit commands. This preserves
the activation, queue, lease, and scheduler-run evidence described above while
preventing automatic Alpaca paper-account mutation.

---

# Final response

Keep the final response concise.

Report only:

1. Baseline and final tests.
2. Files created and modified.
3. Activation state machine.
4. Two-step activation validation.
5. Queue behavior.
6. Lease and due-slot behavior.
7. Scheduler processing order.
8. Safety-gate behavior.
9. Crash-recovery and idempotency proof.
10. CLI commands.
11. Current recurring activation state.
12. Safety confirmation.
13. Deferred items.

Include a compact table:

```text
Requirement → implementation → test
```

Use labels:

```text
CONTROLLED-RECURRING-PAPER
EXPLICIT-ACTIVATION
TWO-STEP-AUDIT
EXPLICIT-CYCLE-QUEUE
SINGLETON-LEASED
MARKET-DAY-AWARE
SAFETY-PAUSE-ENFORCED
POINT-IN-TIME-SAFE
IDEMPOTENT
PAPER-BOOK-ISOLATED
LOCAL-SIMULATION-ONLY
LIVE-TRADING-NOT-IMPLEMENTED
EXTERNAL-BROKER-NOT-INTEGRATED
```

Do not commit or push.
