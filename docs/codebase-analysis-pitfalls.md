# Comprehensive codebase design, correctness, and safety audit

Review the entire `ai_stock_trading` repository.

This is an investigation and audit task, not an implementation task.

Do not modify production code, schemas, configuration, tests, documentation, or dependencies unless explicitly instructed after the audit is reviewed.

Do not commit or push.

---

# Primary objective

Perform a comprehensive technical audit of the repository to identify:

```text
correctness defects
financial-modeling errors
transaction and concurrency hazards
point-in-time and look-ahead violations
idempotency and recovery weaknesses
external broker safety issues
data-integrity problems
security and credential-boundary weaknesses
configuration fail-open behavior
numerical instability
pipeline and orchestration gaps
performance bottlenecks
test blind spots
documentation/implementation drift
maintainability and architectural debt
```

Validate the candidate findings listed in this prompt and search independently for additional issues.

Do not assume a candidate finding is correct merely because it is listed.

For every candidate, classify it as:

```text
CONFIRMED
PARTIALLY_CONFIRMED
ALREADY_FIXED
NOT_REPRODUCIBLE
FALSE_POSITIVE
OUTDATED
DESIGN_TRADEOFF
NEEDS_RUNTIME_EVIDENCE
```

---

# Working mode

You have direct repository access.

Use:

* Git history;
* symbol and reference searches;
* targeted source reads;
* schema inspection;
* configuration inspection;
* test inspection;
* targeted offline test execution;
* small disposable reproduction scripts;
* SQLite temporary databases;
* static type checking where configured.

Do not:

* call real brokers;
* use real credentials;
* make external network requests;
* call Claude or other paid models;
* run opt-in real-provider tests;
* mutate a real or persistent paper-trading database;
* install recurring schedulers;
* activate external paper execution;
* activate live trading;
* modify the repository.

Use temporary directories and in-memory databases for reproductions.

---

# Token and investigation efficiency

1. Inspect repository structure and recent Git history first.
2. Identify the current milestone and latest merge commit.
3. Use symbol search before opening whole files.
4. Read tests alongside the production code they claim to cover.
5. Prefer targeted reproductions over speculation.
6. Do not dump complete source files.
7. Do not produce a chronological investigation diary.
8. Keep scratchpad notes concise.
9. Run full test suites only once for baseline.
10. Run targeted tests thereafter.
11. Do not fix findings during this task.
12. Stop only after all major safety-critical subsystems have been reviewed.

---

# Required baseline

Record:

```bash
git rev-parse HEAD
git status --short
git log --oneline -15
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

Run the configured type checker when available.

Record:

* exact pass/fail/skip totals;
* unavailable tools;
* tests excluded by markers;
* whether CI exists;
* whether the current commit has attached CI evidence.

Do not run credentialed, network, or broker smoke tests.

---

# Scratchpad

Create only when scratchpads are part of the repository’s normal workflow:

```text
.codex/scratchpads/full-codebase-audit.md
```

Use:

```markdown
# Full Codebase Audit

## Repository baseline
## Architecture map
## Candidate-finding validation
## Newly discovered findings
## Test and CI review
## Security boundaries
## Financial and point-in-time integrity
## Persistence and concurrency
## External broker boundary
## Operational readiness
## Final classification
```

Do not include credentials, raw account identifiers, private reasoning, full files, or large command output.

---

# Part 1 — Build an architecture and data-flow map

Before evaluating individual defects, identify the current data flow:

```text
configuration
→ research/evidence providers
→ scheduled research cycle
→ recommendation persistence
→ shadow operations
→ paper-book assignment
→ risk evaluation
→ local simulated execution
→ campaign and activation review
→ recurring scheduler
→ external paper preview/submission
→ isolated paper runtime
→ broker reconciliation
→ metrics/reporting
```

Identify:

* authoritative state for each stage;
* mutable versus append-only tables;
* process boundaries;
* credential boundaries;
* transaction boundaries;
* idempotency keys;
* leases and owner claims;
* recovery checkpoints;
* point-in-time cutoffs;
* configuration gates;
* current live-trading barriers.

Call out duplicated or competing subsystems, especially:

```text
legacy PaperLedger versus paper_books ledger
legacy paper execution versus isolated book execution
campaign state versus attempt state
local order state versus external broker event state
current positions versus immutable historical evidence
```

---

# Part 2 — Validate supplied candidate findings

Treat the following as hypotheses.

For each one:

1. inspect the current implementation;
2. inspect relevant tests;
3. determine whether later milestones already corrected it;
4. reproduce it offline when practical;
5. classify it;
6. assign severity and confidence;
7. describe the narrowest safe remediation.

## Transaction and SQLite candidates

### A1. Implicit SQLite transactions conflict with manual `BEGIN IMMEDIATE`

Investigate:

```text
src/trading_research/storage/database.py
src/trading_research/shadow/lease.py
all other manual BEGIN/COMMIT/ROLLBACK sites
```

Validate whether connections use:

```python
isolation_level=None
```

or Python-managed implicit transactions.

Test:

* `BEGIN IMMEDIATE` after prior DML;
* nested repository calls;
* lease acquisition from two connections;
* rollback behavior;
* transaction state after exceptions.

Determine whether the reported issue remains current.

### A2. Legacy `PaperLedger` cash state has a read-modify-write race

Inspect:

```text
src/trading_research/paper/ledger.py
paper_cash_state
all callers of _load_cash and _save_cash
```

Validate whether two writers can lose updates.

Compare it with:

```text
src/trading_research/paper_books/cash_ledger.py
```

Determine whether the legacy ledger is still used in any active path.

### A3. Append-only paper-book reservation is still non-atomic

Inspect:

```text
cash_ledger.reserve_for_order
available_cash
external BUY reservation
recurring/local execution concurrency
```

Determine whether:

```text
check available cash
→ concurrent reservation
→ insert reservation
```

can over-reserve funds.

Check whether an outer transaction, lease, or `BEGIN IMMEDIATE` now closes the race.

### A4. No general migration framework

Inventory:

* `CREATE TABLE IF NOT EXISTS`;
* `_ensure_columns`;
* ad hoc renames;
* triggers;
* additive migrations;
* schema-version tables;
* migration smoke tests.

Assess whether schema upgrades are deterministic and recoverable across milestones.

---

## Settlement, accounting, and portfolio candidates

### B1. T+1 settlement uses calendar days

Inspect the legacy ledger and current paper-book ledger.

Validate:

* Friday sale settlement;
* holiday settlement;
* early-close dates;
* whether the active execution path models settlement at all;
* whether settled and available cash match intended account semantics.

### B2. Historical drawdown uses future peak equity

Inspect snapshot and drawdown calculations.

Validate whether historical backfills query:

```sql
MAX(equity)
```

without an `as_of` cutoff.

Test out-of-order snapshot insertion.

Search for the same look-ahead pattern in:

* metrics;
* campaign reports;
* activation reviews;
* comparison reports;
* maximum drawdown calculations.

### B3. Missing mark price aborts an entire portfolio snapshot

Determine whether the active snapshot implementation:

* fails the whole portfolio;
* records partial valuation;
* uses a last-known value;
* marks a position unvalued;
* fabricates a price.

Distinguish safe fail-closed behavior from unnecessary operational outage.

### B4. External BUY cash reservation lifecycle

Verify:

* reservation before submission;
* partial-fill reservation reduction;
* delayed fill handling;
* broker `FILLED` with missing fill details;
* cancellation/rejection release;
* crash recovery;
* no over-release.

### B5. External SELL share reservation and oversell prevention

Verify:

* unresolved external SELL exposure;
* local `reserved_quantity`;
* broker open SELL orders;
* multiple closing orders;
* partial fills;
* cancellations;
* ambiguous submissions;
* repeated lifecycle exit evaluation.

### B6. Position, lot, fill, cash, and reservation consistency

Check invariants:

```text
position quantity = open lot quantities
available + reserved = total quantity
BUY fill costs reconcile to cash ledger
SELL fills reconcile to lot consumption and proceeds
reservations never negative
cash availability never negative
one fill is applied once
```

Search for crash boundaries that can violate them.

---

## HTTP and provider candidates

### C1. `HttpJsonClient` creates a new client per retry

Inspect:

```text
src/trading_research/evidence_providers/http_client.py
```

Determine whether:

* a new `httpx.Client` is created per attempt;
* connection pooling is lost;
* clients are closed correctly;
* shared-client reuse is thread-safe;
* retry behavior honors `Retry-After`;
* URL/query secrets are redacted;
* response sizes are bounded.

### C2. `MinIntervalRateLimiter` is not thread-safe

Inspect:

```text
src/trading_research/evidence_providers/rate_limits.py
```

Reproduce with two threads using a deterministic fake clock where practical.

Also check:

* separate providers sharing one limiter;
* monotonic versus wall clock;
* negative clock movement;
* async callers;
* process-level limits versus thread-level limits.

### C3. Provider failure health is too sensitive to small samples

Inspect:

```text
src/trading_research/shadow/health.py
```

Validate whether one failure among one or two symbols immediately causes `PAUSE_REQUIRED`.

Determine:

* current sample-size floor;
* rolling-window behavior;
* distinction between provider outage and candidate-level missing data;
* recovery hysteresis;
* whether persistent failures are still detected promptly.

### C4. SEC disclosure regex is fragile

Inspect:

```text
src/trading_research/evidence_providers/disclosure_extraction.py
```

Test representative variations:

* long legal clauses;
* line breaks;
* HTML residue;
* punctuation;
* tables;
* “substantial doubt” language without an actual going-concern opinion;
* negated phrases;
* auditor versus management wording.

Evaluate false positives and false negatives.

### C5. SEC/company-facts point-in-time availability

Check whether:

* acceptance timestamps are used;
* date-only facts are available too early;
* intraday research can see later filings;
* amendment timing is handled;
* provider status accurately reflects uncertainty.

### C6. Hardcoded cache metadata

Validate whether response metadata always reports:

```text
cache_status = MISS
```

and whether this field is typed, documented, and future-safe.

---

## Configuration candidates

### D1. Hardcoded Reddit sentiment cap

Search for duplicate definitions of the Reddit weight cap.

Determine:

* whether `scoring.yaml` is authoritative;
* whether CLI, scoring, research, and reporting use one value;
* whether changing config changes all paths;
* whether config hashes include the value.

### D2. Configuration loader has filesystem side effects

Inspect:

```text
src/trading_research/config.py
```

Determine whether merely loading/validating config creates directories or files.

Test loading under:

* read-only directory;
* invalid configuration;
* dry-run validation;
* unit-test temporary path.

### D3. Permissive booleans

Search for:

```python
bool(value)
```

in configuration loaders.

Verify strict handling for all execution-sensitive settings:

```text
paper books
lifecycle
campaign
recurring scheduler
external broker
live-broker gates
MCP tools
paper runtime
```

Ensure strings such as `"false"` do not enable capabilities.

### D4. `hash_config` silently stringifies objects

Inspect:

```text
src/trading_research/hashing.py
```

Determine whether `default=str` can produce:

* unstable hashes;
* collisions;
* version-dependent output;
* environment-specific path representations;
* secrets accidentally included.

Test unsupported object types.

### D5. Config and documentation drift

Compare:

* README commands;
* `.env.example`;
* YAML defaults;
* dataclass defaults;
* current implementation;
* milestone documentation.

Identify commands advertised before implementation or stale safety claims.

---

## Runtime and subprocess candidates

### E1. `SubprocessTransport` threads are not joined

Inspect process termination and cleanup.

Test:

* normal shutdown;
* child exits before shutdown;
* timeout and terminate;
* forced kill;
* unread stderr;
* repeated runtime starts;
* thread leakage;
* file descriptor leakage.

### E2. Runtime secret boundary

Verify:

* which process reads broker credentials;
* whether the runtime scans the repository `.env`;
* which environment keys are inherited;
* whether unrelated Anthropic, Reddit, Robinhood, or MCP secrets cross the boundary;
* whether health or errors expose secret values.

### E3. Runtime protocol and payload bounds

Validate:

* strict operations;
* unknown fields;
* malformed JSON;
* response-size limits;
* request/response correlation;
* unexpected stdout;
* runtime crash;
* timeout semantics;
* nonretryable mutation calls.

### E4. Broker numeric normalization

Search for:

```python
int(float(...))
int(Decimal(...))
```

Verify that fractional, `NaN`, and infinite broker quantities are rejected rather than truncated.

---

## Persistence and schema candidates

### F1. `recommendations.run_id` has no foreign key

Inspect current schema and migration compatibility.

Determine:

* whether orphan recommendations exist;
* whether recommendations may validly exist without a screening run;
* whether the missing foreign key is intentional;
* whether adding one would break legacy or alternative workflows.

### F2. `paper_cash_state` is created inside runtime code

Determine whether the table is now schema-managed.

Assess:

* schema drift;
* DDL on object construction;
* migrations;
* test database consistency;
* whether the legacy subsystem should be retired instead.

### F3. Append-only claims versus mutable aggregates

Review every table described as immutable or append-only.

Confirm triggers actually enforce:

* no update;
* no delete;
* valid terminal transitions;
* immutable event identity.

Check mutable aggregates such as positions against immutable ledgers.

### F4. Foreign-key coverage and deletion policy

Search major relationships for missing foreign keys:

```text
campaign → attempts
review → attempts
orders → intents
fills → orders
positions/lots → books
external events → intents
queue items → cycles
scheduler runs → reviews
```

Determine where missing foreign keys are deliberate because of write ordering and whether application-level checks compensate.

---

## Execution and recovery candidates

### G1. Bare exception handling around adapters

Inspect:

```text
execute_paper_recommendation.py
external_broker.py
scheduled_integration.py
campaign runner
runtime client
```

Classify exceptions currently caught.

Check whether serious programming/resource errors are converted into routine business failures.

Also verify that known runtime uncertainty is not allowed to crash without durable evidence.

### G2. Recovery lookup errors are silently swallowed

Inspect:

```text
submit_credentialed_paper_order.py
```

Validate whether failed recovery lookup details are:

* ignored;
* sanitized and persisted;
* visible to operators;
* used to determine `SUBMISSION_UNKNOWN`.

### G3. Ambiguous submission retry evidence

Verify:

* no blind retry;
* deterministic client order ID;
* fresh authoritative `NOT_FOUND`;
* lookup occurs after the current ambiguous event;
* lookup evidence cannot be reused;
* retry count is bounded;
* concurrent retry cannot fork the event chain.

### G4. Order-scope concurrency

Review leases, sequence numbers, uniqueness constraints, and transactions for:

```text
preview
submit
retry
cancel
reconcile
fill application
```

Attempt a two-connection concurrent reproduction.

### G5. Campaign recovery and recurring activation

Validate:

* campaign definition versus attempt status;
* terminal attempt requirements;
* recovery-required dates;
* activation-review supersession;
* point-in-time review evidence;
* recurring activation compatibility;
* singleton scheduler behavior;
* partial-stage crash recovery.

---

## Indicator and analytics candidates

### H1. `_strip()` loses temporal alignment

Inspect:

```text
scripts/indicators.py
```

Test:

* missing values inside the series;
* chained EMAs;
* TRIX;
* MACD;
* unequal warm-up regions;
* short histories.

Compare results against a reference implementation.

### H2. `macro_pillar.closes()` accepts multiple shapes

Inspect and test:

* float lists;
* dictionary lists;
* empty lists;
* mixed lists;
* missing `close`;
* strings;
* `None`;
* nonfinite values.

Determine whether the flexible contract is intentional or fragile.

### H3. Bollinger standard deviation choice

Verify the intended reference:

* TradingView;
* TA-Lib;
* project documentation.

Determine whether population or sample standard deviation is correct for this project.

Do not label the choice a bug solely because another library differs.

### H4. `score_trend()` asymmetric mapping

Document the exact mapping and inspect tests/docs.

Classify as:

```text
intentional conservative policy
undocumented asymmetry
actual coding defect
```

### H5. EMA rebound semantics

Validate the interpretation of:

```text
bars_since_below_ema20
```

Distinguish daily-close strategy design from an intraday-reversal expectation.

Do not treat lack of intraday behavior as a defect unless the project claims intraday semantics.

---

# Part 3 — Independently search for additional issues

Do not limit the audit to the supplied findings.

Review the following domains systematically.

## Financial correctness

Look for:

* use of floats for money;
* rounding and precision drift;
* inconsistent currency assumptions;
* stale or missing prices;
* future timestamps;
* incorrect market-session boundaries;
* improper settlement;
* corporate-action handling;
* split and dividend errors;
* realized/unrealized P&L inconsistencies;
* order notional mismatches;
* fee/slippage double counting;
* survivorship bias;
* look-ahead bias;
* data leakage between baseline and enhanced books.

## Point-in-time integrity

Trace every historical query and confirm that rows after `as_of` cannot influence:

* evidence;
* recommendations;
* valuations;
* health;
* provider history;
* snapshots;
* drawdowns;
* campaign reviews;
* cross-book verification;
* experiment comparisons;
* promotion evidence.

Search for raw string timestamp comparisons.

## Concurrency and idempotency

Search every pattern resembling:

```text
read state
→ calculate
→ write state
```

Check for:

* lost updates;
* duplicate fills;
* duplicate orders;
* duplicate campaign days;
* duplicate scheduler slots;
* stale leases;
* wrong-owner release;
* non-atomic queue claims;
* event-chain forks;
* transaction held during network call;
* inner commits breaking outer transactions.

## External broker safety

Verify:

* paper endpoint only;
* account fingerprint;
* one account/one book;
* long-only;
* no margin/options/shorting;
* limit orders only;
* share reservation;
* cash reservation;
* duplicate broker-order detection;
* partial fills;
* cancellation ambiguity;
* broker/local reconciliation;
* scheduler never submits externally;
* live gateway remains structurally unavailable.

## Security

Review:

* subprocess environment inheritance;
* dotenv discovery;
* credential-bearing URLs;
* HTTP headers;
* exception persistence;
* CLI output;
* log redaction;
* raw broker data;
* account identifiers;
* MCP allowlists and denylists;
* command injection;
* shell usage;
* arbitrary URLs;
* path traversal;
* SQL construction;
* unsafe YAML;
* dependency pinning;
* untrusted repository content in model prompts.

## Reliability and observability

Check:

* silent exception swallowing;
* incomplete audit events;
* alert deduplication;
* critical-alert lifecycle;
* stale health state;
* missing recovery instructions;
* thread/process leaks;
* connection leaks;
* unbounded queues;
* unbounded table scans;
* unbounded JSON payloads;
* large database hashing;
* missing indexes;
* expensive N+1 queries.

## Test quality

Identify tests that:

* only assert key existence;
* assert `len >= 1`;
* mock away the critical behavior;
* test initial execution but not replay;
* test success but not crash boundaries;
* fail to use two database connections;
* claim end-to-end coverage without crossing subsystem boundaries;
* do not test legacy schema upgrades;
* use unrealistic broker responses;
* are skipped because an optional dependency is absent.

## Dependency and CI safety

Check:

* lockfiles;
* version ranges;
* vulnerable dependencies;
* pinned subprocess packages;
* GitHub Actions;
* secret scanning;
* dependency audit;
* type checking;
* migration smoke;
* branch-protection-ready checks;
* reproducible installation.

---

# Part 4 — Evidence standard

Do not report speculative issues as confirmed.

Every reported finding must include:

```text
ID
title
severity
confidence
status
affected files and symbols
current behavior
expected invariant
evidence
offline reproduction or proof
impact
trigger conditions
existing test coverage
why tests did not catch it
recommended remediation
whether it blocks local simulation
whether it blocks recurring scheduling
whether it blocks external paper execution
whether it affects live-trading safety
```

Severity:

```text
CRITICAL
HIGH
MEDIUM
LOW
INFORMATIONAL
```

Confidence:

```text
HIGH
MEDIUM
LOW
```

Use `LOW` confidence only for findings requiring runtime/provider evidence.

---

# Part 5 — False-positive discipline

For every supplied candidate that is not confirmed, explain exactly why:

* code was changed later;
* active path no longer uses the affected subsystem;
* transaction or lease closes the race;
* design is deliberately fail-closed;
* behavior matches documented policy;
* test assumptions were incorrect;
* issue is performance-only rather than correctness;
* concern applies only to a deprecated script.

Do not preserve stale findings merely to make the report look comprehensive.

---

# Part 6 — Prioritization

Group final findings into:

## P0 — Must fix before any real Alpaca paper smoke

Examples:

* duplicated broker order;
* oversell;
* incorrect cash availability;
* blind retry;
* credential exposure;
* broken reconciliation;
* campaign activation using invalid evidence.

## P1 — Must fix before unattended recurring local paper operation

Examples:

* scheduler concurrency;
* crash recovery;
* point-in-time violations;
* transaction races;
* incorrect market-calendar behavior.

## P2 — Should fix before broader evaluation

Examples:

* provider health instability;
* HTTP inefficiency;
* schema drift;
* missing CI;
* weak tests;
* unbounded scans.

## P3 — Maintainability or policy clarification

Examples:

* duplicated constants;
* indicator contract ambiguity;
* documentation drift;
* loader side effects.

---

# Required deliverable

Create:

```text
docs/full-codebase-audit.md
```

This task may create the audit report only. Do not modify any other repository file.

Structure:

```markdown
# Full Codebase Audit

## Executive summary
## Repository and test baseline
## Architecture and trust-boundary map
## Validation of supplied candidate findings
## Newly discovered findings
## P0 findings
## P1 findings
## P2 findings
## P3 findings
## False positives and resolved concerns
## Test and CI gaps
## Recommended remediation sequence
## Operational go/no-go assessment
## Appendix: evidence and reproductions
```

Include a candidate-validation table:

```text
Candidate → status → severity → evidence → recommended action
```

Include a newly discovered findings table:

```text
ID → finding → severity → confidence → subsystem → blocker
```

Include a final capability assessment:

```text
Research-only analysis
Local simulated paper trading
Manual soak campaigns
Recurring local paper scheduler
Manual external Alpaca paper execution
Real Alpaca paper smoke readiness
Live trading
```

Use one of:

```text
READY
READY_WITH_LIMITATIONS
KEEP_DISABLED
NOT_IMPLEMENTED
```

---

# Final response

Keep the final response concise.

Report only:

1. current commit;
2. baseline test results;
3. number of candidates confirmed, partially confirmed, fixed, and rejected;
4. number of newly discovered findings by severity;
5. top five highest-risk findings;
6. systems that must remain disabled;
7. audit-report path;
8. whether any part of the audit was limited by missing dependencies or runtime evidence.

Do not fix findings.

Do not commit or push.
