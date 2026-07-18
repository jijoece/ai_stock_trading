# Milestone 9.3.1 — Campaign resumability and point-in-time integrity

Work directly in the existing `ai_stock_trading` repository.

Milestone 9.3 is merged into `main` through commit:

```text
f3a4394358c530d03815e276ca1ff7248a603509
```

Implement the smallest corrective milestone needed before Milestone 10.

Do not implement recurring scheduling, external broker integration, or live trading.

---

## Objective

Correct the Milestone 9.3 campaign and activation-review integrity gaps:

```text
resumable campaign attempts
→ crash-safe day processing
→ refreshable immutable activation reviews
→ campaign-scoped point-in-time evidence
→ canonical UTC timestamps
→ market-session-safe valuation
→ historically safe cross-book verification
→ stricter qualifying real-provider history
```

The result must remain manually invoked and disabled by default.

---

## Token-efficient working rules

Use Codex repository tools efficiently.

1. Inspect symbols and references before reading entire files.
2. Read only directly relevant modules and tests.
3. Do not re-investigate previous milestones.
4. Keep scratchpad notes brief.
5. Run targeted tests during implementation.
6. Run the full main suite only:

   * once for baseline;
   * once at completion.
7. Run the paper-runtime suite only:

   * once for baseline;
   * once at completion.
8. Use:

```bash
pytest -q --tb=short
```

9. Do not print complete passing-test lists.
10. Avoid broad refactors.
11. Do not make network calls.


---

## Initial files to inspect

Read only the relevant symbols in:

```text
src/trading_research/paper_books/soak_campaign.py
src/trading_research/paper_books/controlled_soak_readiness.py
src/trading_research/paper_books/cross_book_verification.py
src/trading_research/paper_books/valuation.py
src/trading_research/paper_books/cli_support.py
src/trading_research/paper_books/config.py

src/trading_research/research/provider_provenance.py

src/trading_research/evaluation/price_provider.py
src/trading_research/evidence_providers/alpaca_market_data.py
src/trading_research/evaluation/market_calendar.py

src/trading_research/storage/database.py
src/trading_research/storage/paper_books_schema.py
src/trading_research/storage/paper_books_repositories.py
src/trading_research/storage/research_cycle_repositories.py

tests/unit/test_soak_campaign.py
tests/unit/test_controlled_soak_readiness.py
tests/unit/test_cross_book_verification.py
tests/unit/test_provider_provenance.py
```

Read:

```text
docs/milestones/milestone9-3-evidence-integrity-and-soak-campaign.md
docs/runbooks/paper-soak-campaign.md
```

Use the repository as the source of truth when actual symbol names differ.

---

## Scratchpad

Create:

```text
.codex/scratchpads/milestone9-3-1-progress.md
```

Use only:

```markdown
# Milestone 9.3.1 Progress

## Baseline
## Campaign-attempt model
## Resume and crash recovery
## Activation-review integrity
## Point-in-time corrections
## Provider qualification
## Tests
## Documentation
## Known limitations
## Final status
```

Do not include private reasoning, credentials, complete files, or large test output.

---

## Baseline

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

Record only summarized results.

Preserve unrelated worktree changes.

---

# Part 1 — Campaign definition versus campaign attempts

The current implementation treats one `campaign_id` as both the immutable campaign definition and its only execution.

Separate these concepts:

```text
campaign definition
→ attempt 1
→ attempt 2 continuation
→ attempt 3 remediation verification
```

Prefer additive schema rather than rewriting existing immutable Milestone 9.3 rows.

Suggested tables:

```text
paper_soak_campaign_attempts
paper_soak_campaign_attempt_days
```

An attempt should include:

```text
campaign_attempt_id
campaign_id
manifest_hash
config_hash
previous_attempt_id
attempt_number
continue_after_blocker
status
started_at
completed_at
first_blocking_date
first_blocking_status
failure_code
created_at
```

Attempt-day evidence should include:

```text
campaign_attempt_id
campaign_id
as_of
requested_cycle_ids
operator_run_id
lifecycle_run_id
cross_book_verification_id
controlled_readiness_status
all_failed_checks
failure_codes
day_status
created_at
```

Requirements:

* existing Milestone 9.3 evidence remains readable and immutable;
* new attempts are append-only;
* attempt IDs are deterministic from campaign, attempt number, manifest hash, config hash, and policy version;
* a new attempt never overwrites previous day evidence;
* configuration or manifest drift under the same campaign ID fails closed;
* identical replay of a completed attempt is idempotent;
* continuation creates a new attempt rather than modifying skipped rows.

---

# Part 2 — Real continuation behavior

Fix the current `--continue-on-blocker` behavior.

Required workflow:

```text
attempt 1
→ date 1 BLOCKED
→ later dates SKIPPED_AFTER_BLOCKER

operator remediates issue

attempt 2 with explicit continuation
→ preserve attempt 1 evidence
→ process only previously skipped or explicitly retryable dates
→ do not repeat already successfully completed dates
```

Requirements:

* continuation must be explicit;
* previously completed dates are not rerun;
* previously skipped dates may be processed;
* failed dates may only be retried when retry-safe;
* lifecycle integration remains idempotent;
* no duplicate orders, fills, ledger events, snapshots, or lifecycle runs;
* campaign display returns all attempts in deterministic order;
* the latest attempt is clearly identified.

Do not reinterpret a continuation as an update to attempt 1.

---

# Part 3 — Crash recovery

Persist an attempt in `RUNNING` state before processing its first date.

Use explicit transaction or savepoint boundaries for each campaign date.

Required behavior:

```text
attempt row persisted
→ day processing begins
→ lifecycle/integration evidence persists
→ day result persists
→ next day
```

On restart:

* detect incomplete attempts;
* inspect persisted stage evidence;
* resume from the first incomplete date;
* do not blindly rerun completed lifecycle mutations;
* do not create a second completed attempt for the same execution identity;
* do not strand a campaign without a visible attempt status.

If a crash occurs after lifecycle persistence but before attempt-day persistence, reconstruct the day result from existing operator/lifecycle/verification evidence when safe.

When reconstruction is not safe, mark the date:

```text
RECOVERY_REQUIRES_REVIEW
```

Do not silently rerun uncertain mutations.

---

# Part 4 — Refreshable immutable activation reviews

The current review identity is not sufficiently state-sensitive.

Create append-only review events.

A review must include or reference:

```text
activation_review_id
activation_review_scope_id
campaign_id
campaign_attempt_id
manifest_hash
config_hash
evidence_state_hash
supersedes_activation_review_id
campaign_start_as_of
campaign_end_as_of
final_recommendation
reasons
created_at
policy_version
```

Identity rules:

```text
scope ID
= campaign ID + manifest hash

review ID
= scope ID
+ attempt ID
+ config hash
+ evidence-state hash
+ policy version
```

Requirements:

* remediation can create a later review;
* prior reviews remain immutable;
* latest review is determined explicitly;
* identical frozen evidence returns the same review;
* changed evidence produces a new review;
* review output shows what earlier review it supersedes;
* no review automatically activates anything.

---

# Part 5 — Campaign-scoped point-in-time review

Activation reviews must use only evidence relevant to the campaign and available at the campaign cutoff.

Do not use unrestricted current/global state.

Scope review inputs to:

```text
campaign attempt dates
campaign cycle IDs
campaign operator-run IDs
campaign lifecycle-run IDs
campaign verification IDs
campaign start_as_of
campaign end_as_of
```

Correct the following:

* alerts must be evaluated as of `campaign_end_as_of`;
* pause/kill state must be reconstructed as of the cutoff;
* later alerts or pause events must not change an earlier review;
* model cost must include only campaign-associated research runs;
* experiment comparison must match the campaign window/books;
* promotion evidence must match that comparison;
* reconciliations and valuations must be bounded to campaign dates;
* open positions must come from the final campaign snapshot, not current mutable positions;
* future database rows must not affect historical campaign reviews.

When authoritative historical state is unavailable, record:

```text
MISSING
INSUFFICIENT_DATA
POINT_IN_TIME_UNAVAILABLE
```

Do not substitute current state.

---

# Part 6 — Canonical UTC timestamps

Add one canonical UTC conversion helper and reuse it.

Requirements:

* all persisted campaign timestamps are UTC;
* all timestamp comparisons use canonical UTC;
* manifest timestamps with different offsets but representing the same instant canonicalize identically;
* manifest hashing uses canonical UTC;
* date ordering compares actual instants, not raw strings;
* reject naive datetimes;
* prevent mixed-offset SQLite lexical-comparison errors.

Use a single fixed ISO format policy throughout the new code.

Do not broadly migrate unrelated historical tables unless required for compatibility.

---

# Part 7 — Market-session-safe campaign dates

Campaign valuation is end-of-day oriented.

Validate every campaign date against the existing U.S. equity market calendar.

Requirements:

* campaign dates must fall on trading days unless explicitly marked as lifecycle-only;
* dates that use same-day closing prices must be at or after regular market close;
* pre-close timestamps must not use that day’s final close;
* market timezone is `America/New_York`;
* no network calendar lookup;
* early-close limitations must remain documented;
* no current quote may substitute for a historical close.

For lifecycle-only non-trading dates, require an explicit manifest field such as:

```json
{
  "as_of": "...",
  "cycle_ids": [],
  "lifecycle_only": true
}
```

Keep strict unknown-key validation.

---

# Part 8 — Price availability semantics

Extend the historical price seam only as much as needed.

A historical `PricePoint` should expose authoritative availability information, conceptually:

```text
session_date
close
available_at
source
```

Requirements:

* Alpaca historical close is not available before the relevant session closes;
* valuation uses the price’s actual `available_at`;
* staleness uses `as_of - available_at`;
* future availability fails closed;
* fixture providers remain deterministic;
* existing callers remain compatible where practical.

Do not add live quote fetching.

---

# Part 9 — Historically safe cross-book verification

Do not use current mutable position or lot state to declare a historical verification passed.

Correct:

* source-state hashing;
* position/lot consistency;
* namespace checks;
* stale detection.

Requirements:

* every check must honor `as_of`;
* use immutable events, snapshots, or cutoff-bounded rows;
* current positions after the cutoff must not affect an earlier verification;
* future orders, fills, snapshots, and lifecycle runs must not make an earlier verification stale;
* source-state hashing must be bounded to relevant books and cutoff;
* avoid serializing the entire database on every verification;
* use deterministic relevant-row hashes or high-watermarks where safe.

When historical reconstruction is impossible for a check:

```text
status = NOT_APPLICABLE or INSUFFICIENT_DATA
```

Never use current mutable state and report `PASSED`.

---

# Part 10 — Qualifying real-provider cycles

Preserve the existing informational provider counters, but add a stricter readiness counter:

```text
qualifying_real_provider_cycle_count
```

A completed cycle qualifies only when:

* it contains explicit real-provider activity;
* at least one required real provider succeeded;
* no required real-provider category is `FAILED`;
* no required category is `SOURCE_UNAVAILABLE`;
* no required category is `PARTIAL`;
* no required category remains `ATTEMPTED` or `UNKNOWN`.

Use authoritative configured/observed provider requirements where available.

When no authoritative required-category set exists, use the conservative rule that any non-success real-provider row disqualifies the cycle.

Controlled soak readiness and activation review must use the qualifying count, not the current “any success” count.

Keep existing counts for reporting compatibility.

---

# Part 11 — Error handling

Do not catch every `Exception` and convert it into ordinary immutable campaign evidence.

Catch known domain exceptions explicitly.

For unexpected exceptions:

* rollback the active date transaction/savepoint;
* mark the attempt failed with a sanitized error code;
* abort further processing;
* do not store raw tracebacks;
* do not persist credentials, SQL payloads, local paths, or provider response bodies.

Persist bounded fields:

```text
failure_code
failure_stage
sanitized_message
```

---

# Part 12 — SQLite operational hardening

Add minimal connection hardening needed for campaign resume and upcoming scheduling:

```text
PRAGMA foreign_keys = ON
PRAGMA journal_mode = WAL
PRAGMA busy_timeout = bounded positive value
```

Choose and document a conservative synchronous policy.

Requirements:

* existing tests continue to work with temporary databases;
* no unbounded lock retry;
* attempt/day transactions remain explicit;
* repository helper functions must not accidentally commit an outer transaction when used inside an attempt-day unit of work.

Where existing repository functions always commit internally, add a minimal optional transaction-control seam instead of broadly rewriting every repository.

---

# Part 13 — CLI

Keep existing commands compatible.

Enhance:

```bash
python -m trading_research.cli paper-soak-campaign-run \
  --manifest campaign.json

python -m trading_research.cli paper-soak-campaign-run \
  --manifest campaign.json \
  --continue-on-blocker
```

Add only when needed:

```bash
python -m trading_research.cli paper-soak-campaign-resume \
  --campaign-id <id> \
  --operator <name> \
  --reason "<reason>"

python -m trading_research.cli paper-soak-campaign-show \
  --campaign-id <id>

python -m trading_research.cli paper-soak-activation-review \
  --campaign-id <id> \
  [--attempt-id <id>]
```

Requirements:

* continuation/resume requires operator and reason;
* structured bounded JSON;
* attempts listed deterministically;
* latest review clearly identified;
* no scheduler activation;
* no network calls;
* no broker calls;
* no live mode.

---

# Part 14 — Tests

Add focused offline tests.

## Campaign attempts

* first campaign attempt persists;
* blocker creates skipped dates;
* explicit continuation creates attempt 2;
* attempt 1 remains unchanged;
* attempt 2 processes previously skipped dates;
* completed dates are not rerun;
* duplicate continuation is idempotent;
* manifest/config drift fails closed.

## Crash recovery

* crash after attempt creation;
* crash after lifecycle persistence;
* crash after verification persistence;
* resume does not duplicate orders or fills;
* uncertain recovery returns `RECOVERY_REQUIRES_REVIEW`;
* unexpected exception aborts remaining dates.

## Activation reviews

* remediation creates a new review;
* old review remains immutable;
* identical evidence returns the same review;
* changed state creates a different review;
* superseded review linkage correct;
* future unrelated data does not alter an earlier review.

## UTC and market sessions

* equivalent timestamp offsets produce the same canonical manifest hash;
* naive timestamps rejected;
* pre-close same-day close usage rejected;
* post-close date accepted;
* non-trading date rejected unless explicit lifecycle-only;
* future price availability fails closed.

## Cross-book verification

* future rows do not make historical verification stale;
* current mutable positions are not used for historical checks;
* unavailable historical reconstruction cannot become `PASSED`;
* source-state hash is cutoff bounded.

## Provider history

* all-success real cycle qualifies;
* one success plus one failure does not qualify;
* partial real cycle does not qualify;
* unknown real outcome does not qualify;
* fixture-only cycle does not qualify;
* controlled readiness uses qualifying count.

## SQLite

* WAL/busy timeout applied;
* two connections do not duplicate an attempt;
* bounded lock conflict behavior;
* outer attempt transaction is not prematurely committed.

---

# Part 15 — Offline integration tests

Add one deterministic continuation test:

```text
campaign attempt 1
→ day 1 completed
→ day 2 blocked
→ day 3 skipped
→ remediation event
→ explicit continuation
→ attempt 2 created
→ day 1 not repeated
→ day 2/day 3 processed safely
→ new activation review persisted
→ old review preserved
→ no duplicate orders, fills, lifecycle runs, or snapshots
```

Add one historical-integrity test:

```text
campaign completes at cutoff T
→ review R1 persisted
→ later alerts, positions, fills, comparisons, and research cycles added after T
→ review R1 remains unchanged
→ rebuilding identical frozen review returns R1
```

Add one crash-recovery test:

```text
attempt starts
→ lifecycle persists
→ simulated crash before day evidence
→ resume
→ existing lifecycle evidence recognized
→ no duplicate mutation
→ campaign completes or explicitly requires review
```

---

# Part 16 — Documentation

Create:

```text
docs/milestones/milestone9-3-1-campaign-resumability-and-point-in-time-integrity.md
```

Update:

```text
docs/milestones/milestone9-3-evidence-integrity-and-soak-campaign.md
docs/runbooks/paper-soak-campaign.md
```

Document:

* campaign definition versus attempts;
* continuation semantics;
* crash recovery;
* review supersession;
* point-in-time evidence scope;
* UTC normalization;
* market-close requirements;
* lifecycle-only dates;
* qualifying provider-cycle semantics;
* historical verification limitations;
* operational recovery commands.

Do not rewrite unrelated milestone documents.

---

# Out of scope

Do not implement:

```text
Milestone 10 recurring scheduler
launchd or cron installation
external paper broker
Alpaca order submission
Robinhood order submission
live trading
automatic activation
automatic pause clearing
automatic alert resolution
automatic experiment promotion
Reddit MCP hardening
GitHub Actions or branch protection
dependency lockfile redesign
```

These can be handled separately.

---

# Final tests

Run once at completion:

```bash
pytest tests/ -q --tb=short
```

Then:

```bash
cd paper_runtime
pytest tests/ -q --tb=short
```

Do not run opt-in network, broker, Claude, SEC, Reddit, or real-provider tests.

---

# Acceptance criteria

Milestone 9.3.1 is complete when:

1. Existing tests pass.
2. Paper-runtime tests pass.
3. Campaign attempts are append-only.
4. A blocked campaign can be explicitly continued.
5. Previous attempt evidence is never rewritten.
6. Completed dates are never repeated.
7. Crash recovery does not duplicate mutations.
8. Uncertain recovery fails closed.
9. Activation reviews can be superseded.
10. Review IDs are state-sensitive.
11. Historical reviews are unaffected by future rows.
12. Review evidence is campaign-scoped.
13. All campaign timestamps are canonical UTC.
14. Equivalent timestamp offsets hash identically.
15. Same-day closing prices cannot be used before availability.
16. Non-trading-day behavior is explicit.
17. Historical verification never uses current mutable state.
18. Verification hashing is cutoff bounded.
19. Readiness uses qualifying real-provider cycles.
20. Partial or failed provider cycles do not satisfy the floor.
21. Unexpected exceptions are sanitized and abort safely.
22. SQLite connection and transaction behavior support safe resume.
23. No recurring execution is added.
24. No external broker call is added.
25. No live-trading path is added.


---

# Final response

Keep the response concise.

Report only:

1. Baseline and final tests.
2. Files created and modified.
3. Campaign-attempt model.
4. Continuation behavior.
5. Crash-recovery behavior.
6. Activation-review identity and supersession.
7. Point-in-time corrections.
8. UTC and market-session behavior.
9. Cross-book verification corrections.
10. Qualifying-provider semantics.
11. Known limitations.
12. Safety confirmation.

Include a compact table:

```text
Requirement → implementation → test
```

Use labels:

```text
MANUAL-SOAK-ONLY
RESUMABLE
APPEND-ONLY
CRASH-RECOVERABLE
POINT-IN-TIME-SAFE
UTC-CANONICAL
MARKET-SESSION-SAFE
PROVIDER-QUALIFIED
CROSS-BOOK-ISOLATED
IDEMPOTENT
RECURRING-NOT-ACTIVATED
EXTERNAL-BROKER-NOT-INTEGRATED
LIVE-TRADING-NOT-IMPLEMENTED
```

commit push and create an MR.
