# Milestone 11.3 — Remaining Integrity Closure (Migration Fixtures, Provider/Config Robustness, Settlement, Legacy, Schema Versioning)

Work directly in the existing `ai_stock_trading` repository from the latest
`main` branch after PR #9 (`agent/milestone-11-2-full-integrity-closure`,
commit `c5232ad`).

## Context

Milestone 11.2 (`docs/milestone11-2-full-integrity-closure.md`, spec;
`docs/milestone11-2-integrity-closure.md`, closure report) closed 21 of the
original 37 parts: CI hermeticity, migration/trigger versioning, SQLite
transaction discipline, atomic local fills, BUY/SELL reservation
atomicity, local/external execution exclusivity, renewable fenced order
leases, sequence-based event ordering, fail-safe post-submit/post-cancel
fill handling, runtime open-SELL accounting, duplicate broker-order
detection, qualifying-provider activation gating, an audited
retry-preview-refresh action, lookup immutability, runtime timeout/process
cleanup, dedicated runtime env-file allowlisting, and recovery-lookup
failure persistence. Each of those has a passing regression test.

**This milestone closes what 11.2 explicitly left open:**

1. Part 2's full prior-schema migration fixture matrix (only the
   lookup-trigger fixture exists today).
2. Parts 23-31: provider/HTTP/rate-limiter/config robustness.
3. Part 32: settlement semantics.
4. Part 33: legacy paper subsystem quarantine.
5. Part 34: general schema versioning.
6. Part 35 (remainder): documentation beyond the README banners already
   corrected in 11.2.
7. Part 36/37 (remainder): test-quality categories and offline end-to-end
   scenarios not already covered by 11.2's regression tests.

Some findings below may already be partially or fully addressed by 11.2's
changes as a side effect (e.g. Part 37's migration and retry scenarios
overlap with tests added in 11.2) or may have shifted since the audit that
produced them. **Do not blindly implement every stated correction** — for
each finding:

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

## Primary objective

Close the remaining integrity gaps across:

```text
migrations (full prior-schema fixture matrix)
→ provider robustness (health sample floor, HTTP client, rate limiter)
→ configuration safety (strict booleans, deterministic hashing, no side effects)
→ research-content safety (disclosure negation, flexible data-shape validation)
→ SEC point-in-time assurance
→ settlement semantics
→ legacy subsystem isolation
→ schema versioning
→ documentation
→ remaining offline end-to-end scenarios
```

The final system must continue to preserve these boundaries (already true
after 11.2 — do not regress them):

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

## Working mode

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

If a fix would require breaking an existing, deliberate, tested behavior
elsewhere in the codebase (as happened in 11.2 with Part 19 — the literal
spec text conflicted with an existing, tested timeout-recovery pattern),
stop, verify the conflict with the existing tests, and choose the narrower
correction that satisfies the underlying safety property without
regressing the existing tested behavior. Document the deviation instead of
forcing the literal instruction through.

---

## Mandatory scratchpad

Create before beginning detailed implementation:

```text
.codex/scratchpads/milestone11-3-integrity-closure.md
```

Use the same structure as
`.codex/scratchpads/milestone11-2-integrity-closure.md` (metadata,
finding-validation tracker table, implementation checklist, commands/
reproductions, files-changed table, open issues, resume instructions,
final status). Update it after baseline, after every major subsystem,
whenever a high-severity finding changes classification, before/after any
concurrency or crash reproduction, before final test execution, and after
the implementation report is complete.

Do not store credentials, raw broker responses, private reasoning, or
large logs.

---

## Mandatory implementation report

Create:

```text
docs/milestone11-3-integrity-closure.md
```

Follow the same structure as `docs/milestone11-2-integrity-closure.md`:
starting commit, baseline, findings classified, fixes implemented,
findings already fixed, design tradeoffs, schema changes, migration
strategy, transaction boundaries, tests added, final results, remaining
limitations, operational go/no-go status, and a summary table of
`Finding → classification → correction → regression evidence`.

---

## Baseline

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

Also run the full suite under a clean-CI simulation (this dev environment
carries real `ANTHROPIC_API_KEY`/`ALPACA_API_KEY`/`ALPACA_API_SECRET` —
Milestone 11.2 Part 1 found and fixed a real hermeticity bug this exact
way; check whether any new test written in this milestone accidentally
depends on ambient credentials the same way):

```bash
env -u ANTHROPIC_API_KEY -u ANTHROPIC_MODEL -u ANTHROPIC_BATCH_POLL_INTERVAL_SECONDS \
    -u ALPACA_API_KEY -u ALPACA_API_SECRET -u ALPACA_IS_PAPER -u ALPACA_BASE_URL \
    -u REDDIT_MCP_MODE -u REDDIT_MCP_COMMAND -u REDDIT_AUTH_MODE \
    pytest tests/ -q --tb=short
```

Run the configured type checker (pyright; note it currently runs with
`continue-on-error: true` in CI on both the main and paper_runtime steps —
do not claim type checking is passing when errors are ignored).

Run:

```bash
git diff --check
```

Do not continue to final verification with an unidentified failing main
test.

---

## Part 2 — Add real prior-schema migration coverage

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
* external leases (including the `generation` column added in Milestone
  11.2 Part 10 — a pre-11.2 lease row has no `generation` at all);
* share-reservation evidence;
* activation-review attempt references;
* terminal-state triggers;
* append-only guarantees.

`tests/unit/test_paper_external_lookup_trigger_migration.py` (Milestone
11.2) already covers one such fixture (the exact Milestone 11.1 lookup
trigger). Use its pattern — literal prior-version DDL/trigger SQL
reproduced verbatim, not a paraphrase — as the template for the remaining
fixtures.

---

## Part 23 — Add provider-health sample-size protection

A single failure in a one-symbol cycle should not necessarily trigger a
provider-outage pause unless explicitly configured.

**Known starting point (confirmed during Milestone 11.2 triage, not yet
fixed):** `shadow/health.py::CycleHealthInputs` has no request/symbol
count field at all — `provider_success_rate: float | None` is the only
signal, so a 1-request cycle's 100% failure rate is indistinguishable from
a 100-request cycle's. Implementing this requires:

* adding a count field (e.g. `provider_request_count` and/or
  `provider_symbol_count`) to `CycleHealthInputs`;
* threading it through from wherever `CycleHealthInputs` is constructed
  (locate every call site — likely `shadow/scheduler.py` or
  `research/scheduled_cycle.py` — before changing the dataclass);
* adding policy fields `minimum_requests_for_failure_rate`/
  `minimum_symbols_for_failure_rate` to `ShadowOperationsConfiguration`'s
  safety section;
* gating `_rate_check`'s provider-failure-rate check on the sample floor.

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

## Part 24 — Improve the HTTP client

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

## Part 25 — Make rate limiting thread-safe

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

## Part 26 — Strict scheduled-research configuration

Replace permissive `bool(value)` conversions in scheduled-research
configuration.

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

Note: Milestone 11.2's `paper_books/config.py::ExternalBrokerSection`
already uses `_strict_bool`/`_strict_int` helpers throughout, including
for the two new fields added in Part 10
(`order_lease_ttl_seconds`/`order_lease_heartbeat_seconds`) — use that
existing pattern as the template; check whether
`research/scheduled_research_config.py` (or wherever scheduled-research
config actually lives) already follows it or still uses permissive
`bool(...)`.

---

## Part 27 — Make configuration hashing deterministic

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

## Part 28 — Remove filesystem side effects from config loading

Loading or validating configuration must not create directories.

Move directory creation to the operation that first needs the directory or
database.

Tests:

```text
load valid config under read-only parent
validate invalid config
dry-run config load
```

No filesystem mutation should occur.

---

## Part 29 — Improve disclosure extraction safety

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

## Part 30 — Validate flexible market-data shapes

For helpers accepting multiple data shapes, validate the complete sequence
before conversion.

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

## Part 31 — SEC point-in-time assurance

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

## Part 32 — Define paper-book settlement semantics

The current active paper-book ledger may use immediate simulated
settlement.

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

For any retained legacy ledger, fix Friday/holiday calendar-day settlement
or quarantine it (see Part 33).

---

## Part 33 — Quarantine or retire the legacy paper subsystem

The repository currently exposes two paper-ledger systems: the active
`paper_books` subsystem (extensively hardened in Milestone 11/11.1/11.2)
and a separate legacy `paper/` ledger predating it.

Identify legacy commands such as:

```text
paper-status
execute-paper-recommendation
sync-paper-orders
reconcile-paper
```

Choose one:

### Preferred

Remove operator reachability while preserving migration/read-only
inspection where needed.

### Alternative

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

## Part 34 — Improve schema versioning

Persist a real schema version.

Milestone 11.2 added `paper_books_trigger_versions` (Part 3), a table
scoped narrowly to trigger-definition versioning, not a general schema
version. This part is a broader, separate mechanism.

Requirements:

* schema version table;
* ordered migrations;
* each migration idempotent;
* current version checked during connection;
* forward version fails safely;
* migration occurs inside a protected transaction where SQLite permits;
* migration result logged without data leakage;
* tests from multiple prior versions.

Do not replace all existing additive migrations unnecessarily; wrap them
in an explicit versioned sequence.

---

## Part 35 (remainder) — Correct remaining documentation claims

Milestone 11.2 already corrected `README.md`'s top and bottom safety
banners. Audit and correct, as needed:

```text
.env.example
paper_runtime/README.md
Alpaca paper runbook
recurring scheduler runbook
architecture decision records
```

State accurately, consistent with the corrected README:

```text
local simulation is the default
external execution is limited to explicit Alpaca paper-account operations
external paper execution is disabled by default
recurring scheduling does not submit externally
live trading is not implemented
```

Avoid storing implementation prompts as final architecture documentation.

---

## Part 36 (remainder) — Test-quality requirements

Categories already covered by Milestone 11.2's regression suite:
transactions, local-fill crash boundaries, one migration fixture,
external-broker exclusivity/leases/open-SELL/duplicates/fail-safe
fills/retry-preview-refresh/sequence-ordering, runtime timeout/shutdown/
env-file. Add tests proving behavior (not just checking keys or row
counts) for the categories 11.2 left open:

## Migration

* every prior-schema fixture from Part 2 above, not just the lookup
  trigger.

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

## Part 37 (remainder) — Offline end-to-end scenarios

Milestone 11.2 already added, in `tests/unit/`:

* a local-fill crash-recovery scenario (`test_paper_books_local_fill_atomicity.py`, parametrized across 4 crash points);
* a concurrent-BUY-reservation scenario (`test_paper_books_reservation_concurrency.py`);
* a concurrent-SELL-reservation scenario (same file);
* a retry-preview-refresh scenario (`test_external_paper_broker.py::test_refresh_retry_preview_unblocks_retry_after_original_preview_expires`);
* a migration scenario for the lookup trigger (`test_paper_external_lookup_trigger_migration.py`);
* a long-running-lease-with-heartbeat scenario (`test_external_order_lease_fencing.py::test_owner_heartbeats_beyond_original_ttl_and_second_owner_still_cannot_acquire`).

Verify each still holds, then add the one scenario 11.2 did not cover:

```text
reservation + SUBMISSION_REQUESTED commit
→ simulated process crash before broker call
→ restart
→ no blind broker mutation
→ operator sees unresolved pre-submission checkpoint
```

(Milestone 11.2's report flagged that this composition —
`reserve_for_order`'s self-contained commit vs. `_submit_once`'s own
`_append_event` write — was not independently re-verified as a single
atomic transaction; resolve that open question here, and add the crash
test either way.)

All tests must remain offline.

---

## Hard boundaries

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

## Final verification

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

Run migration tests against all prior-schema fixtures added in Part 2.

Run:

```bash
git diff --check
git status --short
```

Do not run real network or credentialed tests.

---

## Acceptance criteria

Milestone 11.3 is complete when:

1. Migration smoke covers real prior schemas (pre-Milestone-11,
   Milestone-11, Milestone-11.1, and Milestone-11.2's own lease-generation
   upgrade).
2. Provider health has a sample floor.
3. HTTP connections are pooled and responses bounded.
4. Rate limiting is thread-safe.
5. Scheduled-research booleans are strict.
6. Config hashes are deterministic.
7. Config loading has no filesystem side effects.
8. Going-concern negation is handled.
9. Flexible data shapes are validated.
10. SEC point-in-time limitations are documented/tested or marked
    `NEEDS_RUNTIME_EVIDENCE`.
11. Settlement policy is explicit.
12. Legacy paper commands cannot be confused with active paper books.
13. Remaining documentation (`.env.example`, `paper_runtime/README.md`,
    runbooks, ADRs) is aligned with the corrected README safety posture.
14. A general schema-version table exists, distinct from Part 3's
    trigger-version table.
15. External paper execution remains disabled by default.
16. Recurring scheduling performs no external mutation.
17. Live trading remains structurally unavailable.
18. No real network or broker call occurred.
19. No commit or push occurred (unless explicitly requested).

---

## Final response

Keep the final response concise.

Report only:

1. starting and ending commit;
2. baseline and final test results;
3. finding classifications;
4. files created and modified;
5. migration changes;
6. provider/configuration changes;
7. settlement and legacy-subsystem handling;
8. schema-versioning changes;
9. documentation changes;
10. remaining limitations;
11. operational go/no-go table;
12. confirmation that no real broker/network call occurred;
13. confirmation that no commit or push occurred (unless requested).

Include a compact table:

```text
Finding → classification → correction → regression test
```

Do not commit or push unless explicitly requested.
