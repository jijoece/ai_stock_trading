Implement a narrowly scoped follow-up in the existing trading-desk repository:

# Milestone 9.1 — Controlled paper-soak activation and readiness closure

Milestones 1–9 are complete for their defined scopes.

Current capability:

```text
manual scheduled research
→ isolated BASELINE/ENHANCED paper-book integration
→ pending-order processing
→ deterministic entry and exit fills
→ snapshots
→ reconciliation
→ metrics
→ soak reporting
→ advisory soak readiness
```

Milestone 9.1 must make this workflow operationally safe and convenient for controlled manual use.

Do not implement unattended recurring execution, launchd activation, external paper brokers, or live trading.

---

# Token-efficiency requirements

My Claude Code usage is limited. Optimize aggressively.

1. Use Pyright/LSP definitions, references, symbols, and call hierarchy before reading files.
2. Read only the specific symbols needed.
3. Do not reread Milestones 7–8 documentation.
4. Read only the required files listed below.
5. Keep the scratchpad short.
6. Do not produce long research narratives.
7. Run targeted tests during implementation.
8. Run the full main suite only:

   * once for baseline;
   * once at completion.
9. Run the paper-runtime suite only:

   * once for baseline;
   * once at completion.
10. Use:

```bash
pytest -q --tb=short
```

11. Do not print full passing-test lists.
12. Do not dump large JSON, database rows, or source files.
13. Do not make network or paid API calls.
14. Do not perform broad refactoring.
15. Stop once the acceptance criteria are met.
16. Do not commit or push.

---

# Required review

Read only:

```text
.claude/scratchpads/milestone9-progress.md
docs/milestones/milestone9-manual-paper-soak-and-lifecycle.md
docs/milestones/milestone8-1-scheduled-paper-book-integration.md
docs/milestones/milestone7-2-shadow-health-diagnostics.md
```

Inspect only relevant symbols in:

```text
src/trading_research/paper_books/lifecycle.py
src/trading_research/paper_books/cli_support.py
src/trading_research/paper_books/config.py
src/trading_research/paper_books/scheduled_integration.py

src/trading_research/shadow/health.py
src/trading_research/shadow/readiness.py
src/trading_research/shadow/pause.py
src/trading_research/shadow/alerts.py
src/trading_research/shadow/scheduler.py

src/trading_research/storage/paper_books_repositories.py
src/trading_research/cli.py

config/paper_books.yaml
config/shadow_operations.yaml
```

Use repository code as the source of truth.

---

# Scratchpad

Create:

```text
.claude/scratchpads/milestone9-1-progress.md
```

Use only:

```markdown
# Milestone 9.1 Progress

## Baseline
## Readiness inputs
## Manual workflow
## Clock correction
## Implementation
## Tests
## Documentation
## Safety review
## Known limitations
## Final status
```

Record summarized commands and results only.

Never include credentials, `.env`, raw model content, account identifiers, or chain-of-thought.

---

# Baseline

Run:

```bash
pytest tests/ -q --tb=short
```

Expected:

```text
1454 passed, 14 skipped
```

Then:

```bash
cd paper_runtime
pytest tests/ -q --tb=short
```

Expected:

```text
33 passed
```

Check Git status and preserve unrelated work.

---

# Hard boundaries

Do not:

* activate recurring execution;
* install or modify launchd;
* add external Alpaca or Robinhood order mutation;
* add live trading;
* add a `--live` flag;
* add margin, shorting, or options;
* make Claude or network calls;
* automatically clear a pause or kill state;
* automatically promote the enhanced arm;
* weaken shadow health thresholds;
* reduce readiness history minimums merely to pass;
* fabricate readiness data;
* treat missing metrics as zero;
* share state between books;
* modify `real_orders`;
* redesign the paper-book subsystem;
* implement partial fills or trailing stops;
* fix unrelated Milestone 7 backlog items.

---

# Primary objectives

Complete only these items:

1. Combine paper-soak readiness with shadow operational readiness.
2. Correct historical lifecycle CLI clock behavior.
3. Add one manually invoked end-to-end operator command.
4. Persist a bounded operator-run summary.
5. Keep all activation decisions advisory.
6. Do not activate a recurring schedule.

---

# 1. Define combined activation-readiness inputs

Trace and reuse authoritative persisted data for:

## Paper-soak readiness

```text
completed paper cycles
market days covered
lifecycle failures
book reconciliation
valuation completeness
enabled books
cross-book violations
```

## Shadow readiness

```text
current pause state
current kill state
latest health status
unexplained PAUSE_REQUIRED results
unresolved critical alerts
provider readiness
pricing readiness
minimum completed-cycle history
minimum real-provider-cycle history
```

Classify every input:

```text
AUTHORITATIVE
DERIVED
NOT_APPLICABLE
MISSING
```

Do not copy logic manually when an existing readiness function already supplies it.

---

# 2. Create combined controlled-soak readiness

Add a focused module or extend the existing paper-book readiness module.

Conceptual result:

```python
@dataclass(frozen=True)
class ControlledSoakReadinessResult:
    status: str
    reasons: tuple[str, ...]
    paper_soak_status: str
    shadow_activation_status: str
    checks: tuple[ReadinessCheck, ...]
    policy_version: str
```

Suggested statuses:

```text
NOT_READY_PAPER_SOAK
NOT_READY_SHADOW_PAUSED
NOT_READY_SHADOW_KILLED
NOT_READY_HEALTH_UNEXPLAINED
NOT_READY_CRITICAL_ALERTS
NOT_READY_PROVIDER_HISTORY
NOT_READY_RECONCILIATION
NOT_READY_VALUATION
READY_FOR_MANUAL_SOAK
READY_FOR_EXTENDED_MANUAL_SOAK
READY_FOR_RECURRING_ACTIVATION_REVIEW
```

`READY_FOR_RECURRING_ACTIVATION_REVIEW` is advisory only.

It must never enable or schedule anything.

---

# 3. Readiness rules

Fail closed in this order:

1. Shadow kill state is active.
2. Shadow pause state is not active/runnable.
3. An unexplained `PAUSE_REQUIRED` exists.
4. Unresolved critical operational alerts exist.
5. Paper-book reconciliation is not `MATCHED`.
6. Valuation is incomplete or point-in-time unsafe.
7. Lifecycle runs contain unresolved failures.
8. Cross-book violation exists.
9. Minimum completed paper cycles not met.
10. Minimum paper market days not met.
11. Minimum real-provider cycles not met.
12. Provider or pricing readiness is insufficient.
13. Otherwise return a manual-soak or activation-review advisory status.

Requirements:

* do not convert missing values to zero;
* do not treat fixture cycles as real-provider cycles;
* expose observed value, threshold, and source for each check;
* deterministic ordering;
* policy version persisted or included in output.

---

# 4. Cross-book violation signal

Determine whether Milestone 8/9 already persists authoritative cross-book reconciliation or isolation violations.

If authoritative records exist, use them.

If no persisted signal exists:

* do not hardcode `False`;
* represent the check as `MISSING` or `NOT_APPLICABLE`;
* prevent recurring-activation-review status when the signal is required but unavailable;
* document the gap.

Do not create a large new duplicate-detection subsystem in this milestone.

---

# 5. Correct lifecycle CLI clock semantics

Current lifecycle service behavior correctly anchors its default clock to `as_of`.

The CLI currently injects wall-clock time.

Correct the CLI so that:

```text
default lifecycle event timestamps = --as-of
```

Add an optional explicit switch only when useful:

```bash
--audit-time-now
```

Behavior:

```text
without --audit-time-now:
    processing clock = as_of

with --audit-time-now:
    audit timestamp = real current time
    market/decision effective time remains as_of
```

Do not let wall-clock audit time change:

* market-day calculations;
* order eligibility;
* price selection;
* holding-period calculation;
* snapshot as_of;
* exit-decision effective date.

Add tests for historical replay dates.

---

# 6. Add a single manual operator workflow

Create one CLI command such as:

```bash
python -m trading_research.cli paper-soak-run \
  --as-of <ISO-8601> \
  [--integrate-cycle-id <id>]...
```

The command must perform, in order:

```text
1. Validate paper-book and lifecycle configuration
2. Validate current shadow pause/kill state
3. Optionally integrate explicitly supplied cycle IDs
4. Run paper-book lifecycle
5. Reconcile both enabled books
6. Generate soak report
7. Generate combined readiness
8. Persist operator-run summary
9. Return sanitized JSON
```

Do not:

* run scheduled research automatically;
* call Claude;
* discover cycles implicitly;
* activate scheduling;
* clear pause state;
* hide lifecycle failures.

The operator must explicitly provide cycle IDs.

---

# 7. Operator-run persistence

Add additive storage only if no equivalent table exists.

Suggested table:

```text
paper_soak_operator_runs
```

Suggested fields:

```text
operator_run_id
as_of
requested_cycle_ids_json
lifecycle_run_id
baseline_reconciliation_status
enhanced_reconciliation_status
soak_report_status
controlled_readiness_status
failure_reasons_json
policy_version
created_at
```

Requirements:

* deterministic ID for the same `as_of` and cycle IDs;
* idempotent;
* sanitized;
* immutable;
* no raw model output;
* no credentials;
* no automatic retry loop.

---

# 8. Unresolved critical-alert handling

Use existing alert repositories and alert status semantics.

Define which alerts block controlled soak or activation review.

At minimum:

```text
unresolved CRITICAL alerts
```

must block `READY_FOR_RECURRING_ACTIVATION_REVIEW`.

Do not treat historical resolved alerts as active blockers.

Persist or explain:

```text
alert count
alert types
oldest unresolved timestamp
latest unresolved timestamp
```

Keep output bounded.

---

# 9. Real-provider history

Separate:

```text
fixture research cycles
real evidence-provider cycles
real Claude cycles
```

Use existing persisted provider/model metadata.

Do not infer “real” from token cost alone.

Add explicit counts to readiness output.

Do not perform real calls in this milestone.

---

# 10. CLI commands

Add:

```bash
python -m trading_research.cli paper-soak-run \
  --as-of <ISO-8601> \
  [--integrate-cycle-id <id>]...

python -m trading_research.cli paper-soak-readiness \
  --as-of <ISO-8601>
```

The existing commands remain unchanged:

```text
paper-book-lifecycle-run
paper-book-soak-report
paper-book-soak-readiness
```

Requirements:

* JSON output;
* deterministic ordering;
* disabled configuration fails closed;
* unknown cycle fails closed;
* no live mode;
* no network call;
* no recurring behavior;
* no raw Claude content.

---

# 11. Tests

Add targeted tests for:

## Combined readiness

* shadow killed;
* shadow paused;
* unexplained pause;
* critical unresolved alert;
* reconciliation mismatch;
* incomplete valuation;
* lifecycle failure;
* insufficient paper cycles;
* insufficient market days;
* insufficient real-provider history;
* manual-soak ready;
* extended-manual-soak ready;
* activation-review ready;
* no automatic activation.

## Clock behavior

* historical `as_of` uses `as_of` by default;
* `--audit-time-now` does not alter effective market time;
* no future-created order relative to historical lifecycle date;
* holding-period calculations remain based on `as_of`.

## Operator workflow

* explicit cycle integration;
* zero cycle IDs allowed for lifecycle-only day;
* unknown cycle fails closed;
* one book failure does not hide the other;
* summary persisted;
* replay is idempotent;
* sanitized output.

## Alerts and providers

* resolved critical alert does not block;
* unresolved critical alert blocks;
* fixture cycle excluded from real-provider count;
* real SEC/Claude metadata counted when already persisted;
* no provider call occurs.

---

# 12. Offline integration test

Add one deterministic test:

```text
persistent test database
→ fixture paper history across several days
→ persisted shadow health/readiness records
→ one resolved critical alert
→ no active pause or kill
→ explicit paper-soak-run
→ lifecycle processing
→ independent reconciliation
→ report
→ combined readiness
→ operator summary
→ replay same command
→ prove idempotency
→ prove no cross-book contamination
→ prove no live/network call
```

Add a second case where:

```text
unresolved CRITICAL alert
or active pause
→ readiness blocked
→ no activation side effect
```

Keep test data small.

---

# 13. Documentation

Create:

```text
docs/milestones/milestone9-1-controlled-soak-readiness.md
docs/runbooks/controlled-paper-soak.md
```

Update `docs/milestones/milestone9-manual-paper-soak-and-lifecycle.md` with a short pointer only.

Document:

* combined readiness inputs;
* blocking order;
* real-versus-fixture cycle counting;
* clock semantics;
* operator workflow;
* commands;
* persistent database;
* recovery from partial failure;
* advisory-only activation-review status;
* no automatic scheduling.

Do not rewrite prior milestone documents.

---

# Deferred items

Keep out of Milestone 9.1:

```text
unattended recurring activation
launchd installation
external paper broker
per-book paper_runtime subprocess pool
partial fills
trailing stops
live trading
automated promotion
remaining corporate actions
dividend entitlement correction
unsupported_claim_rate correction
full duplicate-prevention signal integration
```

---

# Required test execution

During development, run targeted tests only.

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

Milestone 9.1 is complete when:

1. Existing 1,454 tests still pass.
2. Existing 33 paper-runtime tests still pass.
3. Paper and shadow readiness are combined.
4. Kill state blocks readiness.
5. Pause state blocks readiness.
6. Unexplained health pauses block readiness.
7. Unresolved critical alerts block readiness.
8. Reconciliation mismatch blocks readiness.
9. Unsafe valuation blocks readiness.
10. Lifecycle failures block readiness.
11. Fixture cycles do not count as real-provider cycles.
12. Missing required data does not become zero.
13. Historical CLI lifecycle timestamps default to `as_of`.
14. Audit wall time cannot alter effective market time.
15. One manual command runs lifecycle, reconciliation, report, and readiness.
16. Cycle IDs are explicit.
17. Operator-run summary is persisted.
18. Reprocessing is idempotent.
19. Readiness remains advisory only.
20. No pause or kill state is automatically changed.
21. No recurring execution is activated.
22. No network or broker call occurs.
23. No live execution path exists.
24. Documentation matches implementation.
25. No commit or push occurs.

---

# Final response

Keep the final response concise.

Report only:

1. Baseline and final tests.
2. Files created and modified.
3. Combined readiness rules.
4. Clock correction.
5. Manual operator command.
6. Idempotency proof.
7. Alert/provider-history handling.
8. Current readiness result.
9. Safety confirmation.
10. Deferred items.

Include a compact table:

```text
Requirement → implementation → test
```

Use labels:

```text
CONTROLLED-MANUAL-SOAK
COMBINED-READINESS
POINT-IN-TIME-SAFE
IDEMPOTENT
PAPER-BOOK-ISOLATED
SHADOW-SAFETY-RESPECTED
ADVISORY-ONLY
LIVE-TRADING-NOT-IMPLEMENTED
RECURRING-DEPLOYMENT-NOT-ACTIVATED
```

Do not commit or push.
