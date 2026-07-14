# Milestone 7 — Pending Work and Deferred Follow-Ups

**Status:** Open backlog
**Last updated:** 2026-07-13
**Applies to:** Milestones 7, 7.1, and 7.2
**Purpose:** Record known limitations, deferred integrations, activation blockers, and future improvements without reopening completed milestone work.

---

## 1. Current state

Milestone 7 introduced the production-shadow control layer and evidence-completeness architecture.

Milestone 7.1 connected the previously standalone components into the scheduled-cycle runtime, including:

* corporate-status evidence;
* completeness gating;
* provider/model-aware pricing;
* pre-attempt role-budget enforcement;
* actual token and cost settlement;
* cycle telemetry;
* fixture and real scheduler modes.

Milestone 7.2 added:

* field-level health diagnostics;
* persisted health checks;
* health-explanation CLI support;
* pause and alert diagnostics;
* activation-readiness evaluation;
* a corrected retry-exhaustion denominator;
* emergency-margin breach wiring.

The latest recorded test results are:

```text
Main suite:        1266 passed, 14 skipped
Paper runtime:     33 passed
Regressions:       0
```

Recurring shadow deployment has **not** been activated. Current activation readiness is:

```text
NOT_READY_INSUFFICIENT_HISTORY
```

## The persistent development database contains no sustained production-like shadow history because most validation cycles used temporary databases.

# 2. Priority definitions

## P0 — Required before recurring shadow activation

These items must be resolved, intentionally accepted, or explicitly configured as not applicable before enabling an unattended recurring schedule.

## P1 — Important operational and evidence improvements

These are not necessarily immediate activation blockers, but they materially improve reliability, evidence quality, diagnostics, and evaluation confidence.

## P2 — Explicitly deferred enhancements

These are valid future improvements but should not be mixed into the immediate activation-readiness work.

---

# 3. P0 — Activation blockers

## 3.1 Correct `unsupported_claim_rate` semantics

### Current behavior

The current numerator counts individual unsupported-claim failure rows, while the denominator counts attempts.

One attempt may produce several rejected-claim rows, including non-material rejected claims that do not invalidate the overall report. Therefore:

```text
unsupported_claim_count / attempt_count
```

can exceed `1.0` and may overstate the percentage of affected attempts.

Milestone 7.2 identified this as a latent issue, but it was not the cause of the subsequent real health pause and was therefore left unchanged.

### Required decision

Choose and document one semantic definition.

Recommended minimum-compatible definition:

```text
attempts containing at least one unsupported claim
--------------------------------------------------
total attempts evaluated for claim support
```

Alternative:

```text
unsupported claims
------------------
total validated claims
```

The first option is easier to integrate with the existing attempt-oriented health model.

### Required work

* Count distinct affected `attempt_id` values instead of raw failure rows.
* Verify whether low-importance rejected claims should count toward the health metric.
* Keep material and non-material unsupported claims separately observable.
* Ensure the final rate is bounded to `[0,1]`.
* Add tests where one attempt produces multiple rejected claims.
* Add tests where an attempt succeeds overall but contains non-material rejected claims.
* Update the health-check explanation to show numerator and denominator.

### Completion criteria

* The rate has a documented semantic meaning.
* The numerator and denominator use compatible units.
* The value cannot exceed `1.0`.
* Existing claim-to-evidence validation is not weakened.
* Health-policy thresholds are reviewed only after the corrected metric is available.

---

## 3.2 Derive paper-reconciliation health from real records

### Current behavior

`paper_reconciliation_mismatch` is currently hardcoded to `False` in the scheduler health-input construction.

It is not derived from actual paper-order, fill, ledger, or reconciliation records.

### Required behavior

When no paper activity occurred:

```text
paper_reconciliation_mismatch = NOT_APPLICABLE
```

When paper activity occurred:

```text
paper_reconciliation_mismatch =
    authoritative reconciliation result
```

### Required work

* Identify the authoritative paper reconciliation repository or service.
* Associate reconciliation results with the scheduler run and cycle.
* Distinguish:

  * no paper activity;
  * reconciliation not yet performed;
  * reconciliation successful;
  * reconciliation mismatch;
  * reconciliation provider unavailable.
* Preserve `None` or an explicit applicability state rather than fabricating `False`.
* Persist the reconciliation source and timestamp.
* Add tests for zero-order cycles, successful reconciliation, missing reconciliation, and mismatch.

### Completion criteria

* A cycle with no paper order is not represented as having passed reconciliation.
* A real mismatch produces the configured health pause.
* An unavailable reconciliation result cannot silently become success.
* Enhanced-arm activity remains non-executable.

---

## 3.3 Derive duplicate-prevention violations from actual events

### Current behavior

The response to:

```text
duplicate_prevention_violation = True
```

is now safely configured to require a pause independently of reconciliation settings.

However, the scheduler still hardcodes the input to `False`; it is not populated from actual lease, idempotency, duplicate-cycle, or duplicate-submission detection.

### Required signals

Potential authoritative sources include:

* concurrent lease-acquisition conflicts;
* duplicate intended-schedule IDs;
* repeated completed-cycle execution;
* conflicting stale-lease recovery;
* duplicate cycle persistence;
* duplicate paper-submission intent;
* idempotency-key collisions with differing payloads.

Not every normal duplicate invocation is a violation. For example, a correctly rejected repeat invocation is evidence that duplicate prevention worked.

### Required work

Define the difference between:

```text
DUPLICATE_ATTEMPT_PREVENTED
```

and:

```text
DUPLICATE_PREVENTION_VIOLATION
```

Then:

* persist actual violation events;
* associate them with scheduler and cycle IDs;
* populate the health input from those events;
* ensure expected idempotent no-ops do not trigger a pause;
* trigger a critical alert for genuine violations;
* add concurrency and resume tests.

### Completion criteria

* The health input is authoritative rather than hardcoded.
* A prevented duplicate remains healthy or informational.
* A duplicate that bypasses a safety control produces `PAUSE_REQUIRED`.
* Duplicate alerts deduplicate without hiding distinct violations.

---

## 3.4 Correct real-smoke cycle-duration measurement

### Current behavior

The Milestone 7.2 real smoke test used a frozen test clock. This produced:

```text
cycle_duration_seconds = 0.0
```

even though the real Claude call alone took roughly 30 seconds.

This is a test-harness artifact, not a production scheduler defect.

### Required work

Use either:

* an advancing deterministic test clock; or
* a separately injected monotonic duration source.

Keep:

```text
attempt latency
```

and:

```text
whole cycle duration
```

as separate measurements.

### Completion criteria

* A real smoke run records a nonzero cycle duration.
* The duration includes SEC/provider and Claude activity.
* Milliseconds and seconds are not conflated.
* Offline tests remain deterministic.
* Duration health thresholds can be validated meaningfully.

---

## 3.5 Clarify terminal role-failure terminology

### Current behavior

`CODE_RETRY_EXHAUSTED` is recorded whenever a role’s final allowed attempt fails.

When:

```text
max_attempts_per_role = 1
```

the first and only failure is called “retry exhausted,” even though no retry was possible.

The current behavior is fail-safe, but the terminology is misleading.

### Recommended direction

Introduce or migrate toward a broader terminal status such as:

```text
ROLE_ATTEMPTS_EXHAUSTED
```

or:

```text
ROLE_TERMINAL_FAILURE
```

Then optionally distinguish:

```text
INITIAL_ATTEMPT_FAILED_NO_RETRY_ALLOWED
RETRIES_EXHAUSTED
```

### Required work

* Review all consumers of `CODE_RETRY_EXHAUSTED`.
* Preserve backward compatibility in persisted diagnostics.
* Avoid changing historical rows.
* Update health metric naming if appropriate.
* Keep terminal required-role failures fail-safe.

### Completion criteria

* Diagnostic wording accurately reflects one-attempt and multi-attempt configurations.
* Historical metrics remain interpretable.
* No required-role failure becomes less visible.

---

## 3.6 Accumulate persistent shadow history

### Current state

Activation-readiness floors remain:

```text
minimum completed cycles:      10
minimum real-provider cycles:   5
```

The persistent development database currently has insufficient completed history because prior validations used temporary databases.

### Required work

Run controlled manual shadow cycles into a designated persistent evaluation database.

The history should include:

* multiple market days;
* different ticker profiles;
* successful cycles;
* expected evidence blocks;
* provider degradation;
* at least one controlled retry;
* no paper submissions unless specifically testing the existing baseline-paper path;
* no enhanced execution.

For each cycle retain:

* provider identities;
* evidence completeness;
* role outcomes;
* attempt usage;
* cost;
* duration;
* health checks;
* alerts;
* pause state;
* readiness aggregates.

### Completion criteria

* At least 10 completed cycles exist in the designated evaluation store.
* At least 5 cycles use real required providers.
* No unexplained `PAUSE_REQUIRED` remains.
* Cost settlement reconciles for all real Claude cycles.
* Health checks are persisted for every cycle.
* The readiness report can be reproduced from persisted data.

---

## 3.7 Correct the historical Milestone 7.1 pause documentation

### Current state

The original Milestone 7.1 run reportedly had:

```text
attempts: 2
roles: bear + manager
both role-budget checks: PROCEED
```

Its `PAUSE_REQUIRED` reasons were not persisted.

The Milestone 7.2 reruns had a different execution shape:

```text
attempts: 1
bear: terminal failure
manager: not invoked
retry_exhaustion_rate: 1.0
```

The later run explains its own pause but cannot prove the cause of the original Milestone 7.1 pause.

The original temporary database is no longer available.

### Required documentation correction

Use this wording consistently:

> The original Milestone 7.1 health result cannot be reconstructed because its temporary database and field-level diagnostics are unavailable. Milestone 7.2 captured and explained a subsequent real `PAUSE_REQUIRED` result caused by a terminal bear-role failure.

### Completion criteria

* The original and later runs are not conflated.
* The original cause is labeled historically unavailable.
* The later pause remains labeled expected policy behavior.
* No documentation claims unavailable evidence.

---

# 4. P1 — Evidence and provider follow-ups

## 4.1 Validate real Alpaca market data

### Current state

The real Alpaca market-data provider is code-complete, but the environment used for the milestone did not contain:

```text
ALPACA_MARKET_DATA_API_KEY
ALPACA_MARKET_DATA_API_SECRET
```

Only separate Alpaca credentials were present, and the market-data provider remained disabled by default.

### Required work

* Confirm the correct Alpaca entitlement and credential names.
* Run one bounded real historical market-data request.
* Verify point-in-time filtering.
* Verify bar availability timestamps.
* Verify no current quote leaks into a historical snapshot.
* Validate request persistence, caching, and rate limiting.
* Add an opt-in real smoke test.

### Known sharp edge

A previously documented behavior marks a source unsafe when the selected same-day bar has an availability timestamp later than the cycle’s `as_of` value.

This needs explicit test coverage and policy confirmation.

---

## 4.2 Validate real Alpaca News

### Current state

The Alpaca News adapter is implemented and offline-tested but has not completed a real HTTP validation because the market-data credential pair was unavailable.

### Required work

* Validate authentication and entitlement.
* Confirm real response shapes.
* Confirm pagination behavior.
* Confirm symbol filtering.
* Confirm article timestamps and point-in-time cutoff.
* Confirm duplicate-group behavior on syndicated articles.
* Confirm bounded summary normalization.
* Confirm retention classification.
* Add an opt-in real smoke test.

### Completion criteria

* At least one real response is normalized and persisted.
* No future article enters a historical snapshot.
* Syndicated copies do not count as independent confirmation.
* Raw untrusted news content is not exposed as instructions to Claude.

---

## 4.3 Validate real Reddit sentiment and MCP response shape

### Current state

The Reddit path is code-complete but environmentally pending because:

```text
REDDIT_CLIENT_ID
REDDIT_CLIENT_SECRET
```

were unavailable.

The response normalizer currently supports several plausible MCP result shapes, but none was verified against an actual server response.

### Required work

* Register or configure the permitted Reddit application.
* Run the read-only MCP search path.
* Capture only sanitized response structure.
* Verify:

  * post and comment IDs;
  * timestamps;
  * text fields;
  * cross-post fields;
  * pagination;
  * rate-limit behavior.
* Replace speculative response-shape handling with verified handling where possible.
* Preserve fail-closed behavior for unknown shapes.
* Confirm that no mutation tools are available to Claude or application research roles.

### Completion criteria

* One real read-only MCP response is validated.
* Cashtag disambiguation works on real data.
* Historical cutoff is enforced.
* Cross-posts and duplicates are handled correctly.
* Mutation remains structurally unavailable.

---

## 4.4 Integrate corporate-action evidence

### Current state

The following Alpaca corporate-action types are implemented:

```text
forward_split
reverse_split
cash_dividend
```

The corporate-actions client remains standalone and is not connected to the evidence registry or primary evidence snapshot.

### Required work

* Add a typed evidence-provider boundary.
* Normalize supported corporate actions into the evidence snapshot.
* Include effective date, ex-date, payable date, ratios, and source provenance where applicable.
* Preserve separation between:

  * adjusted price bars;
  * actual corporate-action events.
* Add point-in-time filtering.
* Add fixture and real-provider tests.
* Determine whether action evidence affects screening, research only, or evaluation.

### Completion criteria

* Supported actions appear in the canonical snapshot.
* Splits are not inferred from price movements.
* Historical snapshots exclude later-announced actions.
* Claude receives bounded, structured facts only.

---

## 4.5 Harden corporate disclosure extraction

### Current state

A real SEC validation found and fixed a shell-company false positive caused by standard SEC cover-page checkbox wording.

The current fix uses a bounded context heuristic of approximately 200 preceding characters.

### Future work

* Test additional 10-K and 10-Q cover-page layouts.
* Include small-cap, shell-company, SPAC, foreign issuer, and amended filing examples.
* Consider structured cover-page parsing where available.
* Track extraction precision and false-positive cases by rule version.
* Add a corpus of sanitized filing fixtures.
* Keep extraction deterministic and non-LLM-based unless a future ADR explicitly changes that boundary.

---

## 4.6 Resolve actual operating-history evidence

### Current state

The implemented value is a public-reporting-history proxy based on the earliest reliable SEC filing date.

It is intentionally not wired into:

```text
CandidateInput.operating_history_years
```

because SEC reporting history is not equivalent to company age or actual operating history.

### Required decision

Either:

1. source a semantically correct company operating-history value; or
2. introduce a separately named screening factor for public-reporting history.

### Do not

* rename SEC reporting history as company age;
* use an IPO date as operating history without qualification;
* weaken the screener merely to populate the field.

---

# 5. P1 — Operational hardening

## 5.1 Implement destructive retention safely

### Current state

Retention planning and dry-run behavior exist, but:

```text
apply_retention(dry_run=False)
```

still raises `NotImplementedError`.

This was an intentional Milestone 7 boundary.

### Required work

Before enabling deletion:

* classify all tables;
* define audit-preservation requirements;
* define minimum retention periods;
* define backup prerequisites;
* test foreign-key and reference safety;
* add row-count and byte-estimate previews;
* require explicit operator confirmation;
* log an immutable retention action;
* support bounded batches;
* test interrupted deletion and restart behavior.

### Completion criteria

* No audit-relevant row is deleted unexpectedly.
* Dry-run and actual selection use identical filters.
* Backups are verified before deletion.
* Destructive execution is disabled by default.

---

## 5.2 Complete retention classification

The existing retention inventory does not cover the full repository schema.

Review:

* research attempts and failures;
* provider requests;
* source records;
* snapshots;
* corporate-status results;
* completeness results;
* role-budget checks;
* budget usage;
* alerts and deliveries;
* health checks;
* scheduler runs;
* operator actions;
* paper reconciliation;
* evaluation results.

Explicitly classify each as:

```text
PERMANENT_AUDIT
LONG_TERM
MEDIUM_TERM
SHORT_TERM_CACHE
REGENERABLE
SECRET_PROHIBITED
```

---

## 5.3 Add a real outbound alert sink

### Current state

Milestone 7 provides:

* persistence-only alerts;
* structured log alerts.

No webhook, email, Slack, or notification sink was added because no authorized target existed.

### Future work

After selecting an authorized destination:

* implement one bounded sink;
* store destination configuration outside source control;
* redact secrets;
* use bounded retries;
* use alert deduplication;
* persist delivery outcomes;
* provide a test mode;
* ensure delivery failure never removes the underlying alert.

This is not required before manual shadow runs, but it is strongly recommended before unattended operation.

---

# 6. P1 — Evaluation and readiness improvements

## 6.1 Persist and evaluate real market regimes

Current readiness evaluates cycle and provider history but does not yet provide mature market-regime coverage.

Future evaluation may distinguish:

* rising market;
* falling market;
* high volatility;
* low volatility;
* earnings periods;
* sector-specific shocks;
* broad market stress.

Do not promote based only on repeated cycles from one market regime.

---

## 6.2 Add MFE and MAE

Maximum favorable excursion and maximum adverse excursion remain deferred.

Required future work:

* select point-in-time intraday data;
* define measurement windows;
* adjust for splits;
* avoid look-ahead bias;
* associate excursions with frozen recommendations;
* separate evaluation from execution authority.

---

## 6.3 Improve portfolio context

The existing portfolio context has historically used cost-basis-oriented data rather than a complete live mark-to-market representation.

Future work may include:

* current market value;
* cash;
* realized and unrealized P/L;
* sector concentration;
* ticker concentration;
* correlated exposure;
* pending paper orders;
* stale-price indicators.

This should be a separate design task and not silently introduced into shadow health.

---

# 7. P2 — Explicitly deferred features

## 7.1 Remaining corporate-action types

The following documented action types remain deferred:

```text
unit_split
stock_dividend
spin_off
cash_merger
stock_merger
stock_and_cash_merger
redemption
name_change
worthless_removal
rights_distribution
```

Each must be independently verified against official response models before implementation.

---

## 7.2 Separate baseline and enhanced paper books

**Update (2026-07-14):** This item is now addressed by Milestone 8 — see
`docs/milestone8-isolated-paper-portfolios.md` and
`docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md`. `ENHANCED_ONLY`/
`BOTH_SEPARATE_PAPER_BOOKS` are now selectable, gated on explicit per-book enablement in the
new, disabled-by-default `config/paper_books.yaml`. This section's original text is preserved
below unmodified as the historical record of what was true before Milestone 8.

The following experiment policies remain unsupported (as of Milestone 7.2, before Milestone 8):

```text
ENHANCED_ONLY
BOTH_SEPARATE_PAPER_BOOKS
```

A future design must address:

* isolated balances;
* isolated positions;
* independent order IDs;
* experiment attribution;
* reconciliation;
* fair comparison;
* corporate-action handling;
* portfolio constraints.

Enhanced execution must remain blocked until separate-book behavior is implemented and tested.

---

## 7.3 Actual recurring deployment

The launchd artifact exists but remains inert.

Do not activate it until all activation gates in this document are satisfied.

Activation requires a separate explicit operator action. Creating or validating a scheduler artifact is not the same as enabling recurring execution.

---

## 7.4 Live promotion and live trading

Live trading, margin, short selling, options, and enhanced-arm execution remain outside Milestone 7.

They are not unfinished Milestone 7 requirements.

Do not interpret this backlog as approval to implement them.

Current invariants remain:

```text
live trading:                 unavailable
enhanced execution:           unavailable
enhanced paper submission:    unavailable
baseline paper submission:    only under existing explicit policy
```

---

# 8. Recommended implementation sequence

## Milestone 7.3 — Health-metric semantics and operational signals

Recommended scope:

1. Correct `unsupported_claim_rate`.
2. Clarify terminal role-attempt exhaustion terminology.
3. Wire real paper-reconciliation status.
4. Wire real duplicate-prevention violations.
5. Correct real-smoke duration measurement.
6. Correct the historical Milestone 7.1 pause documentation.
7. Preserve all current safety thresholds.

## Milestone 7.4 — Provider validation and evidence coverage

Recommended scope:

1. Real Alpaca market-data validation.
2. Real Alpaca News validation.
3. Real Reddit MCP validation.
4. Corporate-action evidence integration.
5. Disclosure-extraction fixture expansion.
6. Decide the future of operating-history evidence.

## Milestone 7.5 — Sustained shadow soak and activation review

Recommended scope:

1. Use a persistent evaluation database.
2. Accumulate at least 10 completed cycles.
3. Accumulate at least 5 real-provider cycles.
4. Review costs, failures, pauses, alerts, and completeness.
5. Validate real cycle duration.
6. Validate reconciliation and duplicate signals.
7. Produce a formal activation recommendation.
8. Do not activate recurring operation automatically.

---

# 9. Recurring activation gate

Recurring shadow execution must remain disabled until all of the following are true:

```text
[ ] No unexplained PAUSE_REQUIRED results
[ ] unsupported_claim_rate semantics corrected
[ ] paper reconciliation is authoritative or NOT_APPLICABLE
[ ] duplicate-prevention violations are derived from real events
[ ] real cycle duration is measured correctly
[ ] model-specific pricing is configured
[ ] attempt-level cost reconciles with cycle settlement
[ ] pause state is ACTIVE
[ ] health checks are persisted for all relevant runs
[ ] at least 10 completed persistent cycles exist
[ ] at least 5 real-provider persistent cycles exist
[ ] no unresolved critical operational alerts
[ ] provider availability is acceptable
[ ] evidence completeness is acceptable
[ ] no enhanced execution path exists
[ ] no live trading path exists
[ ] operator explicitly approves schedule activation
```

---

# 10. Work that is already complete and should not be reopened without evidence

The following should be treated as completed unless a demonstrated defect is found:

* scheduler single-invocation architecture;
* lease acquisition and stale recovery;
* pause and kill-state persistence;
* model-specific budget reservation;
* pre-attempt role-budget checks;
* idempotent attempt-level usage charging;
* actual Claude cost settlement;
* corporate-status snapshot integration;
* evidence-completeness gating;
* model/provider propagation;
* field-level health diagnostics;
* health-check persistence;
* health-explanation CLI;
* activation-readiness status model;
* enhanced execution block;
* live-trading absence.

Avoid broad rewrites of these components during future backlog work.

---

# 11. Required status language

Future reports should use precise labels:

```text
CODE-COMPLETE
RUNTIME-INTEGRATED
OFFLINE-VALIDATED
REAL-VALIDATED
ENVIRONMENTALLY-PENDING
HISTORICALLY-UNRESOLVABLE
EXPECTED-POLICY-PAUSE
ACTIVATION-BLOCKED
READY-FOR-MANUAL-SHADOW
READY-FOR-LIMITED-RECURRING-SHADOW
ACTUAL-RECURRING-DEPLOYMENT-NOT-ACTIVATED
```

Do not use `COMPLETE` for:

* a provider that has only fixture validation;
* a health signal that is hardcoded;
* a metric whose numerator and denominator have incompatible semantics;
* an activation state without sufficient persistent history;
* a historical incident whose source records no longer exist.

---

# 12. Final backlog status

```text
Milestone 7 architecture:                 COMPLETE
Milestone 7.1 runtime integration:        COMPLETE
Milestone 7.2 health diagnostics:         COMPLETE
Subsequent real health pause:             EXPLAINED, EXPECTED POLICY PAUSE
Original Milestone 7.1 health pause:      HISTORICALLY UNRESOLVABLE
Recurring activation:                     BLOCKED
Primary blocker:                          INSUFFICIENT PERSISTENT HISTORY
Additional blockers:                      INCOMPLETE OPERATIONAL-SIGNAL WIRING
Real News/Reddit validation:              ENVIRONMENTALLY PENDING
Enhanced execution:                       BLOCKED
Live trading:                             NOT IMPLEMENTED
```
