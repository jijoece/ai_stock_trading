Continue implementing the AI-driven stock research and paper-trading project using these documents as the authoritative requirements:

* `docs/AI-Driven-Stock-Trading-Architecture.md`
* `docs/AI-Stock-Trading-Implementation-Plan.md`
* `docs/AI-Stock-Trading-Research-Sources.md`

## Verified baseline from the completed foundation slice

The preceding implementation slice is complete, and 102 of 102 tests pass.

Treat the following as the current repository baseline:

* The research-pipeline table formerly named `recommendations` was renamed to `research_recommendations`.
* The trading system owns the `recommendations` table keyed by `rec_id`.
* A legacy shape-detection migration safely renames old research tables.
* Do not reintroduce the previous table-name collision.
* `database.connect()` applies both research and trading schemas.
* Frozen recommendation rows are protected against UPDATE and DELETE by SQLite triggers.
* The reserved `real_orders` table is protected against INSERT, UPDATE, and DELETE.
* Preserve these protections and add regression tests if this slice touches them.
* Trading recommendations already contain `config_hash` and `git_sha`.
* The recommendation JSON schema already enforces:

  * `ANALYSIS_INCOMPLETE` requires missing-data reasons.
  * `ANALYSIS_INCOMPLETE` cannot contain a risk plan.
  * `NO_ACTION` cannot contain a risk plan.
* Use the existing schema rather than creating an incompatible replacement.
* The verified ticker universe provides:

  * `normalize_symbol()`
  * `TickerUniverse.require()`
  * `UnknownSymbolError`
  * active/inactive and source metadata
* The ticker extractor returns:

  * start and end spans
  * deterministic confidence category
  * ambiguity status
  * contextual-confirmation status
  * rejection reason
* Company-name token matching intentionally excludes the ticker symbol itself to prevent ambiguous symbols such as `ON` from self-confirming.
* Preserve this behavior.
* Existing recommendation fixtures include valid and invalid cases. Extend them rather than replacing them.
* Run the existing 102-test suite before making changes and again after implementation.
* Any regression in the foundation behavior must be fixed before completing this slice.

This run implements only:

* 1B.2 — deterministic sentiment aggregation
* 1C.1 — screener
* 1C.2 — composite scorer
* 1C.3 — deterministic risk engine
* 1C.4 — frozen recommendation builder and offline candidate-analysis service

Do not proceed into simulated fills, paper-order execution, evaluator implementation, live market-data retrieval, or broker integration.

This is the second implementation slice of Milestone 1.

Run the task synchronously from beginning to end.

Do not stop to ask me questions, request design approval, or request permission between steps. Inspect the repository, resolve minor ambiguities using the architecture documents, make the safest reasonable decision, implement the work, run all relevant tests, fix failures caused by the implementation, review the final diff, and provide one final completion report.

Do not place, preview, prepare, stage, or submit any real broker order.

Do not invoke any Robinhood write tool.

Do not invoke any Reddit write tool.

Do not add live-trading execution code.

Do not connect the implementation to a real Robinhood account.

Do not require live Reddit, Robinhood, market-data, news, SEC, or Claude API access for tests.

## Objective

Implement the deterministic analysis and recommendation layer for Milestone 1:

* Story 1B.2 — Deterministic sentiment aggregation interface
* Story 1C.1 — Stock screener
* Story 1C.2 — Composite scorer
* Story 1C.3 — Risk engine
* Story 1C.4 — Frozen recommendation builder

The completed slice must take normalized fixture data and produce a fully traceable, schema-valid recommendation or a fail-closed result such as:

* `ANALYSIS_INCOMPLETE`
* `NO_ACTION`

The LLM must not perform calculations, choose score weights, determine position size, set stops, set targets, or override risk controls.

## Assumptions from the previous implementation slice

The repository should already contain implementations or equivalents for:

* Trading database schema
* Verified ticker universe
* Recommendation JSON schema
* Deterministic ticker mention parser
* Relevant unit-test fixtures

Do not blindly assume these implementations are correct.

Inspect them and integrate with their actual interfaces.

If a small compatibility correction is required to complete this slice:

1. Make the smallest safe correction.
2. Add or update tests.
3. Document the correction in the final report.
4. Do not redesign unrelated components.

## Initial repository inspection

Before changing files:

1. Read:

   * `CLAUDE.md`
   * `SKILL.md`
   * `.claude/settings.json`
   * `.claude/settings.local.json`
   * `.mcp.json`
   * `pyproject.toml`
   * All three architecture and implementation documents
2. Inspect:

   * `src/trading_research/`
   * `tests/`
   * `config/`
   * `schemas/`
   * `scripts/`
   * Existing database repositories
   * Existing exception and domain-model conventions
3. Review the output of the previous implementation slice.
4. Run the existing test suite before making changes.
5. Check Git status and current diff.
6. Preserve unrelated work.
7. Do not overwrite user-created files.
8. Reuse existing types, repositories, configuration mechanisms, and logging conventions where practical.
9. Prefer pure functions and immutable domain objects for financial calculations.
10. Avoid adding unnecessary dependencies or frameworks.

If the pre-existing tests fail before your changes, record those failures, continue with the requested implementation where possible, and clearly distinguish pre-existing failures from failures introduced by this work.

## Required implementation

### 1. Typed domain models

Before implementing business logic, establish or reuse typed models for the relevant inputs and outputs.

Use dataclasses, TypedDicts, Pydantic, or the repository’s existing model standard.

Models should cover where appropriate:

* Security snapshot
* Market-data snapshot
* Fundamental snapshot
* Technical-factor input
* Catalyst and risk flags
* Reddit sentiment aggregates
* Screening decision
* Screening failure
* Factor score
* Pillar score
* Composite score
* Portfolio state
* Risk request
* Risk result
* Recommendation draft
* Frozen recommendation
* Incomplete-state reason
* Warning
* Data-freshness metadata

Requirements:

* Avoid passing large unvalidated dictionaries between modules.
* Validate numeric ranges.
* Use `Decimal` for monetary calculations where practical.
* Use timezone-aware UTC timestamps.
* Make unknown and missing states explicit.
* Do not convert unknown values to zero.
* Do not treat `None` as false when the distinction affects a financial decision.

### 2. Screener

Implement or complete:

`src/trading_research/analysis/screener.py`

Create or update:

`config/screening.yaml`

The screener applies hard eligibility gates. It does not rank candidates and does not make trade recommendations.

Support configurable checks for:

* Maximum share price, initially `$25`
* Minimum market capitalization
* Minimum average daily dollar volume
* Minimum operating or listing history
* OTC exclusion
* Inactive or delisted security exclusion
* Bankruptcy or severe-distress flag
* Going-concern warning
* Shell-company flag where data is available
* Recent reverse split
* Severe dilution
* Excessive shares-outstanding growth
* Low cash runway
* Upcoming earnings restriction
* Abnormal volatility
* Recent trading halt
* Excessive bid/ask spread
* Missing or stale critical data

Each screening result must include:

* Symbol
* Passed or failed
* Individual gate results
* Threshold used
* Observed value
* Reason
* Data timestamp
* Configuration version or hash
* Whether the condition was a hard failure or warning

Rules:

* Hard-gate failure excludes the stock.
* Unknown critical input must fail closed.
* Unknown values must not be replaced with favorable defaults.
* A stock cannot pass merely because data is unavailable.
* Screening thresholds must come from version-controlled configuration.
* The LLM cannot modify thresholds at runtime.
* Gate evaluation order must not affect the final result.
* Preserve all gate outcomes for auditability, not only the first failure.

Examples requiring tests:

* Price above limit
* Insufficient market cap
* Insufficient dollar volume
* OTC stock
* Going-concern warning
* Recent reverse split
* High dilution
* Insufficient cash runway
* Earnings inside restricted window
* Wide spread
* Stale market price
* Missing market cap
* Candidate passing all configured gates

### 3. Deterministic Reddit sentiment aggregation

Implement or complete:

`src/trading_research/analysis/sentiment.py`

This module aggregates already-stored and already-classified records.

It must not retrieve Reddit data.

It must not call an LLM.

It must not infer sentiment directly from raw text unless the repository already contains a deterministic classifier specifically intended for fixture testing.

The production classification interface should remain pluggable so that a bounded Claude API classifier can be added in Milestone 2.

Support deterministic calculation of:

* Unique post count
* Unique comment count
* Total mention count
* Unique author count
* Mentions by subreddit
* Mentions by configured time window
* Mention velocity
* Growth compared with prior equivalent windows
* Engagement-weighted mention count
* Bullish count
* Bearish count
* Neutral count
* Weighted sentiment score
* Sentiment-confidence distribution
* Duplicate-post count
* Cross-post count
* Repeated-link count
* Potential promotion flags
* Ambiguous ticker count
* Context-confirmed ticker count
* Cashtag mention count
* New-account concentration where metadata is available
* Price-before-discussion versus discussion-before-price indicators where suitable timestamped price data is supplied

Requirements:

* Counts and rates must be calculated by Python.
* Window boundaries must be explicit and timezone-aware.
* Duplicate records must not inflate counts.
* Deleted or missing authors must be handled explicitly.
* Missing metadata must not produce invented values.
* Empty input must return a valid zero-data aggregate with low or unavailable confidence, not a bullish or bearish conclusion.
* The aggregation output must identify its observation window and record count.
* The interface must support replacing mock classifications with Claude-generated schema-valid classifications later.
* Reddit-derived metrics must remain supplementary and must not bypass the screener.

Use fixture classifications such as:

* `bullish`
* `bearish`
* `neutral`
* Optional confidence value
* Optional catalyst phrases
* Optional risk phrases

Do not add a live Claude API call in this implementation slice.

### 4. Composite scorer

Implement or complete:

`src/trading_research/analysis/scorer.py`

Create or update a version-controlled scoring configuration, such as:

`config/scoring.yaml`

The scorer must produce an explainable score from deterministic factor inputs.

Initial pillar structure:

* Fundamentals: approximately 35%
* Technicals and momentum: approximately 30%
* Catalysts and verified risks: approximately 25%
* Reddit sentiment: no more than 10%

The exact configured weights may follow existing repository decisions, but:

* Total weight must equal 100%.
* Reddit weight must be programmatically capped at 10%.
* Invalid configurations must fail validation during startup or tests.
* The scorer must not silently renormalize a configuration that exceeds the Reddit cap.
* Missing critical pillars must result in `ANALYSIS_INCOMPLETE`.
* Optional-factor absence must follow an explicit documented policy.
* Do not silently award neutral or favorable scores for unavailable data.

Store or return for every factor:

* Factor name
* Raw value
* Normalized value
* Weight
* Contribution
* Source timestamp
* Data-quality status
* Explanation generated from deterministic templates where useful

Store or return for every pillar:

* Pillar name
* Pillar score
* Pillar weight
* Weighted contribution
* Available factor count
* Missing factor count

Composite output must include:

* Symbol
* Total score
* Pillar breakdown
* Factor breakdown
* Scoring configuration version or hash
* Data timestamp
* Warnings
* Incomplete-state reasons
* Whether Reddit materially changed rank or score

The score must be reconstructible exactly from stored factors.

Tests must verify this reconstruction.

Do not let a natural-language rationale alter the calculated score.

### 5. Deterministic risk engine

Implement or complete:

`src/trading_research/risk/position_sizing.py`

Add additional focused modules under `risk/` only where separation improves testability.

The risk engine must calculate or validate:

* Maximum dollars at risk per trade
* Entry price
* Stop price
* Risk per share
* Maximum share quantity by risk
* Maximum share quantity by settled cash
* Maximum share quantity by position-size cap
* Maximum share quantity by liquidity cap
* Final permitted share quantity
* Total position value
* Total dollars at risk
* Target price
* Reward-to-risk ratio
* Existing position exposure
* Total portfolio exposure
* Sector concentration
* Correlated-exposure or sector-bucket limit
* Maximum daily loss restriction
* Maximum portfolio drawdown restriction
* Earnings-event restriction
* Bid/ask spread restriction
* Data-freshness checks
* Current open-position or duplicate-entry restriction where relevant

The risk engine must use explicit, version-controlled configuration for:

* Maximum risk per trade
* Maximum position fraction
* Minimum reward-to-risk ratio
* Sector exposure limit
* Liquidity participation limit
* Earnings blackout window
* Maximum acceptable spread
* Maximum daily loss
* Maximum drawdown
* Data-staleness thresholds
* ATR or stop-distance rules

Requirements:

* Use `Decimal` for money-sensitive calculations where practical.
* Round share quantity down.
* Never round up into additional risk.
* Never return a negative quantity.
* Never divide by zero.
* Reject stop price equal to or above entry for a long entry.
* Reject nonsensical target or reward-to-risk combinations.
* Unknown position state must fail closed.
* Unknown settled cash must fail closed.
* Unknown current price must fail closed.
* Unknown earnings date must follow the configured fail-closed policy.
* Stale price or portfolio data must fail closed.
* Breached daily-loss or drawdown limit must produce `NO_ACTION`.
* If all calculated quantity caps result in zero shares, return `NO_ACTION`, not an executable recommendation.
* The LLM cannot supply, modify, or override the final position size, stop, target, or risk result.

Create or reuse a clear exception or result type such as:

* `IncompleteStateError`
* `RiskRejected`
* `NoActionReason`

Do not use generic `ValueError` for every domain failure.

### 6. Recommendation builder

Implement or complete the recommendation-building layer.

Use a suitable location such as:

`src/trading_research/recommendations/builder.py`

or the project’s existing convention.

The builder combines:

* Security identity
* Screening result
* Composite score
* Risk result
* Data-freshness information
* Model version
* Prompt version
* Configuration hashes
* Git SHA
* Warnings
* Missing-data reasons

It must emit a record conforming to:

`schemas/recommendation.schema.json`

Supported statuses should include at minimum:

* `ACTIVE`
* `NO_ACTION`
* `ANALYSIS_INCOMPLETE`
* `SCREENED_OUT`
* `EXPIRED`

The exact names may follow the existing schema, but they must be consistent across code, schema, database, CLI, and tests.

Rules:

* Failed screening must never produce order parameters.
* Incomplete analysis must never produce an executable quantity.
* `NO_ACTION` must not contain executable order instructions.
* Active paper recommendations may contain deterministic paper-trade parameters.
* Broker account identifiers must never be included.
* Every recommendation must include data timestamps and configuration identifiers.
* The record must be immutable after freezing.
* The builder must write a recommendation once and must not expose an update method for historical rationale, score, stop, target, or quantity.
* Re-running the same recommendation creation with the same idempotency key must not create conflicting duplicates.
* Not-acted-upon recommendations must still be persisted.
* The recommendation must identify whether it is intended only for paper trading.

Implement validation both:

1. Before persistence using typed-domain checks.
2. Against the JSON schema.

### 7. Persistence and repository integration

Integrate the new modules with the schema and repositories created in the previous slice.

Persist where appropriate:

* Screening run
* Individual gate results or serialized gate evidence
* Candidate score
* Recommendation factors
* Frozen recommendation
* Model version
* Prompt version
* Configuration hashes
* Data-quality and incomplete-state errors

Requirements:

* Avoid duplicate recommendations.
* Preserve immutability.
* Preserve reconstructibility.
* Use transactions where multiple related rows must succeed together.
* A failed write must not leave a partially persisted recommendation.
* Do not add a repository method capable of writing real orders.
* If `real_orders` has a generic repository path by accident, remove or restrict that path and add a regression test.

### 8. End-to-end offline service

Add a small application service or orchestrator for this slice, using a suitable location such as:

`src/trading_research/services/analyze_candidate.py`

It should accept fixture or typed input for one candidate and execute:

1. Validate symbol.
2. Run screener.
3. Aggregate pre-classified Reddit records.
4. Compute deterministic score.
5. Run risk engine if eligible.
6. Build and validate recommendation.
7. Persist frozen recommendation and factor records.
8. Return structured output.

This is not an autonomous agent.

It is a deterministic application service.

It must not:

* Retrieve live data
* Call Reddit
* Call Robinhood
* Call Claude
* Place a paper fill
* Place a real order

Paper-order simulation belongs to the next implementation slice.

## Configuration requirements

Add or update version-controlled configuration for:

* Screening thresholds
* Scoring weights
* Risk limits
* Data-freshness thresholds
* Reddit sentiment cap

Requirements:

* Configuration loading must be typed and validated.
* Invalid configuration must fail early.
* Configuration hashes must be reproducible.
* Environment variables may override non-security operational values only where the repository already supports that pattern.
* Risk limits must not be overridable by Reddit content, model output, tool output, or runtime natural language.
* Use conservative defaults.
* Document every default.

## Testing requirements

Add comprehensive pytest coverage.

### Screener tests

Test each hard gate independently:

* Price cap
* Market-cap floor
* Dollar-volume floor
* OTC exclusion
* Inactive security
* Going-concern flag
* Bankruptcy or distress
* Reverse-split restriction
* Dilution restriction
* Cash-runway restriction
* Earnings blackout
* Volatility restriction
* Halt restriction
* Wide-spread restriction
* Missing input
* Stale input
* Fully passing candidate

Verify all gate outcomes are preserved.

### Sentiment aggregation tests

Test:

* Empty input
* Duplicate posts
* Duplicate comments
* Cross-posts
* Repeated links
* Unique-author counts
* Missing authors
* Subreddit distribution
* Multiple time windows
* Window-boundary behavior
* Mention growth
* Engagement weighting
* Bullish/bearish/neutral aggregation
* Ambiguous versus confirmed mentions
* Cashtag counts
* Promotion-risk indicators
* Deterministic output order
* Reproducibility from identical fixtures

### Scorer tests

Test:

* Weight total validation
* Reddit cap enforcement
* Exact factor-contribution reconstruction
* Missing critical pillar
* Missing optional factor
* Score boundaries
* Negative factors
* Configuration hashing
* Reddit component disabled
* Reddit component at maximum cap
* Same inputs producing identical output

### Risk-engine tests

Test at minimum:

* Normal valid position-sizing case
* Zero risk per share
* Stop above entry
* Stop equal to entry
* Insufficient settled cash
* Unknown settled cash
* Unknown current position
* Stale account state
* Stale price
* Earnings restriction
* Sector concentration breach
* Portfolio exposure breach
* Liquidity cap
* Position-size cap
* Maximum daily-loss breach
* Maximum drawdown breach
* Minimum reward-to-risk failure
* Quantity rounded down
* Quantity reduced by each independent cap
* Quantity reduced to zero
* Property or invariant:

  * final quantity × risk per share must never exceed maximum risk dollars
  * final position value must never exceed configured position cap
  * final quantity must never be negative

Use property-based testing only if the repository already uses it or adding the dependency is clearly justified. Otherwise implement deterministic parameterized tests.

### Recommendation-builder tests

Test:

* Passing active paper recommendation
* Screened-out recommendation
* `ANALYSIS_INCOMPLETE`
* `NO_ACTION`
* Missing required timestamp
* Missing configuration hash
* Forbidden executable fields on incomplete status
* JSON schema validation
* Immutability
* Duplicate idempotency key
* Transaction rollback on persistence failure
* No real-order write path

### Offline integration test

Add at least one full offline integration test:

* Load fixture ticker universe.
* Load fixture security, fundamental, market, catalyst, Reddit-classification, and portfolio data.
* Run the candidate-analysis application service.
* Persist results to a temporary SQLite database.
* Validate the returned recommendation against the JSON schema.
* Reconstruct the score from stored factors.
* Confirm no network call occurred.
* Confirm no broker tool was called.
* Confirm no paper fill was created.
* Confirm no real order was created.

Add a second integration case for incomplete or stale input and verify it produces `ANALYSIS_INCOMPLETE`.

## Security requirements

* No credentials in source, fixtures, logs, test output, or documentation.
* No account numbers in persisted recommendation records.
* No Robinhood write tools.
* No Reddit write tools.
* No network calls in tests.
* No live broker adapter invocation.
* No real-order implementation.
* No dynamic execution of external text.
* No use of `eval`, `exec`, or unsafe deserialization.
* Treat Reddit and news text as untrusted data.
* Do not pass raw external text into scoring or risk calculations.
* Unknown tool names remain denied by the existing tool policy.
* Unknown financial state must fail closed.
* Do not weaken `.claude` permission restrictions.
* Do not change MCP configuration unless necessary to fix a clear safety defect.
* Do not add plaintext secrets to `.claude/settings.local.json`, `.mcp.json`, or `.env.example`.

## Documentation

Update relevant developer documentation to explain:

* Screening gates and configuration
* Scoring pillars and factor reconstruction
* Reddit sentiment cap
* Sentiment aggregation boundaries
* Risk-engine inputs and outputs
* Fail-closed behavior
* Recommendation statuses
* Recommendation freezing and immutability
* How to run the tests
* How to execute the offline single-candidate analysis service
* That the system remains paper-research-only
* That paper fills are not yet implemented
* That live orders remain prohibited

Do not rewrite the architecture documents unless correcting a demonstrated inconsistency.

Record any such correction in the final summary.

## Validation commands

Run all relevant project checks, including where configured:

* Formatter
* Linter
* Type checker
* Unit tests for the new modules
* Offline integration tests
* Full existing test suite
* JSON Schema validation
* Security or secret scan if available

Fix failures introduced by this implementation.

Do not:

* Disable checks
* Lower coverage thresholds
* Mark failing tests as skipped merely to make the run pass
* Delete valid existing tests
* Replace assertions with weaker assertions
* Hide failing command output

## Final code review

Before finishing:

1. Review `git diff`.
2. Check for accidental credential exposure.
3. Search for calls to Robinhood write tools.
4. Search for calls to Reddit write tools.
5. Search for `place_order`, `submit_order`, `execute_order`, and similar names.
6. Confirm no network call exists in the new test path.
7. Confirm the Reddit score cannot exceed 10%.
8. Confirm incomplete recommendations cannot contain executable quantity or order fields.
9. Confirm no repository writes to `real_orders`.
10. Confirm score reconstruction works from persisted factors.
11. Confirm all monetary and risk invariants are tested.
12. Confirm unrelated files were not modified.

## Completion requirements

Do not stop after scaffolding or partial implementation.

Continue until:

1. Screener is implemented and tested.
2. Sentiment aggregation is implemented and tested.
3. Composite scorer is implemented and tested.
4. Risk engine is implemented and tested.
5. Recommendation builder is implemented and tested.
6. Configuration is typed and validated.
7. Persistence is transactional and immutable.
8. Offline candidate-analysis service works.
9. JSON Schema validation passes.
10. Full relevant test suite passes, except clearly documented pre-existing failures.
11. Documentation is updated.
12. No live-trading or paper-fill execution path was added.
13. Final Git diff has been reviewed.

## Final response

At completion, provide one concise but complete report containing:

* Summary of implemented functionality
* Files created
* Files modified
* Configuration introduced or changed
* Tests executed
* Test results
* Coverage result, if available
* Important design decisions
* Fail-closed behaviors implemented
* Security validation performed
* Any compatibility corrections made to the previous slice
* Pre-existing failures, clearly separated from new failures
* Remaining gaps
* Exact recommended next implementation slice:

  * Paper ledger
  * Evaluator
  * Mock adapters
  * CLI and full offline pipeline

Do not ask me what to do next.

Do not proceed into the paper-ledger implementation during this run.

Complete only this implementation slice and report the result.
