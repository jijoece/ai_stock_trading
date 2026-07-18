# Milestone 9.3 — Evidence-integrity corrections and controlled soak campaign

Work directly in the existing `ai_stock_trading` repository.

The repository currently includes Milestone 9.2 commit:

```text
655d3bd672ffc774e69aad280f10920f986c3420
```

Milestone 9.2 introduced:

* provider-provenance persistence;
* cross-book verification;
* alert listing and resolution;
* detailed controlled-soak readiness output;
* cross-book verification inside `paper-soak-run`.

Before building the campaign runner, correct the remaining Milestone 9.2 evidence-integrity gaps described below.

Then implement the controlled multi-day soak campaign.

Do not build recurring scheduling, external broker execution, or live trading.

---

# Working mode

You are operating as a coding agent with direct repository access.

Use repository tools to:

* inspect symbols;
* edit files;
* create additive migrations;
* run targeted tests;
* run the final test suites.

Do not return a hypothetical patch without applying it.

Do not merely describe changes. Implement them.

Do not use network APIs or paid model/provider calls.

---

# Token-efficiency requirements

Keep exploration and output focused.

1. Inspect Git history and confirm HEAD contains commit `655d3bd672ffc774e69aad280f10920f986c3420`.
2. Use symbol search, references, and targeted file reads before opening whole files.
3. Do not reread old milestone documents broadly.
4. Read only the files listed below unless another exact dependency is necessary.
5. Do not create a long investigation report.
6. Keep the scratchpad concise.
7. Run targeted tests while implementing.
8. Run the full main suite only:

   * once for baseline;
   * once at completion.
9. Run the paper-runtime suite only:

   * once for baseline;
   * once at completion.
10. Use:

```bash
pytest -q --tb=short
```

11. Do not print complete passing-test lists.
12. Do not dump full database tables, source files, or large JSON payloads.
13. Do not perform broad refactoring.
14. Stop when the acceptance criteria are met.
15. Do not commit or push unless explicitly instructed after review.

---

# Required initial review

Read only:

```text
.claude/scratchpads/milestone9-2-progress.md
docs/milestones/milestone9-2-soak-evidence-integrity.md
docs/milestones/milestone9-1-controlled-soak-readiness.md
docs/milestones/milestone9-manual-paper-soak-and-lifecycle.md
```

Inspect relevant symbols only in:

```text
src/trading_research/research/provider_provenance.py
src/trading_research/research/scheduled_cycle.py

src/trading_research/shadow/readiness.py

src/trading_research/paper_books/controlled_soak_readiness.py
src/trading_research/paper_books/cross_book_verification.py
src/trading_research/paper_books/lifecycle.py
src/trading_research/paper_books/cli_support.py
src/trading_research/paper_books/config.py

src/trading_research/storage/research_cycle_schema.py
src/trading_research/storage/research_cycle_repositories.py
src/trading_research/storage/paper_books_schema.py
src/trading_research/storage/paper_books_repositories.py
src/trading_research/storage/shadow_alerts_repositories.py

src/trading_research/cli.py
config/paper_books.yaml
```

Use current code as the source of truth.

---

# Scratchpad

Create:

```text
.claude/scratchpads/milestone9-3-progress.md
```

Use only:

```markdown
# Milestone 9.3 Progress

## Baseline
## Milestone 9.2 correction findings
## Provider success semantics
## Readiness aggregation
## Cross-book verification integrity
## Campaign design
## Implementation
## Tests
## Documentation
## Safety review
## Known limitations
## Final status
```

Record summarized commands and outcomes only.

Never include:

* credentials;
* `.env`;
* raw Claude or provider content;
* account identifiers;
* private reasoning;
* large source excerpts.

---

# Baseline

Run:

```bash
pytest tests/ -q --tb=short
```

Expected from Milestone 9.2:

```text
1549 passed, 14 skipped
```

Then:

```bash
cd paper_runtime
pytest tests/ -q --tb=short
```

Expected:

```text
33 passed
```

Check Git status and preserve unrelated work.

If baseline differs, record the exact result and continue unless failures are clearly caused by an incomplete checkout.

---

# Hard boundaries

Do not:

* activate recurring execution;
* install launchd or cron;
* add external paper-broker submission;
* add Alpaca or Robinhood order mutation;
* add live trading;
* add a `--live` flag;
* add margin, shorting, or options;
* make Claude or provider network calls;
* infer provider identity from cost;
* count failed provider attempts as successful provider history;
* fabricate missing provenance;
* fabricate a cross-book verification pass;
* automatically resolve alerts;
* clear pause or kill state;
* weaken readiness thresholds;
* reduce minimum sample requirements;
* automatically promote the enhanced arm;
* modify `real_orders`;
* redesign the isolated paper-book subsystem;
* implement partial fills or trailing stops;
* fix unrelated Milestone 7 metrics.

---

# Part A — Milestone 9.2 evidence-integrity corrections

Complete Part A before implementing the campaign.

## A1. Remove cost-based provider history from controlled readiness

Current controlled readiness calls the existing shadow activation readiness function, whose underlying report still derives `real_provider_cycle_count` from positive `cost_usd`.

Correct this so the controlled-soak path uses authoritative provider provenance for provider-history qualification.

Cost must remain only a:

```text
budget signal
pricing-verification signal
cost-reporting signal
```

It must never remain a provider-identity signal in the controlled-soak readiness path.

Preferred approach:

* add a narrow authoritative-provider-count override or provider-history input to the existing shadow readiness functions; or
* split provider-history gating from the legacy report while reusing the rest of its health logic.

Do not duplicate the entire shadow readiness implementation.

Requirements:

* zero-cost real-provider history can satisfy the provider-history threshold;
* positive-cost fixture history cannot satisfy it;
* cost-based pricing failures still block through the existing pricing gate;
* legacy callers retain current behavior unless they explicitly provide authoritative provenance.

---

## A2. Track successful versus failed provider activity

The current provenance classifier uses `is_real` and `is_fixture` but does not require a successful outcome.

Introduce explicit normalized outcomes, such as:

```text
SUCCEEDED
PARTIAL
FAILED
SOURCE_UNAVAILABLE
ATTEMPTED
UNKNOWN
```

Use existing persisted provider and orchestration statuses wherever possible.

Do not invent success from provider identity alone.

For evidence providers:

* map existing `SourceRecord.status` values into a normalized outcome;
* document exactly which values count as successful;
* `SOURCE_UNAVAILABLE`, hard failure, timeout, invalid response, and similar outcomes must not count as successful real-provider history.

For Claude:

* persist the actual orchestration or research-run status;
* do not hardcode `"ok"`;
* distinguish completed usable analysis from failed, incomplete, or exhausted research attempts.

Add summary fields such as:

```text
real_provider_attempt_cycle_count
real_provider_success_cycle_count
real_provider_failure_cycle_count
partial_provider_cycle_count
```

The readiness minimum must use the successful-cycle count.

A cycle counts once regardless of provider or symbol count.

---

## A3. Count missing completed cycles as `UNKNOWN`

`compute_real_provider_history` must start from completed research cycles at or before `as_of`, not only from cycles that already have provenance rows.

Required invariant:

```text
completed_cycle_count =
    fixture_only_cycle_count
  + real_evidence_only_cycle_count
  + real_claude_only_cycle_count
  + real_evidence_and_claude_cycle_count
  + mixed_cycle_count
  + unknown_cycle_count
```

A completed historical cycle without sufficient provenance becomes:

```text
UNKNOWN
```

It must not silently disappear.

Failed or still-running cycles should be reported separately or excluded according to a documented deterministic rule.

Unknown cycles must not satisfy the real-provider minimum.

---

## A4. Preserve evidence-to-research-run association

Current evidence provenance may be persisted before `research_run_id` is known and remains permanently `NULL`.

Fix the association without mutating immutable provenance facts incorrectly.

Preferred options:

1. add an immutable cycle/symbol-to-research-run association table; or
2. add a separate immutable provenance-link table; or
3. redesign the provenance primary key only when an additive migration can preserve compatibility safely.

Do not overwrite original provider facts merely to add the run ID.

After a successful research run, operators must be able to trace:

```text
cycle
→ symbol
→ evidence provider facts
→ research_run_id
→ Claude provider fact
```

Correct any misleading comments and documentation.

---

## A5. Return all readiness failures

The readiness result must preserve one deterministic primary status while evaluating every safe read-only check.

Do not return immediately after the first blocker.

Evaluate and collect:

```text
shadow kill
shadow pause
unexplained PAUSE_REQUIRED
unresolved critical alerts
paper lifecycle failures
paper reconciliation
valuation
completed cycles
market days
cross-book verification
real-provider success history
provider health
pricing readiness
other inherited shadow readiness failures
```

Then select the primary status using a fixed documented priority.

Expose:

```text
all_failed_checks
blocking_checks
advisory_checks
missing_checks
```

Requirements:

* simultaneous failures all appear;
* primary status remains deterministic;
* missing is distinct from failed;
* no safety mutation is performed while evaluating;
* a killed or paused state still remains the primary result when appropriate.

Add a test containing at least four simultaneous failures and assert all four appear.

---

## A6. Make cross-book verification state-sensitive

Current verification identity is derived from scope metadata but not from verified data or check results.

Correct it so a changed database state produces a new verification event.

Preferred design:

```text
verification_scope_id:
    stable identity for as_of/operator/lifecycle scope

verification_id:
    hash of scope + policy version + deterministic source-state/check-result hash
```

Alternatively, persist append-only verification attempts under one stable scope.

Requirements:

* identical frozen state produces the same verification ID;
* changed relevant state produces a new verification ID;
* a failed verification followed by a corrected database state can persist a later passed verification;
* readiness uses the latest applicable verification;
* prior failed verification remains immutable;
* no in-memory/persisted-status disagreement.

Include relevant source high-watermarks, row identifiers, or normalized check results in the state hash.

---

## A7. Strengthen high-value cross-book checks

Keep the verifier focused; do not build a general database auditor.

Add checks for:

### Position and lot consistency

* recompute book/symbol open quantity from book-scoped lots or fills;
* compare it with `paper_book_positions`;
* detect foreign or inconsistent contributions.

### Settlement references

For event types that require an order or fill reference:

* reference must resolve inside the same book;
* reference resolving nowhere must fail or be explicitly classified invalid;
* non-order events such as initial capital, dividends, or audited cash adjustments must follow their own allowed reference policy.

### Unexpected books

* detect paper-book rows under unconfigured or unknown book IDs;
* do not inspect only the two expected books and ignore additional namespaces.

### Verification freshness

* readiness must not accept a verification that predates newer lifecycle, order, fill, cash, lot, assignment, or reconciliation evidence relevant to the same `as_of`;
* stale verification should become `INSUFFICIENT_DATA` or a dedicated stale status.

Keep existing same-identifier-across-books behavior valid when the records remain correctly scoped.

---

## A8. Part A tests

Add targeted tests proving:

1. zero-cost real-provider success satisfies the controlled readiness provider-history gate;
2. positive-cost fixture activity does not;
3. failed real evidence does not count as successful;
4. failed or incomplete real Claude activity does not count as successful;
5. successful real evidence with fixture Claude is classified correctly;
6. successful real Claude with fixture evidence is classified correctly;
7. completed historical cycles without provenance count as `UNKNOWN`;
8. the provenance category totals reconcile to completed cycles;
9. evidence facts link to the resulting research run;
10. four simultaneous readiness failures are all returned;
11. deterministic primary status is preserved;
12. changed verified state generates a new verification ID;
13. failed then repaired state preserves both immutable verification events;
14. unmatched settlement references fail;
15. unknown paper-book namespaces fail;
16. position/lot quantity inconsistency fails;
17. stale verification does not satisfy recurring-review readiness.

Do not make network calls.

---

# Part B — Controlled multi-day soak campaign

After Part A tests pass, implement the campaign layer.

## B1. Objective

Create tooling to run and evaluate a controlled multi-day local paper-trading soak campaign.

Primary flow:

```text
explicit campaign manifest
→ ordered historical market dates
→ explicit cycle integration
→ paper lifecycle
→ cross-book verification
→ detailed controlled readiness
→ persisted campaign-day result
→ final activation-review report
```

The campaign remains manually invoked.

It must not activate recurring execution.

---

## B2. Campaign configuration

Extend `config/paper_books.yaml` with an optional disabled-by-default section conceptually equivalent to:

```yaml
paper_books:
  soak_campaign:
    enabled: false
    minimum_market_days: 5
    minimum_completed_cycles: 10
    minimum_successful_real_provider_cycles: 5
    maximum_unresolved_warnings: 0
    stop_on_blocker: true
```

Prefer reusing existing thresholds instead of duplicating them.

Requirements:

* disabled by default;
* unknown keys fail closed;
* positive integer validation;
* booleans validated strictly;
* no environment variable enables the campaign;
* campaign configuration included in the campaign hash.

---

## B3. Campaign manifest

Use a small JSON manifest:

```json
{
  "campaign_id": "manual-soak-july-2026",
  "dates": [
    {
      "as_of": "2026-07-15T20:00:00Z",
      "cycle_ids": ["cycle-a", "cycle-b"]
    },
    {
      "as_of": "2026-07-16T20:00:00Z",
      "cycle_ids": []
    }
  ]
}
```

Validation:

* campaign ID required;
* dates required and nonempty;
* timestamps timezone-aware;
* dates strictly increasing;
* duplicate dates rejected;
* duplicate cycle IDs on one date rejected;
* cycle IDs are explicit;
* empty cycle list allowed for lifecycle-only dates;
* unknown top-level or date-level keys fail closed;
* no automatic cycle discovery;
* no provider call.

Add a bounded manifest-size limit.

---

## B4. Campaign service

Create:

```text
src/trading_research/paper_books/soak_campaign.py
```

Conceptual entry point:

```python
run_soak_campaign(
    conn,
    *,
    manifest,
    paper_books_config,
    shadow_config,
    stop_on_blocker=True,
    audit_clock=None,
) -> SoakCampaignResult
```

For every campaign date:

1. Validate configuration and campaign state.
2. Use that date’s `as_of` as the effective clock.
3. Run the existing controlled paper-soak workflow.
4. Integrate only listed cycle IDs.
5. Process lifecycle and exits.
6. Persist cross-book verification.
7. Evaluate all controlled-readiness checks.
8. Persist one immutable campaign-day result.
9. Stop before later dates when a hard blocker exists and `stop_on_blocker=True`.

Do not duplicate lifecycle, readiness, provenance, or verification code.

Create a service-level function shared by:

```text
paper-soak-run
campaign runner
```

rather than invoking one CLI function from another.

---

## B5. Historical replay semantics

Every effective trading timestamp must use the manifest date’s `as_of`.

Do not use wall time for:

* order effective time;
* exit decisions;
* holding-period calculations;
* pending-order age;
* snapshots;
* valuation;
* reconciliation cutoff;
* cross-book verification cutoff;
* readiness cutoff.

Wall time may be stored separately as audit metadata.

Replaying the same campaign manifest against unchanged state must be idempotent.

---

## B6. Persistence

Add additive tables when equivalent storage does not already exist:

```text
paper_soak_campaigns
paper_soak_campaign_days
paper_soak_activation_reviews
```

Persist campaign header:

```text
campaign_id
manifest_hash
config_hash
start_as_of
end_as_of
requested_date_count
requested_cycle_count
status
first_blocking_date
first_blocking_status
created_at
```

Persist each day:

```text
campaign_id
as_of
requested_cycle_ids
operator_run_id
lifecycle_run_id
cross_book_verification_id
cross_book_verification_status
controlled_readiness_status
all_failed_checks
failure_reasons
day_status
created_at
```

Persist activation-review evidence:

```text
activation_review_id
campaign_id
campaign_manifest_hash
completed_market_days
completed_cycles
provider_provenance_counts
provider_success_counts
cross_book_verification_history
reconciliation_history
valuation_history
alert_summary
pause_and_kill_summary
performance_metrics
comparison_id
promotion_evidence_status
controlled_readiness_history
final_recommendation
reasons
policy_version
created_at
```

Requirements:

* additive schema;
* deterministic IDs;
* immutable rows;
* idempotent replay;
* no raw model output;
* no credentials;
* no destructive migration.

---

## B7. Campaign statuses

Use a bounded vocabulary such as:

```text
NOT_STARTED
RUNNING_MANUALLY
BLOCKED
COMPLETED_NOT_READY
COMPLETED_READY_FOR_REVIEW
FAILED
```

Campaign-day statuses:

```text
COMPLETED
COMPLETED_WITH_WARNINGS
BLOCKED
FAILED
SKIPPED_AFTER_BLOCKER
```

`COMPLETED_READY_FOR_REVIEW` is advisory only.

It must not activate or schedule anything.

---

## B8. Hard blocker semantics

Define hard blockers explicitly.

At minimum:

```text
shadow kill active
shadow pause active
unexplained PAUSE_REQUIRED
unresolved CRITICAL alert
paper reconciliation mismatch
unsafe or incomplete valuation
unresolved lifecycle failure
cross-book verification FAILED
required successful real-provider history not met at final review
```

Insufficient early history may allow campaign continuation while remaining not ready.

Distinguish:

```text
continue-soak condition
day blocker
campaign blocker
final-review blocker
```

Do not stop a campaign merely because the first day has not yet reached the minimum sample size.

---

## B9. Activation-review report

Create a read-only report containing:

```text
campaign ID
manifest hash
market days requested
market days completed
cycle IDs requested
cycles successfully integrated
fixture-only cycles
successful real-evidence cycles
successful real-Claude cycles
successful real-evidence-and-Claude cycles
failed real-provider cycles
partial provider cycles
unknown cycles
book-level returns
book-level realized and unrealized P&L
book-level maximum drawdown
open positions
closed trades
pending orders
reconciliation history
cross-book verification history
stale verification count
unresolved alerts
resolved critical-alert history
pause and kill history
valuation completeness
lifecycle failures
estimated model cost
baseline-versus-enhanced comparison
promotion evidence status
controlled readiness history
all remaining failed checks
final advisory recommendation
```

Suggested recommendations:

```text
INSUFFICIENT_EVIDENCE
CONTINUE_MANUAL_SOAK
BLOCKED_REQUIRES_REMEDIATION
READY_FOR_RECURRING_ACTIVATION_REVIEW
```

Do not declare enhanced research promoted.

Do not activate recurring execution.

---

## B10. CLI commands

Add:

```bash
python -m trading_research.cli paper-soak-campaign-validate \
  --manifest <path>

python -m trading_research.cli paper-soak-campaign-run \
  --manifest <path>

python -m trading_research.cli paper-soak-campaign-show \
  --campaign-id <id>

python -m trading_research.cli paper-soak-activation-review \
  --campaign-id <id>
```

Optional:

```text
--continue-on-blocker
```

must require explicit use.

Requirements:

* bounded JSON;
* deterministic ordering;
* no implicit cycle discovery;
* no network calls;
* disabled config fails closed;
* unknown campaign fails closed;
* replay idempotent;
* no activation or schedule side effect.

---

## B11. Campaign tests

Add focused offline tests for:

* valid manifest;
* unknown manifest keys;
* invalid timezone;
* non-increasing dates;
* duplicate date;
* duplicate cycle ID;
* disabled campaign config;
* historical multi-day processing;
* explicit cycle integration;
* lifecycle-only date;
* effective timestamps equal each date’s `as_of`;
* early insufficient history permits continuation;
* hard blocker stops later dates;
* explicit continue-on-blocker;
* campaign replay idempotency;
* campaign-day persistence;
* corrected provider-success aggregation;
* `UNKNOWN` cycle aggregation;
* cross-book verification history;
* failed and stale verification reporting;
* simultaneous readiness failures in campaign output;
* resolved versus unresolved alert handling;
* pause and kill blocker;
* activation-review report;
* no automatic activation;
* no provider/network call;
* no live execution.

Add one small end-to-end campaign covering at least three market dates.

---

# Documentation

Create:

```text
docs/milestones/milestone9-3-evidence-integrity-and-soak-campaign.md
docs/runbooks/paper-soak-campaign.md
```

Update:

```text
docs/milestones/milestone9-2-soak-evidence-integrity.md
```

with a concise correction section describing:

* successful-provider semantics;
* removal of cost-based identity from controlled readiness;
* completed-cycle `UNKNOWN` handling;
* all-failure readiness evaluation;
* state-sensitive cross-book verification;
* evidence-to-research-run association.

Do not rewrite older milestone documents.

---

# Required final test execution

During development, run targeted tests only.

At completion run:

```bash
pytest tests/ -q --tb=short
```

Then:

```bash
cd paper_runtime
pytest tests/ -q --tb=short
```

Do not run real or network tests.

---

# Acceptance criteria

Milestone 9.3 is complete when:

## Evidence-integrity corrections

1. Existing 1,549 tests still pass.
2. Existing 33 paper-runtime tests still pass.
3. Controlled readiness no longer uses cost as provider identity.
4. Provider success, failure, partial, and unavailable outcomes are explicit.
5. Failed real-provider activity does not satisfy successful-provider history.
6. Zero-cost successful real-provider activity can satisfy the provider-history threshold.
7. Completed cycles without provenance count as `UNKNOWN`.
8. Provenance totals reconcile to completed-cycle history.
9. Evidence facts are traceable to the resulting research run.
10. All simultaneous readiness failures are returned.
11. One deterministic primary status is preserved.
12. Verification identity changes when relevant verified state changes.
13. Prior verification events remain immutable.
14. Unmatched settlement references are detected.
15. Unknown book namespaces are detected.
16. Position/lot inconsistencies are detected.
17. Stale verification cannot satisfy recurring-review readiness.

## Campaign

18. Campaign manifests validate deterministically.
19. Dates process in strict order.
20. Only explicit cycle IDs are integrated.
21. Historical effective time uses each date’s `as_of`.
22. Lifecycle-only dates work.
23. Hard blockers stop later dates by default.
24. Insufficient early history does not unnecessarily stop the campaign.
25. Campaign and day records are persisted immutably.
26. Replay is idempotent.
27. Activation-review report includes provider success and failure evidence.
28. Cross-book verification history is included.
29. All readiness failures are visible.
30. Final recommendation remains advisory.
31. No recurring schedule is activated.
32. No external broker or live path exists.
33. No network call occurs in tests.
34. Documentation matches implementation.
35. No commit or push occurs unless explicitly requested.

---

# Final response

Keep the final response concise.

Report only:

1. Baseline and final test results.
2. Evidence-integrity corrections.
3. Files created and modified.
4. Provider-success classification.
5. Readiness aggregation changes.
6. Cross-book verification changes.
7. Campaign manifest and service.
8. CLI commands.
9. Idempotency proof.
10. Current activation-review result.
11. Safety confirmation.
12. Deferred items.

Include a compact table:

```text
Requirement → implementation → test
```

Use labels:

```text
EVIDENCE-INTEGRITY-CORRECTED
SUCCESSFUL-PROVIDER-PROVENANCE
COST-NOT-PROVIDER-IDENTITY
ALL-READINESS-FAILURES-VISIBLE
STATE-SENSITIVE-CROSS-BOOK-VERIFICATION
CONTROLLED-SOAK-CAMPAIGN
POINT-IN-TIME-SAFE
IDEMPOTENT
PAPER-BOOK-ISOLATED
ADVISORY-ONLY
LIVE-TRADING-NOT-IMPLEMENTED
RECURRING-DEPLOYMENT-NOT-ACTIVATED
```

Do not commit or push.
