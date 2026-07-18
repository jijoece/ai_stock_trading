# Milestone 11.3.1 — Execution, Recovery, Transaction, and Snapshot Safety Closure

Work directly in the existing `ai_stock_trading` repository from the latest branch containing PR #11.

This is an implementation task. Inspect the current code before changing it, validate each finding against the actual implementation, apply the narrowest safe correction, add regression coverage, and produce a complete implementation report.

Do not return only recommendations or a hypothetical patch.

## Hard boundaries

The following boundaries must remain unchanged:

```text
research-only operation remains available
local simulation remains the default
external paper execution remains disabled by default
external broker submission remains explicit and operator initiated
recurring scheduling must never submit or cancel external broker orders
Alpaca paper remains the only supported external execution endpoint
live trading remains structurally unavailable
```

Do not:

* enable live trading;
* enable external paper submission by default;
* make real broker, provider, SEC, Anthropic, Reddit, Robinhood, or other network calls;
* use real credentials;
* run credentialed smoke tests;
* place, preview, cancel, modify, or submit a real broker order;
* commit or push unless explicitly requested;
* weaken an existing fail-closed control merely to make a test pass;
* silently discard caller-owned database work.

Use fake runtimes, temporary SQLite databases, deterministic clocks, mock transports, subprocess fakes, and offline tests.

---

# Mandatory scratchpad

Before detailed work, create:

```text
.codex/scratchpads/milestone11-3-1-safety-closure.md
```

Include:

```text
Metadata
Current branch and starting commit
Working-tree status
Baseline test results
Finding validation tracker
Architecture and transaction decisions
Schema changes
Crash and concurrency reproductions
Files changed
Tests added
Commands run
Open issues
Remaining limitations
Resume instructions
Final status
```

Use this finding tracker:

| Item | Finding | Initial status | Evidence | Planned correction | Tests | Final status |
| ---- | ------- | -------------- | -------- | ------------------ | ----- | ------------ |

Allowed statuses:

```text
CONFIRMED
PARTIALLY_CONFIRMED
ALREADY_FIXED
NOT_REPRODUCIBLE
DESIGN_TRADEOFF
NEEDS_RUNTIME_EVIDENCE
FIXED
```

Update the scratchpad:

* after baseline;
* after validating each finding;
* before and after crash tests;
* before and after concurrency tests;
* after schema decisions;
* before final verification;
* after the implementation report is complete.

Do not put credentials, private account information, raw broker responses, large logs, or hidden reasoning in the scratchpad.

---

# Mandatory implementation report

Create:

```text
docs/milestone11-3-1-safety-closure.md
```

The report must include:

```text
Executive summary
Starting commit and branch
Baseline
Finding validation
Architecture decisions
Schema changes
Transaction ownership model
Recovery state machines
Lease and fencing design
Snapshot identity design
Health hysteresis design
Fixes implemented
Tests added
Final test results
Remaining limitations
Operational go/no-go table
Finding → correction → regression evidence summary
```

Also update `docs/INDEX.md` if that file is the repository’s documentation index.

Do not claim a finding is closed unless a regression test demonstrates the relevant safety property.

---

# Baseline

Record:

```bash
git rev-parse HEAD
git branch --show-current
git status --short
git log --oneline -20
```

Run:

```bash
pytest tests/ -q --tb=short
```

Then:

```bash
cd paper_runtime
pytest tests/ -q --tb=short
cd ..
```

Run the main suite with credential-shaped environment variables removed:

```bash
env \
  -u ANTHROPIC_API_KEY \
  -u ANTHROPIC_MODEL \
  -u ANTHROPIC_BATCH_POLL_INTERVAL_SECONDS \
  -u ALPACA_API_KEY \
  -u ALPACA_API_SECRET \
  -u ALPACA_IS_PAPER \
  -u ALPACA_BASE_URL \
  -u REDDIT_MCP_MODE \
  -u REDDIT_MCP_COMMAND \
  -u REDDIT_AUTH_MODE \
  pytest tests/ -q --tb=short
```

Also run:

```bash
pyright
cd paper_runtime && pyright
cd ..
git diff --check
```

Pyright currently may be non-blocking in CI. Record the actual result honestly; do not describe ignored errors as a successful type check.

Do not proceed to final verification with an unexplained regression in the baseline suite.

---

# Item 1 — Recover stranded `SUBMISSION_REQUESTED` orders safely

## Problem

The external submission flow durably commits:

```text
reservation
+
SUBMISSION_REQUESTED event
```

before calling the isolated broker runtime.

That atomic checkpoint is correct, but a hard crash after the checkpoint can leave the order permanently stranded in:

```text
latest event = SUBMISSION_REQUESTED
local intent = PENDING_SUBMISSION
broker outcome = unknown
```

The normal submit path cannot safely retry it, and the explicit retry path currently expects an `UNKNOWN` state.

## Required behavior

Implement an explicit recovery state machine for `SUBMISSION_REQUESTED`.

On restart or an explicit operator recovery action:

```text
SUBMISSION_REQUESTED
→ authoritative broker lookup by deterministic client_order_id
```

Then:

### Broker order found

```text
validate account fingerprint
validate client_order_id
validate symbol
validate side
validate quantity
validate limit price
validate time in force
validate timestamps
persist normalized broker state
apply any authoritative fills
retain or release reservations according to durable fill/terminal state
```

### Lookup fails or remains ambiguous

Persist an explicit ambiguity event such as:

```text
UNKNOWN_REQUIRES_RECONCILIATION
```

Retain reservations and block resubmission.

### Authoritative `NOT_FOUND`

Persist attempt-bound lookup evidence tied to:

```text
book_id
paper_order_intent_id
client_order_id
payload_hash
account_fingerprint
SUBMISSION_REQUESTED event ID
attempt number
lookup start/completion timestamps
```

Then move to an explicit ambiguity/retry-eligible state.

A second broker mutation must still require:

```text
fresh authoritative NOT_FOUND
+
explicit operator retry
+
retry limit
+
valid preview or explicit preview refresh
```

Never interpret broker `NOT_FOUND` as proof that the original submission was never attempted unless the existing policy explicitly defines the required authoritative evidence.

## Required tests

Add offline end-to-end tests for:

1. crash after checkpoint, broker order found on restart;
2. crash after checkpoint, authoritative broker `NOT_FOUND`;
3. crash after checkpoint, lookup timeout;
4. crash after checkpoint, malformed broker response;
5. repeated recovery invocation is idempotent;
6. recovery never calls broker submission;
7. retry remains blocked without attempt-bound `NOT_FOUND`;
8. reservation remains held while outcome is ambiguous;
9. terminal broker state releases only the remaining reservation;
10. recovery after restart uses a fresh database connection.

---

# Item 2 — Replace silent SQLite rollback with explicit transaction ownership

## Problem

The shared transaction helper currently resolves an existing transaction by silently calling:

```python
conn.rollback()
```

This can discard unrelated caller-owned work.

## Required correction

No transaction helper may silently commit or roll back work that it did not start.

Establish one explicit transaction model across the repository.

Preferred approach:

```text
SQLite connections use explicit transaction control
isolation_level=None
all multi-write workflows use a transaction context manager
```

The final design must support:

```text
BEGIN IMMEDIATE
commit on successful completion
rollback on BaseException
clear ownership of the transaction
no accidental nested BEGIN
no silent rollback of caller work
```

Choose one of these intentional nested behaviors:

```text
reject nested transactions with a bounded domain error
```

or:

```text
use explicit SAVEPOINT semantics for approved nested workflows
```

Do not automatically infer that any pre-existing transaction is abandoned.

Audit every `begin_immediate()` caller, including:

```text
cash reservation
share reservation
local fill application
external fill application
external submission checkpoint
lease operations
schema migration
campaign or scheduler writes
```

Update comments and tests so they describe the actual transaction ownership contract.

## Required tests

Add tests proving:

1. pending caller work is never silently lost;
2. nested transaction use fails clearly or uses a tested savepoint;
3. exceptions roll back only the transaction owned by the failing operation;
4. `BaseException` does not leave a dangling transaction;
5. a later unrelated commit cannot half-commit failed work;
6. two connections still serialize `BEGIN IMMEDIATE` workflows correctly;
7. schema creation and migration work under the selected connection mode.

Delete or rewrite any test that currently treats silent data loss as correct behavior.

---

# Item 3 — Make snapshot identity cover all economically material state

## Problem

Portfolio snapshots use cash balances when computing net liquidation value, but the deterministic snapshot identity and source hash do not include all cash and ledger inputs.

A recomputation at the same `as_of` can therefore collide with an existing immutable snapshot after:

```text
cash adjustment
late fill
fee or slippage correction
reservation change
settlement-policy change
```

## Required correction

Define one canonical snapshot-input payload used to derive both the snapshot identity and source hash.

At minimum include:

```text
book_id
as_of
cash_available_usd
cash_reserved_usd
settled_cash_usd
cash-ledger state hash or canonical contributing ledger entries
settlement policy version
snapshot methodology version
position symbol
position quantity
available quantity where relevant
average cost or lot-derived cost basis
realized P&L
selected valuation price
price timestamp
price available_at
price provider
source record ID
point-in-time-safe flag
staleness
valuation status
```

Do not use unordered or unstable data structures.

The identity must change when any economically material input changes.

When persisting:

```text
same snapshot_id + same source_hash
→ idempotent no-op
```

but:

```text
same natural snapshot scope + different source_hash
→ explicit conflict or versioned replacement policy
```

Do not silently ignore a corrected snapshot.

If snapshots are intended to remain immutable, persist a new deterministic version or fail with a bounded integrity error. Document the chosen policy.

## Required tests

Add tests proving the snapshot identity changes after:

1. cash adjustment;
2. reservation creation;
3. reservation release;
4. fee change;
5. slippage change;
6. late fill;
7. cost-basis change;
8. realized-P&L change;
9. selected price provenance change;
10. settlement-policy version change;
11. valuation methodology version change.

Also test:

* exact replay is idempotent;
* different source content cannot silently collide;
* ordering of positions and ledger inputs does not affect the hash;
* no unsupported object is silently stringified.

---

# Item 4 — Enforce real renewable leases and fencing

## Problem

The external order lease currently accepts caller-supplied timestamps, repeatedly receives the operation’s original `now`, ignores failed heartbeats, and does not conditionally fence protected writes.

## Required correction

Use an injected clock and obtain fresh time for every lease operation.

A heartbeat must:

```text
use current clock time
extend expiry from current clock time
verify owner_id
verify generation
fail closed after takeover or expiry
```

A failed heartbeat must immediately stop the operation.

Do not rely only on:

```text
read verify()
→ later unprotected write
```

because ownership may change between the read and write.

Use fencing in the actual protected mutation. Valid approaches include:

* include lease owner and generation in conditional SQL;
* perform lease verification and protected write in the same transaction;
* maintain an execution-scope row whose generation must match the lease generation.

Protect at least:

```text
preview persistence
external event append
reservation mutation
broker lookup evidence
retry evidence consumption
reconciliation persistence
fill application
order-status update
terminal reservation release
cancel-state changes
```

Validate configuration so:

```text
lease TTL > maximum single runtime request timeout + heartbeat margin
```

Do not retain the 30-second fallback if it can be shorter than a runtime request.

## Required tests

Use deterministic clocks and two database connections to prove:

1. heartbeat extends expiry using fresh time;
2. stale timestamps do not renew a lease;
3. failed heartbeat aborts the operation;
4. owner A cannot write after owner B takes over;
5. generation changes after takeover;
6. owner A cannot release owner B’s lease;
7. a write conditional on the old generation affects zero rows;
8. long-running submit/reconcile paths heartbeat before expiry;
9. invalid TTL/timeout combinations fail configuration validation;
10. lease loss leaves durable ambiguity evidence where broker outcome may be uncertain.

---

# Item 5 — Restart the isolated runtime after every request timeout

## Problem

A timed-out sequential JSON-lines runtime can later emit the original response. Reusing the same child for a follow-up lookup can consume that stale response and desynchronize the protocol.

## Required correction

Any request timeout must make the current runtime transport unusable.

Required state transition:

```text
request timeout
→ mark transport unhealthy
→ terminate child
→ close stdin/stdout/stderr resources
→ join pump threads
→ wait, then kill if needed
→ discard transport
```

For a timed-out mutating operation:

```text
do not automatically resubmit
start a clean runtime process
perform read-only lookup using deterministic client_order_id
```

For a timed-out read-only operation, use the existing bounded retry policy only after starting a clean runtime.

The client should expose a clear lifecycle such as:

```text
timeout
→ unhealthy
→ explicit restart/start
→ healthy
```

Do not allow another request to reuse an unhealthy child.

## Required tests

Add process/transport tests for:

1. a late response after timeout cannot be consumed by the next request;
2. timeout marks the transport unhealthy;
3. follow-up requests fail until restart;
4. restart uses a new child/transport instance;
5. broker submission is not automatically retried;
6. read-only recovery lookup runs through the new process;
7. pump threads terminate;
8. process termination escalates to kill when necessary;
9. stderr output remains bounded and sanitized;
10. repeated timeout/restart cycles do not leak threads or file descriptors.

---

# Item 6 — Atomically bind intent, reservation, and execution namespace

This item contains two related corrections.

## Part A — Local BUY intent and reservation

### Problem

The local simulator currently persists the order intent before creating its BUY reservation.

A crash can leave:

```text
intent exists
reservation missing
```

A replay may then skip reservation creation because the intent is no longer new.

### Required correction

For a new local BUY:

```text
claim LOCAL_SIMULATED execution namespace
insert order intent
insert initial reservation
commit
```

All three must be in one transaction.

For an existing pending BUY intent:

* verify the execution namespace is local;
* verify the expected reservation exists;
* verify the reserved amount matches the frozen notional;
* fail closed when state is inconsistent;
* do not fabricate a release.

Fill, cancel and expire operations must release only the actual remaining reservation.

Use the existing bounded remaining-reservation functions instead of writing the full requested notional blindly.

## Part B — Local versus external execution exclusivity

### Problem

An early local check for external evidence is not enough to prevent a concurrent external path from winning after the check.

### Required correction

Add a durable per-intent execution namespace:

```text
UNCLAIMED
LOCAL_SIMULATED
EXTERNAL_PAPER
```

Possible implementation:

```text
paper_order_execution_claims
```

with a unique key on:

```text
book_id
paper_order_intent_id
```

and fields such as:

```text
execution_namespace
claim_generation
claimed_at
claimed_by
```

Both local and external paths must atomically claim the namespace before:

* local reservation;
* local simulated fill;
* external preview;
* external reservation;
* broker submission.

The claim must be immutable after the execution path has created economically meaningful evidence.

External preview should not be allowed after a local claim, and local simulation should not be allowed after an external claim.

Determine and document whether a preview alone permanently claims `EXTERNAL_PAPER` or whether there is a safe explicit preview-abandonment workflow. Prefer the simpler fail-closed policy unless a tested business requirement requires release.

## Required tests

Add two-connection race tests proving:

1. local path wins and external preview/submission is blocked;
2. external path wins and local simulation is blocked;
3. exactly one namespace claim is persisted;
4. no double reservation occurs;
5. no local fill and broker submission can both occur;
6. crash after namespace claim but before intent/reservation is recoverable;
7. crash after intent but before reservation leaves no partial transaction;
8. replay verifies reservation integrity;
9. cancel and expire release only remaining reserved cash;
10. namespace claims survive restart;
11. legacy rows are migrated or handled safely;
12. migration is idempotent.

---

# Item 7 — Apply strict booleans to shadow-operation configuration

## Problem

`scheduled_research` booleans are now strict, but `shadow/config.py` still uses permissive conversions such as:

```python
bool("false") is True
```

## Required correction

Create or reuse a strict boolean parser and apply it to every shadow, schedule, submission, budget, and pause boolean.

At minimum:

```text
shadow_operations.enabled
shadow_operations.allow_baseline_paper_submission
shadow_operations.allow_enhanced_submission
shadow_operations.require_market_open_day
shadow_operations.continue_on_symbol_failure

schedule.enabled

budgets.require_pricing_for_real_claude

safety.pause_on_reconciliation_mismatch
safety.pause_on_budget_breach
```

Also search the complete shadow configuration loader for any other permissive boolean coercion.

Accept only real YAML booleans:

```yaml
true
false
```

Reject:

```text
"true"
"false"
"yes"
"no"
1
0
null
lists
mappings
```

where the field is required.

Maintain the existing structural rule that enhanced submission and live promotion cannot be enabled.

## Required tests

Parametrize every boolean field and prove:

* actual `true` and `false` are accepted where allowed;
* quoted values are rejected;
* integers are rejected;
* null is rejected;
* malformed configuration fails before scheduler, lease, budget, provider, Claude, or broker work;
* default repository config remains disabled;
* configuration hash remains deterministic.

---

# Item 8 — Use real provider-request telemetry and persistent health hysteresis

## Problem

The provider-health sample floor currently uses:

```text
symbols_attempted
```

as a proxy for provider request count.

One symbol may produce several provider calls, retries, and provider-specific failures, so symbol count is not the correct denominator.

The current evaluator is also per-cycle only and does not implement persistent failure/recovery hysteresis.

## Required correction

## Part A — Authoritative request count

Derive provider health from persisted request telemetry.

Use data such as:

```text
provider
operation
success
retry count
retry exhausted
error category
request timestamp
cycle ID
research run ID
symbol
severe error classification
```

Define the counting unit clearly.

Recommended:

```text
provider request attempts or completed logical provider operations
```

Do not call it `provider_request_count` if it actually means symbols.

Thread the real count and real success count into health evaluation.

Do not collapse all providers into one rate if this can hide a complete outage of one required provider. At minimum, preserve:

```text
aggregate rate
per-provider rates
required-provider coverage
```

An absent required provider result must not look successful.

## Part B — Severe provider errors

Define a bounded enum of severe error categories, for example:

```text
AUTHENTICATION_FAILED
DNS_OR_CONNECTION_FAILURE
TLS_FAILURE
PROVIDER_CONFIGURATION_INVALID
REPEATED_RATE_LIMIT_EXHAUSTION
PROTOCOL_OR_SCHEMA_BREAK
```

Do not infer severity merely from raw exception text.

Set `provider_severe_error` from actual telemetry rather than only in tests.

## Part C — Persistent hysteresis

Implement persistent multi-cycle health state.

Example policy:

```text
HEALTHY
→ DEGRADED after warning threshold
→ PAUSE_RECOMMENDED after N consecutive failing qualified cycles
→ PAUSE_REQUIRED after M consecutive failing qualified cycles or severe error
```

Recovery should require:

```text
R consecutive healthy qualified cycles
```

Do not automatically unpause an operator-paused or killed system.

Persist enough state to reproduce the decision:

```text
policy version
window start/end
qualified cycle count
failing cycle count
consecutive failures
consecutive recoveries
per-provider metrics
decision
reasons
```

Cycles below the sample floor should be `INSUFFICIENT_DATA` and should not count as healthy recovery cycles unless policy explicitly defines that behavior.

Use a versioned health policy and include all thresholds in the configuration hash.

## Required tests

Add tests for:

1. one symbol with many provider requests uses the actual request count;
2. multiple symbols with zero provider requests remain insufficient data;
3. one required provider missing is not treated as success;
4. one provider outage is not hidden by success from another provider;
5. severe error bypasses the sample floor;
6. one failing qualified cycle does not immediately pause when hysteresis requires more;
7. consecutive failures cross warning and pause thresholds;
8. intermittent success resets or reduces the failure streak according to policy;
9. recovery requires the configured healthy streak;
10. insufficient-data cycles do not fabricate recovery;
11. process restart preserves hysteresis state;
12. repeated evaluation of the same cycle is idempotent;
13. manual pause and kill states are never automatically cleared;
14. configuration changes produce a new policy/hash boundary.

---

# Cross-cutting validation

After implementing all eight items, perform these offline scenarios.

## Crash scenarios

```text
external checkpoint committed → crash → restart → broker FOUND
external checkpoint committed → crash → restart → broker NOT_FOUND
external checkpoint committed → crash → restart → lookup timeout
local namespace claimed → crash before intent/reservation
local intent/reservation transaction interrupted
snapshot computation interrupted before persistence
runtime request timeout with late response
```

## Concurrency scenarios

```text
two concurrent local BUYs competing for cash
local execution racing external preview
local execution racing external submit
two external operations competing for the same order lease
lease takeover while the old owner attempts a write
two processes applying schema migrations
two snapshot writers for the same natural scope
```

## Idempotency scenarios

```text
repeated recovery
repeated authoritative lookup
repeated fill application
repeated reservation release
repeated namespace claim
repeated migration
repeated snapshot persistence
repeated health evaluation for the same cycle
```

Every scenario must have explicit assertions about:

```text
broker calls
database rows
reservation totals
cash totals
position totals
event states
lease generation
namespace claim
health state
snapshot identity
```

---

# Final verification

Run:

```bash
pytest tests/ -q --tb=short
```

Run under the clean environment again:

```bash
env \
  -u ANTHROPIC_API_KEY \
  -u ANTHROPIC_MODEL \
  -u ANTHROPIC_BATCH_POLL_INTERVAL_SECONDS \
  -u ALPACA_API_KEY \
  -u ALPACA_API_SECRET \
  -u ALPACA_IS_PAPER \
  -u ALPACA_BASE_URL \
  -u REDDIT_MCP_MODE \
  -u REDDIT_MCP_COMMAND \
  -u REDDIT_AUTH_MODE \
  pytest tests/ -q --tb=short
```

Then:

```bash
cd paper_runtime
pytest tests/ -q --tb=short
pyright
cd ..

pyright
git diff --check
git status --short
```

Run focused tests for every new test module separately and record their results in the implementation report.

If practical, run the concurrency-focused tests repeatedly:

```bash
pytest <concurrency-test-files> -q --count=20
```

Do not add a dependency solely for repeat execution if the repository does not already include one; a shell loop is acceptable.

---

# Completion criteria

This milestone is complete only when all of the following are true:

```text
SUBMISSION_REQUESTED has an explicit safe recovery path
no transaction helper silently rolls back caller-owned work
snapshot identity includes economically material cash and ledger state
lease heartbeat uses fresh time and failed heartbeat stops work
fencing is enforced in protected writes
a timed-out runtime process is never reused
local BUY intent and reservation are crash-atomic
local and external execution cannot both claim the same intent
shadow-operation booleans are strictly parsed
provider health uses actual request telemetry
severe provider errors are wired from production telemetry
failure and recovery hysteresis survive process restart
all regression tests pass offline and under a clean environment
```

The implementation report must end with this operational table:

| Capability                             | Status                    | Evidence |
| -------------------------------------- | ------------------------- | -------- |
| Research-only analysis                 | READY / LIMITED / BLOCKED |          |
| Local simulated paper trading          | READY / LIMITED / BLOCKED |          |
| Manual soak campaigns                  | READY / LIMITED / BLOCKED |          |
| Unattended recurring local scheduling  | READY / KEEP_DISABLED     |          |
| Manual external Alpaca paper execution | READY / KEEP_DISABLED     |          |
| Real Alpaca paper smoke                | READY / NOT_READY         |          |
| Live trading                           | NOT_IMPLEMENTED           |          |

Use conservative statuses. A passing unit suite alone is not sufficient to mark external paper execution ready when a crash, timeout, lease-loss, or concurrency path remains unresolved.
