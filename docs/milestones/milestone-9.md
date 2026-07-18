Implement a focused follow-up in the existing trading-desk repository:

# Milestone 9 — Manual paper-trading soak and position lifecycle

Milestones 1–8.1 are complete for their defined scopes.

Current capability:

```text
persisted scheduled research cycle
→ manual paper-book integration
→ isolated BASELINE and ENHANCED books
→ deterministic risk
→ local simulated fills
→ book-specific reconciliation
```

Milestone 9 must add the operational lifecycle needed to run controlled, persistent paper-trading experiments over multiple market days.

Do not implement live trading, an external paper broker, or unattended recurring deployment.

---

# Token-efficiency rules

My Claude Code usage is near its limit.

1. Start with Pyright/LSP symbol navigation.
2. Read only relevant symbols, not entire large files.
3. Do not reread all Milestone 7 or 8 documentation.
4. Keep the scratchpad concise.
5. Do not produce long investigation narratives.
6. Run targeted tests during development.
7. Run the full main suite only:

   * once for baseline;
   * once at completion.
8. Use:

```bash
pytest -q --tb=short
```

9. Do not print complete passing-test lists.
10. Do not run real Claude, SEC, Alpaca, Reddit, broker, or other network calls.
11. Do not perform broad refactoring.
12. Stop when the acceptance criteria are satisfied.
13. Do not commit or push.

---

# Required review

Read only:

```text
.claude/scratchpads/milestone8-1-progress.md
docs/milestones/milestone8-1-scheduled-paper-book-integration.md
docs/milestones/milestone8-isolated-paper-portfolios.md
docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md
```

Inspect relevant symbols in:

```text
src/trading_research/paper_books/
src/trading_research/research/scheduled_cycle.py
src/trading_research/shadow/scheduler.py
src/trading_research/storage/paper_books_repositories.py
src/trading_research/cli.py

config/paper_books.yaml
```

Use the repository as the source of truth.

---

# Scratchpad

Create:

```text
.claude/scratchpads/milestone9-progress.md
```

Use only:

```markdown
# Milestone 9 Progress

## Baseline
## Existing lifecycle gaps
## Lifecycle design
## Exit policy
## Pending-order handling
## Daily processing
## Reporting
## Tests
## Documentation
## Safety review
## Known limitations
## Final status
```

Record summarized commands and results only.

Never record credentials, prompts, responses, account identifiers, `.env`, or chain-of-thought.

---

# Baseline

Run:

```bash
pytest tests/ -q --tb=short
```

Expected:

```text
1394 passed, 14 skipped
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

* add live trading;
* add Alpaca or Robinhood order mutation;
* add an external paper broker;
* add a `--live` flag;
* enable margin, shorting, or options;
* activate launchd;
* create unattended recurring execution;
* make paid API calls;
* modify `real_orders`;
* weaken evidence or recommendation validation;
* share state between paper books;
* let Claude decide exits;
* let Claude override portfolio risk;
* mutate frozen recommendations;
* fabricate prices or fills;
* automatically promote the enhanced arm;
* implement partial fills;
* redesign the existing paper-book subsystem;
* fix unrelated Milestone 7 backlog work.

---

# Primary objective

Build this manual lifecycle:

```text
manual scheduled research run
→ optional paper-book integration
→ process pending paper orders
→ evaluate existing positions
→ create deterministic exit intents
→ simulate eligible fills
→ update cash and positions
→ create portfolio snapshots
→ reconcile each book
→ calculate metrics
→ produce a daily soak report
```

All processing must be:

```text
OFFLINE
DETERMINISTIC
POINT-IN-TIME SAFE
BOOK ISOLATED
IDEMPOTENT
MANUALLY INVOKED
```

---

# 1. Add lifecycle configuration

Extend `config/paper_books.yaml` with a disabled-by-default section similar to:

```yaml
paper_books:
  lifecycle:
    enabled: false

    pending_orders:
      expire_after_market_days: 1

    exits:
      enabled: false
      stop_loss_percent: "0.08"
      profit_target_percent: "0.15"
      maximum_holding_market_days: 20
      exit_on_recommendation_reversal: true

    soak:
      minimum_completed_cycles: 10
      minimum_market_days: 5
```

Use existing configuration conventions.

Requirements:

* disabled by default;
* Decimal-safe values;
* unknown keys fail closed;
* invalid percentages fail closed;
* no environment variable enables lifecycle processing;
* configuration hash persisted;
* no live-broker configuration.

Do not assume these example threshold values are correct. Use conservative values consistent with existing project policy.

---

# 2. Define deterministic exit policy

Create a module such as:

```text
src/trading_research/paper_books/exit_policy.py
```

Add an immutable result equivalent to:

```python
@dataclass(frozen=True)
class PaperExitDecision:
    decision: str
    book_id: str
    symbol: str
    quantity: Decimal
    reference_price: Decimal | None
    reasons: tuple[str, ...]
    policy_version: str
```

Suggested decisions:

```text
HOLD
EXIT_STOP_LOSS
EXIT_PROFIT_TARGET
EXIT_MAX_HOLDING_PERIOD
EXIT_RECOMMENDATION_REVERSAL
EXIT_MANUAL_REQUEST
SKIPPED_MISSING_PRICE
SKIPPED_STALE_PRICE
SKIPPED_POINT_IN_TIME_UNSAFE
SKIPPED_NO_POSITION
```

Requirements:

* deterministic;
* long-only;
* full-position exits only for Milestone 9;
* no model-generated exit authority;
* missing/stale/unsafe prices never trigger a fabricated exit;
* same inputs produce the same decision;
* every decision persisted;
* policy version persisted.

---

# 3. Exit-rule semantics

Implement these independent rules:

## Stop loss

Compare the safe point-in-time reference price against the position’s deterministic cost basis.

Do not use a future or current quote for a historical lifecycle date.

## Profit target

Use the same point-in-time-safe valuation price.

## Maximum holding period

Use market days, not raw calendar days.

Use the repository’s existing market-calendar abstraction.

## Recommendation reversal

A reversal may occur only from a newer, frozen recommendation for the same symbol whose timestamp is:

```text
position_opened_at < recommendation.ts <= lifecycle_as_of
```

Define and document what recommendation status or side constitutes a reversal.

Do not treat a missing recommendation as a sell signal.

## Manual exit

Support an explicit, audited manual request containing:

```text
book_id
symbol
operator
reason
requested_at
idempotency_key
```

Do not provide an unrestricted SQL or arbitrary mutation interface.

---

# 4. Persist lifecycle decisions and manual requests

Add additive persistence only.

Suggested tables:

```text
paper_book_exit_decisions
paper_book_manual_exit_requests
paper_book_lifecycle_runs
paper_book_lifecycle_symbol_results
```

Use existing tables when equivalent storage already exists.

Requirements:

* deterministic IDs;
* book-specific keys;
* idempotent inserts;
* immutable decisions;
* immutable manual requests;
* operator and reason required for manual exits;
* no duplicate exit intent for the same decision;
* no cross-book request;
* no destructive migration.

---

# 5. Create SELL intents

Translate approved exit decisions into existing book-aware paper order intents.

Requirements:

* side `SELL`;
* limit order only;
* quantity cannot exceed available long position;
* full-position quantity for this milestone;
* stable ID includes:

  * book ID;
  * exit decision ID;
  * execution-policy version;
* no recommendation mutation;
* no baseline/enhanced crossover;
* no live destination;
* intent remains pending when simulation inputs are unavailable.

Reuse the existing Milestone 8 execution and position modules.

Do not create another fill simulator.

---

# 6. Pending-order lifecycle

Add deterministic pending-order processing.

Support:

```text
PENDING_SUBMISSION
CANCELLED
EXPIRED
FILLED
```

For every pending order:

1. Load its original book.
2. Verify it remains valid.
3. Obtain a point-in-time-safe market simulation input for lifecycle `as_of`.
4. Simulate a fill using the existing fill engine.
5. Expire it when its configured market-day age is exceeded.
6. Release reserved BUY cash when cancelled or expired.
7. Never reserve cash twice.
8. Never fill an expired or cancelled order.
9. Never apply a fill twice.

Do not implement partial fills.

---

# 7. Daily lifecycle service

Create a focused service such as:

```text
src/trading_research/paper_books/lifecycle.py
```

Conceptual entry point:

```python
run_paper_book_lifecycle(
    conn,
    *,
    as_of,
    paper_books_config,
    price_provider=None,
    integrate_cycle_ids=(),
) -> PaperBookLifecycleResult
```

Processing order:

```text
1. Validate lifecycle configuration
2. Optionally integrate explicitly supplied cycle IDs
3. Process existing pending orders
4. Evaluate exits for open positions
5. Persist exit decisions
6. Create eligible SELL intents
7. Simulate eligible fills
8. Create one snapshot per enabled book
9. Reconcile each enabled book
10. Compute metrics
11. Persist lifecycle-run summary
```

Requirements:

* manually invoked only;
* explicit `as_of`;
* no implicit current-time market lookup;
* one book failure does not mutate the other;
* bounded per-symbol outcomes;
* sanitized result;
* retrying the same lifecycle date is idempotent.

---

# 8. Persistent soak database

Add an explicit CLI database option if the existing CLI does not already provide one safely.

The soak process must use a persistent evaluation database rather than temporary test databases.

Do not hardcode a production path.

The lifecycle result should persist:

```text
lifecycle_run_id
as_of
processed_cycle_ids
books_processed
pending_orders_filled
pending_orders_expired
exit_decisions
exit_orders_created
exit_orders_filled
snapshot_ids
reconciliation_statuses
metrics_ids
failure reasons
```

---

# 9. Daily soak report

Add a read-only report containing, per book:

```text
cash available
cash reserved
net liquidation value
realized P&L
unrealized P&L
open positions
pending orders
orders filled today
exits triggered today
reconciliation status
valuation status
unvalued positions
maximum position concentration
completed experiment cycles
comparable cycles
promotion-evidence status
```

Also show differences between baseline and enhanced books when comparable.

Do not declare a winner automatically.

Suggested status:

```text
NOT_ENOUGH_HISTORY
RUNNING
ATTENTION_REQUIRED
READY_FOR_ACTIVATION_REVIEW
```

`READY_FOR_ACTIVATION_REVIEW` must not activate anything.

---

# 10. Soak-readiness policy

Add a deterministic readiness result.

Suggested checks:

```text
minimum completed cycles
minimum market days
minimum real or fixture research cycles
zero unexplained reconciliation mismatches
zero cross-book violations
acceptable unvalued-position rate
no unresolved critical lifecycle failures
both required books enabled
no active pause state when shadow controls apply
```

Suggested results:

```text
NOT_READY_INSUFFICIENT_CYCLES
NOT_READY_INSUFFICIENT_MARKET_DAYS
NOT_READY_RECONCILIATION
NOT_READY_VALUATION
NOT_READY_LIFECYCLE_FAILURES
READY_FOR_MORE_MANUAL_SOAK
READY_FOR_RECURRING_ACTIVATION_REVIEW
```

Do not automatically enable recurring processing.

---

# 11. Manual CLI commands

Add:

```bash
python -m trading_research.cli paper-book-lifecycle-run \
  --as-of <ISO-8601> \
  [--integrate-cycle-id <id>]...

python -m trading_research.cli paper-book-exit-request \
  --book-id <BASELINE|ENHANCED> \
  --symbol <symbol> \
  --reason "<reason>"

python -m trading_research.cli paper-book-soak-report \
  --as-of <ISO-8601>

python -m trading_research.cli paper-book-soak-readiness \
  --as-of <ISO-8601>
```

Requirements:

* lifecycle run fails closed when disabled;
* structured deterministic JSON;
* no raw model content;
* no network call;
* no live mode;
* explicit database/config conventions;
* unknown book, symbol, cycle, or date fails closed.

---

# 12. Optional scheduler hook

The existing scheduler has an optional paper-book integration hook.

Do not activate it automatically.

You may add an optional lifecycle hook parameter only when needed for future wiring, with:

```text
default = None
```

Requirements:

* zero behavior change for existing callers;
* lifecycle failure recorded separately from research/provider failure;
* no recurring scheduler configuration;
* no automatic invocation from launchd.

Manual CLI remains the primary Milestone 9 path.

---

# 13. Tests

Add focused tests for:

## Exit decisions

* stop loss;
* profit target;
* maximum holding market days;
* recommendation reversal;
* manual request;
* missing price;
* stale price;
* unsafe price;
* no position;
* deterministic decision ID.

## SELL safety

* cannot oversell;
* cannot sell another book’s position;
* limit orders only;
* duplicate decision creates no duplicate intent;
* duplicate fill creates no duplicate settlement.

## Pending orders

* marketable order fills;
* unmarketable order remains pending;
* expiration by market days;
* reservation released once;
* cancelled/expired order cannot fill.

## Lifecycle

* processing order;
* snapshots created;
* reconciliation performed;
* metrics persisted;
* one book failure isolated;
* same run repeated idempotently;
* explicit cycle integration only.

## Reporting and readiness

* insufficient cycles;
* insufficient market days;
* reconciliation mismatch;
* unsafe valuation;
* lifecycle failure;
* manual-soak ready;
* activation-review ready;
* no automatic activation.

## CLI

* disabled lifecycle;
* valid lifecycle run;
* manual exit request;
* soak report;
* readiness;
* sanitized output;
* invalid inputs.

---

# 14. Offline end-to-end test

Add one deterministic integration test:

```text
persistent test database
→ several fixture scheduled cycles across multiple market days
→ integrate cycles into both isolated books
→ process pending entries
→ open positions
→ trigger at least one profit-target exit
→ trigger at least one stop-loss or max-holding exit
→ create SELL intents
→ simulate fills
→ update isolated cash and positions
→ create daily snapshots
→ reconcile both books
→ compute metrics
→ produce soak report
→ evaluate readiness
→ rerun same lifecycle dates
→ prove idempotency
→ prove no cross-book contamination
→ prove no live execution
```

Use fixture providers only.

Do not call Claude or external services.

Keep the sample small enough for a fast test.

---

# 15. Documentation

Create:

```text
docs/milestones/milestone9-manual-paper-soak-and-lifecycle.md
docs/runbooks/manual-paper-trading-soak.md
```

Update the Milestone 8.1 document with a short pointer only.

Document:

* lifecycle configuration;
* exit policies;
* pending-order behavior;
* manual exit workflow;
* CLI sequence;
* persistent database usage;
* reconciliation;
* daily reporting;
* readiness;
* recovery from failed lifecycle runs;
* safety boundaries;
* deferred items.

Do not rewrite old milestone documents.

---

# Deferred items

Keep these out of Milestone 9:

```text
unattended recurring activation
launchd installation
external Alpaca paper broker
per-book paper_runtime subprocess pool
partial fills
trailing stops
tax-lot selection other than FIFO
live trading
automated promotion
remaining corporate-action types
dividend record-date entitlement correction
Milestone 7 health backlog
```

---

# Required test execution

During development, run targeted tests only.

At completion run once:

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

Milestone 9 is complete when:

1. Existing 1,394 tests still pass.
2. Existing 33 paper-runtime tests still pass.
3. Lifecycle processing is disabled by default.
4. Lifecycle execution is manual only.
5. Pending orders can fill, expire, or remain pending deterministically.
6. Expired/cancelled BUY reservations are released exactly once.
7. Stop-loss exits work.
8. Profit-target exits work.
9. Maximum-holding-period exits work.
10. Recommendation-reversal exits work.
11. Manual exit requests are audited.
12. Exit decisions are deterministic and persisted.
13. SELL intents are book-specific and long-only.
14. No oversell is possible.
15. No cross-book position use is possible.
16. Lifecycle retries are idempotent.
17. Daily snapshots are point-in-time safe.
18. Both books reconcile independently.
19. Metrics and soak reports are persisted.
20. Readiness remains advisory only.
21. No automatic activation occurs.
22. No external or live broker path exists.
23. No scheduler or launchd deployment is activated.
24. Documentation matches implementation.
25. No commit or push occurs.

---

# Final response

Keep the final response concise.

Report only:

1. Baseline and final tests.
2. Files created and modified.
3. Exit-policy behavior.
4. Pending-order behavior.
5. Lifecycle entry point.
6. Idempotency proof.
7. Isolation proof.
8. CLI commands.
9. Soak-readiness result.
10. Safety confirmation.
11. Deferred items.

Include:

```text
Requirement → implementation → test
```

Use labels:

```text
MANUAL-PAPER-SOAK
POSITION-LIFECYCLE
DETERMINISTIC-EXITS
PENDING-ORDERS-MANAGED
POINT-IN-TIME-SAFE
PAPER-BOOK-ISOLATED
IDEMPOTENT
PROMOTION-EVIDENCE-ONLY
LIVE-TRADING-NOT-IMPLEMENTED
RECURRING-DEPLOYMENT-NOT-ACTIVATED
```

Do not commit or push.
