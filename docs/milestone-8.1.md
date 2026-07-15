Implement a focused follow-up in the existing trading-desk repository:

# Milestone 8.1 — Scheduled research to isolated paper-book integration

Milestones 1–8 are complete for their defined scopes.

Milestone 8 created an offline, isolated paper-book subsystem with separate `BASELINE` and `ENHANCED` cash, positions, orders, fills, reconciliation, metrics, and promotion evidence.

The missing integration is:

```text
real scheduled research cycle
→ frozen baseline/enhanced recommendations
→ shared EvidenceSnapshot and as_of
→ isolated per-book portfolio valuation
→ deterministic risk decisions
→ book-aware paper intents
→ local simulated fills
→ book-specific reconciliation and evaluation
```

Implement only this integration closure.

Do not begin broad Milestone 9 work.

---

# Token-efficiency requirements

My Claude Code usage is near its limit. Work efficiently.

1. Use Pyright/LSP symbol lookup, references, and call hierarchy before broad grep or full-file reads.
2. Read only the specific symbols needed from large files.
3. Do not reread all Milestone 7 documentation.
4. Do not produce long investigation narratives during implementation.
5. Keep the scratchpad concise and factual.
6. Run targeted tests while developing.
7. Run the full suite only:

   * once to establish baseline;
   * once after implementation.
8. Use `pytest -q --tb=short`.
9. Do not print full passing-test lists.
10. Do not dump complete database rows or large JSON objects.
11. Do not make real Claude, SEC, Alpaca, Reddit, broker, or other network calls.
12. Do not perform broad refactoring.
13. Do not rewrite completed Milestone 8 modules unless integration requires a targeted change.
14. Stop when the acceptance criteria below are satisfied.

---

# Required source review

Read only these documents first:

```text
.claude/scratchpads/milestone8-progress.md
docs/milestone8-isolated-paper-portfolios.md
docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md
```

Inspect only relevant symbols in:

```text
src/trading_research/research/scheduled_cycle.py
src/trading_research/research/models.py
src/trading_research/research/experiment_policy.py

src/trading_research/shadow/scheduler.py

src/trading_research/paper_books/
src/trading_research/storage/paper_books_repositories.py
src/trading_research/cli.py

config/paper_books.yaml
```

Use current repository code as the source of truth.

Do not assume model or field names from this prompt when the implementation already uses different names.

---

# Scratchpad

Create:

```text
.claude/scratchpads/milestone8-1-progress.md
```

Use only:

```markdown
# Milestone 8.1 Progress

## Baseline
## Scheduled-cycle output mapping
## Integration design
## Implementation
## Tests
## Documentation
## Safety review
## Known limitations
## Final status
```

Keep it concise. Record commands and summarized results, not full logs.

Do not record secrets, prompts, responses, credentials, account identifiers, or chain-of-thought.

---

# Baseline

Check Git status and preserve unrelated work.

Run:

```bash
pytest tests/ -q --tb=short
```

Expected:

```text
1355 passed, 14 skipped
```

Run:

```bash
cd paper_runtime
pytest tests/ -q --tb=short
```

Expected:

```text
33 passed
```

Do not commit or push.

---

# Hard boundaries

Do not:

* enable live trading;
* add external paper-broker submission;
* add Robinhood or Alpaca order mutation;
* add a `--live` flag;
* enable margin, shorting, or options;
* activate launchd;
* start a recurring process;
* make paid Claude calls;
* modify `real_orders`;
* weaken evidence completeness;
* weaken recommendation validation;
* weaken paper-book isolation;
* share cash, positions, fills, or orders between books;
* let Claude choose a book;
* let Claude override risk;
* let enhanced recommendations fall back to the baseline book;
* fabricate a recommendation;
* fabricate a market price;
* fabricate a fill;
* mutate frozen recommendations;
* automatically promote the enhanced arm;
* implement exit strategies;
* implement partial fills;
* fix unrelated Milestone 7 backlog items;
* redesign the legacy global paper ledger.

---

# 1. Map scheduled-cycle outputs

Trace the exact persisted and returned outputs of:

```text
run_scheduled_research_cycle
```

Identify how to obtain, for each symbol:

```text
cycle_id
research_run_id
symbol
as_of
evidence_snapshot_id
baseline_recommendation_id
enhanced_recommendation_id
baseline status
enhanced status
evidence completeness
experiment policy
```

Prefer authoritative persisted records over reconstructing data from display output.

Document which fields are:

```text
AUTHORITATIVE
DERIVED
OPTIONAL
NOT_AVAILABLE
```

Do not change the scheduled research result model unless required.

Prefer querying existing repositories by `cycle_id` or `research_run_id`.

---

# 2. Add a narrow integration service

Create a focused module such as:

```text
src/trading_research/paper_books/scheduled_integration.py
```

Use existing naming conventions when a better location already exists.

Provide a deterministic entry point conceptually equivalent to:

```python
integrate_scheduled_cycle_into_paper_books(
    conn,
    cycle_result,
    paper_books_config,
    experiment_policy,
    as_of,
) -> PaperBookCycleIntegrationResult
```

The integration result should contain bounded structured outcomes:

```text
cycle_id
symbol outcomes
assignment IDs
book IDs
risk decision IDs
paper order intent IDs
fill IDs
reconciliation statuses
skip/rejection reasons
```

Do not include raw prompts or Claude responses.

---

# 3. Eligibility rules

A scheduled-cycle recommendation may enter a paper book only when:

* the recommendation already exists and is frozen;
* it belongs to the expected experiment arm;
* the required isolated book is enabled;
* `paper_books.enabled` is true;
* scheduled integration is explicitly enabled;
* the experiment policy permits that arm;
* the evidence snapshot exists;
* the recommendation and snapshot use the same symbol;
* the recommendation does not use information after `as_of`;
* evidence completeness permits the recommendation;
* deterministic portfolio valuation succeeds sufficiently for risk evaluation;
* deterministic paper risk approves a quantity.

Fail closed when any requirement is missing.

Persist a clear reason instead of throwing an unclassified exception.

Suggested outcomes:

```text
EXECUTED
INTENT_CREATED_PENDING_FILL
SKIPPED_BOOK_DISABLED
SKIPPED_POLICY
SKIPPED_RECOMMENDATION_MISSING
SKIPPED_RECOMMENDATION_INVALID
SKIPPED_EVIDENCE_INCOMPLETE
SKIPPED_SNAPSHOT_MISMATCH
SKIPPED_VALUATION_UNAVAILABLE
REJECTED_BY_RISK
FAILED
```

Use existing status conventions where possible.

---

# 4. Shared experiment assignment

For each cycle and symbol, both arms must use:

```text
the same evidence_snapshot_id
the same as_of
the same symbol
the same scheduled-cycle context
```

Persist the `PaperBookExperimentAssignment` before either arm is executed.

Requirements:

* assignment is immutable;
* no assignment after observing fills;
* no selective omission of a failed arm;
* a missing recommendation remains explicitly recorded;
* baseline and enhanced recommendation IDs remain distinct;
* each arm maps only to its configured book;
* duplicate processing is idempotent.

If the existing assignment schema requires both intent IDs at creation time, use the smallest safe approach:

* persist a pre-execution assignment record with recommendation and book identity; or
* create deterministic intent IDs before execution and persist them.

Do not weaken assignment immutability.

---

# 5. Build portfolio state per book

For each eligible arm:

1. Load the correct `PaperBook`.
2. Build a point-in-time portfolio snapshot using the scheduled cycle’s `as_of`.
3. Use the exact book’s cash, reservations, positions, and lots.
4. Select valuation prices through the existing Milestone 8 valuation service.
5. Build `PaperPortfolioContext`.
6. Evaluate the existing deterministic paper risk policy.
7. Persist the risk decision.
8. Create a book-aware order intent only when approved.

Never reuse the other book’s snapshot or portfolio context.

---

# 6. Market simulation inputs

The fixture CLI currently accepts manually supplied bid and ask values. The scheduled integration must not require fixture CLI arguments.

Add a deterministic, versioned builder for local paper simulation inputs.

Use this priority:

1. Safe bid/ask values already present in the shared point-in-time evidence snapshot.
2. A safe point-in-time reference price from the snapshot plus the existing configured local simulation spread/slippage model.
3. If neither is available, create the intent but leave it pending with an explicit:

```text
MARKET_SIMULATION_INPUT_UNAVAILABLE
```

Requirements:

* never call a live quote;
* never use a price after `as_of`;
* never claim a modeled bid/ask is an observed market quote;
* persist whether inputs were `OBSERVED` or `SIMULATED`;
* persist simulation-policy version;
* no fill when required inputs are unavailable;
* no guaranteed fill merely because an intent exists.

Reuse the existing Milestone 8 fill simulator rather than creating another one.

---

# 7. Connect to the scheduler safely

Integrate at the narrowest appropriate boundary after scheduled research results and frozen recommendations exist.

Prefer an optional dependency or service invocation from:

```text
shadow/scheduler.py::run_due_shadow_cycle
```

rather than embedding paper-book logic inside the research committee.

Requirements:

* disabled by default;
* explicitly controlled by `config/paper_books.yaml`;
* manual scheduler invocation only;
* paper-book failure must not mutate or invalidate the frozen research result;
* paper-book failure must be persisted and visible;
* an unexpected integration exception may mark the paper integration failed, but must not be mislabeled as a Claude provider failure;
* no launchd activation;
* no recurring behavior added.

Add a configuration field equivalent to:

```yaml
paper_books:
  enabled: false
  scheduled_integration:
    enabled: false
```

Use existing config conventions.

Credentials and environment variables must not enable it.

---

# 8. Experiment policies

Support through the isolated subsystem:

```text
BASELINE_ONLY
ENHANCED_ONLY
BOTH_SEPARATE_PAPER_BOOKS
```

Preserve existing legacy behavior:

```text
may_submit_enhanced() == false
```

for the global paper ledger.

Requirements:

* `BASELINE_ONLY` targets only `BASELINE`;
* `ENHANCED_ONLY` targets only `ENHANCED`;
* `BOTH_SEPARATE_PAPER_BOOKS` targets both independently;
* disabled required book fails closed;
* no fallback;
* no shared intent;
* no shared fill.

---

# 9. Idempotency

Reprocessing the same scheduled cycle must not create:

* duplicate assignment rows;
* duplicate risk decisions;
* duplicate order intents;
* duplicate reservations;
* duplicate fills;
* duplicate cash settlements;
* duplicate positions;
* duplicate reconciliation rows.

Use existing deterministic IDs and repository idempotency.

Add a test that invokes the integration twice and proves all monetary and position state remains unchanged after the second invocation.

---

# 10. Reconciliation and result persistence

After each book attempt:

* reconcile that book independently;
* persist reconciliation status;
* include it in the integration result;
* never reconcile one book against the other;
* never hide a mismatch because the other book matches.

Do not wire this into Milestone 7 shadow-health metrics in this task. That remains separate backlog work.

---

# 11. CLI support

Add a manual, non-recurring command such as:

```bash
python -m trading_research.cli paper-book-integrate-cycle \
  --cycle-id <cycle-id>
```

Optional explicit policy override may be allowed only when existing CLI conventions permit it safely.

Requirements:

* loads actual persisted scheduled-cycle outputs;
* does not fabricate fixture recommendations;
* uses configured books;
* fails when scheduled integration is disabled;
* structured JSON;
* deterministic ordering;
* no raw model content;
* no network calls;
* no live mode.

Keep the existing fixture-oriented `paper-book-run-cycle` command unchanged for testing.

---

# 12. Tests

Add focused tests for:

## Scheduled output mapping

* baseline and enhanced recommendations found;
* missing baseline recommendation;
* missing enhanced recommendation;
* missing evidence snapshot;
* symbol mismatch;
* timestamp mismatch.

## Policy routing

* baseline only;
* enhanced only;
* both books;
* disabled baseline;
* disabled enhanced;
* no fallback.

## Portfolio isolation

* different cash produces different approved quantity;
* one book’s position does not affect the other;
* separate intent IDs;
* separate fills;
* separate reconciliation.

## Market simulation

* observed point-in-time bid/ask;
* deterministic simulated spread;
* unavailable input leaves pending intent;
* future price rejected;
* no live quote call.

## Idempotency

* same cycle integrated twice;
* no duplicate reservation;
* no duplicate fill;
* no duplicate cash settlement;
* no duplicate lot;
* no duplicate assignment.

## Failure handling

* risk rejection;
* incomplete evidence;
* paper integration exception;
* research result remains immutable;
* failure not classified as Claude-provider failure.

## CLI

* actual persisted cycle;
* disabled integration;
* missing cycle;
* sanitized deterministic JSON.

---

# 13. Offline end-to-end integration test

Add one fixture-only integration test proving:

```text
scheduled research cycle
→ persisted frozen baseline and enhanced recommendations
→ one shared evidence snapshot and as_of
→ experiment assignment
→ BASELINE portfolio snapshot
→ ENHANCED portfolio snapshot
→ independent risk decisions
→ distinct order intents
→ local simulated execution
→ separate cash and positions
→ separate reconciliation
→ repeat integration is idempotent
→ no live execution
```

Use existing scripted/fixture research providers.

Do not call Claude or any network provider.

Use intentionally different portfolio state so the two books produce different deterministic quantities.

---

# 14. Correct ADR and documentation

ADR 0006 currently implies the new subsystem executes through the existing `paper_runtime` subprocess, while Milestone 8 actually uses an in-process local simulator.

Correct ADR 0006 to state:

```text
Milestone 8/8.1 uses an in-process, book-aware,
deterministic local simulator.

OrderIntentPayload contains an additive optional book_id
for possible future subprocess-per-book integration.

Per-book paper_runtime subprocess execution is deferred.
```

Also correct any table-count wording that says “ten” while listing 14 tables.

Create:

```text
docs/milestone8-1-scheduled-paper-book-integration.md
```

Update the Milestone 8 document with a concise pointer.

Do not rewrite the entire Milestone 8 document.

Record these as deferred:

* per-book `paper_runtime` subprocess pool;
* external paper broker;
* automated exits;
* partial fills;
* dividend record-date entitlement;
* recurring activation.

---

# Required test execution

During development, run only relevant tests.

At completion run:

```bash
pytest tests/ -q --tb=short
```

Then:

```bash
cd paper_runtime
pytest tests/ -q --tb=short
```

Do not run opt-in real/network tests.

Do not repeatedly rerun the full suite.

---

# Acceptance criteria

Milestone 8.1 is complete when:

1. Existing 1,355 tests still pass.
2. Existing 33 paper-runtime tests still pass.
3. Actual persisted scheduled-cycle recommendations can feed isolated books.
4. No fixture recommendation is constructed by the scheduled integration path.
5. Both arms use the same snapshot and `as_of`.
6. Experiment assignment is persisted before execution.
7. Baseline maps only to `BASELINE`.
8. Enhanced maps only to `ENHANCED`.
9. Per-book valuation and risk are applied independently.
10. Book-aware order intents are distinct.
11. Market simulation inputs are point-in-time safe and versioned.
12. Missing simulation inputs do not fabricate a fill.
13. Reprocessing is idempotent.
14. Paper integration failure does not mutate research results.
15. No enhanced-to-baseline fallback exists.
16. No live or external broker path exists.
17. Configuration remains disabled by default.
18. Manual CLI integration works from a persisted cycle.
19. ADR 0006 matches the actual execution architecture.
20. No scheduler or recurring deployment is activated.
21. No commit or push occurs unless explicitly requested.

---

# Final response

Keep the final response concise.

Report only:

1. Baseline and final tests.
2. Files created and modified.
3. Scheduled-cycle mapping.
4. Integration entry point.
5. Policy routing.
6. Market simulation source behavior.
7. Idempotency proof.
8. Isolation proof.
9. CLI command.
10. ADR correction.
11. Safety confirmation.
12. Remaining deferred items.

Include a compact table:

```text
Requirement → implementation → test
```

Use these labels:

```text
SCHEDULED-RESEARCH-INTEGRATED
PAPER-BOOK-ISOLATED
OFFLINE-DETERMINISTIC
POINT-IN-TIME-SAFE
IDEMPOTENT
ENHANCED-PAPER-ONLY
LIVE-TRADING-NOT-IMPLEMENTED
RECURRING-DEPLOYMENT-NOT-ACTIVATED
```

Do not commit or push.
