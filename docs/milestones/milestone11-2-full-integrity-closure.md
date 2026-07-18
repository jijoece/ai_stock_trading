# Milestone 11.2 — Full Execution, Transaction, Recovery, and Codebase Integrity Closure

Work directly in the existing `ai_stock_trading` repository from the latest `main` branch after PR #8.

This milestone consolidates:

1. the remaining findings from the PR #8 review;
2. the findings from the independent full-codebase audit;
3. the CI, migration, transaction, and test gaps associated with those findings.

Some findings were produced against different repository commits. Therefore, do not blindly implement every stated correction.

For each finding:

```text
inspect current main
→ verify whether the issue remains
→ classify it
→ implement the narrowest safe correction when unresolved
→ add or retain regression coverage
→ document the result
```

Use these classifications:

```text
CONFIRMED_AND_FIXED
PARTIALLY_CONFIRMED_AND_FIXED
ALREADY_FIXED_TEST_ADDED
ALREADY_FIXED_EXISTING_TEST_SUFFICIENT
DESIGN_TRADEOFF_DOCUMENTED
LEGACY_PATH_QUARANTINED
NOT_REPRODUCIBLE
NEEDS_RUNTIME_EVIDENCE
```

This is an implementation task.

Do not enable live trading.

Do not make real broker or provider calls.

Do not use real credentials.

Do not run opt-in credentialed smoke tests.

Do not commit or push unless explicitly requested.

---

# Primary objective

Close all remaining integrity gaps across:

```text
SQLite transaction discipline
→ local simulated fills
→ cash and share reservations
→ campaign activation
→ recurring scheduling
→ external paper preview and submission
→ ambiguous recovery
→ broker reconciliation
→ runtime subprocess isolation
→ provider robustness
→ configuration safety
→ legacy subsystem isolation
→ migrations
→ CI
```

The final system must preserve these boundaries:

```text
research-only operation remains available
local simulation remains the default
external paper execution remains disabled by default
external submission remains explicit and operator initiated
recurring scheduling never mutates an external broker
Alpaca paper endpoint remains the only external execution endpoint
live trading remains structurally unavailable
```

---

# Working mode

You are a coding agent with direct repository access.

Use:

* Git history;
* symbol and reference search;
* focused source inspection;
* schema inspection;
* test inspection;
* temporary SQLite databases;
* two-connection concurrency reproductions;
* fake broker runtimes;
* offline test execution;
* static type checking.

Implement changes directly.

Do not return only a hypothetical patch.

Avoid broad architectural rewrites where a narrow correction is sufficient.

Preserve unrelated working-tree changes.

---

# Mandatory scratchpad

Create before beginning detailed implementation:

```text
.codex/scratchpads/milestone11-2-integrity-closure.md
```

Use this structure:

```markdown
# Milestone 11.2 Integrity Closure

## Metadata

- Starting commit:
- Branch:
- Started:
- Last updated:
- Working-tree status:
- Main test baseline:
- Paper-runtime test baseline:
- Type-check baseline:
- CI baseline:

## Finding validation tracker

| ID | Finding | Current status | Severity | Evidence | Implementation | Tests |
|---|---|---|---|---|---|---|

## Implementation checklist

### CI and migrations

- [ ] Current GitHub CI failure identified
- [ ] Main test suite fixed
- [ ] Prior-schema migration fixture added
- [ ] Changed triggers explicitly migrated
- [ ] Schema versioning improved
- [ ] Type-check policy improved

### Transaction integrity

- [ ] SQLite transaction mode made explicit
- [ ] Manual BEGIN helpers hardened
- [ ] Local simulated fills made atomic
- [ ] BUY reservations made atomic
- [ ] SELL reservations made atomic
- [ ] Reservation plus submission checkpoint made atomic
- [ ] Queue failure releases committed safely

### External broker integrity

- [ ] Local/external execution exclusivity
- [ ] Renewable and fenced order leases
- [ ] Runtime open-SELL exposure validation
- [ ] Fail-safe post-submit fill handling
- [ ] Fail-safe post-cancel fill handling
- [ ] Duplicate-order detection across all account orders
- [ ] Fresh retry evidence
- [ ] Audited retry-preview refresh
- [ ] Sequence-based event ordering
- [ ] Lookup immutability corrected
- [ ] Critical reconciliation persistence

### Campaign and recurring

- [ ] Activation validates attempt rather than definition state
- [ ] Qualifying real-provider count used
- [ ] Legacy reviews fail closed
- [ ] Scheduler external-mutation separation verified

### Runtime and security

- [ ] Timeout response poisoning fixed
- [ ] Runtime thread/process cleanup fixed
- [ ] Credential environment allowlist verified
- [ ] Dedicated env-file contents validated
- [ ] CLI failures sanitized

### Research and configuration

- [ ] Provider-health sample floor
- [ ] HTTP client pooling
- [ ] Retry-After handling
- [ ] Response-size bounds
- [ ] Thread-safe rate limiter
- [ ] Strict scheduled-research booleans
- [ ] Stable configuration hashing
- [ ] Config loading has no filesystem side effects
- [ ] Disclosure extraction negation handling
- [ ] Flexible data-shape validation
- [ ] SEC availability limitations documented/tested

### Legacy and documentation

- [ ] Legacy paper subsystem quarantined or retired
- [ ] Legacy CLI ambiguity resolved
- [ ] Settlement semantics documented
- [ ] README safety banner corrected
- [ ] Documentation aligned
- [ ] Final implementation report completed

## Commands and reproductions

Record concise summaries only.

## Files changed

| File | Purpose |
|---|---|

## Open issues

## Resume instructions

- Last completed section:
- Exact next task:
- Tests not to repeat:
- Remaining blockers:

## Final status

- [ ] All findings classified
- [ ] All confirmed blockers corrected
- [ ] Full tests pass
- [ ] Migration smoke passes
- [ ] Concurrency tests pass
- [ ] Documentation complete
- [ ] No live-trading path added
```

Update the scratchpad:

1. after baseline;
2. after every major subsystem;
3. whenever a high-severity finding changes classification;
4. before and after concurrency or crash reproductions;
5. before final test execution;
6. after the implementation report is complete.

Do not store credentials, raw broker responses, private reasoning, or large logs.

---

# Mandatory implementation report

Create:

```text
docs/milestones/milestone11-2-integrity-closure.md
```

This file must explain:

* starting commit;
* baseline;
* findings classified;
* fixes implemented;
* findings already fixed;
* design tradeoffs;
* schema changes;
* migration strategy;
* transaction boundaries;
* tests added;
* final results;
* remaining limitations;
* operational go/no-go status.

Use a summary table:

```text
Finding → classification → correction → regression evidence
```

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

Run the configured type checker.

Inspect GitHub Actions configuration and identify why the latest `main-tests` job failed.

Run:

```bash
git diff --check
```

Do not continue to final verification with an unidentified failing main test.

---

# Part 1 — Repair CI before treating the branch as safe

The latest PR was merged while the GitHub `main-tests` job was failing.

Requirements:

* reproduce the failure on a clean Linux-like environment where practical;
* identify the exact failing tests;
* fix implementation defects rather than weakening assertions;
* do not skip or xfail safety tests merely to make CI green;
* ensure main tests and paper-runtime tests use the same dependency resolution as local development;
* ensure test paths do not accidentally depend on untracked files;
* ensure timezone, filesystem, Python-version, and SQLite-version assumptions are explicit.

Required CI jobs:

```text
main-tests
paper-runtime-tests
type-check
migration-smoke
secret-scan
dependency-audit
```

Type checking must not silently pass because every checker step uses `continue-on-error`.

When the current type-error baseline is too large to fix in this milestone:

1. establish a checked safety-critical package subset;
2. make new errors in that subset blocking;
3. store a baseline count or exclusion policy;
4. prevent the baseline from increasing.

Do not claim type checking is passing when errors are ignored.

---

# Part 2 — Add real prior-schema migration coverage

The migration smoke must not only create the current schema twice.

Add prior-schema fixtures representing at least:

```text
pre-Milestone-11 schema
Milestone-11 schema
Milestone-11.1 schema
```

For each fixture:

```text
create exact prior schema
→ insert representative rows
→ apply current schema upgrade
→ verify columns
→ verify indexes
→ verify triggers
→ verify data preservation
→ exercise upgraded behavior
```

At minimum verify:

* external lookup consumption;
* external event `scope_sequence`;
* external leases;
* share-reservation evidence;
* activation-review attempt references;
* terminal-state triggers;
* append-only guarantees.

---

# Part 3 — Correct changed-trigger migration

Do not rely on:

```sql
CREATE TRIGGER IF NOT EXISTS
```

to modify the behavior of an existing trigger.

The prior external-lookup trigger prohibited every update. The current behavior requires exactly one controlled transition of:

```text
consumed_by_retry_event_id: NULL → retry event ID
```

Implement an explicit migration:

```text
inspect trigger definition
→ drop old trigger when version differs
→ create current trigger
→ verify resulting SQL
```

The current trigger must enforce:

* every lookup field is immutable;
* `consumed_by_retry_event_id` may change only from `NULL` to one nonempty retry event ID;
* a second update is rejected;
* clearing the field is rejected;
* modifying another column during consumption is rejected;
* deletion is rejected.

Add a regression using an actual previous trigger definition.

---

# Part 4 — Make SQLite transaction mode explicit

Current connection behavior must not depend on Python’s legacy implicit transaction mode.

Choose and document one consistent model.

Preferred:

```python
sqlite3.connect(..., isolation_level=None)
```

with explicit transaction helpers.

Requirements:

* explicit `BEGIN`, `BEGIN IMMEDIATE`, `COMMIT`, and `ROLLBACK`;
* no hidden transaction starts;
* no helper unexpectedly commits unrelated caller work;
* repository methods accepting `commit=False` participate in the caller transaction;
* functions using manual `BEGIN` verify or safely handle existing transaction state;
* nested operations use savepoints where appropriate;
* bounded `SQLITE_BUSY` handling;
* no write transaction across network or subprocess calls.

Add tests for:

```text
pending DML before BEGIN IMMEDIATE
manual transaction success
manual transaction rollback
helper called inside outer transaction
two real SQLite connections
busy timeout
connection usable after a failed transaction
```

---

# Part 5 — Harden all manual transaction helpers

Inspect every location using:

```text
BEGIN
BEGIN IMMEDIATE
COMMIT
ROLLBACK
SAVEPOINT
```

At minimum inspect:

```text
shadow/lease.py
paper_books/recurring_scheduler.py
paper_books/external_broker.py
paper_books/execution.py
cash_ledger.py
positions.py
```

Move `BEGIN IMMEDIATE` inside protected `try` blocks.

Required structure:

```python
started = False
try:
    conn.execute("BEGIN IMMEDIATE")
    started = True
    ...
    conn.commit()
except Exception:
    if started or conn.in_transaction:
        conn.rollback()
    raise
```

After any failed operation:

* `conn.in_transaction` must be false;
* another connection must not remain blocked by the failed caller;
* the same connection must remain usable.

---

# Part 6 — Make local simulated fill application atomic

The local simulated path must persist these effects atomically:

```text
fill
lot
position
cash settlement
fees
slippage
reservation release
order status
```

Required behavior:

```text
all effects commit
or
none of the effects commit
```

Do not persist the fill first and then apply its consequences using separate commits.

Use the same transaction pattern already used by the external fill path:

```text
BEGIN IMMEDIATE
→ check fill idempotency
→ save fill with commit=False
→ apply position/lot with commit=False
→ settle cash with commit=False
→ adjust reservation with commit=False
→ update status with commit=False
→ COMMIT
```

On exception:

```text
ROLLBACK
```

The idempotency check must not cause a partially applied persisted fill to be treated as complete.

Add invariant verification capable of detecting historical partial fill state:

```text
fill exists
but lot/position/cash effects missing
```

Either:

* reconstruct safely from immutable fill evidence; or
* fail closed with a critical reconciliation requiring explicit repair.

Do not silently return `FILLED`.

Required crash tests:

```text
crash after save_fill
crash after lot insert
crash after position update
crash after cash settlement
crash before reservation release
```

After restart, no duplicate fill and no permanent incomplete application may remain.

---

# Part 7 — Make BUY cash reservation atomic across orders

Two different BUY orders for the same book must not reserve the same available cash.

The complete operation must be serialized at book scope:

```text
calculate settled cash
→ calculate existing reservations
→ validate availability
→ insert reservation
```

Use:

```text
BEGIN IMMEDIATE
```

or an equivalent book-scoped reservation lease.

Requirements:

* different client order IDs cannot bypass the lock;
* reservation idempotency remains per intent;
* available cash never becomes negative;
* reservation plus `SUBMISSION_REQUESTED` is committed in the same local transaction for external submission;
* a crash before the broker call leaves a durable submission checkpoint;
* a crash before that local transaction commits leaves neither reservation nor checkpoint.

Add two-connection tests where both callers initially observe enough cash individually but not collectively.

---

# Part 8 — Make SELL reservation atomic across intents

Serialize reservations by:

```text
book_id + symbol
```

The following must occur in one transaction:

```text
load position
→ calculate available quantity
→ verify no conflicting reservation
→ append reservation event
→ update position aggregate
→ append SUBMISSION_REQUESTED
```

Requirements:

* two SELL intents cannot reserve the same shares;
* reservation evidence and position aggregate cannot diverge;
* `quantity = available_quantity + reserved_quantity`;
* no negative values;
* crash before commit leaves no reservation;
* crash after commit leaves an auditable unresolved submission checkpoint;
* ambiguous broker outcome keeps shares reserved;
* partial fill consumes only the filled portion;
* cancellation/rejection/expiration releases only the remainder.

Add true two-connection tests, not sequential single-connection tests.

---

# Part 9 — Enforce local/external execution exclusivity in both directions

The local simulator already refuses to fill after external evidence exists.

Add the reverse invariant.

External preview and submission must reject an intent when:

```text
a local fill exists
local status is FILLED
local status is REJECTED or EXPIRED
local execution has otherwise completed
local execution effects already exist
```

Define an explicit externally eligible local status, such as:

```text
PENDING_SUBMISSION
```

Requirements:

* preview revalidates eligibility;
* submit revalidates eligibility after acquiring the order lease;
* retry revalidates eligibility;
* local simulator continues to reject after any external preview/event/fill evidence;
* no intent can be filled in both namespaces.

Add tests for:

```text
local fill → external preview rejected
local fill → external submit rejected
external preview → local fill rejected
concurrent local/external attempts → one namespace wins atomically
```

---

# Part 10 — Use renewable, configurable, fenced order leases

A fixed 30-second lease is not sufficient when runtime calls can take 30 seconds each.

Add configuration:

```text
external_broker.order_lease_ttl_seconds
external_broker.order_lease_heartbeat_seconds
```

Validate:

```text
TTL > maximum single runtime timeout
TTL > heartbeat interval × safety factor
```

Add:

```text
lease generation or fencing token
heartbeat
owner validation
```

Requirements:

* each acquisition receives a monotonically increasing generation;
* every event-chain mutation verifies current owner and generation;
* stale owners cannot write after takeover;
* heartbeat occurs between runtime calls;
* lease can be renewed without releasing;
* lease release requires owner and generation;
* reconciliation, submit, retry, and cancellation use the same scope;
* no unbounded waiting.

Add a two-process/fake-clock test:

```text
owner A acquires
→ operation runs beyond original TTL while heartbeating
→ owner B cannot acquire
```

and:

```text
owner A stops heartbeating
→ lease expires
→ owner B acquires new generation
→ owner A’s later write rejected
```

---

# Part 11 — Use sequence ordering as the event-chain authority

For upgraded external event chains, `scope_sequence` must determine order.

Replace current-event queries based primarily on:

```sql
ORDER BY created_at DESC, rowid DESC
```

with:

```sql
ORDER BY scope_sequence DESC
```

where sequence is available.

Legacy events with null sequence must be migrated or handled explicitly.

Requirements:

* backfill deterministic sequence values for existing chains;
* preserve append order;
* sequence starts at zero or one consistently;
* one unique sequence per order scope;
* next sequence assigned atomically;
* timestamps remain audit metadata but are not chain ordering authority;
* clock regression cannot select an earlier event as current.

Add mixed-offset and backward-clock tests.

---

# Part 12 — Make post-submit fill handling fail-safe

After broker submission succeeds, no fill-related failure may escape without persisted critical reconciliation evidence.

Use one protected flow:

```text
persist broker event
→ enter fail-safe reconciliation/fill application
→ persist critical result on any failure
```

Do not perform an unprotected fill sweep before entering the fail-safe wrapper.

Cover:

* fill retrieval;
* malformed payload;
* numeric conversion;
* timestamp validation;
* namespace validation;
* fill insertion;
* lot and position application;
* cash settlement;
* reservation adjustment;
* order-status update.

On failure, persist a bounded critical result such as:

```text
MALFORMED_BROKER_FILL
FILL_APPLICATION_FAILED
RESERVATION_MISMATCH
SHARE_RESERVATION_MISMATCH
RECONCILIATION_INTERNAL_ERROR
```

The result must block new exposure-producing submissions.

---

# Part 13 — Make post-cancel fill handling fail-safe

Cancellation responses may include fills that occurred before cancellation completed.

Use the same protected reconciliation path after cancellation.

Required order:

```text
persist CANCEL_REQUESTED
→ invoke broker cancellation
→ persist resulting broker state
→ reconcile all fills
→ consume or release remaining reservation
→ persist reconciliation
```

If reconciliation fails:

* do not release the complete reservation;
* persist a critical blocker;
* retain unresolved cash or share exposure;
* show the state as reconciliation required.

---

# Part 14 — Strengthen runtime-side open-SELL validation

The isolated runtime must independently prevent multiple open closing orders from overselling the account.

For a new SELL:

```text
confirmed long position
- quantity committed to active open SELL orders
= available broker quantity
```

Active SELL states should include applicable broker states such as:

```text
NEW
ACCEPTED
PENDING_NEW
PARTIALLY_FILLED
HELD
PENDING_CANCEL
```

Exclude:

* terminal orders;
* the current deterministic client order ID during retry/idempotent lookup;
* unrelated symbols;
* BUY orders.

Use exact `Decimal` and integral validation.

Add tests:

```text
position 10, no open SELL → SELL 10 allowed
position 10, open SELL 6 → new SELL 5 rejected
position 10, open SELL 6 → new SELL 4 allowed
same client ID retry → not double-counted
fractional open-order quantity → fail closed
```

---

# Part 15 — Detect duplicate broker orders across the full account

Do not skip an order merely because its client order ID lacks the project prefix.

Compare all bounded recent account orders.

Use:

```text
symbol
side
quantity
limit price
time in force
active/terminal state
submission time window
broker order ID
client order ID
```

Classify:

* duplicate same client ID;
* materially identical duplicate with another client ID;
* manual order conflict;
* another application’s order conflict;
* unrelated order.

Requirements:

* manually created equivalent Alpaca paper orders are detectable;
* malformed or unavailable recent-order results fail closed during reconciliation;
* bounded result count;
* bounded time window;
* duplicate identifiers stored only in sanitized form;
* `BROKER_ORDER_DUPLICATE` remains critical.

Add tests with non-prefixed client IDs.

---

# Part 16 — Require strict qualifying-provider activation evidence

Recurring activation must not use a metric that counts a cycle merely because one real provider succeeded.

Persist and validate:

```text
qualifying_real_provider_cycles
```

A qualifying cycle must satisfy the project’s complete required-provider policy.

Requirements:

* activation review includes informational and qualifying counts;
* readiness uses qualifying count;
* recurring activation uses qualifying count;
* error text names qualifying count;
* legacy reviews lacking the qualifying field fail closed and require regeneration;
* partially failed provider cycles do not count.

---

# Part 17 — Add an audited retry-preview refresh

A confirmed broker `NOT_FOUND` result must not become unrecoverable solely because the original preview expired.

Add an explicit read-only operator action:

```text
external-paper-refresh-retry-preview
```

Allowed only when:

```text
current state = UNKNOWN_REQUIRES_RECONCILIATION
fresh authoritative NOT_FOUND exists
lookup is unconsumed
payload/account/attempt match
retry count remains within limit
```

Requirements:

* operator required;
* reason required;
* no broker mutation;
* account check permitted;
* frozen intent cannot change;
* new preview gets a new preview ID and expiry;
* links to ambiguous event and authoritative lookup;
* does not consume lookup;
* retry consumes the lookup;
* refresh cannot occur after broker order is found;
* refresh does not permit arbitrary resubmission.

---

# Part 18 — Fully enforce lookup immutability

Database triggers must ensure that lookup consumption is the only allowed mutation.

Required trigger conditions:

```text
NEW.lookup_id = OLD.lookup_id
NEW.book_id = OLD.book_id
NEW.paper_order_intent_id = OLD.paper_order_intent_id
NEW.client_order_id = OLD.client_order_id
NEW.account_fingerprint = OLD.account_fingerprint
NEW.result = OLD.result
NEW.authoritative = OLD.authoritative
NEW.runtime_request_id = OLD.runtime_request_id
NEW.created_at = OLD.created_at
NEW.attempt_number = OLD.attempt_number
NEW.ambiguous_event_id = OLD.ambiguous_event_id
NEW.payload_hash = OLD.payload_hash
NEW.lookup_started_at = OLD.lookup_started_at
NEW.lookup_completed_at = OLD.lookup_completed_at
OLD.consumed_by_retry_event_id IS NULL
NEW.consumed_by_retry_event_id IS NOT NULL
```

Anything else must abort.

---

# Part 19 — Fix runtime timeout response poisoning

After a request timeout, a late response can remain in the stdout queue and be mistaken for the next request.

Choose one fail-safe policy:

```text
restart runtime after every request timeout
```

or implement validated queue resynchronization.

Preferred:

```text
timeout
→ mark runtime transport unhealthy
→ terminate child
→ join pump threads
→ clear queues
→ require clean restart
```

Never reuse the same process for a later mutation after an uncertain timeout.

Tests:

```text
request A times out
→ late A response arrives
→ request B cannot consume A
→ runtime restarted before B
```

---

# Part 20 — Clean up runtime threads and child processes

Ensure:

* stdout pump thread joins;
* stderr pump thread joins;
* process receives graceful termination first;
* kill fallback is followed by `wait()`;
* queues are drained or discarded;
* file descriptors close;
* repeated start/shutdown cycles do not leak threads or zombies;
* diagnostics remain bounded and sanitized.

Add lifecycle tests using a fake child process.

---

# Part 21 — Validate dedicated runtime environment files

`PAPER_RUNTIME_ENV_FILE` must not allow the general project `.env` to expose unrelated credentials.

Parse the explicitly named file before or during runtime configuration and allow only:

```text
ALPACA_API_KEY
ALPACA_API_SECRET
ALPACA_IS_PAPER
ALPACA_BASE_URL
PAPER_BROKER_PROVIDER
```

Requirements:

* unknown keys cause configuration failure;
* duplicate keys handled deterministically;
* relative paths rejected or resolved under an explicit policy;
* symlinks considered;
* repository root `.env` explicitly rejected;
* file permissions checked when practical;
* no values logged;
* environment remains disabled when validation fails.

The main process should avoid interpreting credential values. Prefer passing only the env-file path or operating-system secret references rather than constructing a dictionary containing the secrets.

Document any unavoidable limitation clearly.

---

# Part 22 — Persist recovery-lookup failures

Where a broker recovery lookup fails:

```text
do not silently convert exception to None
```

Persist bounded evidence:

```text
lookup attempted
lookup result unknown
stable error category
timestamp
request ID
```

Do not persist raw exception text, response bodies, URLs containing credentials, or account IDs.

The resulting execution state must remain:

```text
SUBMISSION_UNKNOWN
```

or its current equivalent.

---

# Part 23 — Add provider-health sample-size protection

A single failure in a one-symbol cycle should not necessarily trigger a provider-outage pause unless explicitly configured.

Add policy fields such as:

```text
minimum_requests_for_failure_rate
minimum_symbols_for_failure_rate
```

Below the minimum sample:

```text
INSUFFICIENT_DATA
```

not success and not automatic provider outage.

Requirements:

* severe explicit provider errors may still trigger immediate pause;
* persistent failures cross the threshold;
* recovery requires hysteresis;
* sample floor configuration included in config hash;
* tests cover 1/1, 2/2, threshold crossing, and recovery.

---

# Part 24 — Improve the HTTP client

Reuse a persistent `httpx.Client` or equivalent transport.

Requirements:

* connection pooling;
* explicit close/context lifecycle;
* thread-safety documented;
* bounded response bytes;
* bounded JSON depth/size where practical;
* retry only idempotent reads;
* honor valid `Retry-After`;
* exponential backoff with maximum delay;
* no retry storm;
* credential-bearing query values redacted;
* raw response body not persisted in errors.

Add fake-transport tests.

---

# Part 25 — Make rate limiting thread-safe

Protect limiter state with a lock.

Requirements:

* monotonic clock;
* concurrent callers cannot acquire the same interval;
* no negative sleep;
* clock rollback handled;
* provider-specific limiter scope documented;
* no lock held during the network call itself.

Add deterministic two-thread tests.

---

# Part 26 — Strict scheduled-research configuration

Replace permissive `bool(value)` conversions in scheduled-research configuration.

Apply strict booleans to:

```text
enabled
submit_paper_orders
allow_live_promotion
all execution or promotion gates
```

Reject:

```text
"false"
"true"
0
1
null where required
```

Accept actual YAML booleans only.

Ensure live-promotion or execution remains disabled by default.

---

# Part 27 — Make configuration hashing deterministic

Remove unrestricted:

```python
json.dumps(..., default=str)
```

from safety-relevant configuration hashing.

Support only explicit canonical types:

```text
None
bool
int
finite Decimal
string
list/tuple
mapping with string keys
```

Reject:

```text
Path
set
datetime unless explicitly normalized
custom objects
NaN
infinity
```

Requirements:

* stable key ordering;
* stable Decimal representation;
* equivalent configuration hashes identically;
* unsupported types fail loudly;
* secrets excluded from hashes.

---

# Part 28 — Remove filesystem side effects from config loading

Loading or validating configuration must not create directories.

Move directory creation to the operation that first needs the directory or database.

Tests:

```text
load valid config under read-only parent
validate invalid config
dry-run config load
```

No filesystem mutation should occur.

---

# Part 29 — Improve disclosure extraction safety

Add negation and alleviation handling for going-concern phrases.

Do not classify phrases such as:

```text
no substantial doubt about ability to continue as a going concern
substantial doubt has been alleviated
conditions no longer raise substantial doubt
```

as active going-concern findings.

Requirements:

* bounded context window;
* preserve true positive language;
* distinguish auditor statement from management boilerplate when possible;
* tests for HTML residue, line breaks, punctuation, tables, and negation;
* classify ambiguous text conservatively.

---

# Part 30 — Validate flexible market-data shapes

For helpers accepting multiple data shapes, validate the complete sequence before conversion.

Reject:

* mixed floats and mappings;
* mappings missing `close`;
* strings;
* `None`;
* `NaN`;
* infinity;
* empty history when minimum length required.

Raise bounded domain errors rather than raw `KeyError` or `TypeError`.

---

# Part 31 — SEC point-in-time assurance

Inspect company-facts and filing availability handling.

Requirements:

* filing acceptance timestamp is authoritative when available;
* date-only facts are not available before a conservative safe time;
* intraday historical research cannot see later same-day facts;
* amendment timing respected;
* uncertainty sets point-in-time-safe false;
* add offline fixtures for morning `as_of` and later filing.

Any behavior requiring live EDGAR verification must remain marked:

```text
NEEDS_RUNTIME_EVIDENCE
```

Do not invent proof.

---

# Part 32 — Define paper-book settlement semantics

The current active paper-book ledger may use immediate simulated settlement.

Choose and document an explicit policy:

```text
IMMEDIATE_SIMULATED_SETTLEMENT
```

or implement deterministic market-day T+1.

Do not leave behavior implicit.

When retaining immediate settlement:

* state that it is a simulation simplification;
* ensure risk and buying-power calculations use the same policy;
* do not call it regulatory settlement;
* record policy version in relevant snapshots or config hashes.

For any retained legacy ledger, fix Friday/holiday calendar-day settlement or quarantine it.

---

# Part 33 — Quarantine or retire the legacy paper subsystem

The repository currently exposes two paper-ledger systems.

Identify legacy commands such as:

```text
paper-status
execute-paper-recommendation
sync-paper-orders
reconcile-paper
```

Choose one:

## Preferred

Remove operator reachability while preserving migration/read-only inspection where needed.

## Alternative

Rename clearly:

```text
legacy-paper-*
```

and require an explicit legacy flag.

Requirements:

* no accidental mixing with `paper_books`;
* separate database tables clearly documented;
* legacy subsystem cannot feed campaign, recurring, or external execution;
* runtime DDL removed from constructors where practical;
* known legacy drawdown, settlement, and RMW limitations documented;
* deprecated status visible in CLI help.

Do not perform destructive data migration without an explicit plan.

---

# Part 34 — Improve schema versioning

Persist a real schema version.

Requirements:

* schema version table;
* ordered migrations;
* each migration idempotent;
* current version checked during connection;
* forward version fails safely;
* migration occurs inside a protected transaction where SQLite permits;
* migration result logged without data leakage;
* tests from multiple prior versions.

Do not replace all existing additive migrations unnecessarily; wrap them in an explicit versioned sequence.

---

# Part 35 — Correct documentation claims

Update the README safety banner.

It must no longer claim that the repository contains no real order preview or submission.

State accurately:

```text
local simulation is the default
external execution is limited to explicit Alpaca paper-account operations
external paper execution is disabled by default
recurring scheduling does not submit externally
live trading is not implemented
```

Update:

```text
README.md
.env.example
paper_runtime/README.md
Alpaca paper runbook
recurring scheduler runbook
architecture decision records
```

Avoid storing implementation prompts as final architecture documentation.

---

# Part 36 — Test-quality requirements

Add tests proving behavior rather than only checking keys or row counts.

Required categories:

## Transactions

* pending transaction before manual `BEGIN`;
* rollback leaves connection usable;
* helper does not commit unrelated work;
* two-connection reservation race;
* transaction never spans runtime call.

## Local fill crash boundaries

* each stage listed in Part 6;
* replay does not duplicate;
* incomplete historical fill detected.

## Migration

* exact older trigger;
* exact older columns;
* representative data;
* upgraded lookup consumption.

## External broker

* local/external exclusivity;
* lease heartbeat and fencing;
* open-SELL subtraction;
* manual duplicate order;
* post-submit fill failure;
* post-cancel fill failure;
* retry-preview refresh;
* sequence ordering.

## Runtime

* late timeout response;
* repeated shutdown;
* child kill/wait;
* environment-file unknown key.

## Providers/config

* health sample floor;
* Retry-After;
* response-size bound;
* rate-limit threads;
* strict booleans;
* hash unsupported object;
* no config side effect;
* disclosure negation.

---

# Part 37 — Offline end-to-end scenarios

Add one local fill recovery scenario:

```text
approved local BUY
→ fill generated
→ injected crash during position application
→ transaction rolls back
→ rerun
→ one fill
→ one lot
→ correct position
→ correct cash
→ reservation released once
```

Add one concurrent BUY scenario:

```text
available cash = 100
→ two separate BUY intents each require 80
→ concurrent reservation attempts
→ exactly one succeeds
→ available cash remains nonnegative
```

Add one concurrent SELL scenario:

```text
position = 10
→ two separate SELL intents each require 7
→ concurrent reservation attempts
→ exactly one succeeds
→ quantity invariant preserved
```

Add one external submission crash scenario:

```text
reservation + SUBMISSION_REQUESTED commit
→ simulated process crash before broker call
→ restart
→ no blind broker mutation
→ operator sees unresolved pre-submission checkpoint
```

Add one long-running lease scenario:

```text
reconciliation exceeds original lease duration
→ heartbeat keeps ownership
→ second process cannot enter
```

Add one retry scenario:

```text
UNKNOWN
→ authoritative NOT_FOUND
→ original preview expires
→ explicit retry-preview refresh
→ explicit retry
→ lookup consumed once
```

Add one migration scenario:

```text
Milestone 11 lookup trigger
→ apply current migration
→ consume lookup once succeeds
→ second consumption fails
→ modifying other fields fails
```

All tests must remain offline.

---

# Hard boundaries

Do not:

* enable live trading;
* add Robinhood mutations;
* add automatic external submission;
* add automatic external cancellation;
* add market orders;
* add shorting;
* add options;
* add margin;
* add fractional execution;
* add crypto execution;
* add extended-hours execution;
* add automatic recovery resolution;
* run real broker smoke tests;
* use real credentials;
* weaken assertions to make CI pass;
* commit or push.

---

# Final verification

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

Run blocking type checks for the configured safety-critical scope.

Run migration tests against all prior-schema fixtures.

Run:

```bash
git diff --check
git status --short
```

Do not run real network or credentialed tests.

---

# Acceptance criteria

Milestone 11.2 is complete when:

1. Main tests pass locally and in clean CI.
2. Paper-runtime tests pass.
3. Migration smoke covers real prior schemas.
4. Changed triggers are explicitly upgraded.
5. SQLite transaction behavior is explicit.
6. Failed manual transactions do not wedge connections.
7. Local simulated fill application is atomic.
8. Partial historical fill application is detected.
9. BUY reservations are atomic across different orders.
10. SELL reservations are atomic across different intents.
11. Reservation and `SUBMISSION_REQUESTED` commit together.
12. Local and external execution are mutually exclusive.
13. Order leases heartbeat and use fencing.
14. Stale lease owners cannot write.
15. Event ordering uses `scope_sequence`.
16. Runtime validation subtracts active SELL exposure.
17. Post-submit fill failures persist critical evidence.
18. Post-cancel fill failures persist critical evidence.
19. Manual and non-prefixed duplicate orders are detected.
20. Recurring activation uses qualifying-provider cycles.
21. Expired previews can be explicitly refreshed for an eligible retry.
22. Lookup consumption works on upgraded databases.
23. Lookup rows remain otherwise immutable.
24. Timed-out runtime processes are not reused unsafely.
25. Runtime threads and child processes are cleaned up.
26. Dedicated runtime env files reject unknown keys.
27. Recovery lookup failures persist sanitized evidence.
28. Provider health has a sample floor.
29. HTTP connections are pooled and responses bounded.
30. Rate limiting is thread-safe.
31. Scheduled-research booleans are strict.
32. Config hashes are deterministic.
33. Config loading has no filesystem side effects.
34. Going-concern negation is handled.
35. Flexible data shapes are validated.
36. Settlement policy is explicit.
37. Legacy paper commands cannot be confused with active paper books.
38. README safety claims are accurate.
39. Type checking cannot silently regress.
40. External paper execution remains disabled by default.
41. Recurring scheduling performs no external mutation.
42. Live trading remains structurally unavailable.
43. No real network or broker call occurred.
44. No commit or push occurred.

---

# Final response

Keep the final response concise.

Report only:

1. starting and ending commit;
2. baseline and final test results;
3. CI failure root cause and correction;
4. finding classifications;
5. files created and modified;
6. migration changes;
7. transaction and local-fill changes;
8. reservation changes;
9. broker and recovery changes;
10. runtime and credential changes;
11. provider/configuration changes;
12. legacy subsystem handling;
13. documentation changes;
14. remaining limitations;
15. operational go/no-go table;
16. confirmation that no real broker/network call occurred;
17. confirmation that no commit or push occurred.

Include a compact table:

```text
Finding → classification → correction → regression test
```

Use these labels where applicable:

```text
CI-GREEN
PRIOR-SCHEMA-MIGRATED
TRANSACTIONS-EXPLICIT
LOCAL-FILLS-ATOMIC
CASH-RESERVATION-ATOMIC
SHARE-RESERVATION-ATOMIC
LOCAL-EXTERNAL-EXCLUSIVE
LEASES-FENCED
EVENT-CHAIN-SEQUENCED
POST-SUBMIT-FAIL-CLOSED
POST-CANCEL-FAIL-CLOSED
OPEN-SELL-ACCOUNTED
DUPLICATES-DETECTED
QUALIFYING-PROVIDER-GATED
RETRY-PREVIEW-REFRESHABLE
LOOKUPS-IMMUTABLE
RUNTIME-TIMEOUT-SAFE
RUNTIME-SECRETS-ISOLATED
STRICT-CONFIG
LEGACY-PAPER-QUARANTINED
EXTERNAL-PAPER-DISABLED
RECURRING-EXTERNAL-NOT-CONNECTED
LIVE-TRADING-NOT-IMPLEMENTED
```

Do not commit or push.
