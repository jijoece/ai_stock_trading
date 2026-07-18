You are continuing work in my existing AI-driven trading-desk repository.

This is a new Claude Code session focused on:

# Milestone 6.1 — Research failure diagnostics and Milestone 6 hardening

This is a direct implementation task, not a research-only investigation.

Milestones 1 through 6 are already implemented. Do not restart Milestone 6, create a new trading-desk project, replace the existing research architecture, or begin broad Milestone 7 work.

Your primary objectives are to:

1. Determine the exact validation failures that caused the real Claude `bear` role to exhaust both attempts during the Milestone 6 scheduled-cycle validation.
2. Persist precise, structured failure reasons for every research attempt.
3. Make failures queryable through repositories, replay, CLI output, and metrics.
4. Improve retry diagnostics and model remediation feedback.
5. Reproduce the failure deterministically where possible.
6. Fix any genuine prompt, schema, provider, orchestration, persistence, or validator defects.
7. Review other issues reported in Milestone 6 and fix only narrowly scoped, safe hardening defects.
8. Preserve all existing fail-closed behavior and execution boundaries.

Do not stop after producing an investigation report. Inspect persisted data, implement the observability and hardening changes, add regression tests, run the complete test suites, perform a narrow real Claude validation when explicitly enabled, and document the actual result.

---

# Source of truth

Before doing anything else, read:

```text
.claude/scratchpads/milestone6-progress.md
docs/milestones/milestone6-real-evidence-continuous-evaluation.md
docs/adr/0004-real-evidence-provider-boundary.md
docs/milestones/milestone5-evidence-backed-claude-research.md
docs/adr/0003-claude-research-boundary.md
```

Also inspect the current repository and persisted research data.

Use the Milestone 6 scratchpad and current code as the source of truth. Do not rely solely on this prompt.

Do not repeat the entire Milestone 6 repository investigation. Focus on the research-failure and hardening paths relevant to this task.

---

# Mandatory scratchpad update

Continue using:

```text
.claude/scratchpads/milestone6-progress.md
```

Do not create a competing scratchpad.

Append a new section:

```markdown
## Milestone 6.1 — Research failure diagnostics and hardening

Started: <UTC timestamp>
Branch: <branch name>
Status: STARTING

### Baseline

### Bear-role incident investigation

### Historical persistence findings

### Failure taxonomy

### Reproduction cases

### Root-cause classification

### Fixes implemented

### Other Milestone 6 issues reviewed

### Issues fixed

### Issues deferred

### Test run log

### Real Claude validation

### Security review

### Known limitations

### Final status
```

Scratchpad requirements:

1. Update it after each major step.
2. Preserve the existing Milestone 6 history.
3. Record actual commands and test results.
4. Record the evidence supporting the bear-role root-cause conclusion.
5. Distinguish:

   * application bugs;
   * prompt defects;
   * provider failures;
   * expected validator rejections;
   * missing evidence;
   * observability gaps;
   * environmental limitations;
   * deferred Milestone 7 work.
6. Never store:

   * API keys;
   * API secrets;
   * authorization headers;
   * raw `.env` contents;
   * full raw Claude responses;
   * hidden chain-of-thought;
   * account identifiers.
7. Do not rewrite prior entries to make the history appear cleaner.
8. Before the final response, ensure the scratchpad accurately reflects completed work, unresolved items, tests, and environmental validation.
9. Do not commit the scratchpad unless repository conventions intentionally version `.claude/scratchpads/`.

---

# Confirmed starting state

Milestone 6 completed with:

```text
Main test suite:
654 passed, 6 skipped

Paper-runtime suite:
33 passed
```

Milestone 6 implemented and validated:

* real SEC EDGAR evidence;
* real Alpaca market data using the IEX feed;
* SEC-derived fundamentals;
* point-in-time evidence normalization;
* provider caching;
* bounded provider retries;
* provider request persistence;
* scheduled research cycles;
* deterministic baseline and Claude-enhanced experiment arms;
* `SHADOW_ENHANCED` execution policy;
* time-to-fill metrics;
* turnover metrics;
* confidence calibration;
* deterministic promotion gates;
* provider health metrics;
* real SEC API smoke test;
* real Alpaca market-data smoke test;
* real scheduled-cycle smoke test;
* a manual real-Claude scheduled cycle.

The real Claude scheduled cycle used:

```text
symbol: AAPL
model: claude-sonnet-5
evidence_outcome: COMPLETE
experiment mode: SHADOW_ENHANCED
paper submission: disabled
```

Reported research behavior:

* nine total research attempts;
* analyst roles included fundamental, technical, bull, and bear;
* `bear` exhausted its two allowed attempts;
* both bear attempts had `success=0`;
* the manager role was correctly not invoked after the required bear role failed;
* baseline side remained `screened_out`;
* enhanced side remained `screened_out`;
* the overlay correctly did not promote a screened-out baseline;
* cost remained `PRICING_NOT_CONFIGURED`;
* no paper or live order was created.

The historical run ID was reported as:

```text
run-e4544adb0ac3e1faf405846132bdcf3d
```

Do not assume the row still exists. Search persisted data safely.

The Milestone 6 report did not include the exact bear-role failure stages or codes. This is the primary observability gap.

---

# Existing safety invariants

Preserve all of these:

* Claude is a research provider only.
* Claude cannot submit, preview, modify, or cancel orders.
* Claude cannot calculate final quantity.
* Claude cannot increase position size.
* Claude cannot bypass screening.
* Claude cannot promote a screened-out candidate.
* Natural-language output cannot enter execution.
* Recommendations remain immutable after freezing.
* Unknown or missing financial state fails closed.
* Unsupported numeric claims remain rejected.
* Unknown evidence IDs remain rejected.
* Point-in-time-unsafe evidence remains rejected.
* Research retries are bounded.
* Non-retryable Anthropic client errors are not retried.
* The enhanced arm cannot execute in shadow mode.
* The paper ledger remains authoritative.
* `real_orders` remains write-blocked.
* Robinhood mutating tools remain unavailable.
* The main process remains isolated from LumiBot.
* No secret is stored in research persistence.

---

# Non-goals

Do not implement:

* real news-provider integration;
* real Reddit authentication;
* Robinhood execution;
* live-money trading;
* options;
* short selling;
* margin;
* separate paper portfolios;
* recurring cloud deployment;
* broad corporate-status NLP;
* new market-data providers;
* MFE or MAE estimation from daily data;
* automatic prompt or model promotion;
* automatic execution of the enhanced arm;
* broader Milestone 7 features.

A narrow deterministic SEC metadata improvement may be added only when it directly fixes a proven Milestone 6 defect and is fully point-in-time safe.

---

# Step 1 — Establish the current baseline

Before editing:

1. Check:

   * current branch;
   * Git status;
   * uncommitted changes;
   * current database locations;
   * whether Milestone 6 changes are committed.
2. Do not discard or overwrite unrelated uncommitted work.
3. Read the scratchpad and relevant research files.
4. Run:

```bash
pytest tests/ -q
```

Expected:

```text
654 passed, 6 skipped
```

5. Run:

```bash
cd paper_runtime
pytest tests/ -q
```

Expected:

```text
33 passed
```

6. Record exact results in the scratchpad.

If the baseline differs, investigate before making unrelated changes.

---

# Step 2 — Inspect the research-failure pipeline

Read at minimum:

```text
src/trading_research/research/models.py
src/trading_research/research/anthropic_provider.py
src/trading_research/research/provider_protocol.py
src/trading_research/research/output_validation.py
src/trading_research/research/claim_validation.py
src/trading_research/research/evidence_validation.py
src/trading_research/research/orchestration.py
src/trading_research/research/replay.py
src/trading_research/research/prompts.py
src/trading_research/research/prompt_registry.py
src/trading_research/research/usage.py
src/trading_research/research/scheduled_cycle.py
src/trading_research/storage/research_schema.py
src/trading_research/storage/research_repositories.py
src/trading_research/cli.py
```

Also inspect:

* prompt files for the bear role;
* prompt files for the manager role;
* current structured output schemas;
* validator result models;
* current attempt-error persistence;
* research-run CLI and replay outputs;
* tests for retries, role failures, and real Claude smoke validation.

Determine:

* what failure details currently exist in memory;
* what is persisted;
* what is lost;
* whether individual claim rejections survive persistence;
* whether retry feedback is persisted;
* whether manager-skip reasons are persisted;
* whether stop reasons and token truncation survive persistence;
* whether failures can be reconstructed through replay.

Record findings before schema changes.

---

# Step 3 — Locate and inspect the real bear attempts

Search persisted research data safely.

Start with:

```text
run-e4544adb0ac3e1faf405846132bdcf3d
```

If unavailable, search by:

```text
symbol: AAPL
role: bear
model: claude-sonnet-5
attempt success: false
attempt count: 2
timestamp: Milestone 6 real-Claude validation window
```

Inspect:

* research run ID;
* snapshot ID;
* attempt ID;
* attempt number;
* role;
* model;
* prompt version and hash;
* schema version;
* attempt status;
* provider request status;
* provider request ID when safely available;
* stop reason;
* input tokens;
* output tokens;
* tool-use block presence;
* tool name;
* tool-input extraction result;
* JSON decoding result;
* schema-validation result;
* claim-validation result;
* evidence-reference result;
* numeric-claim result;
* retry classification;
* retry feedback;
* persisted error code;
* persisted error message;
* manager-skip record;
* retry-exhaustion record.

Do not print full model responses or prompts.

Use a redacted diagnostic script that emits only:

```text
research_run_id
attempt_id
role
attempt_number
failure_stage
failure_code
field_path
claim_id
evidence_ids
sanitized_message
retryable
stop_reason
input_tokens
output_tokens
prompt_version
schema_version
```

If historical persistence is insufficient to determine the exact cause, explicitly record:

```text
OBSERVABILITY GAP — historical attempt data insufficient
```

Do not fabricate a root cause.

---

# Step 4 — Define or extend a structured failure taxonomy

Inspect existing exception types and validation-result models first.

Reuse existing concepts instead of creating duplicate taxonomies.

Support validated failure stages similar to:

```text
PROVIDER_REQUEST
PROVIDER_RESPONSE
TOOL_USE_EXTRACTION
JSON_DECODING
STRUCTURED_SCHEMA
ROLE_REPORT_VALIDATION
CLAIM_EVIDENCE_VALIDATION
NUMERIC_CLAIM_VALIDATION
PROMPT_INJECTION_VALIDATION
RETRY_EXHAUSTED
REQUIRED_ROLE_FAILED
MANAGER_SKIPPED
PERSISTENCE
UNKNOWN
```

Support failure codes similar to:

```text
PROVIDER_TIMEOUT
PROVIDER_RATE_LIMITED
PROVIDER_UNAVAILABLE
PROVIDER_CLIENT_ERROR
PROVIDER_SERVER_ERROR
OUTPUT_TRUNCATED
EXPECTED_TOOL_USE_MISSING
UNEXPECTED_TOOL_NAME
MULTIPLE_TOOL_BLOCKS
MALFORMED_TOOL_INPUT
INVALID_JSON
SCHEMA_REQUIRED_FIELD_MISSING
SCHEMA_TYPE_MISMATCH
SCHEMA_ENUM_INVALID
SCHEMA_EXTRA_FIELD
SCHEMA_LIST_LIMIT_EXCEEDED
UNKNOWN_EVIDENCE_ID
CROSS_SNAPSHOT_EVIDENCE
CROSS_SYMBOL_EVIDENCE
STALE_EVIDENCE_REFERENCE
POINT_IN_TIME_UNSAFE_EVIDENCE
UNSUPPORTED_NUMERIC_CLAIM
NUMERIC_VALUE_MISMATCH
UNIT_MISMATCH
UNSUPPORTED_MATERIAL_CLAIM
MISSING_BEAR_CASE
MISSING_RISK
MISSING_REQUIRED_ROLE
RETRY_EXHAUSTED
MANAGER_NOT_INVOKED
PERSISTENCE_FAILURE
UNCLASSIFIED_VALIDATION_FAILURE
```

Requirements:

* validated constants or enums;
* unknown errors map explicitly to `UNCLASSIFIED_VALIDATION_FAILURE`;
* no generic success status for a failed validation;
* retryability stored independently from failure code;
* claim-level and attempt-level failures distinguishable.

---

# Step 5 — Add an immutable structured failure model

Add or extend a framework-neutral immutable model similar to:

```python
@dataclass(frozen=True)
class ResearchValidationFailure:
    failure_id: str
    research_run_id: str
    attempt_id: str
    role: str
    attempt_number: int
    stage: str
    code: str
    message: str
    field_path: str | None
    claim_id: str | None
    evidence_ids: tuple[str, ...]
    retryable: bool
    model_name: str
    prompt_version: str
    schema_version: str
    occurred_at: datetime
    metadata: Mapping[str, Any]
```

Validation requirements:

* UTC-aware timestamp;
* stable or deterministic failure ID;
* validated role;
* validated stage;
* validated code;
* positive attempt number;
* bounded sanitized message;
* bounded field path;
* valid evidence IDs;
* allowlisted metadata keys;
* no secret-like metadata keys;
* no raw authorization data;
* no hidden chain-of-thought;
* no unrestricted raw provider response.

Potential safe metadata:

```text
stop_reason
input_tokens
output_tokens
provider_status_code
expected_type
actual_type
allowed_evidence_count
numeric_tolerance
```

Do not store arbitrary metadata mappings without validation.

---

# Step 6 — Add structured failure persistence

Inspect the existing schema before deciding the migration.

Prefer additive schema changes.

A separate child table is likely appropriate because one failed attempt can produce multiple claim-level failures:

```text
research_attempt_failures
```

Suggested fields:

```text
failure_id
attempt_id
research_run_id
role
attempt_number
stage
code
message
field_path
claim_id
evidence_ids_json
retryable
model_name
prompt_version
schema_version
metadata_json
occurred_at
```

Requirements:

* link failures to attempts and research runs;
* support multiple failures per attempt;
* append-only;
* stable unique failure ID;
* idempotent insertion;
* earlier failed attempts remain after a later retry succeeds;
* completed runs remain immutable;
* no deletion of historical failures;
* no credentials or raw prompts;
* queryable by:

  * research run;
  * attempt;
  * role;
  * stage;
  * code;
  * retryability.

Add repository methods consistent with existing conventions, such as:

```python
save_attempt_failure(...)
save_attempt_failures(...)
list_attempt_failures(...)
list_role_failures(...)
list_run_failures(...)
summarize_run_failures(...)
```

Do not overload an unrelated repository abstraction.

---

# Step 7 — Capture provider and extraction failures

At the Anthropic provider boundary, classify and persist:

* timeout;
* rate limit;
* authentication or billing failure;
* non-retryable 4xx;
* retryable explicit 5xx or 529;
* missing tool-use block;
* unexpected tool name;
* multiple tool-use blocks when only one is allowed;
* malformed tool input;
* output truncation;
* provider response missing required metadata.

Preserve the Milestone 5 behavior:

```text
Only explicit transient server or overload failures are retryable.
Unclassified 4xx failures are non-retryable.
```

For output truncation, record:

* stop reason;
* output-token count;
* configured maximum output tokens;
* whether retrying with the same limit would be pointless.

Do not persist raw provider payloads unless existing policy already safely permits it.

---

# Step 8 — Capture structured-output failures

Update local validation so each failure can produce:

* failure stage;
* failure code;
* field path;
* bounded message;
* retryability;
* expected type or enum;
* actual safe type description.

Map common schema errors deterministically:

```text
required property missing
→ SCHEMA_REQUIRED_FIELD_MISSING

wrong type
→ SCHEMA_TYPE_MISMATCH

enum mismatch
→ SCHEMA_ENUM_INVALID

unexpected property
→ SCHEMA_EXTRA_FIELD

local list bound exceeded
→ SCHEMA_LIST_LIMIT_EXCEEDED
```

Remember that Anthropic’s strict tool schema uses a reduced wire schema while the complete local schema remains authoritative.

Do not weaken local list limits or required fields.

---

# Step 9 — Capture claim-validation failures

Persist each rejected material claim separately.

For each failure, record:

* claim ID;
* claim type;
* field path;
* referenced evidence IDs;
* failure code;
* sanitized reason;
* submitted numeric value where safe;
* submitted unit where safe;
* source numeric value where safe;
* source unit;
* tolerance rule;
* whether the failure invalidates the role report.

Map failures such as:

```text
unknown evidence
→ UNKNOWN_EVIDENCE_ID

wrong snapshot
→ CROSS_SNAPSHOT_EVIDENCE

wrong symbol
→ CROSS_SYMBOL_EVIDENCE

stale evidence
→ STALE_EVIDENCE_REFERENCE

point-in-time unsafe
→ POINT_IN_TIME_UNSAFE_EVIDENCE

number absent from evidence
→ UNSUPPORTED_NUMERIC_CLAIM

number disagrees beyond tolerance
→ NUMERIC_VALUE_MISMATCH

unit incompatible
→ UNIT_MISMATCH

material qualitative claim unsupported
→ UNSUPPORTED_MATERIAL_CLAIM
```

Do not collapse multiple claim failures into one generic error.

Do not accept model-derived numeric claims merely because they appear financially plausible.

---

# Step 10 — Persist retry and orchestration failures

Persist structured records for:

* retry requested;
* failure codes supplied to retry feedback;
* retry exhaustion;
* required role failure;
* manager skipped;
* final `ANALYSIS_INCOMPLETE`.

The manager-skip record should identify:

* which required role failed;
* failed attempt IDs;
* blocking failure codes;
* whether retries were exhausted;
* why invoking the manager would have violated orchestration policy.

Do not invoke the manager using incomplete required-role input merely to complete the run.

---

# Step 11 — Improve retry feedback

Inspect the existing retry-feedback prompt.

Generate concise structured remediation feedback.

Example:

```text
Your previous response failed local validation.

Failure:
- code: UNKNOWN_EVIDENCE_ID
- field: claims[3].evidence_ids[0]
- explanation: The referenced evidence ID is not part of the supplied snapshot.

Return a complete replacement report.

Requirements:
- Use only evidence IDs from the supplied allowed-evidence index.
- Do not reuse the invalid evidence ID.
- Do not introduce unsupported numeric values.
- Do not provide order instructions, allocation, or position size.
```

For multiple failures:

* group by failure code;
* prioritize report-blocking failures;
* bound the maximum number included;
* provide an allowed evidence-ID index where useful;
* do not paste the complete prior response;
* do not include secrets;
* require a full replacement report rather than a patch;
* preserve the same immutable evidence snapshot.

Persist the exact structured feedback codes supplied to the retry without storing sensitive prompt content.

Add tests proving attempt two receives correct remediation.

---

# Step 12 — Reproduce the bear-role failure deterministically

Create a fixture or scripted-provider scenario matching the actual or best-supported failure category.

The deterministic flow should reproduce:

```text
bear attempt 1
→ one or more structured failures persisted
→ bounded retry feedback generated
→ bear attempt 2
→ one or more structured failures persisted
→ retry exhausted
→ required role marked failed
→ manager skip persisted
→ final result ANALYSIS_INCOMPLETE
→ baseline remains intact
→ no executable enhanced recommendation
→ no paper intent
```

Potential fixture categories:

* unknown evidence ID;
* unsupported numeric downside estimate;
* missing required bear case;
* missing required risks;
* schema mismatch;
* malformed tool input;
* cross-snapshot citation;
* output truncation;
* multiple rejected material claims.

Use the real root cause when historical data supports it.

If historical persistence is insufficient, implement the observability fix first, then reproduce a representative failure and perform a new narrow real validation.

---

# Step 13 — Classify the incident root cause

Use one or more of these labels:

```text
APPLICATION BUG
PROMPT DEFECT
WIRE-SCHEMA DEFECT
LOCAL-SCHEMA DEFECT
CLAIM-VALIDATOR DEFECT
VALID EXPECTED REJECTION
MISSING EVIDENCE
OUTPUT-TOKEN LIMIT
PROVIDER FAILURE
OBSERVABILITY GAP
ENVIRONMENTAL LIMITATION
```

For each classification, record:

* supporting evidence;
* affected attempt IDs;
* affected failure codes;
* whether a code fix is required;
* whether validation should remain unchanged;
* whether prompt version must change;
* whether revalidation is needed.

Do not label an expected unsupported-claim rejection as a validator defect.

---

# Step 14 — Apply only evidence-backed fixes

## Prompt defect

Improve the bear-role prompt to explicitly require:

* only supplied evidence IDs;
* no invented numeric downside percentages;
* no unsupported price targets;
* no implied probability estimates unless present in evidence;
* clear separation of fact, inference, and uncertainty;
* required risks;
* required uncertainties;
* complete replacement output after retry feedback;
* no order, size, stop, or allocation instructions.

Increment the prompt version or ensure prompt hash changes create a new run identity.

## Evidence-presentation defect

Improve the supplied evidence index:

* stable IDs;
* category;
* source;
* units;
* normalized values;
* freshness state;
* conflict state;
* point-in-time state;
* concise allowed-ID list.

Do not alter source facts merely to make the role pass.

## Wire-schema defect

Modify only the Anthropic-compatible wire transformation.

Continue enforcing the complete local schema.

## Local-schema defect

Fix only a demonstrated incorrect schema rule.

Add regression tests.

## Claim-validator defect

Fix only demonstrated false rejection or false acceptance.

Do not broaden tolerances without evidence.

## Output-token limit

Confirm:

* provider stop reason;
* configured limit;
* actual output-token count;
* whether evidence or prompt verbosity can be reduced.

Increase limits only when bounded and configuration-driven.

Document cost and latency implications.

## Valid expected rejection

Do not weaken validation.

Improve:

* persistence;
* diagnostics;
* retry guidance;
* prompt clarity;
* failure metrics.

---

# Step 15 — Add CLI diagnostics

Add an argparse command consistent with repository conventions:

```bash
python -m trading_research.cli research-failures \
  --research-run-id <id>
```

Optional filters:

```text
--role bear
--attempt 1
--stage CLAIM_EVIDENCE_VALIDATION
--code UNKNOWN_EVIDENCE_ID
```

Return structured JSON containing:

* research run ID;
* attempt ID;
* role;
* attempt number;
* failure stage;
* failure code;
* field path;
* claim ID;
* evidence IDs;
* sanitized message;
* retryable;
* stop reason;
* input tokens;
* output tokens;
* prompt version;
* schema version;
* counts by stage;
* counts by code.

Do not output:

* raw prompts;
* complete raw responses;
* chain-of-thought;
* secrets;
* authorization headers;
* `.env` values.

Unknown run IDs must fail safely with a non-zero exit code.

Enhance existing replay or research-run output with a concise failure summary where appropriate.

---

# Step 16 — Add replay support

Update replay so it can:

1. Load persisted attempts.
2. Load persisted structured failures.
3. Rerun local schema and claim validators.
4. Reconstruct expected failures.
5. Compare persisted and reconstructed failure IDs or normalized signatures.
6. Report:

   * match;
   * missing persisted failure;
   * unexpected persisted failure;
   * changed validator result;
   * validator-version difference.
7. Reconstruct retry exhaustion.
8. Reconstruct manager skipping.
9. Avoid provider calls.
10. Avoid recommendation execution.

Add tests for:

* exact failure reconstruction;
* missing failure;
* extra failure;
* changed validator version;
* prompt-version mismatch;
* retry-exhaustion replay;
* manager-skip replay;
* no Anthropic call;
* no execution call.

---

# Step 17 — Add failure metrics

Implement deterministic research-failure metrics:

* failures by role;
* failures by stage;
* failures by code;
* attempts per completed role;
* retry-success rate;
* retry-exhaustion rate;
* required-role failure rate;
* manager-skip rate;
* unknown-evidence-ID rate;
* unsupported-numeric-claim rate;
* schema-failure rate;
* output-truncation rate;
* provider-error rate;
* average failed-attempt tokens;
* average failed-attempt latency;
* tokens spent on exhausted retries.

Return explicit:

```text
OK
INSUFFICIENT_DATA
UNDEFINED
```

Do not report misleading zeroes when there are no attempts.

Integrate these into:

```text
research-usage
research-performance
```

or add:

```text
research-failure-metrics
```

Follow existing CLI and evaluation conventions.

---

# Step 18 — Review remaining Milestone 6 issues

Review each item and classify it as:

```text
FIXED
EXPECTED
ENVIRONMENTALLY_PENDING
DEFERRED_TO_MILESTONE_7
NOT_A_BUG
```

## News provider

No API key or implementation target currently exists.

Expected result:

```text
ENVIRONMENTALLY_PENDING
```

Confirm explicit fail-closed behavior and tests.

Do not add an arbitrary provider.

## Reddit sentiment

Credentials are absent.

Expected result:

```text
ENVIRONMENTALLY_PENDING
```

Confirm:

* explicit missing-data reason;
* read-only MCP policy;
* no direct Claude MCP access.

## Corporate-status and going-concern evidence

Inspect whether official SEC metadata can safely provide deterministic signals such as:

* recent 10-K or 10-Q presence;
* recent NT 10-K or NT 10-Q;
* bankruptcy-related filing forms;
* registration termination;
* delisting-related forms;
* inactive filing history.

Only implement narrowly scoped metadata if:

* sourced directly from SEC data;
* point-in-time safe;
* deterministic;
* fully tested;
* clearly labeled as partial coverage.

Do not implement broad filing-text NLP in this task.

Do not claim complete going-concern coverage.

## Portfolio context uses cost basis

Determine whether this creates an actual current-state defect.

If paper market value is already available with a valid timestamp, add an explicit market-value path and fallback status.

Do not insert current values into historical snapshots.

Otherwise defer.

## Provider cache hits not persisted

This is an in-scope hardening candidate.

Where safe, persist or count:

```text
NETWORK_REQUEST
CACHE_HIT
CACHE_MISS
STALE_CACHE_REJECTED
CACHE_CORRUPT
```

Requirements:

* no raw payload duplication;
* no credentials;
* cache-hit latency tracked;
* provider usage metrics include cache activity;
* existing network-request persistence remains correct.

## Single-provider concentration

Do not add providers.

Expose concentration explicitly through health output:

```text
market_data_provider_count
filing_provider_count
fundamentals_provider_count
news_provider_count
redundancy_status
```

## Recurring deployment

Defer.

Confirm only:

```text
IDEMPOTENT SCHEDULED-CYCLE IMPLEMENTATION
```

not:

```text
ACTUAL RECURRING DEPLOYMENT
```

## MFE and MAE

Defer until a valid intraday provider exists.

Do not derive from daily closes.

## Real-Claude cost and latency

Ensure per-role and failed-attempt summaries include:

* attempts;
* input tokens;
* output tokens;
* latency;
* retry outcome;
* failure codes;
* cost only when pricing is configured.

---

# Step 19 — Tests

Preserve all existing tests.

Add focused tests.

## Failure model tests

* valid failure;
* invalid stage;
* invalid code;
* naive timestamp rejected;
* bounded message;
* bounded field path;
* secret-like metadata rejected;
* unknown metadata key rejected;
* deterministic failure ID.

## Persistence tests

* multiple failures per attempt;
* duplicate insertion idempotent;
* failed attempt retained after later success;
* query by run;
* query by role;
* query by stage;
* query by code;
* retry-exhaustion record;
* manager-skip record.

## Provider-classification tests

* non-retryable 400;
* billing/auth client error;
* rate limit;
* retryable explicit server error;
* retryable 529;
* timeout;
* missing tool use;
* unexpected tool name;
* malformed input;
* output truncation.

## Schema-validation tests

* required field missing;
* wrong type;
* invalid enum;
* extra property;
* local list bound exceeded;
* exact field path;
* multiple failures retained.

## Claim-validation tests

* unknown evidence ID;
* cross-snapshot evidence;
* cross-symbol evidence;
* stale evidence;
* unsafe evidence;
* unsupported numeric claim;
* numeric mismatch;
* unit mismatch;
* unsupported material claim;
* multiple claim failures persisted.

## Retry-feedback tests

* failures included in retry;
* correct failure codes;
* bounded count;
* allowed evidence IDs supplied;
* full replacement requested;
* raw previous response excluded;
* secrets excluded.

## Orchestration tests

* bear attempt one fails;
* failure persisted;
* bear attempt two fails;
* second failure persisted;
* retry exhaustion persisted;
* required-role failure persisted;
* manager skip persisted;
* final decision incomplete;
* deterministic baseline retained;
* no enhanced execution;
* no paper intent.

## Replay tests

* reconstructed failures match;
* mismatch detected;
* validator-version difference;
* no provider call;
* no execution.

## CLI tests

* full run query;
* role filter;
* stage filter;
* code filter;
* sanitized output;
* missing run;
* no raw prompt;
* no secret output.

## Metrics tests

* failures by role;
* failures by code;
* retry success;
* retry exhaustion;
* manager skip;
* unsupported numeric rate;
* empty dataset returns insufficient data.

## Other hardening tests

* cache-hit persistence if implemented;
* provider concentration output;
* explicit portfolio-context fallback if modified;
* partial SEC status evidence if implemented.

---

# Step 20 — Real Claude revalidation

After all offline tests pass, run a narrow real Claude validation only when explicitly enabled.

Use:

* an immutable persisted AAPL evidence snapshot;
* bear role only where possible;
* the intended model;
* no broker tools;
* no paper submission;
* no execution path;
* bounded token and cost configuration.

The validation should demonstrate:

1. Correct prompt version.
2. Correct evidence index.
3. Structured tool-use output.
4. Structured failures persisted when validation fails.
5. Retry feedback persisted when a retry occurs.
6. Retry exhaustion persisted if both attempts fail.
7. Manager behavior remains correct.
8. No recommendation is promoted.
9. No paper order is created.

Record only:

```text
research_run_id
attempt_ids
role
attempt_count
success/failure
failure stages
failure codes
stop reason
input tokens
output tokens
latency
retry used
```

Do not print raw Claude output or credentials.

If the role passes after a prompt or evidence fix, report the new result.

If it still fails for a correct validator reason, preserve the rejection and report it honestly.

---

# Step 21 — Documentation

Update:

```text
docs/milestones/milestone6-real-evidence-continuous-evaluation.md
```

Add a section:

```text
Milestone 6.1 — Bear-role failure diagnostics
```

Document:

* the original incident;
* historical persistence sufficiency;
* exact root-cause classification;
* failure taxonomy;
* schema changes;
* repository APIs;
* retry feedback;
* CLI diagnostics;
* replay behavior;
* failure metrics;
* fixes made;
* correct rejections retained;
* real Claude revalidation outcome;
* remaining limitations.

Create a new ADR only if structured failure persistence introduces a significant architectural decision not already covered by ADR 0003.

Update the scratchpad final status.

---

# Safety review

Before completion, verify:

* no validator was weakened merely to improve model success;
* no unsupported numeric claim is accepted;
* no unknown evidence ID is accepted;
* no stale evidence is accepted;
* no cross-snapshot citation is accepted;
* no raw chain-of-thought is persisted;
* no raw prompt or response is exposed through CLI;
* no secret is stored;
* no `.env` output was printed;
* no broker or execution tool is available to Claude;
* no paper order was created;
* no enhanced shadow recommendation executed;
* no screened-out candidate was promoted;
* no `real_orders` write path was added;
* no recommendation immutability trigger was weakened;
* no infinite retry was introduced;
* no failed attempt was erased after later success;
* no Milestone 1–6 test was weakened or deleted.

---

# Suggested implementation order

Proceed in this order:

1. Read the scratchpad and key documentation.
2. Append the Milestone 6.1 scratchpad section.
3. Confirm Git state and test baselines.
4. Inspect existing failure models and persistence.
5. Locate historical bear attempts.
6. Record whether historical data is sufficient.
7. Define or extend the failure taxonomy.
8. Add the immutable failure model.
9. Add additive schema and repository methods.
10. Capture provider failures.
11. Capture tool-use and structured-output failures.
12. Capture claim-level failures.
13. Persist retry exhaustion and manager skipping.
14. Improve retry feedback.
15. Add deterministic bear-failure reproduction.
16. Classify the real incident root cause.
17. Apply the narrow root-cause fix.
18. Add CLI diagnostics.
19. Add replay failure comparison.
20. Add failure metrics.
21. Review each remaining Milestone 6 issue.
22. Implement only safe in-scope hardening.
23. Run targeted tests.
24. Run the full main suite.
25. Run the paper-runtime suite.
26. Run narrow real Claude validation when explicitly enabled.
27. Update documentation.
28. Finalize the scratchpad.
29. Perform the safety review.

Avoid broad unrelated refactoring.

---

# Acceptance criteria

Milestone 6.1 is complete only when:

1. All existing Milestone 1–6 tests continue to pass.
2. New diagnostics tests pass.
3. Every failed attempt can persist multiple structured failures.
4. Failure stage is queryable.
5. Failure code is queryable.
6. Claim-level failures retain claim and evidence references.
7. Earlier failed retries remain visible after later success.
8. Retry exhaustion is persisted.
9. Required-role failure is persisted.
10. Manager skipping is persisted.
11. Retry feedback is actionable and bounded.
12. The bear-role incident has an evidence-backed root-cause classification.
13. Correct validator rejections remain rejected.
14. Genuine bugs identified by the incident are fixed.
15. CLI diagnostics produce sanitized structured output.
16. Replay reconstructs and compares failures.
17. Failure metrics handle insufficient data safely.
18. No raw chain-of-thought is persisted.
19. No secrets are stored or printed.
20. No execution path is invoked.
21. No enhanced-arm paper intent is created.
22. No screened-out candidate is promoted.
23. `real_orders` remains write-blocked.
24. Every remaining Milestone 6 issue is classified as:

    * fixed;
    * expected;
    * environmentally pending;
    * deferred to Milestone 7;
    * not a bug.
25. Cache-hit observability is fixed when safely in scope or explicitly deferred with justification.
26. Real Claude validation is reported honestly.
27. The scratchpad accurately reflects all work and results.
28. Documentation is updated.
29. No commit or push occurs unless explicitly requested.

---

# Required final response

At completion, provide:

1. Baseline verification.
2. Git-state findings.
3. Scratchpad section updated.
4. Historical bear-attempt findings.
5. Whether historical persistence was sufficient.
6. Exact failure stages.
7. Exact failure codes.
8. Affected attempt IDs.
9. Root-cause classification.
10. Failure taxonomy implemented.
11. Failure model implemented.
12. Schema and migration changes.
13. Repository methods added.
14. Provider and output-classification changes.
15. Claim-validation persistence changes.
16. Retry-feedback changes.
17. Prompt changes.
18. Evidence-presentation changes.
19. Validator changes, if any, with justification.
20. CLI diagnostics.
21. Replay changes.
22. Failure metrics.
23. Other Milestone 6 issues reviewed.
24. Issues fixed.
25. Issues deferred.
26. Tests added.
27. Main-suite result.
28. Paper-runtime result.
29. Real Claude validation result.
30. Commands run.
31. Safety review.
32. Known limitations.
33. Recommended next task.

Include a concise table:

```text
Requirement → implementation file → verifying test
```

Use these labels in the report:

```text
APPLICATION BUG
PROMPT DEFECT
WIRE-SCHEMA DEFECT
LOCAL-SCHEMA DEFECT
CLAIM-VALIDATOR DEFECT
VALID EXPECTED REJECTION
MISSING EVIDENCE
OUTPUT-TOKEN LIMIT
PROVIDER FAILURE
OBSERVABILITY GAP
ENVIRONMENTAL LIMITATION
FIXED
DEFERRED TO MILESTONE 7
```

Do not claim the bear role is fixed unless:

* the deterministic reproduction passes; and
* any real Claude revalidation result is reported accurately.

Do not claim validator improvement when the only change was weakening a safety check.

Do not commit or push unless explicitly asked.
