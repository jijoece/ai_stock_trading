You are continuing implementation of my existing AI-driven trading-desk repository.

This is a new Claude Code session focused exclusively on:

# Milestone 7.1 — Shadow-control runtime integration closure

Milestones 1 through 7 are already implemented.

Milestone 7 built and tested the major shadow-operations components, including corporate-status evidence, evidence-completeness classification, scheduler support, leases, pause/kill state, budget reservation, role-budget logic, alerts, health evaluation, readiness reporting, CLI commands, and deployable launchd artifacts.

However, several components currently exist as individually tested modules without being fully connected to the real scheduled-cycle runtime.

Your task is to close those specific integration gaps.

Do not restart Milestone 7, redesign the entire architecture, create a new project, or begin broad Milestone 8 work.

This is a direct implementation task. Do not stop after writing an investigation or architecture report. Inspect the actual code, implement the missing runtime wiring, add regression tests, run the complete suites, perform narrowly gated real validation where available, and document the actual results.

---

# Primary goal

Complete this end-to-end runtime path:

```text
Due shadow-cycle invocation
        ↓
Resolve real provider and model identity
        ↓
Acquire lease
        ↓
Reserve model-specific budget
        ↓
Build real point-in-time evidence
        ↓
Build and persist corporate-status evidence
        ↓
Normalize corporate status into EvidenceSnapshot
        ↓
Evaluate and persist screening/research completeness
        ↓
Block Claude when critical completeness fails
        ↓
Before every Claude attempt:
    enforce role/attempt/token/latency/cost budget
        ↓
After every Claude attempt:
    record actual observed usage idempotently
        ↓
Complete deterministic baseline and enhanced shadow flow
        ↓
Aggregate research telemetry
        ↓
Settle reservation using actual priced usage
        ↓
Populate health and readiness using real telemetry
        ↓
Persist run summary and alerts
        ↓
Release lease
```

The implementation must preserve the existing authority model:

```text
Evidence providers:
    Retrieve and normalize facts.

Claude:
    Analyze supplied evidence only.

Deterministic application code:
    Validate, screen, score, size, freeze, schedule, budget,
    pause, execute, reconcile, evaluate, alert, and promote.
```

---

# Mandatory source-of-truth review

Before editing, read:

```text
.claude/scratchpads/milestone7-progress.md
docs/milestone7-production-shadow-operations.md
docs/adr/0005-production-shadow-operations-boundary.md
docs/runbooks/shadow-operations.md
docs/runbooks/shadow-incident-response.md
docs/milestone6-real-evidence-continuous-evaluation.md
docs/adr/0004-real-evidence-provider-boundary.md
docs/milestone5-evidence-backed-claude-research.md
docs/adr/0003-claude-research-boundary.md
```

Also inspect the current implementations of:

```text
src/trading_research/research/evidence.py
src/trading_research/research/evidence_completeness.py
src/trading_research/research/orchestration.py
src/trading_research/research/scheduled_cycle.py
src/trading_research/research/models.py
src/trading_research/research/configuration.py
src/trading_research/research/usage.py
src/trading_research/research/cost_tracking.py

src/trading_research/evidence_providers/corporate_status.py
src/trading_research/evidence_providers/corporate_status_adapters.py
src/trading_research/evidence_providers/filing_documents.py
src/trading_research/evidence_providers/disclosure_extraction.py
src/trading_research/evidence_providers/evidence_adapters.py
src/trading_research/evidence_providers/fixture_clients.py
src/trading_research/evidence_providers/sec_provider.py

src/trading_research/shadow/scheduler.py
src/trading_research/shadow/budget.py
src/trading_research/shadow/role_budget.py
src/trading_research/shadow/health.py
src/trading_research/shadow/readiness.py
src/trading_research/shadow/config.py
src/trading_research/shadow/alerts.py

src/trading_research/storage/corporate_status_repositories.py
src/trading_research/storage/research_repositories.py
src/trading_research/storage/research_cycle_repositories.py
src/trading_research/storage/shadow_operations_repositories.py
src/trading_research/storage/shadow_operations_schema.py
src/trading_research/storage/shadow_alerts_schema.py

src/trading_research/cli.py
config/research.yaml
config/research_pricing.yaml
config/scheduled_research.yaml
config/shadow_operations.yaml
config/evidence_providers.yaml
```

Use the current code and scratchpad as the source of truth. The names in this prompt are starting points, not permission to duplicate abstractions that already exist.

---

# Mandatory scratchpad

Create before implementation edits:

```text
.claude/scratchpads/milestone7-1-progress.md
```

Use this structure:

```markdown
# Milestone 7.1 Progress

Started: <UTC timestamp>
Branch: <branch>
Status: STARTING

## Baseline

## Current integration gaps confirmed

## Architecture decisions

## Corporate-status pipeline integration

## Evidence-completeness gating

## Attempt-control hook design

## Per-attempt budget enforcement

## Model and pricing propagation

## Usage telemetry design

## Budget reservation and settlement

## Health and readiness telemetry

## Fixture corrections

## CLI real-mode wiring

## Schema and migration changes

## Files created

## Files modified

## Tests added

## Test run log

## Real validation

## Bugs discovered and fixed

## Security and secret review

## Documentation consistency review

## Known limitations

## Deferred work

## Final status
```

Scratchpad rules:

1. Update it after every major step.
2. Preserve failures and rejected approaches.
3. Record actual commands and results.
4. Record exact baseline and final test counts.
5. Never include:

   * API keys;
   * secrets;
   * `.env` contents;
   * authorization headers;
   * raw Claude responses;
   * raw prompts;
   * chain-of-thought;
   * brokerage account identifiers.
6. Credential checks must report Boolean presence only.
7. Distinguish code completion from real environmental validation.
8. Do not commit or push unless explicitly asked.

---

# Confirmed starting state

Verify this before editing:

```text
Main suite:
1174 passed, 12 skipped

Paper-runtime suite:
33 passed
```

Milestone 7 reported:

* 414 new tests;
* zero regressions;
* real SEC corporate-status validation;
* one real SEC-backed shadow cycle;
* two real Claude shadow-cycle calls;
* no paper submissions;
* no enhanced execution;
* real news environmentally pending;
* real Reddit environmentally pending;
* launchd artifact created but not activated;
* no recurring deployment activated.

Known runtime integration gaps include:

1. Corporate-status evidence is not included in the primary `EvidenceSnapshot`.
2. No corporate-status provider is connected to `build_evidence_snapshot`.
3. `evaluate_completeness` is not automatically called from the scheduled-cycle path.
4. Corporate completeness does not yet gate Claude calls.
5. `check_role_budget` is not called immediately before actual role attempts.
6. Actual Claude attempt usage is not incrementally charged to the shadow reservation.
7. Final shadow budget consumption does not accurately reflect persisted Claude usage.
8. Scheduler health receives `None` for several research-quality and usage fields.
9. `CycleIntent.model_name` is not populated correctly.
10. Model-specific pricing lookup is therefore incomplete.
11. `FixtureSecClient.list_filings()` returns no useful deterministic filings.
12. The real scheduler CLI path is not fully assembled for an explicit real-provider mode.
13. ADR 0005 describes some target behaviors as active even though Milestone 7 documentation records them as not wired.

Do not assume this list is perfectly complete. Confirm each item from code before changing it.

---

# Hard safety boundaries

Do not:

* enable live trading;
* add a live-trading CLI flag;
* enable enhanced-arm execution;
* enable Robinhood mutation;
* expose broker tools to Claude;
* expose provider credentials to Claude;
* let Claude decide budget eligibility;
* let Claude decide evidence completeness;
* let Claude decide pause or kill state;
* weaken claim-to-evidence validation;
* accept unsupported numeric claims;
* convert `NOT_FOUND_IN_SEARCHED_SOURCES` to `FALSE`;
* convert public-reporting history into company age;
* silently populate `operating_history_years` with SEC filing history;
* insert current evidence into a historical snapshot;
* substitute a current quote for historical market data;
* treat budget exhaustion as provider failure;
* treat a skipped budget-gated attempt as a failed provider request;
* fabricate zero tokens, zero cost, or zero latency when data is unavailable;
* double-charge a retry or resumed run;
* activate launchd;
* run an infinite daemon;
* install a recurring schedule;
* modify `real_orders`;
* weaken recommendation immutability;
* automatically promote the enhanced arm;
* perform unrelated broad refactoring.

---

# Non-goals

Do not implement in this milestone:

* additional news vendors;
* real Reddit credentials or app registration;
* remaining deferred corporate-action types;
* destructive retention;
* separate baseline/enhanced paper books;
* portfolio market-value redesign;
* MFE/MAE;
* live promotion;
* actual recurring scheduler activation;
* a new broker;
* a new research model;
* a replacement for SQLite;
* an LLM-based corporate disclosure extractor;
* a semantically incorrect workaround for `operating_history_years`.

---

# Step 1 — Establish baseline and working-tree state

Before changing code:

1. Check branch and Git status.
2. Identify all uncommitted Milestone 7 files.
3. Do not reset or discard anything.
4. Confirm database paths used by tests and local CLI.
5. Run:

```bash
pytest tests/ -q
```

Expected:

```text
1174 passed, 12 skipped
```

6. Run:

```bash
cd paper_runtime
pytest tests/ -q
```

Expected:

```text
33 passed
```

7. Record exact outcomes in the scratchpad.

If the baseline differs, investigate and document the reason before proceeding.

---

# Step 2 — Confirm the actual runtime gaps

Trace one scheduled shadow run end to end:

```text
cli.run_due_shadow_cycle_cli
→ shadow.scheduler.run_due_shadow_cycle
→ research.scheduled_cycle.run_scheduled_research_cycle
→ per-symbol evidence construction
→ research committee
→ cycle result
→ budget settlement
→ health
→ readiness
```

Document where these values are lost or never connected:

* corporate-status result;
* completeness result;
* research run IDs;
* model name;
* role name;
* attempt number;
* input tokens;
* output tokens;
* latency;
* pricing status;
* priced attempt cost;
* retry count;
* retry-exhaustion count;
* provider failures;
* unsupported-claim count;
* output-truncation count;
* budget-gated role count.

Do not start schema or API changes until the data-flow gaps are explicitly recorded.

---

# Step 3 — Reconcile ADR 0005 with implementation

ADR 0005 currently describes the target behavior for:

* completeness gating;
* role-level pre-call budget checks;
* actual usage settlement.

Milestone 7 documentation records that some of these behaviors are not active.

Use Milestone 7.1 to make the implementation match the accepted ADR wherever this prompt requires.

After implementation:

* update ADR 0005 to identify Milestone 7.1 as the closure point;
* do not leave architecture prose claiming behavior that still does not exist;
* mark any remaining target behavior explicitly as pending.

Do not rewrite the ADR history to imply Milestone 7 originally completed these integrations.

---

# Step 4 — Add a corporate-status evidence-provider boundary

Inspect the existing evidence-provider Protocols before adding anything.

Add the smallest backward-compatible extension needed for corporate status.

A likely contract is:

```python
class CorporateStatusEvidenceProvider(Protocol):
    def fetch(
        self,
        symbol: str,
        as_of: datetime,
    ) -> CorporateStatusEvidence:
        ...
```

Alternatively, use the existing `EvidenceBundle` provider shape if that fits without losing the typed `CorporateStatusEvidence` needed by completeness evaluation and persistence.

Requirements:

* preserve typed `CorporateStatusEvidence`;
* support a real SEC-backed implementation;
* support a deterministic fixture implementation;
* no model call;
* point-in-time-safe;
* no current filing leakage;
* provenance retained;
* no false negative derived from absent metadata;
* optional for existing callers so Milestone 1–7 tests remain compatible;
* missing provider must produce an explicit unknown/unavailable state when corporate completeness is required.

Do not create parallel SEC clients when the existing `SecEdgarClient` can be reused.

---

# Step 5 — Normalize corporate status into the EvidenceSnapshot

Convert corporate-status facts into bounded, provenance-backed evidence items.

Include only facts that can be supported precisely, such as:

```text
corporate reporting status
earliest reliable SEC filing date
latest annual filing
latest quarterly filing
late-filing notices
bankruptcy-related signals
delisting signals
registration-termination signals
shell-company signals
going-concern extraction outcome
disclosure extraction rule version
```

Requirements:

* stable evidence IDs;
* source accession number where applicable;
* source form type;
* accepted/available timestamp;
* point-in-time state;
* bounded content;
* no full filing text;
* no unsupported statement that a risk is absent;
* `NOT_FOUND_IN_SEARCHED_SOURCES` retained verbatim;
* corporate-status evidence participates in canonical snapshot hashing;
* provider name included in `used_providers`;
* replay remains deterministic.

Do not expose raw SEC HTML to Claude.

---

# Step 6 — Compose metadata and bounded disclosure extraction

Inspect the existing `derive_corporate_status`, filing-document retrieval, and deterministic disclosure extractor.

Create one safe composition path that can:

1. Retrieve point-in-time filing metadata.
2. Identify the bounded filing documents required for supported disclosure checks.
3. Retrieve only the required documents.
4. Apply deterministic extraction.
5. Merge extraction results into the typed corporate-status result.
6. Preserve metadata-only uncertainty when document retrieval is unavailable or incomplete.

Requirements:

* bounded number of documents;
* bounded document size;
* deterministic rule version;
* no LLM extraction;
* no “not found” converted into false;
* no filing accepted after `as_of`;
* explicit `SEARCH_INCOMPLETE`;
* explicit `DOCUMENT_UNAVAILABLE`;
* extraction failures fail closed;
* no unrestricted full-document persistence outside existing documented policy.

Avoid making every cycle retrieve an unbounded filing history.

---

# Step 7 — Persist corporate-status and completeness association per cycle symbol

The standalone corporate-status and completeness tables already exist.

Connect them to the cycle/symbol that produced them.

Prefer additive persistence.

Possible approach:

```text
research_cycle_symbol_evidence_status
```

with:

```text
cycle_id
symbol
snapshot_id
corporate_status_evidence_id
completeness_result_id
screening_status
research_status
blocking_reasons_json
policy_version
created_at
```

Do not add a new table when existing schema can represent this relationship cleanly.

Requirements:

* stable association;
* idempotent save;
* immutable result;
* queryable by cycle and symbol;
* reused on an idempotent resume;
* no duplicate corporate-status retrieval for a completed symbol;
* no destructive schema migration.

---

# Step 8 — Activate evidence-completeness evaluation in the scheduled cycle

Call `evaluate_completeness` automatically after:

* the primary evidence snapshot is built;
* corporate-status evidence is built;
* both are persisted.

Persist its result before any Claude call.

Define explicit blocking behavior.

At minimum, block enhanced research for:

```text
MISSING_CRITICAL_CORPORATE_STATUS
MISSING_CRITICAL_MARKET_DATA
MISSING_CRITICAL_FUNDAMENTALS
CONFLICTING_CRITICAL_DATA
POINT_IN_TIME_UNSAFE
PROVIDER_UNAVAILABLE
```

Use the existing policy’s exact output instead of introducing a second competing list when possible.

Requirements:

* blocking result skips Claude before any provider call;
* no Claude token usage occurs;
* no research attempt is fabricated;
* baseline deterministic result remains persisted;
* symbol result records the completeness reason;
* enhanced result remains non-executable;
* no paper intent is created from the enhanced arm;
* news or sentiment absence alone does not block deterministic screening unless policy says so;
* no model can alter completeness;
* idempotent resume reuses the persisted completeness result.

Do not silently treat unknown corporate status as safe.

---

# Step 9 — Preserve operating-history semantics

Do not wire the SEC public-reporting-history proxy into:

```text
CandidateInput.operating_history_years
```

unless the existing screener explicitly expects public-reporting history rather than actual operating history and that fact is proven from code and documentation.

Expected Milestone 7.1 behavior:

* retain the public-reporting-history proxy as separately named evidence;
* expose it to Claude with accurate semantics;
* preserve it in corporate-status persistence;
* leave `operating_history_years` unknown when actual operating history is not known;
* keep deterministic screening fail-closed.

Do not make the candidate pass screening merely to demonstrate integration.

---

# Step 10 — Correct the SEC fixture

Update `FixtureSecClient.list_filings()` to return deterministic filing records.

Include fixtures for:

* annual filing;
* quarterly filing;
* optional amendment;
* late-filing notice;
* historical earliest filing;
* future filing that must be excluded;
* one risk-signal fixture where useful.

Requirements:

* acceptance timestamps;
* accession numbers;
* form types;
* point-in-time filtering;
* stable deterministic ordering;
* no network;
* compatible with current fixture tests;
* enough data for the complete offline corporate-status path.

Do not disable the filing provider in end-to-end fixture tests as a workaround.

---

# Step 11 — Propagate actual provider and model identity

Remove any hardcoded:

```text
model_name=None
```

from real scheduler intent construction.

Thread the actual configured values through:

```text
research configuration
→ scheduled-cycle configuration
→ shadow-cycle invocation
→ CycleIntent
→ budget estimate
→ pricing selection
→ research attempts
→ telemetry
→ run summary
```

Requirements:

* one source of truth for provider and model;
* configuration hash includes relevant identity;
* model name persisted;
* prompt/model diagnostics remain accurate;
* scheduled Anthropic runs fail closed when model is missing;
* scheduled Anthropic runs fail closed when no pricing entry matches provider/model/date;
* deterministic and scripted providers remain usable without Anthropic pricing;
* no guessed model;
* no automatic model change based on environment variables alone.

Use the existing model configuration convention rather than adding a duplicate model field when possible.

---

# Step 12 — Introduce generic attempt-control hooks

Do not import `shadow` modules directly into core research orchestration.

Add a small framework-neutral optional interface around role attempts.

A possible shape:

```python
@dataclass(frozen=True)
class AttemptControlRequest:
    research_run_id: str
    symbol: str
    role: str
    attempt_number: int
    model_name: str
    prompt_version: str
    prompt_hash: str
    max_input_tokens: int | None
    max_output_tokens: int
    requested_at: datetime


@dataclass(frozen=True)
class AttemptControlDecision:
    allowed: bool
    code: str
    reason: str | None


class ResearchAttemptController(Protocol):
    def before_attempt(
        self,
        request: AttemptControlRequest,
    ) -> AttemptControlDecision:
        ...

    def after_attempt(
        self,
        request: AttemptControlRequest,
        attempt: ResearchAttemptRecord,
    ) -> None:
        ...
```

Use repository conventions and existing models instead of copying this literally if a better extension point exists.

Requirements:

* optional and backward-compatible;
* default no-op behavior;
* called before every provider attempt, including retries and manager;
* called after every completed provider attempt;
* after hook runs for:

  * valid output;
  * schema rejection;
  * claim rejection;
  * provider error where an attempt record exists;
  * malformed output;
  * output truncation;
* no provider call when before hook denies;
* denial is not classified as provider failure;
* denial has a clear operational reason;
* no direct SQLite dependency in research orchestration;
* existing analyst-only and manager-required behavior remains intact.

---

# Step 13 — Connect role-budget enforcement to the attempt controller

Create a shadow-specific attempt controller that adapts:

```text
ResearchAttemptController
→ shadow.role_budget.check_role_budget
→ shadow budget reservation
```

Before each attempt, check:

* allowed role;
* symbol count where relevant;
* role index;
* attempt number;
* maximum attempts;
* remaining input-token budget;
* remaining output-token budget;
* remaining latency budget;
* remaining cost budget;
* selected provider/model pricing.

Use the same pricing entry used by the cycle reservation.

Do not perform a second inconsistent pricing lookup.

When denied:

```text
decision = SKIPPED_BUDGET_EXHAUSTED
```

Requirements:

* no Anthropic call;
* no research provider call;
* no provider-failure record;
* no fabricated attempt usage;
* research result becomes explicitly incomplete when a required role is skipped;
* remaining roles and symbols are not started when the cycle budget cannot support them;
* budget-gated count is included in telemetry;
* manager is budget-checked separately;
* retries are individually budget-checked.

---

# Step 14 — Persist role-budget checks

For auditability, persist each pre-attempt budget decision.

Prefer an additive append-only table such as:

```text
shadow_role_budget_checks
```

Suggested fields:

```text
check_id
reservation_id
scheduler_run_id
cycle_id
research_run_id
symbol
role
attempt_number
provider
model_name
decision
reason
remaining_input_tokens
remaining_output_tokens
remaining_latency_ms
remaining_cost_usd
maximum_attempt_input_tokens
maximum_attempt_output_tokens
maximum_attempt_latency_ms
maximum_attempt_cost_usd
checked_at
```

Requirements:

* deterministic/idempotent check identity;
* append-only;
* no credentials;
* Decimal cost storage;
* queryable by scheduler run, role, symbol, and decision;
* same attempt must not create duplicate check rows on resume;
* successful real validation can prove the check ran before each Claude call.

Do not create this table when an existing audit table cleanly supports the same facts.

---

# Step 15 — Record actual usage after every attempt

Connect the attempt controller’s `after_attempt` hook to shadow-budget usage.

Record observed:

* attempt ID;
* input tokens;
* output tokens;
* latency;
* priced usage cost;
* provider;
* model;
* role;
* attempt number;
* success status.

Requirements:

* use actual provider response metadata;
* no token fabrication;
* no cost fabrication;
* cost is calculated from actual observed tokens and the selected versioned pricing entry;
* label it as priced observed usage, not provider invoice data;
* retry usage is charged;
* failed validation after a successful provider response is charged;
* non-retryable provider errors with no tokens record only available latency/status;
* idempotent on attempt ID;
* resumed cycles do not double-charge;
* daily/monthly usage sees the incremental consumption;
* the next role-budget check sees updated remaining budget.

If `shadow_budget_usage` lacks an idempotency key, add the smallest safe additive schema change or companion table.

---

# Step 16 — Define cycle telemetry

Add an immutable aggregate representing actual research-cycle usage and quality.

A suitable model may include:

```python
@dataclass(frozen=True)
class ResearchCycleTelemetry:
    status: str
    research_run_ids: tuple[str, ...]
    attempt_count: int
    successful_attempt_count: int
    failed_attempt_count: int
    retry_count: int
    retry_exhaustion_count: int
    required_role_failure_count: int
    provider_failure_count: int
    unsupported_claim_count: int
    output_truncation_count: int
    budget_skipped_attempt_count: int
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    priced_usage_cost_usd: Decimal | None
    pricing_status: str
    missing_usage_record_count: int
```

Use existing terminology where possible.

Requirements:

* derived from authoritative persisted attempts/failures/budget checks;
* no duplicated in-memory counter as sole source of truth;
* exact research-run association;
* status distinguishes:

  * COMPLETE;
  * PARTIAL;
  * UNAVAILABLE;
* counts may be zero only when the relevant authoritative data was inspected;
* token/cost fields remain `None` when genuinely unavailable;
* no unknown cost represented as zero;
* Decimal for money;
* retries distinguished from first attempts;
* budget skips distinguished from provider failures.

Expose telemetry through `ResearchCycleResult`, an associated repository query, or another minimal authoritative boundary.

---

# Step 17 — Settle budget from actual usage

Change the scheduler’s settlement path so:

```text
budget_consumed_usd
```

is based on actual observed and priced research usage.

Requirements:

* reserved estimate is greater than or equal to the pre-run worst-case estimate;
* consumed cost equals the idempotently recorded attempt-level priced usage;
* unused reservation is released;
* settlement is idempotent;
* final aggregate reconciles to attempt-level usage;
* mismatch is explicit;
* mismatch never silently settles to zero;
* emergency-margin breach is checked;
* breach triggers configured pause/alert behavior;
* exceptions still settle or expire the reservation safely;
* partial cycles retain their usage;
* no cost is charged twice at both attempt and cycle level.

For deterministic/scripted providers:

* cost may remain `None` or zero according to existing conventions;
* do not require Anthropic pricing;
* never fabricate a Claude cost.

---

# Step 18 — Populate health with real telemetry

Feed actual values into `CycleHealthInputs`:

* provider success rate;
* evidence-completeness rate;
* Claude role success rate;
* retry rate;
* retry-exhaustion rate;
* unsupported-claim rate;
* output-truncation rate;
* input tokens;
* output tokens;
* latency;
* priced cost;
* pricing configured;
* budget breach;
* reconciliation mismatch;
* duplicate-prevention violation;
* cycle duration.

Requirements:

* no `None` merely because the scheduler failed to propagate an available value;
* retain `None` where data genuinely does not exist;
* no unavailable metric converted to zero;
* health reasons identify telemetry incompleteness;
* partial telemetry cannot produce an unjustified `HEALTHY`;
* persisted `shadow_run_summaries` uses the same authoritative aggregate;
* readiness queries use these populated summaries.

---

# Step 19 — Readiness and promotion reporting

Ensure readiness consumes the newly populated telemetry.

At minimum verify:

* real-provider cycle count;
* completed cycle count;
* evidence-completeness rate;
* role-completion rate;
* retry-exhaustion rate;
* unsupported-claim rate;
* cost per completed cycle;
* scheduler stability;
* budget breaches;
* alert-delivery status.

Do not declare readiness from one successful cycle.

Do not change minimum-sample floors merely to produce a positive status.

Where the existing promotion-status CLI has Milestone 7 fields that are currently never populated, wire them only when the new telemetry provides authoritative values.

Do not expand promotion authority and do not create a live-trading status.

---

# Step 20 — Add explicit fixture and real provider modes to the scheduler CLI

Inspect current `run-due-shadow-cycle` CLI assembly.

Support an explicit provider mode consistent with the existing CLI conventions:

```bash
python -m trading_research.cli run-due-shadow-cycle \
  --provider-mode fixture
```

and:

```bash
python -m trading_research.cli run-due-shadow-cycle \
  --provider-mode real
```

Requirements:

* fixture remains safe for offline tests;
* real mode is explicit;
* credentials do not silently switch the mode;
* shipped configuration remains disabled by default;
* real mode builds the real SEC/corporate-status provider;
* real mode builds the configured market/news/sentiment providers only when enabled;
* missing required provider configuration fails closed;
* missing Anthropic pricing fails before a Claude call;
* no live-trading option;
* enhanced submission remains impossible;
* structured JSON output identifies provider mode, provider identities, model, telemetry status, and budget outcome;
* no credentials printed.

Update the launchd example documentation to show the explicit intended mode, but do not install or activate it.

---

# Step 21 — Offline end-to-end tests

Add an offline integration test proving:

```text
due scheduler invocation
→ lease acquired
→ model/pricing resolved
→ budget reserved
→ fixture SEC filings returned
→ corporate status built
→ corporate status normalized into snapshot
→ completeness persisted
→ completeness allows research
→ role budget checked before analyst attempt
→ scripted analyst call
→ actual usage recorded
→ role budget checked before manager attempt
→ scripted manager call
→ actual usage recorded
→ enhanced result remains shadow-only
→ cycle telemetry aggregated
→ consumed budget reconciles
→ health receives populated telemetry
→ run summary persisted
→ lease released
```

Also test:

## Blocking completeness

```text
critical corporate status unknown
→ completeness persisted
→ Claude never called
→ no role-budget check required
→ no Claude usage
→ explicit incomplete symbol result
→ enhanced execution absent
```

## Mid-cycle budget exhaustion

```text
analyst call consumes most reservation
→ manager pre-attempt check denied
→ no manager provider call
→ SKIPPED_BUDGET_EXHAUSTED persisted
→ not counted as provider failure
→ cycle incomplete
→ actual analyst usage retained
→ reservation settled correctly
```

## Retry accounting

```text
first attempt rejected
→ tokens/cost charged
→ second attempt budget checked using reduced balance
→ second attempt succeeds
→ both attempt usages retained
→ no double charge
```

## Resume idempotency

```text
completed cycle invoked again
→ no new provider call
→ no new Claude call
→ no duplicate budget checks
→ no duplicate usage
→ no duplicate completeness result
```

## Pricing failure

```text
anthropic provider + unknown model pricing
→ fail before lease work that could spend money, or before any Claude call
→ no provider call
→ explicit pricing failure
```

Follow existing scheduler ordering and document where the pricing preflight belongs relative to lease acquisition.

---

# Step 22 — Unit tests

Add tests for:

## Corporate-status provider

* real-adapter shape using fixtures;
* deterministic fixture provider;
* annual filing;
* quarterly filing;
* amendment;
* future filing exclusion;
* metadata plus disclosure merge;
* source unavailable;
* search incomplete;
* stable evidence IDs;
* snapshot hashing includes corporate status.

## Completeness integration

* result persisted;
* cycle-symbol association;
* blocking status;
* nonblocking missing news;
* nonblocking missing sentiment;
* unsafe corporate evidence;
* conflicting evidence;
* idempotent reuse.

## Attempt-control hooks

* no-op default;
* before hook called once per attempt;
* retries separately checked;
* manager separately checked;
* denied attempt never calls provider;
* after hook called after validation rejection;
* after hook called after valid output;
* no shadow import in core orchestration.

## Role budget

* same pricing entry as reservation;
* allowed role;
* disallowed role;
* attempt limit;
* input-token limit;
* output-token limit;
* latency limit;
* cost limit;
* manager estimate;
* denial not provider failure.

## Usage accounting

* one usage row per attempt;
* idempotent duplicate;
* retry charged;
* validation failure charged;
* provider error with unavailable token data;
* Decimal cost;
* daily cap includes current usage;
* monthly cap includes current usage.

## Telemetry

* complete;
* partial;
* unavailable;
* retries;
* retry exhaustion;
* unsupported claim;
* output truncation;
* budget skip;
* provider failure;
* unknown cost remains unknown.

## Health/readiness

* populated telemetry;
* partial telemetry not healthy;
* cost reconciliation;
* budget breach;
* single successful real cycle still insufficient for readiness.

## CLI

* fixture mode;
* real mode;
* explicit mode required or documented default;
* missing credentials;
* missing pricing;
* model included;
* sanitized output;
* disabled no-op;
* no live flag.

---

# Step 23 — Real validation

After all offline tests pass, perform a narrowly bounded real validation only when required credentials and pricing are available.

Use:

* one symbol;
* real SEC corporate-status evidence;
* real Claude;
* bounded role set, preferably `bear` plus `manager`;
* maximum one attempt per role for the smoke test unless the existing test convention requires two;
* fixture market data when real market-data credentials are unavailable, clearly labeled;
* no news when credentials are unavailable;
* no Reddit when credentials are unavailable;
* no paper submission;
* no enhanced execution;
* temporary test database;
* explicit cost cap;
* explicit latency cap;
* no scheduler activation.

The real validation must prove:

```text
corporate status was fetched from real SEC
corporate status entered the EvidenceSnapshot
completeness result was persisted
completeness gate was evaluated
CycleIntent contained the actual model
model-specific pricing matched
reserved estimated cost > 0
role-budget check persisted before analyst
role-budget check persisted before manager
real Claude attempts occurred only after approved checks
actual input tokens > 0
actual output tokens > 0
actual latency > 0
priced consumed cost > 0
consumed cost equals persisted attempt-level priced usage
health received real retry/token/latency/cost values
run summary contains the same values
paper submissions = 0
enhanced executions = 0
lease released
```

Do not print:

* raw responses;
* raw prompts;
* credentials;
* full `.env`;
* authorization headers.

Record only sanitized:

```text
scheduler_run_id
cycle_id
research_run_ids
symbol
provider identities
model
corporate-status ID
completeness status
role-budget decisions
attempt count
input tokens
output tokens
latency
reserved cost
consumed priced cost
health status
paper submission count
```

If pricing or credentials are missing, report the exact environmental block and do not bypass the preflight.

Do not add guessed pricing merely to make the test run.

---

# Step 24 — Documentation

Create:

```text
docs/milestone7-1-shadow-integration-closure.md
```

Update:

```text
docs/milestone7-production-shadow-operations.md
docs/adr/0005-production-shadow-operations-boundary.md
docs/runbooks/shadow-operations.md
docs/runbooks/shadow-incident-response.md
```

Document:

* original Milestone 7 integration gaps;
* corporate-status provider integration;
* snapshot evidence representation;
* disclosure-composition behavior;
* completeness gating;
* attempt-controller boundary;
* role-budget enforcement;
* actual usage accounting;
* telemetry aggregation;
* model/pricing propagation;
* scheduler real mode;
* budget reconciliation;
* health/readiness population;
* offline tests;
* real validation;
* remaining limitations.

Explicitly distinguish:

```text
MODULE IMPLEMENTED
RUNTIME INTEGRATED
REAL VALIDATED
ENVIRONMENTALLY PENDING
ACTUAL RECURRING DEPLOYMENT ACTIVATED
```

Do not claim recurring activation. No `launchctl load` should be run.

---

# Security and safety review

Before completion, verify:

* no secret committed;
* no `.env` printed;
* no provider header persisted;
* no account ID persisted;
* no Claude access to SEC, Alpaca, Reddit, MCP, or broker tools;
* no paper submission from enhanced arm;
* no live trading;
* no Robinhood mutation;
* no budget decision influenced by Claude;
* no completeness decision influenced by Claude;
* no unknown cost treated as zero;
* no duplicate attempt charge;
* no duplicate reservation settlement;
* no provider call after budget denial;
* no provider-failure metric increment from budget denial;
* no future filing in historical snapshot;
* no `NOT_FOUND_IN_SEARCHED_SOURCES` converted to false;
* no public-reporting history mislabeled as company age;
* no screener weakening;
* no recommendation immutability weakening;
* `real_orders` remains write-blocked;
* launchd remains inactive;
* existing tests were not weakened, deleted, or newly skipped to obtain a pass.

---

# Suggested implementation order

Proceed in this order:

1. Create the Milestone 7.1 scratchpad.
2. Inspect Git state and baselines.
3. Trace the current end-to-end data flow.
4. Record confirmed gaps.
5. Add the corporate-status provider boundary.
6. Add deterministic fixture corporate-status support.
7. Compose metadata and disclosure extraction.
8. Normalize corporate status into EvidenceSnapshot.
9. Persist cycle/symbol completeness association.
10. Activate completeness evaluation and gating.
11. Correct `FixtureSecClient.list_filings`.
12. Thread provider and model identity.
13. Add framework-neutral attempt-control hooks.
14. Implement shadow role-budget controller.
15. Persist budget checks.
16. Record actual usage after every attempt.
17. Add cycle telemetry aggregation.
18. Reconcile budget settlement.
19. Populate health and run summaries.
20. Populate readiness and existing promotion reporting where authoritative.
21. Add explicit fixture/real scheduler CLI modes.
22. Add targeted unit tests.
23. Add offline end-to-end integration tests.
24. Run the full main suite.
25. Run the paper-runtime suite.
26. Perform bounded real validation when enabled.
27. Update ADR, docs, and runbooks.
28. Complete security review.
29. Finalize scratchpad.
30. Do not commit or push unless explicitly asked.

---

# Acceptance criteria

Milestone 7.1 is complete only when:

1. All existing 1,174 default main tests continue to pass.
2. All existing 33 paper-runtime tests continue to pass.
3. Corporate status is represented in the primary evidence snapshot.
4. Corporate-status provenance and point-in-time metadata are preserved.
5. Corporate-status and completeness results are associated with cycle and symbol.
6. Completeness is evaluated automatically in the real scheduled-cycle path.
7. Blocking completeness prevents Claude calls.
8. Missing news or sentiment alone does not incorrectly block screening.
9. Public-reporting history is not mislabeled as operating history.
10. Fixture SEC filings exercise the complete offline path.
11. Actual provider and model identity reach `CycleIntent`.
12. Anthropic pricing is selected by actual model and date.
13. Unknown Anthropic pricing blocks calls.
14. Every role attempt is budget-checked before provider invocation.
15. Retries are budget-checked separately.
16. Manager calls are budget-checked separately.
17. Budget denial does not call the provider.
18. Budget denial is not counted as provider failure.
19. Every provider attempt records actual available usage.
20. Retry usage is charged.
21. Validation-rejected output is charged.
22. Usage persistence is idempotent.
23. Resumed cycles do not double-charge.
24. Cycle telemetry is derived from authoritative persisted records.
25. Unknown usage remains unknown.
26. Budget consumption reconciles with attempt-level usage.
27. Final consumed cost is not silently zero after a real Claude run.
28. Health receives actual research-quality and usage telemetry.
29. Run summaries contain the same authoritative values.
30. Readiness does not claim readiness from insufficient cycles.
31. Explicit fixture and real scheduler modes exist.
32. Real mode never activates from credential presence alone.
33. Enhanced execution remains impossible.
34. Paper submission remains disabled in validation.
35. No recurring deployment is activated.
36. ADR 0005 and Milestone 7 documentation match actual implementation.
37. No safety invariant from Milestones 1–7 is weakened.
38. Scratchpad contains actual results and unresolved limitations.
39. No commit or push occurs unless explicitly requested.

---

# Required final response

At completion, provide:

1. Git and baseline state.
2. Scratchpad path and status.
3. Confirmed original integration gaps.
4. Corporate-status provider design.
5. Corporate-status snapshot representation.
6. Disclosure-composition behavior.
7. Completeness persistence and gate behavior.
8. Fixture SEC correction.
9. Operating-history semantic decision.
10. Provider/model propagation.
11. Attempt-controller design.
12. Role-budget integration.
13. Budget-check persistence.
14. Actual usage accounting.
15. Retry accounting.
16. Cycle telemetry model.
17. Budget reservation and settlement result.
18. Health telemetry result.
19. Readiness integration.
20. CLI real-mode behavior.
21. Schema changes.
22. Files created.
23. Files modified.
24. Tests added.
25. Targeted-test results.
26. Full main-suite result.
27. Paper-runtime result.
28. Real SEC validation.
29. Real Claude validation.
30. Reserved versus consumed cost.
31. Proof of pre-attempt budget checks.
32. Proof of no paper/enhanced execution.
33. ADR consistency changes.
34. Security review.
35. Known limitations.
36. Recommended Milestone 8 scope.

Include a mapping:

```text
Requirement → implementation file → verifying test
```

Use these labels:

```text
OFFLINE-DETERMINISTIC
RUNTIME-INTEGRATED
REAL-SEC-VALIDATED
REAL-CLAUDE-VALIDATED
REAL-MARKET-DATA
FIXTURE-MARKET-DATA
MODEL-PRICING-RESOLVED
ROLE-BUDGET-ENFORCED
ACTUAL-USAGE-SETTLED
HEALTH-TELEMETRY-COMPLETE
ENVIRONMENTALLY-PENDING
ENHANCED-SHADOW-ONLY
ACTUAL-RECURRING-DEPLOYMENT-NOT-ACTIVATED
```

Do not claim Milestone 7.1 is complete when actual Claude usage still settles as zero, role-budget checks are not in the provider-call path, or corporate completeness is not an automatic cycle gate.

Do not commit or push unless explicitly asked.
