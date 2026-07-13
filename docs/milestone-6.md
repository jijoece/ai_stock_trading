You are continuing implementation of my existing AI-driven trading-desk repository.

Milestones 1 through 5 are implemented. Do not start a new repository, replace the existing architecture, or reimplement completed functionality.

Your task is to implement:

# Milestone 6 — Real evidence acquisition and continuous paper evaluation

This is a direct implementation task, not a research-only investigation.

The goal is to replace fixture-only research inputs with safe, point-in-time-aware real evidence providers and create a repeatable evaluation loop that compares:

* the deterministic baseline;
* the Claude-enhanced research path;
* paper-execution outcomes;
* benchmark-relative forward performance;
* research latency, token usage, and cost.

The existing trading desk must remain authoritative for:

* screening;
* deterministic scoring;
* evidence normalization;
* recommendation construction;
* risk and position sizing;
* recommendation freezing;
* execution eligibility;
* paper execution;
* ledger accounting;
* broker reconciliation;
* performance evaluation.

Claude remains a research provider only.

Do not stop after creating a plan. Inspect the repository, implement the vertical slice, add tests, run the complete test suites, and document the actual outcome.

---

# Mandatory progress scratchpad

Before making any code changes, create:

```text
.claude/scratchpads/milestone6-progress.md
```

Use this file as the persistent source of truth for implementation progress.

The scratchpad must contain at least:

```markdown
# Milestone 6 Progress

Started: <UTC timestamp>
Branch: <branch name>
Status: STARTING

## Baseline
- Main test suite:
- Paper runtime suite:
- Git status:
- Available credentials/providers:
- Explicitly unavailable credentials/providers:

## Repository findings

## Gap analysis

## Architecture decisions

## Provider decisions
- Provider:
- Purpose:
- Authentication:
- Point-in-time guarantees:
- Rate limits:
- Licensing/usage restrictions:
- Fallback behavior:

## Implementation checklist

## Files created

## Files modified

## Schema and migration changes

## Tests added

## Test run log

## Bugs discovered and fixed

## Security and secret-handling incidents

## Environmental validation

## Known limitations

## Remaining work

## Final status
```

Scratchpad rules:

1. Create it before editing implementation files.
2. Update it after every major implementation step.
3. Record actual test commands and results.
4. Record important design decisions when they are made.
5. Record bugs found during real-provider smoke tests.
6. Preserve earlier entries; do not rewrite history to make progress look cleaner.
7. Never put API keys, secrets, tokens, account IDs, or full credential-bearing environment output in the scratchpad.
8. Do not use commands that print secret values.
9. If checking credential presence, record only Boolean presence or a redacted result.
10. Before the final response, make the scratchpad accurately reflect:

    * completed work;
    * tests run;
    * environmental validation completed;
    * environmental validation pending;
    * known limitations.
11. If the context window becomes constrained, reread the scratchpad and continue from it instead of repeating completed work.
12. Do not commit the scratchpad unless repository conventions indicate scratchpads are intentionally version-controlled. Inspect `.gitignore` first.

---

# Confirmed starting state

Milestone 5 reported:

```text
Main repository:
571 passed, 2 skipped

Isolated paper runtime:
33 passed
```

Milestone 5 implemented:

* immutable point-in-time evidence snapshots;
* source provenance;
* evidence validation;
* prompt-injection defenses;
* Claude provider boundary;
* forced structured output through Anthropic tool use;
* role-based research reports;
* claim-to-evidence validation;
* bounded retries;
* research-run persistence;
* prompt registry and versioning;
* deterministic replay;
* deterministic research overlay;
* baseline-versus-Claude experiment assignments;
* model usage, latency, and cost tracking;
* research CLI commands;
* evaluation comparison;
* opt-in Claude API smoke testing.

The real Claude API smoke test reached Anthropic but was blocked by account billing or insufficient credit. It did not validate a successful real structured output.

Milestone 4 is now fully validated for a credentialed paper-broker acknowledgement:

* verified Alpaca paper endpoint;
* real paper-account retrieval;
* real non-marketable limit-order submission;
* real broker order ID;
* real order lookup;
* real cancellation;
* reconciliation confirming no fill and no position change.

The paper-broker smoke test passed after fixing:

* subprocess `PYTHONPATH` and working-directory wiring;
* isolated-runtime `.env` loading before credential inspection;
* explicit `python-dotenv` dependency.

Important safety invariants already established:

* main application does not import LumiBot;
* broker runtime is isolated;
* no real-money trading;
* no Robinhood write operations;
* no LLM-to-order path;
* recommendation immutability;
* paper-order idempotency;
* duplicate-fill prevention;
* `real_orders` write blocking;
* unknown state fails closed;
* missing financial values are not fabricated;
* historical evaluation avoids current-price substitution;
* Claude cannot increase position size or bypass screening.

Verify all of this against the actual repository before modifying anything.

---

# Core Milestone 6 architecture

Use this data flow:

```text
Scheduled research run
        ↓
Configured candidate universe
        ↓
Existing deterministic screen and score
        ↓
Real point-in-time evidence providers
        ↓
Raw source records
        ↓
Normalized immutable EvidenceSnapshot
        ↓
Deterministic baseline arm
        │
        └────────────────────────────┐
                                     │
Claude research committee            │
        ↓                            │
Validated ResearchDecision           │
        ↓                            │
Deterministic research overlay       │
        ↓                            │
Claude-enhanced arm                  │
        └────────────────────────────┘
                    ↓
Existing recommendation builder
                    ↓
Frozen recommendations
                    ↓
Existing paper-execution pipeline
                    ↓
Broker sync, ledger, and reconciliation
                    ↓
Forward evaluation and attribution
                    ↓
Promotion, rollback, or rejection decision
```

Ownership must remain:

```text
Evidence providers:
    Retrieve raw facts and source metadata

Claude:
    Analyze supplied evidence

Deterministic application code:
    Validate, compare, decide, size, execute, reconcile, and evaluate
```

---

# Milestone objectives

Implement a safe vertical slice that can:

1. Run the existing screener and scorer for a configured universe.
2. Retrieve real point-in-time evidence for selected candidates.
3. Persist raw provider responses or normalized source records safely.
4. Normalize evidence into the existing immutable evidence contracts.
5. Identify stale, incomplete, conflicting, or point-in-time-unsafe evidence.
6. Run both deterministic and Claude-enhanced experiment arms.
7. Create frozen recommendations through existing builders.
8. Optionally route eligible recommendations to the existing paper pipeline.
9. Sync paper-broker outcomes.
10. Evaluate recommendations at configured forward horizons.
11. Compare baseline and Claude-enhanced outcomes.
12. Track evidence-provider reliability, latency, usage, and cost.
13. Support repeatable scheduled runs.
14. Resume safely after interruption.
15. Avoid duplicate evidence snapshots, research runs, recommendations, and paper orders.
16. Keep all default tests offline and deterministic.
17. Provide explicit opt-in smoke tests for each real provider.
18. Preserve all Milestone 1–5 controls.

---

# Non-goals

Do not implement:

* live-money trading;
* Robinhood order placement;
* Robinhood order cancellation;
* Robinhood order modification;
* autonomous broker execution;
* options;
* short selling;
* margin;
* fractional shares unless already explicitly supported;
* extended-hours execution;
* high-frequency trading;
* streaming market-data infrastructure unless required for a narrowly scoped provider;
* unrestricted web browsing by Claude;
* unrestricted MCP access by Claude;
* direct Claude access to raw provider credentials;
* direct Claude access to broker tools;
* model-generated position size;
* model-generated final quantity;
* model override of deterministic policy;
* model promotion based only on narrative quality;
* replacing existing evaluation modules;
* replacing existing evidence models;
* replacing the isolated paper runtime;
* adding several overlapping market-data providers without a justified need.

---

# Step 1 — Inspect and establish the baseline

Before editing:

1. Check Git status and branch.
2. Create the mandatory scratchpad.
3. Read:

   * Milestone 1 documentation;
   * `docs/milestone2-analysis-layer.md`;
   * `docs/milestone3-lumibot-paper-integration.md`;
   * `docs/milestone4-isolated-paper-broker.md`;
   * `docs/milestone5-evidence-backed-claude-research.md`;
   * all existing ADRs;
   * `docs/milestone-6.md` if it exists;
   * existing research models and persistence;
   * evidence builders and validators;
   * experiment and comparison modules;
   * price-provider protocols;
   * market-calendar logic;
   * recommendation builder;
   * candidate-analysis service;
   * paper execution services;
   * evaluation repositories and metrics;
   * configuration and CLI conventions.
4. Run:

```bash
pytest tests/ -q
```

Expected:

```text
571 passed, 2 skipped
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

6. Record the exact results in the scratchpad.
7. Inspect whether the Milestone 4 credentialed fixes and Milestone 5 changes are committed.
8. Do not silently work on top of unrelated dirty changes.

Produce a concise repository assessment before implementation.

---

# Step 2 — Gap analysis

Compare the repository with the target architecture.

Classify each item as:

```text
IMPLEMENTED
PARTIALLY_IMPLEMENTED
MISSING
CONFLICTING
ENVIRONMENTALLY_BLOCKED
```

At minimum assess:

* real historical-price provider;
* real current quote provider;
* real fundamentals provider;
* SEC filing provider;
* real news provider;
* real sentiment provider;
* read-only portfolio-context provider;
* provider caching;
* source-response persistence;
* point-in-time availability metadata;
* provider rate-limit handling;
* provider retries;
* scheduled pipeline;
* candidate batching;
* experiment creation;
* paper-execution linkage;
* evaluation scheduling;
* provider health metrics;
* promotion gates;
* rollback behavior;
* data-retention policy.

Record the gap analysis in the scratchpad before implementing.

---

# Step 3 — Select the minimum provider set

Do not add providers casually.

The first production-quality provider set should cover:

1. Historical and current equity prices.
2. Company fundamentals.
3. SEC filings and company facts.
4. News or catalyst evidence.
5. Existing Reddit sentiment, when configured.
6. Optional read-only portfolio context.

## Provider-selection rules

Before adding a provider:

* inspect official documentation;
* confirm authentication requirements;
* confirm available timestamps;
* confirm historical data behavior;
* confirm rate limits;
* confirm symbol conventions;
* confirm adjustment behavior for splits and dividends;
* confirm licensing and redistribution restrictions;
* confirm whether data may be persisted;
* confirm whether point-in-time data is actually available;
* confirm supported markets;
* confirm SDK or API stability;
* confirm the dependency footprint.

Prefer:

* official SEC EDGAR APIs for filings and company facts;
* one primary market-data provider;
* one primary news provider;
* existing read-only MCP or API integrations where they already satisfy safety requirements.

Avoid:

* scraping financial websites against their terms;
* undocumented endpoints;
* HTML scraping where a documented API exists;
* combining conflicting providers without explicit reconciliation;
* relying on a broker quote as the only historical research source;
* silently substituting one provider when another fails.

Record the provider decision and rationale in the scratchpad.

---

# Step 4 — Framework-neutral provider contracts

Extend the existing evidence layer with narrowly scoped protocols.

Possible layout:

```text
src/trading_research/evidence_providers/
├── __init__.py
├── models.py
├── errors.py
├── market_data_protocol.py
├── fundamentals_protocol.py
├── filings_protocol.py
├── news_protocol.py
├── sentiment_protocol.py
├── portfolio_context_protocol.py
├── provider_registry.py
├── health.py
├── rate_limits.py
└── normalization.py
```

Use repository conventions rather than forcing this exact structure.

Suggested contracts:

```python
class MarketDataProvider(Protocol):
    def get_quote(
        self,
        symbol: str,
        *,
        as_of: datetime,
    ) -> QuoteEvidence:
        ...

    def get_price_history(
        self,
        symbol: str,
        *,
        start: date,
        end: date,
        as_of: datetime,
    ) -> tuple[PriceBar, ...]:
        ...


class FundamentalsProvider(Protocol):
    def get_fundamentals(
        self,
        symbol: str,
        *,
        as_of: datetime,
    ) -> FundamentalsEvidence:
        ...


class FilingProvider(Protocol):
    def list_filings(
        self,
        symbol: str,
        *,
        available_by: datetime,
    ) -> tuple[FilingRecord, ...]:
        ...


class NewsProvider(Protocol):
    def list_news(
        self,
        symbol: str,
        *,
        published_after: datetime,
        available_by: datetime,
    ) -> tuple[NewsRecord, ...]:
        ...
```

Requirements:

* no provider-specific objects outside provider adapters;
* all datetimes must be timezone-aware;
* providers must expose source timestamps;
* providers must distinguish retrieval failure from empty result;
* providers must not invent missing fields;
* providers must support injected HTTP clients or transports for testing;
* providers must not write recommendations;
* providers must not call execution services;
* provider retries must be bounded;
* unknown provider errors fail closed.

---

# Step 5 — Provider response and source-record persistence

Persist enough information to reproduce and audit evidence without violating provider licensing.

Potential tables:

* `evidence_provider_requests`;
* `evidence_provider_responses`;
* `evidence_provider_failures`;
* `evidence_provider_health`;
* `evidence_raw_documents`;
* `evidence_normalization_runs`;
* `evidence_conflicts`.

Persist:

* provider;
* operation;
* request identity;
* symbol;
* requested as-of time;
* retrieval time;
* provider response timestamp;
* HTTP status where appropriate;
* provider request ID where safe;
* content hash;
* normalized-record hash;
* cache status;
* rate-limit metadata;
* latency;
* error code;
* retryability;
* licensing or retention classification;
* raw-payload storage status.

Do not persist:

* API keys;
* authorization headers;
* signed URLs containing credentials;
* unredacted account identifiers;
* secrets;
* oversized raw payloads without limits.

If provider terms do not permit raw response persistence:

* persist normalized data;
* persist a content hash;
* persist source locator and metadata;
* document the limitation.

---

# Step 6 — SEC filing and company-facts provider

Implement an official SEC-based provider.

Support, where available:

* CIK resolution;
* recent filing metadata;
* form type;
* accession number;
* filing date;
* accepted timestamp;
* report period;
* filing URL or SEC locator;
* company facts;
* normalized financial concepts;
* source provenance.

Point-in-time requirements:

* use the filing acceptance or availability timestamp;
* do not treat a later amendment as known before its publication;
* retain amendment relationships;
* distinguish filing period from availability time;
* avoid using the latest company-facts value for an earlier historical as-of date unless it was available then;
* prevent look-ahead through restated values where detectable.

Implement deterministic rate limiting and SEC-compliant identification headers according to current official requirements.

Do not scrape rendered filing pages when official structured endpoints provide the required data.

---

# Step 7 — Market-data provider

Implement one real primary market-data adapter.

Support:

* current or as-of quote;
* daily adjusted or explicitly unadjusted bars;
* volume;
* corporate-action metadata where available;
* trading calendar compatibility;
* historical benchmark prices;
* symbol validation;
* provider timestamps.

Requirements:

* explicitly record adjusted versus unadjusted;
* do not combine adjusted prices with unadjusted volumes without documenting behavior;
* avoid survivorship-only symbol assumptions;
* do not substitute the current quote for a missing historical close;
* reject future bars relative to the requested `as_of`;
* reject duplicate or non-monotonic bars;
* validate high/low/open/close relationships;
* validate nonnegative volume;
* record missing trading sessions;
* preserve Decimal precision where practical;
* cache immutable historical ranges;
* apply short TTLs to current quotes.

Add a provider-independent normalizer.

---

# Step 8 — Fundamentals provider

Implement a real fundamentals provider, or derive fundamentals from official SEC company facts where sufficient.

Potential normalized fields:

* revenue;
* revenue growth;
* earnings;
* earnings growth;
* gross margin;
* operating margin;
* free cash flow;
* operating cash flow;
* cash;
* debt;
* shares outstanding;
* dilution indicators;
* market capitalization;
* valuation ratios when inputs are valid.

Requirements:

* every normalized value references source evidence;
* preserve reporting period;
* preserve currency;
* preserve units;
* preserve filing or provider availability time;
* distinguish trailing, annual, and quarterly values;
* reject incompatible units;
* do not compute ratios when required inputs are missing;
* do not treat zero as missing;
* do not treat missing as zero;
* do not silently merge conflicting values;
* identify restated values where possible.

All derived values must be deterministic and reconstructible.

---

# Step 9 — News and catalyst provider

Implement one real news provider or a provider-neutral adapter with an opt-in real implementation.

Normalize:

* article ID;
* headline;
* source;
* publication time;
* provider availability time;
* URL or locator;
* symbols;
* summary or excerpt;
* category;
* duplicate group;
* content hash;
* source trust classification.

Requirements:

* preserve original publication timestamp;
* deduplicate syndicated articles;
* avoid counting copied headlines as independent confirmation;
* enforce article and content-size limits;
* annotate prompt-injection risk;
* treat article text as untrusted data;
* exclude future-published articles from historical snapshots;
* identify missing or ambiguous symbol association;
* do not infer earnings dates from low-confidence articles when an official source exists.

Do not permit Claude to follow links or browse independently during the research run.

---

# Step 10 — Sentiment and Reddit integration

Reuse the existing sentiment pipeline.

If a real Reddit integration is enabled:

* use existing MCP tool policy;
* discover actual read-only tools;
* explicitly allowlist them;
* reject unknown tools;
* reject mutation;
* reject posting;
* reject voting;
* reject messaging;
* reject account mutation;
* do not expose MCP tools directly to Claude.

Continue to normalize:

* duplicate posts;
* cross-posts;
* ambiguous symbols;
* cashtags;
* engagement;
* author metadata where available;
* subreddit;
* creation time;
* classification;
* injection risk.

Sentiment evidence must not become a direct execution signal without deterministic validation.

---

# Step 11 — Read-only portfolio context

Portfolio context may be included in evidence only through deterministic application code.

Potential fields:

* existing position quantity;
* current portfolio weight;
* sector exposure;
* unrealized P&L;
* existing open paper orders;
* recent completed paper orders;
* concentration;
* available paper cash.

Requirements:

* read-only;
* no account mutation;
* no broker tools exposed to Claude;
* account identifiers removed or redacted;
* snapshot timestamp recorded;
* portfolio context clearly separated from company evidence;
* unavailable portfolio state causes explicit missing context;
* Claude cannot use portfolio context to calculate final order quantity.

---

# Step 12 — Evidence normalization and conflict handling

Build a deterministic normalizer that converts provider outputs into the existing `EvidenceSnapshot`.

Requirements:

* stable canonical serialization;
* deterministic snapshot IDs;
* source-record linkage;
* category-level freshness rules;
* required-evidence rules;
* optional-evidence rules;
* provider-priority rules;
* conflict-group preservation;
* no silent “last value wins” behavior;
* point-in-time safety check;
* maximum snapshot size;
* deterministic truncation;
* prompt-injection annotation;
* no provider-specific objects in snapshots.

Create explicit outcomes such as:

```text
COMPLETE
COMPLETE_WITH_CONFLICTS
INCOMPLETE_REQUIRED_DATA
STALE_REQUIRED_DATA
POINT_IN_TIME_UNSAFE
PROVIDER_UNAVAILABLE
```

A snapshot that is unsafe or missing required evidence must not enter the enhanced recommendation path.

---

# Step 13 — Provider caching and rate limiting

Implement deterministic provider caching.

Cache identity should include:

* provider;
* operation;
* canonical symbol;
* requested as-of time;
* date range;
* normalized request parameters;
* provider schema version.

Use separate policies for:

* immutable historical data;
* filings;
* company facts;
* news;
* current quotes;
* portfolio context.

Requirements:

* historical immutable data may use long-lived caching;
* current quotes must use short TTLs;
* stale cache must be identified;
* cache hits and misses must be recorded;
* corrupted cache entries must fail closed;
* retries must honor provider rate limits;
* rate-limit errors must be distinguishable;
* no thundering-herd retry;
* no infinite retry;
* test clock must control TTL behavior.

---

# Step 14 — Scheduled research-run service

Implement an idempotent scheduled-run service, not an uncontrolled daemon.

Suggested service:

```python
def run_scheduled_research_cycle(
    *,
    as_of: datetime,
    universe_id: str,
    configuration: ScheduledResearchConfiguration,
    repositories: ResearchCycleRepositories,
    providers: EvidenceProviderRegistry,
    research_provider: ResearchModelProvider,
    clock: Clock,
) -> ResearchCycleResult:
    ...
```

Expected sequence:

1. Create or load a deterministic cycle ID.
2. Record cycle start.
3. Load configured universe.
4. Run deterministic screening.
5. Run deterministic scoring.
6. Select the bounded candidate set.
7. Build real evidence snapshots.
8. Create deterministic baseline recommendations.
9. Run Claude-enhanced research when explicitly enabled.
10. Apply deterministic overlay.
11. Create enhanced recommendations.
12. Freeze both arms.
13. Optionally submit eligible paper recommendations according to experiment policy.
14. Persist cycle results.
15. Record failures per symbol without losing the whole cycle.
16. Mark cycle complete or partially complete.

Requirements:

* same `as_of` across both arms;
* same deterministic inputs across both arms;
* bounded number of candidates;
* bounded Claude calls;
* idempotent rerun;
* resumable per symbol;
* no duplicate recommendation;
* no duplicate paper intent;
* explicit cycle status;
* explicit partial-failure status;
* no live execution;
* no hidden background thread.

---

# Step 15 — Experiment execution policy

Define how recommendations enter paper execution.

Possible initial policies:

```text
OBSERVE_ONLY
BASELINE_ONLY
ENHANCED_ONLY
BOTH_SEPARATE_PAPER_BOOKS
SHADOW_ENHANCED
```

Prefer initially:

```text
BASELINE_ONLY
```

or:

```text
SHADOW_ENHANCED
```

In shadow mode:

* enhanced recommendations are generated and evaluated;
* enhanced recommendations do not submit paper orders;
* simulated or reference-price evaluation is used;
* baseline retains the actual paper-execution path.

Do not submit two competing orders into the same paper account without a clear strategy for portfolio isolation and attribution.

If both arms execute later, implement separate logical paper portfolios or allocation namespaces first.

---

# Step 16 — Continuous evaluation lifecycle

Extend the evaluation layer with a cycle-based evaluation service.

Support:

* recommendation created;
* recommendation frozen;
* order intent created;
* order acknowledged;
* order filled;
* order partially filled;
* order rejected;
* order cancelled;
* no order generated;
* forward horizon pending;
* forward horizon complete;
* benchmark unavailable;
* symbol unavailable;
* evaluation incomplete.

Track:

* recommendation price;
* intended entry price;
* actual fill price;
* slippage;
* fees;
* time to acknowledgement;
* time to first fill;
* time to full fill;
* unfilled duration;
* turnover;
* paper cash utilization;
* realized and unrealized paper P&L;
* benchmark-relative return;
* deterministic versus enhanced decision difference.

Do not drop no-action or incomplete outcomes from evaluation.

---

# Step 17 — Time-to-fill, turnover, and calibration metrics

Implement the Milestone 5 remaining metrics.

## Time-to-fill

Track:

* submission to acknowledgement;
* acknowledgement to first fill;
* first fill to terminal state;
* total submission to full fill;
* censored unfilled orders.

Do not treat cancelled or expired orders as zero fill time.

## Turnover

Define turnover explicitly and document the denominator.

Support:

* daily turnover;
* cycle turnover;
* rolling turnover;
* baseline versus enhanced turnover;
* fees and slippage attributable to turnover.

## Confidence calibration

Model confidence is not a probability unless calibrated.

Support:

* confidence buckets;
* observed hit rate by bucket;
* return distribution by bucket;
* incomplete-analysis rate by bucket;
* calibration error only when mathematically appropriate;
* minimum-sample thresholds;
* explicit insufficient-data status.

Do not present confidence calibration with inadequate samples.

---

# Step 18 — Promotion gates

Implement deterministic promotion criteria for research models and prompts.

A model or prompt version must not become the preferred configuration merely because it is newer.

Potential gate inputs:

* minimum completed evaluation count;
* minimum number of market regimes;
* benchmark-relative performance;
* maximum drawdown;
* incomplete-analysis rate;
* unsupported-claim rate;
* provider failure rate;
* retry rate;
* average latency;
* average cost;
* critical-risk detection;
* false downgrade rate;
* turnover;
* slippage;
* reproducibility success rate.

Suggested statuses:

```text
INSUFFICIENT_DATA
REJECTED
SHADOW_ONLY
ELIGIBLE_FOR_PAPER
PREFERRED_FOR_PAPER
ROLLBACK_REQUIRED
```

Requirements:

* deterministic;
* versioned;
* reconstructible;
* no self-promotion by Claude;
* no automatic live-trading promotion;
* no promotion based solely on backtest results;
* no promotion when safety metrics regress;
* rollback path preserved.

---

# Step 19 — Provider and pipeline health

Add operational health metrics:

* requests by provider;
* success rate;
* timeout rate;
* rate-limit rate;
* invalid-response rate;
* stale-data rate;
* point-in-time-unsafe rate;
* cache-hit rate;
* average latency;
* p95 latency where sample size permits;
* cost;
* evidence completeness rate;
* cycle-completion rate;
* symbols skipped;
* research-incomplete rate;
* paper-submission rate;
* reconciliation mismatch rate.

Create explicit health statuses rather than misleading zeroes.

---

# Step 20 — Configuration

Add safe configuration consistent with repository conventions.

Example:

```yaml
scheduled_research:
  enabled: false
  universe_id: low_price_growth
  max_candidates_per_cycle: 10
  experiment_policy: SHADOW_ENHANCED
  submit_paper_orders: false
  require_complete_evidence: true
  require_point_in_time_safe: true
  continue_on_symbol_failure: true

providers:
  market_data:
    enabled: false
    provider: null
    request_timeout_seconds: 30
    max_attempts: 2

  sec:
    enabled: true
    request_timeout_seconds: 30
    max_attempts: 2

  news:
    enabled: false
    provider: null
    request_timeout_seconds: 30
    max_attempts: 2

  sentiment:
    enabled: false

evaluation:
  enabled: true
  benchmark: SPY
  horizons_trading_days:
    - 1
    - 5
    - 10
    - 20
    - 60

promotion:
  enabled: false
  policy_version: research-promotion.v1
  minimum_completed_evaluations: 100
  allow_live_promotion: false
```

Requirements:

* scheduled research defaults to disabled;
* real providers require explicit enablement;
* no provider selected silently;
* absent credentials fail closed;
* paper submission defaults to false;
* live promotion remains false;
* environment variables may provide credentials but cannot enable capabilities;
* unknown provider fails closed;
* unknown experiment policy fails closed;
* unknown promotion status fails closed.

---

# Step 21 — CLI commands

Extend existing argparse conventions.

Suggested commands:

```bash
python -m trading_research.cli provider-health

python -m trading_research.cli fetch-evidence \
  --symbol AAPL \
  --as-of 2026-07-01T20:00:00Z

python -m trading_research.cli run-research-cycle \
  --as-of 2026-07-01T20:00:00Z \
  --provider-mode fixture

python -m trading_research.cli run-research-cycle \
  --as-of 2026-07-01T20:00:00Z \
  --provider-mode real

python -m trading_research.cli resume-research-cycle \
  --cycle-id <id>

python -m trading_research.cli evaluate-research-cycle \
  --cycle-id <id>

python -m trading_research.cli compare-research-cycles

python -m trading_research.cli research-promotion-status

python -m trading_research.cli evidence-provider-usage
```

Requirements:

* fixture mode remains available;
* real mode requires explicit selection;
* show selected providers;
* show cycle ID;
* show provider-health status;
* show incomplete and skipped reasons;
* never print secrets;
* no CLI command may enable live trading;
* no CLI command may accept natural-language order instructions;
* no CLI command may expose arbitrary provider operations;
* non-zero exit code on failed validation.

---

# Step 22 — Testing strategy

Preserve every existing test.

The default suite must run without:

* network access;
* real provider credentials;
* Anthropic credits;
* Robinhood;
* Reddit;
* LumiBot in the main process;
* paper-runtime startup;
* a credentialed broker.

## A. Provider-contract tests

Test:

* quote normalization;
* bar normalization;
* fundamentals normalization;
* filing normalization;
* news normalization;
* timezone requirements;
* invalid symbol;
* missing timestamp;
* malformed payload;
* provider timeout;
* rate limit;
* non-retryable client error;
* bounded retry;
* secret redaction.

## B. SEC tests

Use recorded or synthetic fixtures.

Test:

* CIK resolution;
* filing acceptance timestamp;
* amendment handling;
* company-fact units;
* historical as-of filtering;
* restatement awareness;
* missing filing;
* rate limiting;
* identification headers;
* no future filing leakage.

## C. Market-data tests

Test:

* adjusted/unadjusted designation;
* future bar rejection;
* duplicate date rejection;
* invalid OHLC;
* negative volume;
* missing session;
* benchmark history;
* split metadata;
* cache behavior;
* no current-price historical substitution.

## D. Fundamentals tests

Test:

* period preservation;
* currency preservation;
* units;
* quarterly versus annual;
* trailing calculations;
* missing versus zero;
* ratio reconstruction;
* conflicting values;
* unavailable data;
* no unsupported derived value.

## E. News tests

Test:

* publication timestamp;
* duplicate syndication;
* ambiguous symbol;
* future article exclusion;
* injection annotation;
* deterministic truncation;
* article limits;
* missing publication time;
* provider failure.

## F. Provider-cache tests

Test:

* immutable historical cache;
* quote TTL;
* stale cache;
* cache corruption;
* canonical cache identity;
* rate-limit backoff;
* concurrent identical request deduplication where implemented;
* cache-hit metrics.

## G. Evidence-snapshot tests

Test:

* real-provider normalized snapshot;
* deterministic hash;
* conflict retention;
* required-data failure;
* stale-data failure;
* point-in-time-unsafe failure;
* provider-unavailable status;
* prompt-injection annotation;
* no provider-specific object leakage.

## H. Scheduled-cycle tests

Test:

* deterministic cycle ID;
* same as-of across arms;
* bounded candidates;
* symbol-level failure isolation;
* resumability;
* idempotent rerun;
* no duplicate recommendation;
* no duplicate research run;
* no duplicate paper intent;
* partial cycle status;
* disabled configuration;
* missing provider configuration.

## I. Experiment-policy tests

Test:

* observe only;
* baseline only;
* shadow enhanced;
* enhanced cannot execute in shadow mode;
* both arms cannot share ambiguous position attribution;
* unknown policy fails closed.

## J. Evaluation tests

Test:

* no action;
* incomplete analysis;
* unfilled order;
* partial fill;
* full fill;
* cancellation;
* rejection;
* time to acknowledgement;
* time to first fill;
* time to terminal state;
* turnover;
* benchmark-relative outcome;
* missing benchmark;
* future horizon pending;
* no look-ahead.

## K. Promotion-gate tests

Test:

* insufficient sample;
* safety regression;
* cost regression;
* latency regression;
* unsupported-claim regression;
* drawdown regression;
* eligible for paper;
* preferred for paper;
* rollback required;
* no live promotion;
* deterministic reconstruction.

## L. End-to-end offline integration tests

Implement:

```text
fixture universe
→ deterministic screen
→ deterministic score
→ fixture real-provider responses
→ normalized EvidenceSnapshot
→ deterministic baseline
→ scripted Claude research
→ deterministic overlay
→ frozen recommendations
→ shadow experiment
→ evaluation records
→ comparison report
```

Also test:

```text
one provider unavailable
→ required evidence incomplete
→ Claude arm ANALYSIS_INCOMPLETE
→ baseline retained
→ no enhanced paper intent
→ cycle completes partially
```

And:

```text
same cycle rerun
→ provider cache reused
→ research run reused
→ recommendations reused
→ no duplicate paper intent
→ no duplicate evaluation
```

---

# Step 23 — Opt-in real-provider smoke tests

Create separately marked tests such as:

```text
@pytest.mark.sec_api
@pytest.mark.market_data_api
@pytest.mark.news_api
@pytest.mark.claude_api
```

Require explicit flags:

```text
RUN_SEC_API_TESTS=true
RUN_MARKET_DATA_TESTS=true
RUN_NEWS_API_TESTS=true
RUN_CLAUDE_RESEARCH_TESTS=true
```

Rules:

* never run automatically because credentials happen to exist;
* use one small known symbol;
* use a fixed historical as-of date;
* avoid current trading recommendations;
* validate timestamps and provenance;
* do not submit paper orders;
* do not mutate Robinhood;
* do not persist secrets;
* report acknowledgement versus actual data retrieved honestly.

## SEC smoke test

Should validate:

* official endpoint access;
* identity header;
* CIK resolution;
* one filing or company-facts response;
* publication/availability timestamps;
* normalized evidence.

## Market-data smoke test

Should validate:

* provider authentication;
* one historical range;
* quote or latest eligible bar;
* benchmark range;
* adjusted/unadjusted metadata;
* normalized evidence.

## News smoke test

Should validate:

* provider authentication;
* one bounded historical query;
* publication timestamps;
* normalized records;
* deduplication.

## Claude smoke test

After Anthropic billing is available:

* use one persisted immutable snapshot;
* invoke one role;
* require valid forced-tool structured output;
* validate evidence IDs;
* record tokens and latency;
* do not execute anything.

---

# Step 24 — Real scheduled-cycle smoke test

Add one explicitly gated integration test or script that can run:

```text
real providers
→ one or two symbols
→ fixed as-of timestamp
→ evidence snapshots
→ deterministic baseline
→ optional Claude-enhanced shadow arm
→ frozen recommendations
→ no paper submission
→ evaluation pending records
```

Require explicit opt-in:

```text
RUN_REAL_RESEARCH_CYCLE=true
```

The first real cycle must remain:

```text
SHADOW_ENHANCED
submit_paper_orders: false
```

Do not combine first-time provider validation with broker execution.

---

# Step 25 — Documentation and ADR

Create:

```text
docs/milestone6-real-evidence-continuous-evaluation.md
docs/adr/0004-real-evidence-provider-boundary.md
```

Document:

* provider-selection rationale;
* source licensing and retention restrictions;
* SEC behavior;
* market-data behavior;
* fundamentals behavior;
* news behavior;
* point-in-time guarantees;
* provider caching;
* retries and rate limits;
* scheduled-cycle architecture;
* experiment policy;
* shadow mode;
* paper attribution;
* continuous evaluation;
* turnover;
* time-to-fill;
* confidence calibration;
* promotion gates;
* rollback;
* CLI usage;
* real-provider smoke tests;
* known limitations;
* secret-handling rules.

Include Mermaid diagrams for:

1. evidence-provider architecture;
2. scheduled research cycle;
3. provider caching and retry;
4. baseline versus shadow-enhanced experiment;
5. paper execution and attribution;
6. continuous evaluation;
7. promotion and rollback.

Update the scratchpad with documentation completion and final test results.

---

# Safety requirements

These are hard requirements:

* No live-money trading.
* No Robinhood write operations.
* No Claude-to-order path.
* No model-generated quantity.
* No model-generated final allocation.
* No model override of risk policy.
* No model bypass of screening.
* No unrestricted web browsing by Claude.
* No unrestricted MCP access.
* No provider credentials exposed to Claude.
* No credentials in logs, scratchpad, database, fixtures, or documentation.
* No commands that print `.env` contents.
* No raw secret values in error messages.
* No fabricated prices.
* No fabricated fundamentals.
* No fabricated publication timestamps.
* No missing value converted to zero.
* No future data in historical evidence.
* No current quote substituted for historical close.
* No silent provider fallback.
* No infinite retry.
* No silent conflict resolution.
* No duplicate snapshots.
* No duplicate recommendations.
* No duplicate research runs.
* No duplicate paper orders.
* No duplicate fills.
* No execution from a shadow experiment arm.
* No automatic model promotion.
* No promotion to live trading.
* No weakening of recommendation immutability.
* No writes to `real_orders`.
* No changes to the isolated paper runtime unless a narrow compatibility fix is required and fully justified.
* No weakening or deletion of Milestone 1–5 tests.

---

# Suggested implementation order

Proceed in this order:

1. Create and initialize the scratchpad.
2. Inspect the repository.
3. Run and record both baselines.
4. Produce the gap analysis.
5. Draft the provider-boundary ADR.
6. Select the minimum provider set.
7. Add provider-neutral contracts.
8. Add provider request/response persistence.
9. Implement SEC provider.
10. Implement primary market-data provider.
11. Implement fundamentals normalization.
12. Implement news provider.
13. Integrate existing sentiment data.
14. Add portfolio-context adapter if needed.
15. Add deterministic normalization and conflict handling.
16. Add caching and rate limiting.
17. Add scheduled-cycle persistence.
18. Implement scheduled-cycle service.
19. Add experiment execution policies.
20. Add continuous evaluation lifecycle.
21. Add time-to-fill and turnover metrics.
22. Add confidence calibration.
23. Add promotion gates.
24. Add provider and pipeline health metrics.
25. Add configuration.
26. Add CLI commands.
27. Add offline tests.
28. Add opt-in provider smoke tests.
29. Run real smoke tests only when explicitly enabled.
30. Add the real scheduled-cycle smoke test.
31. Complete documentation.
32. Run the full main suite.
33. Run the paper-runtime suite.
34. Reread and finalize the scratchpad.
35. Self-review for secrets, look-ahead bias, provider leakage, duplication, execution leakage, and unsupported claims.

Keep implementation changes focused and testable.

Do not perform broad unrelated refactoring.

---

# Acceptance criteria

Milestone 6 is code-complete only when:

1. All Milestone 1–5 tests still pass.
2. All paper-runtime tests still pass.
3. Default tests require no network or credentials.
4. At least one real SEC provider exists.
5. At least one real market-data provider exists.
6. A real fundamentals path exists.
7. A real news path exists or is clearly marked environmentally pending.
8. Provider outputs normalize into existing evidence contracts.
9. Every real evidence item has provenance.
10. Point-in-time availability is recorded.
11. Future evidence is rejected.
12. Missing required evidence fails closed.
13. Provider conflicts remain visible.
14. Provider caching is deterministic.
15. Provider retries are bounded.
16. Rate-limit errors are explicit.
17. Scheduled cycles are idempotent.
18. Scheduled cycles are resumable.
19. Both experiment arms use identical deterministic inputs.
20. Enhanced shadow recommendations cannot execute.
21. Paper attribution is unambiguous.
22. No-action and incomplete results remain in evaluation.
23. Time-to-fill is implemented.
24. Turnover is implemented.
25. Confidence calibration handles insufficient samples safely.
26. Promotion gates are deterministic and versioned.
27. No model can promote itself.
28. No provider can mutate execution state.
29. No Claude code path reaches broker operations.
30. No secrets appear in persisted records or logs.
31. `real_orders` remains write-blocked.
32. The scratchpad accurately records implementation progress and final status.
33. Documentation clearly distinguishes:

    * fixture provider behavior;
    * recorded-provider fixtures;
    * real provider connectivity;
    * real data retrieval;
    * real Claude output;
    * shadow evaluation;
    * actual paper execution.
34. The final report does not claim real-provider validation unless the corresponding opt-in smoke test ran successfully.
35. The final report does not claim Claude improves performance without sufficient out-of-sample evidence.

Environmental validation may remain pending for providers whose credentials or billing are unavailable, but each pending item must be identified precisely.

---

# Required final response

At completion, provide:

1. Baseline verification.
2. Scratchpad path and final status.
3. Repository and gap-analysis findings.
4. Provider-selection decisions.
5. Provider contracts.
6. SEC implementation.
7. Market-data implementation.
8. Fundamentals implementation.
9. News implementation.
10. Sentiment integration.
11. Portfolio-context behavior.
12. Point-in-time protections.
13. Provider caching and rate limiting.
14. Persistence and migration changes.
15. Scheduled-cycle behavior.
16. Experiment execution policies.
17. Paper-attribution behavior.
18. Evaluation lifecycle.
19. Time-to-fill metrics.
20. Turnover metrics.
21. Confidence-calibration behavior.
22. Promotion and rollback gates.
23. Provider-health metrics.
24. Configuration changes.
25. CLI commands.
26. Files created.
27. Files modified.
28. Tests added.
29. Main-suite result.
30. Paper-runtime result.
31. SEC smoke-test result.
32. Market-data smoke-test result.
33. News smoke-test result.
34. Claude smoke-test result.
35. Real scheduled-cycle result.
36. Commands run.
37. Bugs discovered during real-provider validation.
38. Security and secret-handling review.
39. Known limitations.
40. Recommended Milestone 7.

Include a concise mapping:

```text
Requirement → implementation file → verifying test
```

Label implementation areas as:

```text
OFFLINE-DETERMINISTIC
RECORDED-PROVIDER-FIXTURE
REAL-SEC-DATA
REAL-MARKET-DATA
REAL-NEWS-DATA
REAL-CLAUDE-STRUCTURED-OUTPUT
SHADOW-EXPERIMENT
REAL-PAPER-EXECUTION
ENVIRONMENTALLY-PENDING
```

Do not claim a real provider worked unless an explicit real-provider smoke test completed successfully.

Do not claim continuous operation merely because a single cycle command exists. Clearly distinguish:

```text
IDEMPOTENT SCHEDULED-CYCLE IMPLEMENTATION
```

from:

```text
ACTUAL RECURRING DEPLOYMENT
```

Do not commit or push unless explicitly asked, following the project’s existing commit-only-when-asked convention.
