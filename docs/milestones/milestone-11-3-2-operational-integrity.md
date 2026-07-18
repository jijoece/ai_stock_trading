# Milestone 11.3.2 — Operational Health, Telemetry, Lease Fencing, and Snapshot Concurrency Closure

Work directly in the current `ai_stock_trading` repository from the latest `main` branch after PR #15.

This milestone addresses the remaining issues identified during the PR #15 review.

This is an implementation task. Inspect the current code before modifying it, reproduce each issue with focused tests, implement the narrowest safe correction, and update the repository directly.

Do not return only recommendations or a hypothetical patch.

Do not commit or push unless explicitly requested.

---

# Primary objective

Correct the remaining integrity gaps in:

```text
provider-health telemetry
→ cycle correlation
→ required-provider coverage
→ insufficient-data handling
→ persistent hysteresis
→ automatic pause decisions
→ severe transport-error classification
→ external-order lease fencing
→ concurrent snapshot persistence
→ health decision auditability
```

The final system must ensure:

```text
one noisy cycle does not automatically pause unless it contains a clearly structural severe error
persistent failures eventually trigger the configured pause action
insufficient-data cycles count as neither healthy nor failing
provider telemetry belongs to exactly one research cycle
missing required providers cannot be hidden by successful optional providers
a stale lease owner cannot write after takeover
concurrent identical snapshot persistence is idempotent
health decisions retain sufficient evidence for later audit
```

---

# Hard safety boundaries

Preserve all current boundaries:

```text
research-only operation remains available
local simulation remains the default
scheduled research performs research only
external paper execution remains disabled by default
paper submission remains explicit and operator initiated
recurring scheduling never submits or cancels broker orders
Alpaca paper remains the only external execution endpoint
live trading remains structurally unavailable
```

Do not:

* enable external paper submission;
* enable live trading;
* place or preview real broker orders;
* invoke real Alpaca, Anthropic, Claude Code, SEC, Reddit, or other network services;
* use real credentials;
* run credentialed smoke tests;
* weaken safety checks to make tests pass;
* infer a healthy provider state from missing telemetry;
* silently fall back to symbol-based provider-health counts;
* silently ignore a lost lease;
* commit or push.

Use temporary SQLite databases, fake providers, fake runtimes, deterministic clocks, barriers, threads or processes, and offline fixtures.

---

# Mandatory scratchpad

Create:

```text
.codex/scratchpads/milestone11-3-2-operational-integrity.md
```

Use this structure:

```markdown
# Milestone 11.3.2 Operational Integrity

## Metadata

- Starting commit:
- Branch:
- Working-tree status:
- Started:
- Last updated:

## Baseline

- Main tests:
- Paper-runtime tests:
- Clean-environment tests:
- Pyright root:
- Pyright paper_runtime:
- CI configuration:
- Scheduling defaults:
- External execution defaults:

## Finding tracker

| ID | Finding | Current status | Evidence | Implementation | Tests | Final status |
|---|---|---|---|---|---|---|

## Architecture decisions

### Provider-cycle correlation

### Required-provider policy

### Health qualification

### Hysteresis and pause action

### Severe-error taxonomy

### Lease fencing

### Snapshot persistence

## Schema changes

## Concurrency reproductions

## Files changed

| File | Purpose |
|---|---|

## Commands run

## Open issues

## Resume instructions

- Last completed item:
- Exact next step:
- Tests already run:
- Remaining blockers:

## Final status
```

Use these finding classifications:

```text
CONFIRMED
PARTIALLY_CONFIRMED
ALREADY_FIXED
NOT_REPRODUCIBLE
DESIGN_TRADEOFF
FIXED
NEEDS_RUNTIME_EVIDENCE
```

Update the scratchpad:

* after baseline;
* after reproducing each finding;
* before and after every concurrency test;
* after every schema change;
* before final verification;
* after completing the implementation report.

Do not store credentials, raw provider payloads, hidden reasoning, or large logs.

---

# Mandatory implementation report

Create:

```text
docs/milestone11-3-2-operational-integrity.md
```

Include:

1. starting commit and branch;
2. baseline;
3. each finding and its classification;
4. health-state architecture;
5. provider-cycle correlation design;
6. required-provider policy;
7. severe-error taxonomy;
8. lease-fencing design;
9. snapshot concurrency behavior;
10. schema and migration changes;
11. tests added;
12. final test results;
13. remaining limitations;
14. operational go/no-go status.

Include this summary table:

```text
Finding → classification → correction → regression evidence
```

Update `docs/INDEX.md`.

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
  -u ANTHROPIC_AUTH_TOKEN \
  -u ANTHROPIC_MODEL \
  -u CLAUDE_CODE_OAUTH_TOKEN \
  -u ALPACA_API_KEY \
  -u ALPACA_API_SECRET \
  -u ALPACA_IS_PAPER \
  -u ALPACA_BASE_URL \
  -u REDDIT_MCP_MODE \
  -u REDDIT_MCP_COMMAND \
  -u REDDIT_AUTH_MODE \
  pytest tests/ -q --tb=short
```

Run:

```bash
pyright
cd paper_runtime && pyright
cd ..
git diff --check
```

Record actual Pyright results honestly. Do not treat a `continue-on-error` CI step as a passing type check.

---

# Item 1 — Make persistent hysteresis govern the real pause action

## Problem

The scheduler currently applies the single-cycle health result before evaluating persistent hysteresis:

```text
evaluate_cycle_health
→ apply_health_result
→ evaluate_and_persist_hysteresis
→ discard hysteresis decision
```

This means:

* one failing cycle can pause immediately;
* configured multi-cycle thresholds do not delay pausing;
* a later hysteresis transition to `PAUSE_REQUIRED` does not pause;
* hysteresis recovery does not affect operational readiness;
* persisted hysteresis is observational only.

## Required design

Refactor the control flow to:

```text
build health inputs
→ evaluate single-cycle checks
→ determine whether provider-health evidence is qualified
→ evaluate and persist hysteresis
→ combine structural immediate-pause checks with hysteresis decision
→ apply the effective health decision
→ persist summary, checks, reasons, and state
```

The effective operational decision must distinguish:

## Immediate structural failures

These may bypass ordinary hysteresis:

```text
duplicate-prevention violation
reconciliation mismatch where configured to pause
budget breach where configured to pause
kill switch
manual pause
clearly classified provider authentication failure
clearly classified provider configuration failure
confirmed protocol/schema break
```

## Rate or transient provider-health failures

These should pass through hysteresis:

```text
ordinary timeout
temporary 5xx
single rate-limit incident
general failure-rate threshold
retry-exhaustion rate
intermittent provider instability
```

Do not allow persistent hysteresis to automatically clear:

```text
PAUSED_MANUAL
KILLED
operator-required recovery
unresolved critical reconciliation
```

Do not call `resume()` automatically.

## Suggested result model

Create or reuse a type such as:

```python
@dataclass(frozen=True)
class EffectiveHealthDecision:
    single_cycle_status: str
    hysteresis_status: str
    effective_status: str
    immediate_pause: bool
    reasons: tuple[str, ...]
    triggering_flags: tuple[str, ...]
```

The pause actor must consume `effective_status`, not the raw single-cycle status.

## Required tests

Add scheduler-level tests proving:

1. one ordinary failing qualified cycle produces `DEGRADED`, not an immediate pause;
2. the second failure reaches `PAUSE_RECOMMENDED`;
3. the configured failure streak reaches `PAUSE_REQUIRED` and requests a pause;
4. a severe authentication failure pauses immediately;
5. an ordinary timeout does not bypass hysteresis;
6. an insufficient-data cycle does not change the failure streak;
7. an insufficient-data cycle does not change the recovery streak;
8. recovery requires the configured healthy streak;
9. hysteresis reaching `HEALTHY` does not automatically resume a paused system;
10. a manual pause remains blocking;
11. a killed state remains blocking;
12. repeated evaluation of the same cycle is idempotent;
13. the persisted health summary records single-cycle, hysteresis, and effective states.

---

# Item 2 — Treat zero requests and sub-floor samples as insufficient data

## Problem

The current scheduler falls back to:

```text
provider_request_count = symbols_attempted
provider_success_rate = completed_symbols / symbols_attempted
```

when no provider-request rows exist.

This allows a completed symbol with zero actual provider calls to look healthy.

The scheduler also currently determines hysteresis qualification using:

```python
bool(provider_request_count)
```

This treats one request as qualified even when the configured minimum sample is five.

## Required correction

Remove the symbol-count fallback from production health evaluation.

When no real provider-request records exist for the cycle:

```text
provider_request_count = 0
provider_success_rate = None
provider health check = INSUFFICIENT_DATA
hysteresis qualified = false
```

Do not fabricate a provider success rate from symbol status.

If deterministic or fixture-only cycles need separate treatment, represent that explicitly:

```text
provider_health_mode = NOT_APPLICABLE
```

or:

```text
provider_telemetry_expected = false
```

Do not disguise fixture mode as real provider success.

Derive hysteresis qualification from the provider health check:

```text
PASS      → qualified
WARNING   → qualified
FAIL      → qualified
INSUFFICIENT_DATA → not qualified
NOT_APPLICABLE    → not qualified
```

Do not derive qualification from a nonzero count alone.

## Required tests

Prove:

1. zero real provider rows remains insufficient;
2. one successful symbol with zero provider requests is not healthy evidence;
3. one provider request with minimum sample five is insufficient;
4. four provider requests with minimum sample five are insufficient;
5. five requests meet the sample floor;
6. severe structural errors may bypass the sample floor;
7. insufficient samples move neither hysteresis streak;
8. fixture mode is explicitly identified and does not contaminate production health records;
9. provider-health summaries persist the sample size and qualification decision.

---

# Item 3 — Enforce required-provider and required-category coverage

## Problem

`compute_cycle_provider_telemetry()` can identify missing required providers, but the scheduler does not supply the required-provider set and does not act on missing providers.

## Required correction

Derive the expected provider coverage from the frozen evidence-provider configuration used by the cycle.

Prefer required categories rather than only a flat provider list.

Example:

```text
market_data:
  required provider = alpaca-data

corporate_filings:
  required provider = sec-edgar

news:
  optional providers = ...

social:
  optional providers = ...
```

Persist the expected coverage with the cycle, or persist a canonical policy hash and the resolved expected provider/category mapping.

The health calculation must distinguish:

```text
provider configured and successfully called
provider configured and failed
provider required but never called
category required but no enabled provider available
optional provider absent
```

Required-provider absence must not be treated as success.

Choose and document one versioned policy:

```text
missing required provider/category → provider-health FAIL
```

or:

```text
missing required provider/category → INSUFFICIENT_DATA
```

For production scheduling, fail closed. A missing required category should normally prevent a healthy result.

Do not allow aggregate success from one provider to hide complete failure or absence of another required provider.

## Required persisted evidence

Store:

```text
required categories
resolved required providers
observed providers
missing required providers
missing required categories
per-provider request count
per-provider success count
per-provider failure count
per-provider success rate
policy version
configuration hash
```

## Required tests

Prove:

1. required Alpaca and SEC both succeed;
2. Alpaca succeeds but SEC is absent;
3. SEC succeeds but Alpaca is absent;
4. optional provider absence does not fail the cycle;
5. one required provider’s outage is not hidden by another provider’s success;
6. a disabled required category fails readiness before provider execution;
7. provider aliases and configured names normalize deterministically;
8. the resolved expected-provider set is persisted with the cycle;
9. a configuration change produces a new policy/hash boundary.

---

# Item 4 — Correlate every provider request to one cycle and run

## Problem

Provider requests are currently associated with a cycle using:

```text
symbol
+
created_at time window
```

This can mix:

* a scheduled cycle;
* a manual research run;
* another catch-up cycle;
* overlapping research for the same symbol.

## Required schema change

Add explicit correlation fields to `evidence_provider_requests`.

At minimum:

```text
research_cycle_id
scheduler_run_id
research_run_id or orchestration_run_id
symbol_attempt_id
provider_request_group_id
```

Use only fields that match existing repository concepts, but at least one immutable cycle/run identifier must be mandatory for scheduled production requests.

Prefer:

```text
research_cycle_id NOT NULL
```

for scheduled research request rows.

For legacy/manual callers, define an explicit nullable or separate mode rather than silently fabricating a cycle ID.

The orchestration layer must pass the cycle context into every evidence-provider call and persistence operation.

Do not derive ownership from timestamps after this migration.

## Query behavior

Replace:

```sql
WHERE symbol IN (...)
AND created_at >= ?
AND created_at < ?
```

with:

```sql
WHERE research_cycle_id = ?
```

or the canonical cycle ownership field.

Symbols and timestamps may be additional validation filters but not the primary correlation.

## Migration

Add a versioned migration.

For legacy rows:

* preserve existing rows;
* leave correlation fields null;
* never attribute uncorrelated legacy rows to a new cycle;
* document that legacy rows cannot be used for exact per-cycle health.

## Required tests

Use overlapping cycles and prove:

1. scheduled cycle A sees only A’s provider requests;
2. scheduled cycle B sees only B’s provider requests;
3. a manual AAPL request does not affect the scheduled AAPL cycle;
4. overlapping catch-up cycles remain isolated;
5. retry attempts retain the same cycle correlation;
6. every provider adapter receives correlation context;
7. missing required cycle correlation in scheduled mode fails closed;
8. migration preserves existing request rows;
9. uncorrelated legacy rows do not get assigned to current cycles;
10. cycle-scoped query order is deterministic.

---

# Item 5 — Distinguish transient timeouts from structural provider outages

## Problem

The current persisted taxonomy uses `ProviderRequestError` for:

```text
timeout
network failure
connection refusal
non-2xx response
```

A failed `ProviderRequestError` with no HTTP status is currently classified as a severe DNS or connection failure.

A one-off timeout can therefore bypass the sample floor and immediately pause.

## Required correction

Add a bounded transport-failure category at the point where the original exception is mapped.

Suggested enum:

```text
NONE
TIMEOUT
DNS_FAILURE
CONNECTION_REFUSED
CONNECTION_RESET
TLS_FAILURE
AUTHENTICATION_FAILURE
RATE_LIMITED
HTTP_CLIENT_ERROR
HTTP_SERVER_ERROR
PROTOCOL_ERROR
CONFIGURATION_ERROR
UNKNOWN_TRANSPORT_ERROR
```

Persist this category as a structured field.

Do not determine it later from raw exception strings.

Use library-specific exception types at the adapter boundary and map them once into this bounded taxonomy.

## Severe categories

Immediate severe categories may include:

```text
AUTHENTICATION_FAILURE
CONFIGURATION_ERROR
TLS_FAILURE where clearly identified
PROTOCOL_ERROR or schema break
DNS_FAILURE or CONNECTION_REFUSED only under a documented policy
```

Transient categories should not immediately bypass hysteresis:

```text
TIMEOUT
CONNECTION_RESET
HTTP_SERVER_ERROR
ordinary rate limit
temporary network interruption
```

Repeated or retry-exhausted transient errors should influence:

```text
failure rate
retry exhaustion
persistent hysteresis
```

Do not classify an unknown no-response error as severe by default unless the policy explicitly requires that fail-closed behavior.

## Required tests

Prove:

1. timeout is categorized as `TIMEOUT`;
2. DNS resolution failure is categorized separately;
3. TLS failure is categorized separately;
4. connection refusal is categorized separately;
5. HTTP 401/403 is authentication failure;
6. HTTP 429 is rate-limited;
7. HTTP 500 is server error;
8. malformed response is protocol/schema failure;
9. one timeout does not bypass hysteresis;
10. authentication failure does bypass hysteresis;
11. retry-exhausted timeouts eventually cross hysteresis thresholds;
12. raw exception text is not persisted.

---

# Item 6 — Make lease fencing atomic with protected writes

## Problem

The current pattern is:

```text
verify lease using SELECT
→ lease may expire or be reclaimed
→ perform unconditioned write
```

This leaves a time-of-check/time-of-use race.

## Required correction

Lease verification and every protected mutation must occur atomically.

An acceptable pattern is:

```text
BEGIN IMMEDIATE
→ verify owner_id + generation + ACTIVE + unexpired
→ perform protected write
→ COMMIT
```

Because the transaction holds the SQLite write lock, another owner cannot reclaim the lease between verification and mutation.

Alternatively, use one conditional SQL statement whose `WHERE EXISTS` clause includes:

```text
lease_key
owner_id
generation
status = ACTIVE
expires_at > current_time
```

and require exactly one affected row.

Do not use only:

```text
verify_or_raise()
→ later write
```

for safety-critical writes.

## Protected writes

Audit and atomically fence at minimum:

```text
preview persistence
external-order event append
reservation plus SUBMISSION_REQUESTED checkpoint
lookup persistence
lookup consumption
retry event append
reconciliation persistence
fill application
cash-reservation release
share-reservation mutation
order-status update
cancel-state transition
terminal-state transition
```

## Lease API

Provide a helper such as:

```python
@contextmanager
def fenced_write_transaction(conn, lease_handle):
    ...
```

or:

```python
lease.run_fenced_write(lambda conn: ...)
```

Requirements:

* begin an explicit write transaction;
* verify lease generation inside it;
* use a fresh time;
* roll back on `BaseException`;
* raise `OrderLeaseLostError` when ownership is invalid;
* never release a newer owner’s lease;
* do not hold the write transaction during runtime or network calls.

## Required concurrency tests

Use two real SQLite connections and synchronization barriers.

Prove:

1. owner A verifies inside a write transaction;
2. owner B cannot reclaim while A’s protected transaction is open;
3. after A commits, B can reclaim only when expiration rules allow;
4. owner A cannot write after B has acquired the next generation;
5. takeover between an external runtime call and the following write causes A’s write to fail;
6. stale owner cannot append an event;
7. stale owner cannot persist a preview;
8. stale owner cannot apply a fill;
9. stale owner cannot consume retry evidence;
10. stale owner cannot release B’s lease;
11. no transaction spans the runtime call;
12. failed fenced writes leave no partial rows.

The test must force a real takeover between the runtime return and the protected write. A monkeypatched verification failure alone is not sufficient.

---

# Item 7 — Make concurrent snapshot persistence idempotent

## Problem

Current persistence follows:

```text
SELECT snapshot
→ if absent, INSERT
```

Two connections can both observe absence and race to insert the same snapshot.

## Required correction

Use one atomic insert:

```sql
INSERT INTO paper_book_snapshots (...)
VALUES (...)
ON CONFLICT(book_id, snapshot_id) DO NOTHING
```

When the insert affects zero rows:

1. load the existing snapshot;
2. compare `source_hash`;
3. return idempotent no-op when it matches;
4. raise `SnapshotIdentityConflictError` when it differs.

Persist snapshot header and position rows in one explicit transaction.

Do not leave:

```text
header exists
position rows incomplete
```

after a failure.

Ensure repeated position-row insertion is also idempotent or guarded by the header insertion outcome.

## Required tests

Use two real connections and barriers.

Prove:

1. concurrent identical snapshot writers produce one snapshot;
2. both callers complete without an unhandled uniqueness error;
3. one caller reports inserted and one reports idempotent replay;
4. concurrent same-ID/different-hash input fails closed;
5. snapshot header and position rows remain atomic;
6. failure after header insertion rolls back position rows and header;
7. retry after rollback succeeds;
8. exact sequential replay remains idempotent;
9. different economic content gets a distinct snapshot ID;
10. concurrent snapshot creation does not block indefinitely.

---

# Item 8 — Persist complete hysteresis decision evidence

## Problem

The hysteresis table includes `per_provider_metrics_json`, but the current evaluator does not receive or write current provider metrics. The prior value is carried forward.

The stored decision therefore cannot fully explain:

* which provider failed;
* which required provider was missing;
* whether the sample floor was met;
* which severe category was present.

## Required correction

Persist one complete evidence record per evaluated cycle, not only the rolling singleton state.

Recommended table:

```text
shadow_health_hysteresis_evaluations
```

Fields should include:

```text
evaluation_id
scope
cycle_id
policy_version
policy_hash
single_cycle_status
previous_hysteresis_status
new_hysteresis_status
effective_status
qualified
sample_size
minimum_sample_size
aggregate_success_rate
required_providers_json
observed_providers_json
missing_required_providers_json
per_provider_metrics_json
severe_error_categories_json
consecutive_failures_before
consecutive_failures_after
consecutive_recoveries_before
consecutive_recoveries_after
reasons_json
evaluated_at
```

Retain the singleton state table for the latest rolling counters, but write the evaluation history append-only.

The state update and evaluation-history insert must be atomic.

Repeated evaluation of the same:

```text
scope + cycle_id + policy_hash
```

must be idempotent.

Do not overwrite historical decisions.

## Required tests

Prove:

1. every cycle writes one evaluation record;
2. replay writes no duplicate record;
3. per-provider metrics reflect the current cycle;
4. missing required providers are persisted;
5. severe categories are persisted;
6. sample qualification is persisted;
7. before/after streak counts are correct;
8. policy change creates a new evaluation boundary;
9. state and history update atomically;
10. a failed state update leaves no evaluation row;
11. a failed history insert leaves no state change;
12. CLI or readiness output can explain the latest effective decision without recalculating it from raw rows.

---

# CI and type-check follow-up

The current CI type-check job uses `continue-on-error`.

Do not claim that Pyright passes.

For this milestone:

1. run Pyright and record the baseline;
2. avoid increasing errors in modified production modules;
3. add a blocking Pyright command for the newly modified safety-critical modules when practical;
4. keep tests out of the blocking subset if existing fixture typing prevents a clean baseline;
5. document any remaining errors.

Suggested blocking subset:

```text
src/trading_research/evidence_providers/health.py
src/trading_research/evidence_providers/persistence.py
src/trading_research/shadow/health.py
src/trading_research/shadow/health_hysteresis.py
src/trading_research/shadow/scheduler.py
src/trading_research/paper_books/external_broker.py
src/trading_research/paper_books/valuation.py
src/trading_research/storage/paper_books_repositories.py
```

Do not globally suppress errors to obtain a green result.

---

# Cross-cutting scenarios

After implementing all items, add offline integration scenarios.

## Provider-health outage

```text
cycle 1:
  five qualified requests
  ordinary timeout failures
  → DEGRADED, no pause

cycle 2:
  same qualified failures
  → PAUSE_RECOMMENDED, no required pause yet

cycle 3:
  same qualified failures
  → PAUSE_REQUIRED
  → automatic health pause requested
```

## Provider recovery

```text
system auto-paused by health
→ two healthy qualified cycles
→ hysteresis reaches HEALTHY
→ system remains paused until explicit operator resume
```

## Insufficient-data cycle

```text
zero provider requests
→ provider health INSUFFICIENT_DATA
→ no failure streak movement
→ no recovery streak movement
```

## Missing required provider

```text
Alpaca succeeds
SEC never called
→ required provider missing
→ cycle cannot be HEALTHY
```

## Overlapping runs

```text
manual AAPL run
+
scheduled AAPL cycle
+
catch-up AAPL cycle
→ each health calculation uses only its own correlated request rows
```

## Lease takeover

```text
owner A completes runtime call
→ lease expires
→ owner B acquires generation N+1
→ owner A attempts fenced database mutation
→ mutation rejected
→ no partial external event or ledger state
```

## Snapshot race

```text
two connections compute identical snapshot
→ concurrent persistence
→ one insert
→ one idempotent replay
→ one complete header and position set
```

---

# Migration requirements

Add versioned migrations for:

```text
provider-request cycle correlation
transport failure category
hysteresis evaluation history
any new lease-fencing support columns or constraints
```

Requirements:

* preserve all existing rows;
* migration is idempotent;
* forward-versioned databases fail safely;
* migration runs inside explicit transaction control;
* prior-schema fixtures cover the exact previous PR #15 schema;
* reopened databases retain data and constraints.

Add migration tests beginning with a database shaped exactly like the PR #15 schema.

---

# Documentation updates

Update:

```text
README.md
docs/INDEX.md
docs/milestone11-3-1-safety-closure.md
docs/milestone11-3-2-operational-integrity.md
applicable shadow-operations runbook
applicable provider-health ADR
```

Correct the PR #15 report’s status.

It must no longer state that Item 8 is fully operationally closed unless the hysteresis decision actually governs pause behavior.

Update operational status conservatively.

---

# Final verification

Run:

```bash
pytest tests/ -q --tb=short
```

Run the clean-environment suite:

```bash
env \
  -u ANTHROPIC_API_KEY \
  -u ANTHROPIC_AUTH_TOKEN \
  -u ANTHROPIC_MODEL \
  -u CLAUDE_CODE_OAUTH_TOKEN \
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

Run concurrency tests repeatedly:

```bash
for i in {1..20}; do
  pytest \
    tests/unit/test_health_hysteresis*.py \
    tests/unit/test_provider_health_telemetry*.py \
    tests/unit/test_external_order_lease*.py \
    tests/unit/test_snapshot_identity*.py \
    -q || exit 1
done
```

Use the actual test filenames after implementation.

No real network or broker calls may occur.

---

# Acceptance criteria

The milestone is complete only when:

1. Hysteresis controls the operational provider-health pause decision.
2. One ordinary failing cycle does not immediately pause when the policy requires multiple failures.
3. Persistent qualified failures eventually request a pause.
4. Severe structural failures may bypass hysteresis through explicit policy.
5. Zero provider requests remain insufficient data.
6. Samples below the configured floor remain insufficient data.
7. Insufficient-data cycles move neither hysteresis streak.
8. Required-provider coverage is derived from frozen configuration.
9. Missing required providers cannot be hidden by aggregate success.
10. Every provider request is correlated with exactly one cycle or explicit manual run.
11. Time-window and symbol matching are no longer the primary ownership mechanism.
12. Timeouts are distinguished from DNS, TLS, authentication, and configuration failures.
13. One timeout does not bypass hysteresis.
14. Lease verification and protected writes occur atomically.
15. A stale lease generation cannot mutate external-order state.
16. No write transaction spans a runtime call.
17. Concurrent identical snapshot persistence is idempotent.
18. Snapshot headers and position rows persist atomically.
19. Hysteresis evaluation history contains complete per-cycle evidence.
20. Hysteresis state and history update atomically.
21. Prior-schema migration tests pass.
22. Main and paper-runtime tests pass offline.
23. Clean-environment tests pass.
24. Safety-critical production-module type checking does not regress.
25. External paper submission remains disabled by default.
26. Scheduled execution remains research-only.
27. Live trading remains unavailable.
28. No real provider or broker call occurred.
29. No commit or push occurred.

---

# Final response

Keep the final response concise.

Report:

1. starting and ending commit;
2. finding classifications;
3. health-control-flow changes;
4. qualification and sample-floor changes;
5. required-provider coverage changes;
6. cycle-correlation schema changes;
7. transport-error taxonomy changes;
8. lease-fencing changes;
9. snapshot-concurrency changes;
10. hysteresis audit-history changes;
11. migrations;
12. tests and results;
13. Pyright results;
14. remaining limitations;
15. operational go/no-go status;
16. confirmation that no network or broker call occurred;
17. confirmation that no commit or push occurred.

End with:

| Capability                               | Status                |
| ---------------------------------------- | --------------------- |
| Research-only analysis                   | READY / LIMITED       |
| Local simulated paper trading            | READY / LIMITED       |
| Manual soak campaigns                    | READY / LIMITED       |
| Unattended recurring research scheduling | READY / KEEP_DISABLED |
| Manual external Alpaca paper execution   | READY / KEEP_DISABLED |
| Real Alpaca paper smoke                  | READY / NOT_READY     |
| Live trading                             | NOT_IMPLEMENTED       |

Use conservative statuses and evidence from the implemented tests.
