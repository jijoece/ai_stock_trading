# Milestone 12.1 — Provider, Health-Control, Telemetry, and CI Integrity Closure

Work directly in the current `ai_stock_trading` repository from the latest `main` branch containing PRs #17 and #18.

This milestone addresses the major correctness and production-safety findings from the combined review of PRs #16, #17, and #18.

This is an implementation task.

Inspect the current code before modifying it, reproduce each issue with focused tests, implement the narrowest safe correction, and update the repository directly.

Do not return only recommendations or a hypothetical patch.

Do not commit or push unless explicitly requested.

---

# Primary objective

Close the remaining major issues in:

```text
CI reliability
→ typed provider failures
→ Codex CLI compatibility
→ Codex terminal failure classification
→ reasoning-token accounting
→ health qualification
→ required-provider health
→ scheduled telemetry ownership
→ diagnostic transport rates
→ persistent hysteresis configuration
```

The final system must guarantee:

```text
main tests are green before merge
provider health uses stable typed codes rather than message text
unsupported Codex CLI contracts fail before inference
terminal Codex failures use the same typed taxonomy as nonzero exits
reasoning-token semantics are explicit and budget-safe
one health dimension cannot suppress another dimension’s failure
each required provider is evaluated independently
scheduled health uses only the current scheduler run’s telemetry
typed transport categories drive diagnostic rates
hysteresis thresholds come from strict, frozen configuration
```

---

# Hard safety boundaries

Preserve all existing boundaries:

```text
research-only operation remains available
local simulation remains the default
scheduled research performs research only
external paper execution remains disabled by default
paper submission remains explicit and operator initiated
recurring scheduling never submits or cancels external broker orders
Alpaca paper remains the only external execution endpoint
live trading remains structurally unavailable
```

Do not:

* enable live trading;
* enable external paper submission;
* enable recurring paper submission;
* place, preview, modify, or cancel a real broker order;
* invoke real Codex, Claude Code, Alpaca, Anthropic, SEC, Reddit, or other network services from tests;
* use real credentials;
* weaken assertions to obtain green CI;
* classify operational failures by arbitrary human-readable text;
* hide a required-provider failure behind aggregate success;
* treat missing telemetry as healthy;
* silently accept untested future Codex CLI versions;
* commit or push.

Use fake executables, fake JSONL streams, deterministic clocks, temporary SQLite databases, barriers, multiple connections, and offline fixtures.

---

# Mandatory scratchpad

Create:

```text
.codex/scratchpads/milestone12-1-provider-health-ci-integrity.md
```

Use:

```markdown
# Milestone 12.1 Provider, Health, and CI Integrity

## Metadata

- Starting commit:
- Branch:
- Working-tree status:
- Started:
- Last updated:

## Baseline

- Main tests:
- Clean-environment main tests:
- Paper-runtime tests:
- Pyright root:
- Pyright paper_runtime:
- GitHub CI status:
- Known failing CI test:
- Codex CLI compatibility configuration:
- Current provider failure flow:
- Current health qualification flow:

## Finding tracker

| ID | Finding | Classification | Evidence | Correction | Tests | Final status |
|---|---|---|---|---|---|---|

## Architecture decisions

### Provider failure propagation

### Codex version compatibility

### Codex terminal failure classification

### Usage and reasoning-token semantics

### Dimension-specific health state

### Required-provider health policy

### Scheduler-run telemetry ownership

### Transport diagnostic rates

### Hysteresis configuration

## Schema and migration changes

## CI reproductions

## Files changed

| File | Purpose |
|---|---|

## Commands run

## Open issues

## Resume instructions

- Last completed item:
- Exact next task:
- Tests already run:
- Remaining blockers:

## Final status
```

Use these classifications:

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
* after identifying the exact CI failure;
* after validating each finding;
* before and after migration changes;
* before and after concurrency tests;
* before final verification;
* after completing the implementation report.

Do not store credentials, raw provider output, raw prompts, hidden reasoning, or large logs.

---

# Mandatory implementation report

Create:

```text
docs/milestone12-1-provider-health-ci-integrity.md
```

Include:

1. starting commit and branch;
2. exact CI failure and root cause;
3. finding classifications;
4. provider failure propagation design;
5. Codex compatibility policy;
6. terminal error-classification design;
7. token-accounting policy;
8. health-dimension architecture;
9. required-provider health policy;
10. scheduler-run correlation model;
11. transport-metric corrections;
12. hysteresis configuration;
13. schema and migration changes;
14. tests added;
15. final verification;
16. remaining limitations;
17. operational go/no-go table.

Include:

```text
Finding → classification → correction → regression evidence
```

Update `docs/INDEX.md`.

---

# Baseline and CI reproduction

Record:

```bash
git rev-parse HEAD
git branch --show-current
git status --short
git log --oneline -25
```

Run:

```bash
pytest tests/ -q --tb=short
```

Run with credential-shaped environment variables removed:

```bash
env \
  -u OPENAI_API_KEY \
  -u CODEX_API_KEY \
  -u ANTHROPIC_API_KEY \
  -u ANTHROPIC_AUTH_TOKEN \
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
cd ..
```

Run:

```bash
pyright
cd paper_runtime && pyright
cd ..
git diff --check
```

## Mandatory CI-failure investigation

PRs #17 and #18 were merged while the GitHub `main-tests` job was failing.

Identify the exact failure.

Reproduce the GitHub environment as closely as practical:

```text
Ubuntu
Python 3.11
fresh virtual environment
pip install -e ".[dev]"
no credentials
clean checkout
```

Use a container or local Linux-compatible environment when available.

Investigate:

* Python-version assumptions;
* macOS-only path assumptions;
* subprocess signal behavior;
* process-group cleanup;
* filesystem permissions;
* timing-sensitive tests;
* SQLite lock timing;
* test-order dependencies;
* locale/timezone assumptions;
* executable-path fixtures;
* missing package data;
* nondeterministic concurrency tests.

Do not label the failure “flaky” without a reproducible explanation.

Do not skip or xfail the failing safety test merely to make CI green.

The milestone cannot be complete while `main-tests` remains red or unexplained.

---

# Item 1 — Propagate typed provider failures into health controls

## Problem

Provider exceptions are mapped into stable structured failures, but the attempt controller later decides whether to pause by searching the free-text `failure_reason`.

Current behavior is conceptually:

```text
typed ProviderUnavailableError
→ structured ResearchValidationFailure
→ ResearchAttemptRecord stores only message
→ attempt controller searches message for words
→ pause decision
```

This abandons the typed taxonomy at the point where operational action is taken.

## Required correction

Extend `ResearchAttemptRecord` with bounded structured fields such as:

```python
failure_code: str | None
failure_stage: str | None
failure_retryable: bool | None
failure_metadata: Mapping[str, object]
```

Use the repository’s existing immutable or frozen conventions.

Do not persist arbitrary exception dictionaries.

Allow only bounded metadata fields already approved by the provider failure taxonomy.

When creating a failed attempt:

```text
copy stable code
copy stable stage
copy retryability
copy sanitized allowlisted metadata
store human-readable message separately
```

Update:

```text
ResearchAttemptRecord
research attempt schema
repository persistence
repository loading
attempt-controller hooks
usage/failure reports
tests
migrations
```

## Health-policy input

Replace free-text checks such as:

```python
if "authentication failed" in reason:
```

with explicit allowlists.

Example:

```python
IMMEDIATE_PROVIDER_PAUSE_CODES = {
    "CODEX_NOT_AUTHENTICATED",
    "CODEX_UNEXPECTED_AUTH_METHOD",
    "CODEX_VERSION_UNSUPPORTED",
    "CODEX_CREDIT_EXHAUSTED",
    "CLAUDE_CODE_NOT_AUTHENTICATED",
    "CLAUDE_CODE_UNEXPECTED_AUTH_METHOD",
    "CLAUDE_CODE_VERSION_UNSUPPORTED",
    "CLAUDE_CODE_CREDIT_EXHAUSTED",
    "USAGE_METADATA_MISSING",
}
```

Separate:

```text
immediate structural provider failure
retryable transient provider failure
ordinary validation failure
budget gating
retry exhaustion
```

Do not let the word `retry` in a message trigger a pause.

## Required tests

Prove:

1. authentication failure pauses using its code;
2. changing the human-readable message does not change the action;
3. quota or credit exhaustion pauses using its code;
4. retryable timeout does not immediately pause;
5. schema validation failure does not masquerade as provider unavailability;
6. a message containing the word `retry` does not pause without an allowed code;
7. failure metadata is sanitized;
8. raw stderr is not persisted;
9. legacy attempt rows load safely with null structured fields;
10. migration preserves existing attempts.

---

# Item 2 — Pin Codex CLI compatibility to tested JSONL contracts

## Problem

The JSONL parser was validated against Codex CLI `0.144.5`, but the provider accepts `0.144.0` and every future version.

A future CLI may produce new or changed events that the strict parser rejects.

## Required correction

Define an explicit compatibility policy.

Preferred:

```python
SUPPORTED_CODEX_CLI_RANGES = (
    VersionRange(
        minimum="0.144.5",
        maximum_exclusive="0.145.0",
        adapter_version="codex-jsonl/v1",
    ),
)
```

Alternative:

```text
exact tested versions
+
version-specific adapter registry
```

Requirements:

* minimum cannot be below the version used to validate the parser;
* future minor/major versions fail preflight until explicitly approved;
* prerelease versions fail unless explicitly tested;
* adapter version is persisted in provider provenance;
* readiness reports supported/unsupported status;
* documentation explains the compatibility policy;
* operator can update the allowed range only through versioned configuration or code review.

Do not silently accept all higher versions.

## Required tests

Test:

```text
0.144.4 → rejected
0.144.5 → accepted
0.144.9 → accepted only if within declared range
0.145.0 → rejected
1.0.0 → rejected
malformed version → rejected
prerelease → rejected unless explicitly supported
```

Also prove:

* no inference call occurs after failed version preflight;
* the selected JSONL adapter version is recorded;
* readiness exposes the supported range without leaking paths or credentials.

---

# Item 3 — Classify `turn.failed` through the typed provider taxonomy

## Problem

A Codex JSONL stream may contain a terminal:

```text
turn.failed
```

The adapter captures its message, but a zero-exit process can currently become a generic `CODEX_PROCESS_EXITED` error instead of a typed quota, rate-limit, authentication, or transient failure.

## Required correction

Create one centralized diagnostic classifier used for:

```text
nonzero process exit
terminal turn.failed
preflight failure
known JSONL error event
```

Input must be bounded and sanitized.

Output should be a typed result such as:

```python
@dataclass(frozen=True)
class CodexFailureClassification:
    code: str
    error_type: type[ResearchError]
    retryable: bool
    health_category: str
```

Classify allowlisted patterns for:

```text
authentication
quota/credit exhaustion
rate limit
temporary service failure
network failure
unsupported model
invalid configuration
schema rejection
unknown terminal failure
```

Do not expose raw terminal messages in persisted records.

Unknown failures should remain bounded and fail closed.

## Required tests

Test `turn.failed` with:

* authentication failure;
* quota exhaustion;
* rate limit;
* transient service failure;
* network timeout;
* unknown safe message;
* malicious or oversized message;
* exit code zero;
* exit code nonzero.

The same logical failure must map to the same typed code regardless of whether it arrives through stderr, a nonzero exit, or `turn.failed`.

---

# Item 4 — Make reasoning-token accounting explicit and budget-safe

## Problem

Codex usage parsing obtains:

```text
input_tokens
output_tokens
cached_input_tokens
reasoning_output_tokens
```

but reasoning tokens are not clearly persisted or included in policy ceilings.

## Required correction

First determine and document the supported Codex CLI contract:

```text
Does output_tokens include reasoning_output_tokens?
```

Do not guess.

Implement one explicit policy.

## Policy A — Reasoning is included in output

When official contract or validated fixtures prove that:

```text
reasoning_output_tokens ⊆ output_tokens
```

then:

* persist `reasoning_output_tokens` separately for audit;
* do not add it again to billed output;
* add an invariant:

```text
0 <= reasoning_output_tokens <= output_tokens
```

* reject or fail closed when the invariant is violated.

## Policy B — Reasoning is separate

When reasoning tokens are separate:

```text
total_effective_output_tokens =
    output_tokens + reasoning_output_tokens
```

Use that value for:

* per-role ceilings;
* cycle token ceilings;
* daily/monthly reporting;
* estimated API-equivalent cost;
* budget settlement;
* health reports.

## Schema changes

Extend usage records with:

```text
reasoning_output_tokens
token_accounting_policy
```

Update:

* provider response usage parsing;
* `UsageRecord`;
* attempt persistence;
* aggregate telemetry;
* budget reservation/settlement;
* reports;
* migrations;
* tests.

Do not silently discard an available usage category.

## Required tests

Prove:

1. reasoning-token value is persisted;
2. missing reasoning tokens are represented as unavailable or zero according to the external contract;
3. no double-counting occurs;
4. invalid negative values fail;
5. invalid reasoning greater than total output fails under inclusion policy;
6. token ceilings use the documented effective total;
7. budget estimates and settlement remain deterministic;
8. legacy usage rows load safely.

---

# Item 5 — Make health qualification dimension-specific

## Problem

The scheduler sends the complete single-cycle health status into persistent hysteresis, but decides whether the cycle is qualified using only the evidence-provider failure-rate check.

This can suppress failures in:

```text
retry exhaustion
unsupported claims
model-provider availability
output validation
other thresholded dimensions
```

when evidence-provider request volume is insufficient.

## Required architecture

Do not use one global boolean:

```text
qualified
```

for all health dimensions.

Create dimension-specific state.

Suggested dimensions:

```text
EVIDENCE_PROVIDER_FAILURE
MODEL_PROVIDER_FAILURE
RETRY_EXHAUSTION
UNSUPPORTED_CLAIMS
OUTPUT_TRUNCATION
BUDGET
RECONCILIATION
DUPLICATE_PREVENTION
```

Structural dimensions such as reconciliation or duplicate-prevention may remain immediate.

Rate-based dimensions should have independent:

```text
qualified
failing
sample_size
minimum_sample_size
streak
decision
reasons
```

## Suggested models

```python
@dataclass(frozen=True)
class HealthDimensionObservation:
    dimension: str
    check_status: str
    qualified: bool
    failure: bool
    immediate_pause: bool
    sample_size: int | None
    minimum_sample_size: int | None
    reasons: tuple[str, ...]
```

```python
@dataclass(frozen=True)
class HealthDimensionDecision:
    dimension: str
    previous_status: str
    new_status: str
    effective_status: str
    consecutive_failures: int
    consecutive_recoveries: int
```

The overall effective decision should be the worst of:

```text
all dimension decisions
+
structural immediate failures
+
manual pause/kill state
```

An insufficient evidence-provider sample must affect only the evidence-provider dimension.

It must not neutralize retry exhaustion or model-provider failure.

## Required tests

Prove:

1. evidence-provider sample insufficient plus retry exhaustion fail;
2. retry-exhaustion hysteresis advances;
3. evidence-provider hysteresis does not advance;
4. unsupported-claim failures advance their own streak;
5. healthy evidence-provider results do not clear model-provider streaks;
6. recovery is dimension-specific;
7. overall status reflects the worst dimension;
8. manual pause and kill remain blocking;
9. repeated cycle evaluation is idempotent per dimension;
10. history can explain which dimension caused the pause.

---

# Item 6 — Evaluate every required provider independently

## Problem

The current health result uses:

```text
aggregate provider success rate
+
missing-provider detection
```

A required provider that was called but failed completely can be diluted by successful calls to another provider.

## Required correction

Calculate a health result for each required provider or required category.

Suggested statuses:

```text
PASS
WARNING
FAIL
INSUFFICIENT_DATA
MISSING
NOT_APPLICABLE
```

For every required category persist:

```text
category
acceptable providers
observed provider
request count
success count
failure count
success rate
sample floor
status
reasons
severe categories
```

Fail closed when:

```text
required category is missing
required provider has zero successful requests
required provider status is FAIL
required provider sample is insufficient under the configured production policy
```

Choose and document whether a low request sample with successful evidence is:

```text
INSUFFICIENT_DATA
```

or acceptable for categories that naturally make one request per cycle.

Do not apply one global minimum blindly to providers with different expected request counts.

Support per-category policy such as:

```yaml
provider_health:
  required_categories:
    market_data:
      providers: [alpaca-data]
      minimum_requests: 1
      minimum_success_rate: 1.0

    corporate_filings:
      providers: [sec-edgar]
      minimum_requests: 1
      minimum_success_rate: 1.0
```

Optional-provider success must not dilute required-provider failure.

## Required tests

Prove:

1. SEC 9 successes plus Alpaca 1 failure is not healthy;
2. Alpaca healthy plus SEC absent is not healthy;
3. each required provider healthy yields healthy coverage;
4. optional news failures do not fail required coverage unless separately configured;
5. provider aliases normalize correctly;
6. category-specific sample floors work;
7. per-provider reasons are persisted;
8. aggregate rate remains informational only;
9. the failing category determines the health dimension result.

---

# Item 7 — Scope scheduled provider telemetry by cycle and scheduler run

## Problem

Scheduled provider requests persist both:

```text
research_cycle_id
scheduler_run_id
```

but operational health queries only by `research_cycle_id`.

A later scheduler invocation reusing the same deterministic cycle can see earlier requests.

## Required correction

Add an exact operational query:

```python
list_provider_requests_for_scheduled_run(
    conn,
    *,
    research_cycle_id: str,
    scheduler_run_id: str,
) -> list[dict]
```

SQL must include:

```sql
WHERE correlation_mode = 'SCHEDULED'
  AND research_cycle_id = ?
  AND scheduler_run_id = ?
ORDER BY created_at, request_id
```

Use this query for the current scheduler run’s health decision.

Retain cycle-only queries for:

```text
historical cycle-wide reporting
aggregate provenance
manual analysis
auditing
```

Do not use cycle-only telemetry for operational pause decisions.

## Scheduler resumption policy

Define what happens when a scheduler run resumes after a process crash.

Choose an explicit identity model:

```text
same scheduler_run_id resumes the same operational attempt
```

or:

```text
new scheduler_run_id creates a new operational attempt
```

Ensure telemetry follows that model.

Do not accidentally combine both.

## Required tests

Use overlapping and repeated runs:

1. scheduler run A sees only A;
2. scheduler run B sees only B;
3. same cycle ID across A and B remains separated;
4. manual cycle requests do not enter scheduled health;
5. catch-up invocation remains isolated;
6. exact resumption follows documented identity policy;
7. missing scheduler ID fails closed in scheduled mode;
8. cycle-wide reports can still aggregate both runs intentionally.

---

# Item 8 — Calculate diagnostic rates from typed transport categories

## Problem

`timeout_rate` currently counts rows based on the old generic:

```text
ProviderRequestError
```

This can include 5xx, connection failures, and non-timeout errors.

## Required correction

Compute diagnostic rates from:

```text
transport_failure_category
```

At minimum:

```text
timeout_rate
dns_failure_rate
connection_refused_rate
connection_reset_rate
tls_failure_rate
authentication_failure_rate
rate_limit_rate
http_client_error_rate
http_server_error_rate
protocol_error_rate
configuration_error_rate
unknown_transport_error_rate
```

Use exact enum/category matching.

Do not infer categories from:

```text
error message
generic exception class
missing HTTP status
```

Maintain:

```text
overall success rate
```

but make typed rates independently available.

## Required tests

Use a mixed set of persisted requests and prove:

* timeout rate counts only `TIMEOUT`;
* HTTP 500 does not count as timeout;
* DNS does not count as timeout;
* 429 counts as rate limit;
* authentication is distinct;
* successful requests count in the denominator according to documented formula;
* missing legacy category is represented explicitly;
* rates are deterministic and bounded between zero and one.

---

# Item 9 — Move hysteresis thresholds into strict frozen configuration

## Problem

The scheduler constructs:

```python
PersistentHealthPolicyConfig()
```

using hard-coded defaults.

The thresholds are versioned in Python but are not operator-configurable through the frozen deployment configuration.

## Required correction

Extend strict shadow configuration with a section such as:

```yaml
health_hysteresis:
  policy_version: persistent-health/v2

  evidence_provider:
    warning_after_failures: 1
    pause_recommended_after_failures: 2
    pause_required_after_failures: 3
    recovery_streak: 2

  model_provider:
    warning_after_failures: 1
    pause_recommended_after_failures: 2
    pause_required_after_failures: 3
    recovery_streak: 2

  retry_exhaustion:
    warning_after_failures: 1
    pause_recommended_after_failures: 2
    pause_required_after_failures: 3
    recovery_streak: 2

  unsupported_claims:
    warning_after_failures: 1
    pause_recommended_after_failures: 2
    pause_required_after_failures: 3
    recovery_streak: 2
```

Use strict parsing.

Reject:

```text
quoted integers
floats
booleans
null
zero
negative values
inconsistent ordering
unknown dimensions
unknown fields
```

Validate:

```text
warning <= recommended <= required
recovery_streak >= 1
```

Include all thresholds in:

```text
configuration hash
health policy hash
hysteresis evaluation records
readiness reports
run summaries
```

A policy change must create a new state boundary rather than applying new thresholds to an old streak.

## Required tests

Prove:

1. valid configuration loads;
2. quoted numbers fail;
3. negative values fail;
4. ordering violations fail;
5. policy hash changes when thresholds change;
6. each dimension uses its configured thresholds;
7. old state resets or versions correctly at a policy boundary;
8. safe repository defaults keep unattended scheduling disabled;
9. readiness reports the active policy version and hash.

---

# Item 10 — Make CI and merge gates enforce safety

## Workflow correction

The repository must not allow the situation seen in PRs #17 and #18 where `main-tests` failed but the PR was merged.

Update CI and repository documentation.

At minimum:

```text
main-tests must be required
paper-runtime-tests must be required
migration-smoke must be required
safety-critical type-check subset must be required
```

## Safety-critical type-check subset

Whole-project Pyright may retain a pre-existing non-blocking baseline.

Add a separate blocking command for modified production modules, for example:

```text
src/trading_research/research/codex_provider.py
src/trading_research/research/codex_jsonl_adapter.py
src/trading_research/research/orchestration.py
src/trading_research/shadow/attempt_controller.py
src/trading_research/shadow/health.py
src/trading_research/shadow/health_hysteresis.py
src/trading_research/shadow/scheduler.py
src/trading_research/evidence_providers/health.py
src/trading_research/evidence_providers/persistence.py
```

Use a dedicated strict Pyright config when necessary.

Do not globally disable errors.

## CI concurrency tests

Run deterministic concurrency tests in CI.

Avoid fragile wall-clock sleeps.

Use:

```text
barriers
events
bounded retry loops
database busy timeout
deterministic fake clocks
```

When a concurrency test fails intermittently, fix the test or implementation. Do not simply rerun the entire job until it passes.

## Documentation

Add branch-protection guidance:

* base branch must be `main` for feature delivery;
* required checks must complete before merge;
* at least one review recommended or required;
* no administrator bypass for ordinary feature PRs;
* branch-sync PRs should be labeled accurately.

Repository settings may need manual configuration outside code. Document exact steps but do not claim they were applied unless verified.

---

# Schema and migration requirements

Add versioned migrations for any new columns or tables, including:

```text
structured attempt failure fields
reasoning token fields
dimension-specific hysteresis state
dimension-specific hysteresis history
scheduler-run scoped indexes
provider/category health evidence
```

Requirements:

* preserve existing rows;
* legacy structured fields become null or explicit unknown values;
* migration is idempotent;
* forward schema versions fail safely;
* indexes support exact scheduled-run telemetry queries;
* history remains append-only;
* rolling state and evaluation history update atomically;
* prior-schema fixture begins with the exact PR #18 schema.

Add migration tests for:

```text
PR #18 database
→ current migration
→ old attempts still readable
→ old usage still readable
→ provider telemetry retained
→ hysteresis state retained or version-reset according to policy
```

---

# Cross-cutting integration scenarios

## Scenario 1 — Insufficient evidence telemetry plus retry exhaustion

```text
provider requests below sample floor
→ evidence-provider dimension = INSUFFICIENT_DATA

model role exhausts retries
→ retry-exhaustion dimension = FAIL
→ retry hysteresis advances
→ effective status reflects retry failure
```

## Scenario 2 — Required-provider dilution

```text
SEC requests: 9 successful
Alpaca requests: 1 failed
→ aggregate = 90%
→ required market-data category = FAIL
→ cycle cannot be healthy
```

## Scenario 3 — Repeated scheduler cycle identity

```text
scheduler run A executes cycle X
scheduler run B later revisits cycle X
→ A health sees only A requests
→ B health sees only B requests
→ historical cycle report can intentionally aggregate A+B
```

## Scenario 4 — Typed Codex authentication failure

```text
turn.failed contains authentication failure
process exits zero
→ CODEX_NOT_AUTHENTICATED
→ non-retryable
→ typed attempt failure persisted
→ model-provider health pauses according to structural policy
```

## Scenario 5 — Codex timeout

```text
process timeout
→ typed CODEX_PROCESS_TIMEOUT
→ retryable
→ no immediate structural pause
→ model-provider hysteresis advances only after configured repeated failures
```

## Scenario 6 — Reasoning tokens

```text
turn.completed includes input/output/reasoning usage
→ policy invariant validated
→ reasoning tokens persisted
→ effective token total computed once
→ budget and reports agree
```

## Scenario 7 — Unsupported Codex version

```text
installed version outside supported range
→ readiness fails
→ no inference subprocess
→ scheduler cycle blocked before budget reservation or provider invocation
```

---

# Required focused tests

Add or update focused modules covering:

```text
typed attempt failures
Codex version compatibility
Codex turn.failed classification
Codex reasoning-token accounting
dimension-specific health
required-provider health
scheduler-run telemetry ownership
typed transport rates
strict hysteresis configuration
CI/Linux subprocess behavior
```

Tests must remain offline.

No unit or CI test may invoke the real installed Codex or Claude Code binary.

Use fake executables.

---

# Final verification

Run:

```bash
pytest tests/ -q --tb=short
```

Run again with credential-shaped variables removed:

```bash
env \
  -u OPENAI_API_KEY \
  -u CODEX_API_KEY \
  -u ANTHROPIC_API_KEY \
  -u ANTHROPIC_AUTH_TOKEN \
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
cd ..
```

Run the new blocking safety subset:

```bash
pyright --project pyright-safety.json
```

Also run the existing full checks honestly:

```bash
pyright
cd paper_runtime && pyright
cd ..
```

Run:

```bash
git diff --check
git status --short
```

Run focused concurrency and subprocess tests repeatedly:

```bash
for i in {1..20}; do
  pytest \
    tests/unit/test_codex_provider*.py \
    tests/unit/test_codex_jsonl*.py \
    tests/unit/test_health_dimension*.py \
    tests/unit/test_provider_health*.py \
    tests/unit/test_scheduler_telemetry*.py \
    -q || exit 1
done
```

Use actual filenames after implementation.

When possible, run the complete suite in a fresh Linux Python 3.11 environment matching GitHub Actions.

No real provider, model, or broker call may occur.

---

# Acceptance criteria

The milestone is complete only when:

1. The exact PR #17/#18 CI failure is identified.
2. `main-tests` passes in the GitHub-equivalent environment.
3. Failed attempts persist stable typed failure codes.
4. Provider-health actions no longer inspect free-text messages.
5. Codex compatibility begins at the actually tested CLI version.
6. Untested future Codex versions fail preflight.
7. `turn.failed` uses the same typed taxonomy as nonzero exits.
8. Quota, auth, rate-limit, transient, and unknown failures remain distinct.
9. Reasoning-token semantics are documented and validated.
10. Reasoning-token data is persisted when available.
11. Token budgets do not double-count or discard reasoning usage.
12. Health qualification is dimension-specific.
13. An insufficient evidence-provider sample cannot suppress retry-exhaustion failure.
14. Required providers are evaluated independently.
15. Optional-provider success cannot dilute a required-provider failure.
16. Scheduled operational health is scoped by both cycle and scheduler run.
17. Manual and prior-run telemetry cannot contaminate current scheduled health.
18. Diagnostic rates use typed transport categories.
19. Timeout rate counts only timeout events.
20. Hysteresis thresholds come from strict frozen configuration.
21. Policy changes create explicit hash/version boundaries.
22. Safety-critical type checking is blocking.
23. Main, paper-runtime, migration, and safety checks are green.
24. External paper submission remains disabled.
25. Recurring scheduling remains research-only.
26. Live trading remains unavailable.
27. No real provider or broker call occurred.
28. No commit or push occurred.

---

# Final response

Keep the final response concise.

Report:

1. starting and ending commit;
2. exact CI failure root cause;
3. finding classifications;
4. typed attempt-failure changes;
5. Codex version policy;
6. terminal failure classification;
7. reasoning-token policy;
8. dimension-specific health changes;
9. required-provider health changes;
10. scheduler-run telemetry changes;
11. diagnostic-rate changes;
12. hysteresis configuration changes;
13. migrations;
14. CI and type-check changes;
15. tests and results;
16. remaining limitations;
17. operational go/no-go status;
18. confirmation that no real provider or broker call occurred;
19. confirmation that no commit or push occurred.

End with:

| Capability                      | Status                              |
| ------------------------------- | ----------------------------------- |
| Deterministic research          | READY / LIMITED                     |
| Manual Codex research           | READY / SUPERVISED_ONLY / NOT_READY |
| Manual Claude Code research     | READY / SUPERVISED_ONLY / NOT_READY |
| Local simulated paper trading   | READY / LIMITED                     |
| Manual soak campaigns           | READY / LIMITED                     |
| Unattended scheduled research   | READY / KEEP_DISABLED               |
| External Alpaca paper execution | READY / KEEP_DISABLED               |
| Real Alpaca paper smoke         | READY / NOT_READY                   |
| Live trading                    | NOT_IMPLEMENTED                     |

Use conservative statuses supported by test evidence.
