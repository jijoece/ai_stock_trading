You are continuing implementation of my existing AI-driven trading-desk repository.

Milestones 1 through 6.1 are complete. Do not create a new repository, replace the existing architecture, or reimplement completed functionality.

Your task is to implement:

# Milestone 7 — Production shadow operations and evidence-completeness expansion

This is a direct implementation task, not a research-only investigation.

The goal is to turn the existing manually invoked research-cycle implementation into a safe, observable, budget-controlled shadow-operations system while improving the real evidence needed for candidates to pass deterministic screening.

The system must remain:

* research-first;
* paper-only where execution already exists;
* shadow-enhanced by default;
* deterministic at every money-affecting boundary;
* fail-closed;
* point-in-time safe;
* auditable;
* idempotent;
* resumable;
* disabled by default.

Claude remains a research provider only.

Do not stop after producing an architecture plan. Inspect the repository, implement the vertical slices, add tests, run all test suites, perform explicitly gated real validations when credentials exist, and document the actual outcome.

---

# Mandatory Milestone 7 scratchpad

Before changing implementation files, create:

```text
.claude/scratchpads/milestone7-progress.md
```

Use it as the persistent source of truth for this milestone.

Required structure:

```markdown
# Milestone 7 Progress

Started: <UTC timestamp>
Branch: <branch name>
Status: STARTING

## Baseline
- Main suite:
- Paper runtime:
- Git status:
- Existing credentials, boolean presence only:
- Environmentally unavailable providers:
- Existing scheduler/deployment environment:

## Repository findings

## Gap analysis

## Architecture decisions

## Evidence-completeness decisions

## News-provider decision

## Reddit-sentiment decision

## Shadow-operations design

## Budget and cost-control design

## Alerting design

## Scheduler/deployment decision

## Implementation checklist

## Files created

## Files modified

## Schema and migration changes

## Tests added

## Test run log

## Real validation

## Bugs discovered and fixed

## Security and secret-handling review

## Operational runbook findings

## Known limitations

## Deferred work

## Final status
```

Scratchpad rules:

1. Create it before implementation edits.
2. Update it after every major step.
3. Record actual commands and results.
4. Preserve historical entries; do not rewrite failures out of the record.
5. Never store:

   * API keys;
   * API secrets;
   * authorization headers;
   * full `.env` contents;
   * account identifiers;
   * raw Claude responses;
   * hidden chain-of-thought.
6. Credential checks must report only Boolean presence.
7. Record real-validation failures before fixing them.
8. Distinguish:

   * implementation complete;
   * real connectivity confirmed;
   * real data retrieved;
   * real Claude output confirmed;
   * actual recurring deployment activated;
   * environmentally pending.
9. Do not commit the scratchpad unless repository conventions intentionally version it.
10. Do not commit or push any work unless explicitly asked.

---

# Confirmed starting state

Verify these against the repository before modifying anything.

Current reported baseline:

```text
Main suite:
760 passed, 7 skipped

Paper-runtime suite:
33 passed
```

Milestone 6 and 6.1 currently provide:

* real SEC EDGAR filings and company facts;
* real Alpaca historical market data using an explicit IEX feed;
* real SEC-derived fundamentals;
* point-in-time evidence snapshots;
* provider caching and rate limiting;
* provider request and cache-event persistence;
* provider-health and concentration reporting;
* scheduled-cycle implementation;
* idempotent and resumable cycle IDs;
* baseline and Claude-enhanced experiment arms;
* `SHADOW_ENHANCED` policy;
* no enhanced-arm execution;
* continuous evaluation;
* time-to-fill;
* turnover;
* confidence calibration;
* deterministic promotion gates;
* structured research-failure persistence;
* research-failure CLI and metrics;
* replay-time validator comparison;
* bounded retry feedback;
* versioned `bear/v2` prompt;
* successful real-Claude bear-role validation;
* safe analyst-only orchestration;
* fail-closed manager requirements.

The real bear-role smoke test reported:

```text
prompt: bear/v2
attempt_count: 1
validation_result: VALID_REPORT
failure_codes: []
input_tokens: 3588
output_tokens: 1878
latency_ms: 22113
```

The manager-invocation defect is fixed:

* manager configured: normal behavior;
* manager omitted with `require_decision=True`: fail immediately;
* manager omitted with `require_decision=False`: analyst-only result, no decision fabricated;
* no false `MANAGER_SKIPPED` record when manager was never configured.

Known remaining gaps include:

* no real news provider;
* no real Reddit sentiment because credentials are unavailable;
* incomplete corporate-status, operating-history, bankruptcy/distress, shell-company, and going-concern evidence;
* no actual recurring scheduler deployment;
* no controlled daily/monthly Claude budget enforcement;
* no operational alert sink;
* no separate paper-book namespaces;
* no intraday MFE/MAE;
* only one primary market-data source and one filings/fundamentals source;
* enhanced research remains shadow-only.

---

# Core Milestone 7 objectives

Implement a production-quality shadow-operations vertical slice that can:

1. Improve corporate-status and operating-history evidence using official, point-in-time-safe sources.
2. Add one real news-provider path when a documented provider and credentials are available.
3. Complete the real Reddit sentiment path when credentials are available, while preserving fail-closed behavior otherwise.
4. Run bounded scheduled shadow cycles through an external scheduler-compatible entry point.
5. Prevent overlapping or duplicate scheduled cycles.
6. Enforce per-cycle, daily, and monthly research budgets.
7. Enforce role, attempt, token, latency, and cost limits.
8. Pause automatically when safety or operational limits are exceeded.
9. Produce structured operational alerts.
10. Persist scheduler runs, leases, budgets, alerts, and operator actions.
11. Support an explicit pause and kill switch.
12. Support safe catch-up behavior after missed cycles.
13. Continue evaluating baseline and enhanced arms without allowing enhanced execution.
14. Produce readiness reports for whether shadow operations are stable enough to continue.
15. Preserve every Milestone 1–6.1 safety invariant.

---

# Core architecture

Use this operating flow:

```text
External scheduler or explicit operator command
                ↓
Shadow-run preflight
                ↓
Global pause / kill-switch check
                ↓
Market-calendar and configured-window check
                ↓
Distributed or database-backed lease acquisition
                ↓
Budget reservation
                ↓
Provider-health and evidence-readiness preflight
                ↓
Idempotent scheduled research cycle
                ↓
Real point-in-time evidence
                ↓
Deterministic baseline
                ↓
Claude-enhanced shadow arm
                ↓
No enhanced execution
                ↓
Optional existing baseline paper path
                ↓
Evaluation and attribution
                ↓
Budget settlement
                ↓
Operational-health evaluation
                ↓
Alerts and run summary
                ↓
Lease release
```

Ownership must remain:

```text
Evidence providers:
    Retrieve and normalize facts.

Claude:
    Analyze the supplied evidence.

Deterministic application code:
    Screen, score, validate, size, freeze, execute, reconcile,
    evaluate, schedule, budget, pause, alert, and promote.
```

---

# Hard safety boundaries

Do not implement:

* live-money trading;
* Robinhood mutation;
* Robinhood order submission;
* options;
* margin;
* short selling;
* unrestricted Claude web access;
* direct provider credentials exposed to Claude;
* direct broker tools exposed to Claude;
* model-generated position quantity;
* model-generated final allocation;
* model override of screening;
* model override of risk policy;
* model promotion of screened-out candidates;
* enhanced-arm execution;
* automatic live promotion;
* silent scheduler activation;
* an always-running daemon without explicit configuration;
* automatic OS scheduler installation without explicit user authorization;
* arbitrary web scraping;
* undocumented financial endpoints;
* fabricated corporate-status defaults;
* fabricated going-concern conclusions;
* “not found” treated as “false”;
* current evidence inserted into historical snapshots;
* current prices substituted for unavailable historical prices;
* broad filing-text NLP without explicit provenance and deterministic validation.

---

# Step 1 — Inspect and establish the baseline

Before editing:

1. Check:

   * branch;
   * Git status;
   * uncommitted changes;
   * commits containing Milestone 6.1 and follow-ups;
   * configured database locations;
   * current `.gitignore`.
2. Create the Milestone 7 scratchpad.
3. Read:

   * all Milestone 1–6.1 documentation;
   * ADRs 0001–0004;
   * Milestone 6 and 6.1 scratchpads;
   * evidence-provider code;
   * SEC provider;
   * fundamentals normalizer;
   * news and sentiment stubs;
   * scheduled-cycle implementation;
   * experiment policies;
   * paper-execution gate;
   * evaluation and promotion modules;
   * research budgets or cost code, if any;
   * provider health;
   * failure metrics;
   * configuration and CLI;
   * database and repository conventions.
4. Run:

```bash
pytest tests/ -q
```

Expected:

```text
760 passed, 7 skipped
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

6. Record the exact results.

Do not discard unrelated uncommitted work.

---

# Step 2 — Gap analysis

Classify each item as:

```text
IMPLEMENTED
PARTIALLY_IMPLEMENTED
MISSING
CONFLICTING
ENVIRONMENTALLY_BLOCKED
DEFERRED
```

At minimum assess:

## Evidence completeness

* operating-history evidence;
* active reporting status;
* recent annual filing;
* recent quarterly filing;
* late-filing notices;
* bankruptcy/distress signals;
* delisting or registration-termination signals;
* shell-company evidence;
* going-concern disclosure;
* corporate actions;
* market-cap reconstruction;
* debt/cash-flow completeness;
* real news;
* real Reddit sentiment.

## Shadow operations

* external scheduler entry point;
* recurring schedule configuration;
* market-calendar gating;
* run-window enforcement;
* duplicate-run prevention;
* lease or lock;
* catch-up policy;
* pause switch;
* kill switch;
* operator override;
* budget reservation;
* daily budget;
* monthly budget;
* token limit;
* cost limit;
* latency limit;
* provider-call limit;
* alerting;
* alert deduplication;
* runbook;
* retention;
* backup;
* scheduler status CLI.

## Evaluation readiness

* cycle stability metrics;
* real-evidence completeness rate;
* Claude role-completion rate;
* retry-exhaustion rate;
* unsupported-claim rate;
* provider availability;
* cost per completed cycle;
* cost per valid recommendation;
* baseline versus enhanced performance;
* market-regime coverage;
* promotion readiness;
* rollback readiness.

Record the analysis before implementation.

---

# Step 3 — Draft the architecture ADR

Create:

```text
docs/adr/0005-production-shadow-operations-boundary.md
```

Document:

* external scheduler versus internal business logic;
* why the repository does not run an uncontrolled daemon;
* lease and idempotency design;
* pause and kill-switch behavior;
* cost and budget enforcement;
* evidence-completeness boundaries;
* news-provider selection;
* Reddit policy;
* alert-sink boundary;
* baseline-paper versus enhanced-shadow behavior;
* retention and audit requirements;
* what remains explicitly outside Milestone 7.

Draft the ADR before completing the implementation, then update it with actual decisions.

---

# Step 4 — Corporate-status evidence model

Add deterministic evidence contracts for corporate status.

Possible normalized model:

```python
@dataclass(frozen=True)
class CorporateStatusEvidence:
    symbol: str
    as_of: datetime
    reporting_status: str
    reporting_status_reason: str | None
    earliest_reliable_filing_date: date | None
    operating_history_years: Decimal | None
    latest_annual_filing: FilingReference | None
    latest_quarterly_filing: FilingReference | None
    late_filing_notices: tuple[FilingReference, ...]
    bankruptcy_signals: tuple[CorporateRiskSignal, ...]
    delisting_signals: tuple[CorporateRiskSignal, ...]
    registration_status_signals: tuple[CorporateRiskSignal, ...]
    shell_company_signals: tuple[CorporateRiskSignal, ...]
    going_concern_signals: tuple[CorporateRiskSignal, ...]
    completeness_status: str
    sources: tuple[SourceRecord, ...]
```

Do not force this exact class when existing models can be extended safely.

Use explicit statuses:

```text
CONFIRMED
NOT_FOUND_IN_SEARCHED_SOURCES
UNKNOWN
SOURCE_UNAVAILABLE
POINT_IN_TIME_UNSAFE
CONFLICTING
```

Do not convert:

```text
NOT_FOUND_IN_SEARCHED_SOURCES
```

into:

```text
FALSE
```

unless the official source makes that conclusion valid.

---

# Step 5 — Official SEC corporate-status provider

Extend the SEC-based evidence path using current official SEC documentation and endpoints.

Before implementation, verify from official sources:

* available filing metadata;
* form types and meanings;
* availability timestamps;
* filing-text retrieval rules;
* request identification requirements;
* rate limits;
* structured versus rendered filing access;
* whether the desired data is available point-in-time.

Potential evidence may include:

* earliest reliable filing date;
* recent annual and quarterly filing presence;
* late-filing notices;
* bankruptcy-related filing forms or explicit disclosed events;
* registration termination;
* delisting-related filings;
* reporting inactivity;
* explicit shell-company disclosure;
* explicit going-concern disclosure.

Requirements:

* use accepted or availability timestamps;
* preserve accession numbers;
* preserve form types;
* preserve source URLs or locators;
* preserve publication time;
* exclude filings unavailable at the requested `as_of`;
* distinguish current company status from historical status;
* no broad inference from an absent form;
* no unsupported “healthy company” default;
* no model-generated status flag;
* no current SEC metadata leaking into historical snapshots.

---

# Step 6 — Filing-text retrieval and deterministic disclosure extraction

Implement bounded filing-text retrieval only where necessary.

Possible package:

```text
src/trading_research/evidence_providers/
├── filing_documents.py
├── corporate_status.py
├── disclosure_extraction.py
└── corporate_status_adapters.py
```

Requirements:

* use official filing documents;
* cap document size;
* cache immutable filing content;
* preserve content hash;
* sanitize HTML deterministically;
* retain section provenance where possible;
* treat filing text as untrusted input;
* annotate prompt-injection risk;
* do not send unrestricted full filings to Claude;
* do not persist content where licensing or policy prohibits it;
* SEC public filings may be retained according to the documented retention policy.

Going-concern extraction must be deterministic and conservative.

Support outcomes such as:

```text
EXPLICIT_DISCLOSURE_FOUND
EXPLICIT_DISCLOSURE_NOT_FOUND_IN_SEARCHED_SECTIONS
SEARCH_INCOMPLETE
DOCUMENT_UNAVAILABLE
AMBIGUOUS_DISCLOSURE
```

Do not claim a company has no going-concern issue merely because a phrase was not found.

Add evidence references to:

* filing;
* section;
* excerpt hash;
* availability time;
* extraction-rule version.

---

# Step 7 — Operating-history derivation

Derive operating history only from reliable, point-in-time-safe evidence.

Potential approaches:

* earliest accepted annual filing available through the official source;
* earliest reliable SEC registration or filing evidence;
* official incorporation information when documented and available.

Requirements:

* document exactly what “operating history” means;
* distinguish:

  * company age;
  * public-reporting history;
  * exchange-listing history;
  * operating-history proxy.
* do not represent public-reporting history as company age;
* retain the derivation method;
* retain earliest-known source;
* return `UNKNOWN` when the source cannot establish sufficient history.

Integrate the value into deterministic screening only after tests demonstrate that its semantics match the screener’s requirement.

Do not weaken the screener to accept a weaker proxy silently.

---

# Step 8 — Corporate-action evidence

Inspect whether the existing primary market-data provider exposes documented corporate-action data.

Where officially supported, add:

* splits;
* dividends;
* symbol changes;
* mergers or reorganizations;
* trading halts where available.

Requirements:

* preserve provider timestamp;
* preserve effective date;
* preserve announcement date when available;
* prevent future actions from entering historical snapshots;
* distinguish adjusted price bars from separately modeled actions;
* do not infer missing actions from price jumps alone.

If the provider does not support this safely, document it as deferred rather than scraping.

---

# Step 9 — Real news-provider selection

Choose one real news provider only after inspecting:

* existing credentials;
* official documentation;
* historical query support;
* publication timestamps;
* provider-availability timestamps;
* symbol mapping;
* rate limits;
* licensing;
* persistence restrictions;
* pricing;
* dependency footprint.

Consider whether an already-authorized provider exposes a documented news API, but verify this from current official documentation before selecting it.

Do not assume an endpoint or entitlement exists.

Implement one provider-neutral adapter satisfying the existing news protocol.

Normalize:

* provider article ID;
* canonical headline;
* source;
* publication time;
* provider availability time;
* symbols;
* URL or locator;
* category;
* duplicate group;
* content hash;
* source trust classification;
* prompt-injection risk;
* retention classification.

Requirements:

* exclude future articles;
* deduplicate syndicated copies;
* cap article count and content size;
* do not treat duplicate syndication as independent confirmation;
* no arbitrary browsing by Claude;
* no provider-key leakage;
* no silent fallback to fixtures in a real run;
* explicit `ENVIRONMENTALLY_PENDING` when credentials or entitlement are unavailable.

---

# Step 10 — Real Reddit sentiment path

Complete the real Reddit path only when credentials are available.

Reuse:

* existing MCP adapter;
* existing tool policy;
* read-only allowlist;
* existing sentiment aggregation;
* existing prompt-injection annotation.

Requirements:

* application invokes MCP, not Claude;
* no MCP tool exposed directly to Claude;
* no posting;
* no voting;
* no messaging;
* no account mutation;
* no moderation actions;
* unknown tools fail closed;
* result size bounded;
* subreddits configurable;
* query terms deterministic;
* historical cutoff honored;
* ambiguous cashtags handled;
* duplicates and cross-posts normalized;
* missing credentials produce explicit missing-data evidence.

Add a real smoke test only when credentials exist.

---

# Step 11 — Evidence-completeness policy

Add a deterministic evidence-completeness evaluator.

Possible statuses:

```text
COMPLETE_FOR_SCREENING
COMPLETE_FOR_RESEARCH
PARTIAL_NONCRITICAL
MISSING_CRITICAL_CORPORATE_STATUS
MISSING_CRITICAL_MARKET_DATA
MISSING_CRITICAL_FUNDAMENTALS
MISSING_NEWS
MISSING_SENTIMENT
CONFLICTING_CRITICAL_DATA
POINT_IN_TIME_UNSAFE
PROVIDER_UNAVAILABLE
```

Requirements:

* screening completeness and research completeness must be distinct;
* news or sentiment absence must not automatically invalidate deterministic screening unless policy explicitly requires it;
* critical corporate-status uncertainty must remain fail-closed;
* completeness policy must be versioned;
* policy result must be persisted;
* result must explain which categories are blocking;
* no model influence over completeness status.

Integrate with existing snapshot classification without duplicating it unnecessarily.

---

# Step 12 — Shadow-operations configuration

Create a safe configuration file consistent with repository conventions, for example:

```text
config/shadow_operations.yaml
```

Suggested structure:

```yaml
shadow_operations:
  enabled: false
  mode: SHADOW_ENHANCED
  allow_baseline_paper_submission: false
  allow_enhanced_submission: false
  require_market_open_day: true
  run_window_timezone: America/Los_Angeles
  run_window_start: "06:30"
  run_window_end: "08:30"
  max_catch_up_cycles: 1
  lease_ttl_seconds: 3600
  stale_run_timeout_seconds: 7200
  continue_on_symbol_failure: true

schedule:
  enabled: false
  cadence: DAILY_MARKET_DAY
  intended_local_time: "06:45"

budgets:
  require_pricing_for_real_claude: true
  max_symbols_per_cycle: 10
  max_roles_per_symbol: 5
  max_attempts_per_role: 2
  max_input_tokens_per_cycle: 100000
  max_output_tokens_per_cycle: 50000
  max_latency_seconds_per_cycle: 900
  max_estimated_cost_per_cycle_usd: 5.00
  max_actual_cost_per_day_usd: 10.00
  max_actual_cost_per_month_usd: 100.00

safety:
  pause_on_provider_failure_rate: 0.50
  pause_on_retry_exhaustion_rate: 0.50
  pause_on_unsupported_claim_rate: 0.25
  pause_on_reconciliation_mismatch: true
  pause_on_budget_breach: true
```

Use values appropriate to repository conventions; do not blindly copy these numbers.

Requirements:

* disabled by default;
* enhanced submission always false;
* environment variables cannot enable capabilities;
* unknown mode fails closed;
* pricing required for recurring real-Claude operation;
* missing pricing blocks recurring Claude calls;
* manual smoke tests may remain separately gated;
* budget values validated;
* negative or zero invalid values rejected where inappropriate.

---

# Step 13 — Shadow-run identity and persistence

Add persistence for operational runs.

Potential tables:

```text
shadow_scheduler_runs
shadow_run_leases
shadow_budget_reservations
shadow_budget_usage
shadow_pause_state
shadow_operator_actions
shadow_alerts
shadow_alert_deliveries
shadow_run_summaries
```

Persist:

* scheduler run ID;
* scheduled time;
* actual start;
* actual finish;
* cycle ID;
* configuration hash;
* mode;
* lease owner;
* lease expiration;
* status;
* pause state;
* budget reserved;
* budget consumed;
* symbols attempted;
* symbols completed;
* symbols skipped;
* provider failures;
* research failures;
* paper submissions;
* alert count;
* failure reason;
* operator action;
* deployment source.

Requirements:

* additive schema;
* idempotent application;
* append-only audit history;
* no secrets;
* no account IDs;
* deterministic or stable run identity;
* queryable by status and date;
* retain partial runs;
* preserve stale-run recovery history.

---

# Step 14 — Lease and duplicate-run prevention

Implement a database-backed or otherwise repository-consistent lease.

The lease must prevent:

* two scheduler invocations running the same intended cycle concurrently;
* duplicated Claude calls;
* duplicated recommendations;
* duplicated paper intents.

Lease behavior:

```text
acquire
renew
release
expire
recover stale
```

Requirements:

* atomic acquisition;
* owner identity;
* expiration timestamp;
* bounded TTL;
* renewal;
* safe release;
* stale-owner recovery;
* audit record;
* clock injection for tests;
* no permanent lock after crash;
* no force unlock without a recorded operator action.

Test concurrent acquisition using separate database connections where practical.

---

# Step 15 — Pause and kill switch

Implement a deterministic global operational state.

Suggested states:

```text
ACTIVE
PAUSED_MANUAL
PAUSED_BUDGET
PAUSED_PROVIDER_HEALTH
PAUSED_RESEARCH_QUALITY
PAUSED_RECONCILIATION
KILLED
```

Requirements:

* persisted state;
* reason;
* source;
* operator;
* timestamp;
* optional expiration;
* previous state;
* audit history;
* CLI visibility;
* fail closed.

Behavior:

* `KILLED` blocks every scheduled cycle before provider or Claude calls;
* paused states block new scheduled work;
* manual diagnostic commands may require an explicit override;
* override must be recorded;
* no override can enable live trading;
* no model can change pause state;
* no provider can change pause state directly;
* automatic health rules may request a pause through deterministic code.

---

# Step 16 — Budget reservation and settlement

Implement budget enforcement before and after each run.

Budget categories:

* symbols;
* provider requests;
* Claude attempts;
* input tokens;
* output tokens;
* estimated cost;
* actual cost;
* elapsed time.

Flow:

```text
estimate maximum run cost
        ↓
reserve budget
        ↓
run bounded work
        ↓
record actual usage
        ↓
settle reservation
        ↓
release unused reservation
```

Requirements:

* recurring real-Claude operation requires configured pricing;
* no fabricated cost;
* unknown pricing blocks scheduled real-Claude operation;
* cost estimates use versioned pricing;
* daily and monthly usage queries;
* concurrent reservations counted;
* reservation is idempotent;
* abandoned reservation expires safely;
* actual usage can exceed estimate only within a configured emergency margin;
* breach triggers pause according to policy;
* remaining symbols are not started after budget exhaustion;
* already-completed results remain persisted.

Do not rely solely on post-run accounting.

---

# Step 17 — Role and token budgeting

Before each Claude call, enforce:

* allowed role;
* role count;
* attempt count;
* remaining input-token budget;
* remaining output-token budget;
* remaining latency budget;
* remaining cost budget.

Requirements:

* manager call included in the estimate;
* retries included;
* analyst-only diagnostic behavior remains supported;
* provider failures do not consume fabricated tokens;
* successful provider usage comes from actual response metadata;
* no role call starts when maximum possible output would breach the remaining budget;
* budget rejection produces structured operational status;
* budget rejection is not classified as provider failure.

Suggested status:

```text
SKIPPED_BUDGET_EXHAUSTED
```

---

# Step 18 — External scheduler-compatible runner

Implement a single-run entry point suitable for invocation by:

* cron;
* launchd;
* GitHub Actions;
* CI scheduler;
* cloud scheduler;
* manual operator command.

Do not embed an uncontrolled infinite loop.

Suggested CLI:

```bash
python -m trading_research.cli run-due-shadow-cycle
```

Expected behavior:

1. Load configuration.
2. Check enabled state.
3. Resolve intended schedule.
4. Check market calendar.
5. Check pause or kill state.
6. Acquire lease.
7. Reserve budget.
8. Perform preflight health checks.
9. Run exactly one due cycle.
10. Evaluate health rules.
11. Emit alerts.
12. Settle budget.
13. Release lease.
14. Return structured JSON and exit code.

Requirements:

* one process invocation performs at most one intended scheduled cycle;
* rerunning the command is idempotent;
* not-due is a successful no-op;
* market holiday is a successful no-op;
* disabled is a successful no-op;
* paused or killed returns explicit status;
* partial cycle is visible;
* crash recovery is supported.

---

# Step 19 — Schedule and catch-up semantics

Define exact schedule behavior.

Support:

* market-day schedule;
* timezone-aware intended time;
* allowed run window;
* missed-cycle detection;
* maximum catch-up count;
* skip old cycles beyond the catch-up window;
* daylight-saving transitions;
* market holidays;
* early-close days if relevant;
* no future cycle execution.

Suggested statuses:

```text
NOT_DUE
DUE
MISSED_WITHIN_CATCHUP
MISSED_TOO_OLD
MARKET_HOLIDAY
OUTSIDE_RUN_WINDOW
ALREADY_COMPLETED
LEASE_HELD
PAUSED
KILLED
```

Use the existing market-calendar abstraction where possible.

Do not create two cycles for the same intended schedule time.

---

# Step 20 — Deployable scheduler artifact

Create at least one documented scheduler artifact suitable for this repository’s environment.

Because this project is used on a Mac, a launchd example may be appropriate, but inspect repository and user conventions first.

Possible artifact:

```text
deploy/launchd/com.agentic-trading-desk.shadow.plist.example
```

or a GitHub Actions scheduled workflow when that is the better repository fit.

Requirements:

* artifact disabled or example-only by default;
* no credentials embedded;
* uses repository virtual environment safely;
* invokes only `run-due-shadow-cycle`;
* captures logs;
* documents working directory;
* documents environment loading;
* documents failure behavior;
* does not install or activate itself.

Do not install an OS-level schedule without explicit user approval.

Clearly distinguish:

```text
DEPLOYABLE SCHEDULER ARTIFACT
```

from:

```text
ACTUAL RECURRING DEPLOYMENT ACTIVATED
```

---

# Step 21 — Operational alerts

Create a provider-neutral alert model.

Suggested severities:

```text
INFO
WARNING
ERROR
CRITICAL
```

Suggested alert types:

```text
CYCLE_FAILED
CYCLE_PARTIALLY_COMPLETE
LEASE_CONFLICT
LEASE_STALE_RECOVERED
BUDGET_NEAR_LIMIT
BUDGET_EXCEEDED
PROVIDER_UNAVAILABLE
PROVIDER_FAILURE_RATE_HIGH
EVIDENCE_COMPLETENESS_LOW
RESEARCH_RETRY_EXHAUSTION_HIGH
UNSUPPORTED_CLAIM_RATE_HIGH
OUTPUT_TRUNCATION
CLAUDE_COST_SPIKE
RECONCILIATION_MISMATCH
SCHEDULER_MISSED_RUN
PAUSE_ACTIVATED
KILL_SWITCH_ACTIVATED
```

Implement:

```python
class AlertSink(Protocol):
    def send(self, alert: OperationalAlert) -> AlertDeliveryResult:
        ...
```

Provide:

* persistence-only sink;
* console or structured-log sink;
* optional webhook or email sink only when repository conventions support it.

Requirements:

* alerts persisted before delivery;
* delivery attempt persisted;
* retry bounded;
* duplicate alert suppression;
* deduplication window;
* secrets removed;
* provider response bounded;
* alert failure does not erase the underlying operational failure;
* critical alert failure is visible.

Do not add a third-party notification dependency without justification.

---

# Step 22 — Operational health rules

Create deterministic health evaluation after each cycle.

Inputs:

* provider success rate;
* evidence completeness;
* Claude role success;
* retries;
* retry exhaustion;
* unsupported claims;
* output truncation;
* latency;
* token usage;
* cost;
* paper reconciliation;
* duplicate prevention;
* cycle duration.

Outputs:

```text
HEALTHY
DEGRADED
PAUSE_RECOMMENDED
PAUSE_REQUIRED
```

Requirements:

* versioned health policy;
* persisted result;
* exact reasons;
* thresholds from configuration;
* no model-generated health status;
* automatic pause only when configured;
* no automatic unpause after a critical event;
* operator acknowledgement required where appropriate.

---

# Step 23 — Shadow readiness report

Implement a readiness report answering:

```text
Is the system safe and stable enough to continue recurring shadow operation?
```

Potential categories:

* evidence readiness;
* provider readiness;
* research readiness;
* budget readiness;
* scheduler readiness;
* paper readiness;
* evaluation readiness;
* operational readiness.

Suggested statuses:

```text
READY
READY_WITH_WARNINGS
NOT_READY
INSUFFICIENT_DATA
ENVIRONMENTALLY_BLOCKED
```

The report must include:

* minimum completed cycle count;
* minimum successful real-provider cycle count;
* evidence completeness rate;
* role completion rate;
* retry exhaustion;
* unsupported claim rate;
* provider failure rate;
* cost per completed cycle;
* cycle duration;
* scheduler misses;
* lease conflicts;
* reconciliation mismatches;
* alert-delivery health.

Do not equate one successful smoke test with production readiness.

---

# Step 24 — Evaluation and promotion integration

Extend evaluation without changing existing promotion authority.

Add:

* cost-adjusted enhanced value;
* shadow-cycle completion rates;
* evidence-completeness stratification;
* performance by completeness status;
* performance by research outcome;
* performance by market regime;
* performance excluding incomplete runs;
* separate reporting of excluded samples;
* role-failure correlation;
* prompt-version comparison.

Promotion remains deterministic.

Requirements:

* no promotion before minimum samples;
* no promotion when recurring operations are unstable;
* no promotion when costs are unknown;
* no promotion when unsupported-claim rate exceeds threshold;
* no promotion when reconciliation mismatches exist;
* no live-trading promotion status;
* enhanced execution remains disabled.

---

# Step 25 — CLI commands

Add commands consistent with existing conventions.

Suggested commands:

```bash
python -m trading_research.cli run-due-shadow-cycle

python -m trading_research.cli shadow-status

python -m trading_research.cli shadow-readiness

python -m trading_research.cli shadow-run-history

python -m trading_research.cli shadow-budget-status

python -m trading_research.cli shadow-alerts

python -m trading_research.cli shadow-pause \
  --reason "operator maintenance"

python -m trading_research.cli shadow-resume \
  --reason "maintenance complete"

python -m trading_research.cli shadow-kill \
  --reason "critical safety issue"

python -m trading_research.cli shadow-lease-status

python -m trading_research.cli corporate-status \
  --symbol AAPL \
  --as-of 2026-07-10T20:00:00Z
```

Requirements:

* structured JSON;
* no credentials;
* no raw provider payloads;
* no raw prompts;
* no raw Claude responses;
* non-zero exit code on actual errors;
* safe no-op statuses are not errors;
* operator actions persisted;
* resume cannot override `KILLED` without a separate explicit process;
* no CLI live-trading option.

---

# Step 26 — Data retention and cleanup

Create a deterministic retention policy.

Classify:

* immutable evidence;
* raw SEC filing documents;
* provider request metadata;
* account-linked normalized market data;
* research attempts;
* structured failures;
* scheduler runs;
* alerts;
* budget reservations;
* delivery logs.

Requirements:

* no destructive cleanup by default;
* dry-run cleanup command;
* retention config;
* protected audit records;
* evidence hashes retained when content is removed;
* licensing restrictions honored;
* no secrets;
* no removal of records needed for active evaluation;
* no removal that breaks replay without an explicit status.

Suggested CLI:

```bash
python -m trading_research.cli retention-plan
python -m trading_research.cli retention-apply --dry-run
```

Do not implement permanent deletion without tests and explicit configuration.

---

# Step 27 — Testing strategy

Preserve every existing test.

The default suite must remain offline and deterministic.

## A. Corporate-status tests

Test:

* earliest filing derivation;
* annual and quarterly filing presence;
* late-filing notice;
* bankruptcy signal;
* delisting signal;
* registration-termination signal;
* shell disclosure;
* explicit going-concern disclosure;
* disclosure absent from searched sections;
* document unavailable;
* future filing excluded;
* amendment handling;
* duplicate filing;
* point-in-time filtering;
* no false healthy default.

## B. Filing-document tests

Test:

* official locator;
* document-size cap;
* HTML normalization;
* section extraction;
* content hash;
* cache;
* corrupted cache;
* injection annotation;
* no unrestricted full filing passed to Claude.

## C. Operating-history tests

Test:

* public-reporting-history derivation;
* unknown history;
* historical as-of;
* source method retained;
* proxy not mislabeled as company age;
* integration with screener only when semantically valid.

## D. News-provider tests

Test:

* authentication presence;
* article normalization;
* publication cutoff;
* duplicate syndication;
* symbol ambiguity;
* size cap;
* provider failure;
* rate limit;
* licensing retention;
* explicit environment pending.

## E. Reddit tests

Test:

* missing credentials;
* read-only tools;
* unknown tool rejection;
* mutation rejection;
* duplicate post handling;
* cashtag ambiguity;
* cutoff;
* injection risk;
* no Claude-direct MCP access.

## F. Evidence-completeness tests

Test every status.

Verify:

* news missing but screening complete;
* critical status missing blocks;
* conflicting critical evidence blocks;
* unsafe evidence blocks;
* policy version persisted.

## G. Lease tests

Test:

* acquisition;
* conflict;
* renewal;
* release;
* expiration;
* stale recovery;
* concurrent connections;
* operator force release audit;
* no duplicate cycle.

## H. Pause and kill-switch tests

Test:

* active;
* manual pause;
* automatic pause;
* killed;
* resume;
* override audit;
* provider not called while paused;
* Claude not called while paused;
* killed cannot silently resume.

## I. Budget tests

Test:

* reservation;
* duplicate reservation;
* concurrent reservation;
* daily cap;
* monthly cap;
* token cap;
* output cap;
* cost cap;
* pricing missing;
* partial settlement;
* abandoned reservation expiry;
* budget breach pause;
* no new symbol after exhaustion.

## J. Scheduler tests

Test:

* due;
* not due;
* holiday;
* outside window;
* daylight-saving transition;
* missed within catch-up;
* missed too old;
* already completed;
* lease held;
* pause;
* kill;
* exactly one cycle per intended time.

## K. Alert tests

Test:

* persistence;
* delivery;
* bounded retry;
* deduplication;
* failure visibility;
* sanitized payload;
* severity;
* pause alert;
* budget alert;
* provider alert.

## L. Health and readiness tests

Test:

* healthy;
* degraded;
* pause recommended;
* pause required;
* insufficient data;
* environmentally blocked;
* reconciliation mismatch;
* cost unknown;
* unstable scheduler;
* single successful cycle not ready.

## M. End-to-end offline shadow tests

Implement:

```text
due scheduled run
→ lease acquired
→ budget reserved
→ fixture corporate-status evidence
→ fixture news
→ deterministic baseline
→ scripted Claude committee
→ enhanced shadow recommendation
→ no enhanced execution
→ evaluation records
→ health result
→ alert persistence
→ budget settlement
→ lease release
```

Also:

```text
second concurrent invocation
→ lease conflict
→ no provider call
→ no Claude call
→ no duplicate cycle
```

And:

```text
budget exhausted after symbol one
→ symbol one retained
→ remaining symbols skipped
→ budget pause persisted
→ alert persisted
→ no additional Claude call
```

And:

```text
critical corporate status unknown
→ deterministic screening incomplete
→ Claude skipped
→ no recommendation execution
→ explicit evidence-completeness reason
```

---

# Step 28 — Opt-in real smoke tests

Add separately gated tests.

Suggested markers and flags:

```text
RUN_CORPORATE_STATUS_TESTS=true
RUN_NEWS_API_TESTS=true
RUN_REDDIT_SENTIMENT_TESTS=true
RUN_REAL_SHADOW_CYCLE=true
RUN_REAL_CLAUDE_SHADOW_CYCLE=true
```

## Real corporate-status smoke

Use one stable symbol and fixed historical as-of.

Validate:

* real SEC access;
* point-in-time filings;
* operating-history derivation;
* at least one corporate-status category;
* normalized evidence;
* provenance.

## Real news smoke

Validate only when credentials and entitlement exist:

* authentication;
* bounded historical query;
* publication timestamps;
* normalized records;
* deduplication;
* no future data.

## Real Reddit smoke

Validate only when credentials exist:

* read-only call;
* bounded results;
* timestamps;
* normalized sentiment;
* no mutation.

## Real shadow-cycle smoke

Run:

```text
one symbol
→ real SEC
→ real market data
→ real corporate-status evidence
→ optional real news
→ deterministic baseline
→ scripted research provider
→ SHADOW_ENHANCED
→ no paper submission
→ budget reservation and settlement
→ health result
→ no alert or expected alert
```

## Real Claude shadow-cycle smoke

Use:

* one symbol;
* immutable evidence;
* bounded role set;
* manager required;
* configured pricing;
* strict cost cap;
* no paper submission;
* no execution.

Report:

* roles;
* attempts;
* failures;
* token usage;
* latency;
* estimated and actual cost where pricing is configured;
* no enhanced execution.

Do not combine first-time scheduler activation with first-time real-Claude validation.

---

# Step 29 — Scheduler artifact validation

Validate the scheduler artifact without activating recurring execution.

For launchd or equivalent:

* validate syntax;
* validate paths;
* validate working directory;
* validate environment-loading approach;
* run the invoked command manually;
* verify disabled/not-due behavior;
* verify logs contain no secrets.

Do not install or enable the schedule unless explicitly instructed.

---

# Step 30 — Documentation

Create:

```text
docs/milestone7-production-shadow-operations.md
docs/runbooks/shadow-operations.md
docs/runbooks/shadow-incident-response.md
```

Document:

* corporate-status evidence;
* operating-history semantics;
* going-concern limitations;
* news provider;
* Reddit path;
* evidence-completeness policy;
* scheduler architecture;
* leases;
* pause and kill switch;
* budgets;
* alerts;
* health rules;
* readiness;
* retention;
* CLI;
* scheduler artifact;
* activation procedure;
* rollback;
* incident response;
* known limitations.

Include Mermaid diagrams for:

1. corporate-status evidence flow;
2. scheduler invocation;
3. lease lifecycle;
4. budget reservation and settlement;
5. pause and kill switch;
6. shadow-cycle flow;
7. alert flow;
8. readiness and promotion gating.

Clearly distinguish:

```text
CODE-COMPLETE SCHEDULER SUPPORT
```

```text
DEPLOYABLE SCHEDULER ARTIFACT
```

```text
ACTUAL RECURRING DEPLOYMENT ACTIVATED
```

Do not claim the third unless it genuinely occurred.

---

# Step 31 — Security and safety review

Before finalizing, verify:

* no secrets committed;
* no `.env` content printed;
* no authorization headers persisted;
* no raw account identifiers persisted;
* no Claude access to provider credentials;
* no Claude access to broker tools;
* no enhanced execution;
* no live execution;
* no model pause/resume authority;
* no model budget authority;
* no silent scheduler activation;
* no duplicate cycle;
* no duplicate paper intent;
* no stale lease deadlock;
* no unknown-cost recurring Claude run;
* no unsupported corporate-status default;
* no future filing leakage;
* no future article leakage;
* no current price historical substitution;
* no `real_orders` write path;
* no recommendation immutability weakening;
* no test weakening or deletion.

---

# Suggested implementation order

Proceed in this order:

1. Create the Milestone 7 scratchpad.
2. Inspect repository and Git state.
3. Run both baselines.
4. Complete the gap analysis.
5. Draft ADR 0005.
6. Add corporate-status models.
7. Extend official SEC corporate-status retrieval.
8. Add bounded filing-document retrieval.
9. Add deterministic disclosure extraction.
10. Add operating-history derivation.
11. Add corporate-action evidence where officially supported.
12. Select and implement one real news provider.
13. Complete real Reddit path when credentials permit.
14. Add evidence-completeness policy.
15. Add shadow-operations configuration.
16. Add operational schemas and repositories.
17. Add lease handling.
18. Add pause and kill switch.
19. Add budget reservation and settlement.
20. Add role/token/cost enforcement.
21. Add the single-run scheduler entry point.
22. Add schedule and catch-up semantics.
23. Add deployable scheduler artifact.
24. Add alert models and sinks.
25. Add health rules.
26. Add readiness report.
27. Extend evaluation and promotion reporting.
28. Add CLI commands.
29. Add retention planning.
30. Add offline tests.
31. Add opt-in real tests.
32. Validate scheduler artifact without activating it.
33. Run the full main suite.
34. Run the paper-runtime suite.
35. Update documentation and runbooks.
36. Finalize the scratchpad.
37. Perform the security and safety review.

Avoid broad unrelated refactoring.

---

# Acceptance criteria

Milestone 7 is code-complete only when:

1. All Milestone 1–6.1 tests continue to pass.
2. Default tests require no network or credentials.
3. Corporate-status evidence exists with provenance.
4. Operating-history semantics are explicit.
5. Missing corporate-status evidence remains fail-closed.
6. Going-concern results distinguish found, not found in searched sources, unknown, and unavailable.
7. Real news integration exists or is honestly environmentally pending.
8. Real Reddit integration exists or is honestly environmentally pending.
9. Evidence-completeness policy is deterministic and versioned.
10. One-run scheduler entry point exists.
11. Scheduler invocation is idempotent.
12. Duplicate concurrent runs are prevented.
13. Lease expiration and stale recovery work.
14. Pause and kill switch work.
15. Operator actions are audited.
16. Per-cycle budget is enforced before calls.
17. Daily and monthly budgets are enforced.
18. Missing pricing blocks recurring real-Claude use.
19. Token, role, attempt, latency, and cost limits are enforced.
20. Budget breach prevents additional calls.
21. Enhanced recommendations remain non-executable.
22. No live execution path exists.
23. Operational alerts are persisted.
24. Alert deduplication works.
25. Health rules are deterministic.
26. Readiness does not declare production readiness from one successful smoke test.
27. Scheduler artifact contains no secrets.
28. Scheduler artifact is not activated automatically.
29. Retention behavior is safe and dry-run first.
30. Real smoke-test claims match actual execution.
31. `real_orders` remains write-blocked.
32. Recommendation immutability remains intact.
33. Paper runtime tests remain unchanged.
34. Documentation distinguishes code support from actual recurring activation.
35. Scratchpad accurately records all work and limitations.
36. No commit or push occurs unless explicitly requested.

Environmental validation may remain pending when credentials or provider entitlement are unavailable, but every pending item must be named precisely.

---

# Required final response

At completion, provide:

1. Baseline verification.
2. Git-state findings.
3. Scratchpad path and final status.
4. Gap-analysis summary.
5. ADR decision summary.
6. Corporate-status implementation.
7. Operating-history semantics.
8. Going-concern extraction behavior.
9. Corporate-action implementation.
10. News-provider decision and result.
11. Reddit result.
12. Evidence-completeness policy.
13. Shadow-operations configuration.
14. Persistence and schema changes.
15. Lease behavior.
16. Pause and kill-switch behavior.
17. Budget reservation and settlement.
18. Token, role, attempt, latency, and cost enforcement.
19. Scheduler entry point.
20. Schedule and catch-up behavior.
21. Scheduler artifact.
22. Whether actual recurring deployment was activated.
23. Alerting implementation.
24. Operational health rules.
25. Readiness report.
26. Evaluation and promotion changes.
27. Retention behavior.
28. CLI commands.
29. Files created.
30. Files modified.
31. Tests added.
32. Main-suite result.
33. Paper-runtime result.
34. Corporate-status smoke result.
35. News smoke result.
36. Reddit smoke result.
37. Real shadow-cycle result.
38. Real Claude shadow-cycle result.
39. Scheduler artifact validation.
40. Bugs discovered through real validation.
41. Security review.
42. Known limitations.
43. Recommended Milestone 8.

Include a concise mapping:

```text
Requirement → implementation file → verifying test
```

Label implementation and validation areas as:

```text
OFFLINE-DETERMINISTIC
REAL-SEC-CORPORATE-STATUS
REAL-FILING-DISCLOSURE
REAL-MARKET-DATA
REAL-NEWS-DATA
REAL-REDDIT-SENTIMENT
REAL-CLAUDE-SHADOW
SCHEDULED-SHADOW-SUPPORT
DEPLOYABLE-SCHEDULER-ARTIFACT
ACTUAL-RECURRING-DEPLOYMENT
BASELINE-PAPER-EXECUTION
ENHANCED-SHADOW-ONLY
ENVIRONMENTALLY-PENDING
```

Do not claim:

* real news validation without a successful real request;
* real Reddit validation without a successful read-only request;
* real recurring deployment merely because a scheduler artifact exists;
* production readiness from a single cycle;
* enhanced performance improvement without sufficient out-of-sample evidence;
* any live-trading capability.

Do not commit or push unless explicitly asked.
