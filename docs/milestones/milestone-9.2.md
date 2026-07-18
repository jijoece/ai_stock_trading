Implement a narrowly scoped follow-up in the existing trading-desk repository:

# Milestone 9.2 — Soak evidence-integrity closure

Milestones 1–9.1 are complete for their defined scopes.

Current controlled-soak readiness is intentionally capped because:

1. Real-provider history is inferred from `cost_usd > 0`, which is not authoritative provider provenance.
2. No dedicated cross-book verification result is persisted.
3. Critical alerts can be resolved through repository code, but operators have no standard CLI workflow.
4. Combined readiness exposes individual checks, but its primary status may hide other simultaneous failures.

Milestone 9.2 must close only these evidence-integrity and operational-diagnostics gaps.

Do not implement recurring activation, external paper brokers, or live trading.

---

# Token-efficiency requirements

My Claude Code usage is limited. Optimize aggressively.

1. Use Pyright/LSP symbol lookup, references, and call hierarchy first.
2. Read only the required files and specific relevant symbols.
3. Do not reread Milestones 7–9 documentation broadly.
4. Keep the scratchpad concise.
5. Do not produce long investigation narratives.
6. Run targeted tests while developing.
7. Run the full main suite only:

   * once for baseline;
   * once at completion.
8. Run the paper-runtime suite only:

   * once for baseline;
   * once at completion.
9. Use:

```bash
pytest -q --tb=short
```

10. Do not print complete passing-test lists.
11. Do not dump large database rows, JSON payloads, or source files.
12. Do not make Claude, SEC, Alpaca, Reddit, broker, or other network calls.
13. Do not perform broad refactoring.
14. Stop when the acceptance criteria are satisfied.


---

# Required review

Read only:

```text
.claude/scratchpads/milestone9-1-progress.md
docs/milestone9-1-controlled-soak-readiness.md
docs/milestone9-manual-paper-soak-and-lifecycle.md
docs/milestone8-1-scheduled-paper-book-integration.md
```

Inspect only relevant symbols in:

```text
src/trading_research/paper_books/controlled_soak_readiness.py
src/trading_research/paper_books/cli_support.py
src/trading_research/paper_books/lifecycle.py
src/trading_research/paper_books/scheduled_integration.py
src/trading_research/paper_books/experiment_assignment.py

src/trading_research/research/scheduled_cycle.py
src/trading_research/research/models.py

src/trading_research/evidence_providers/
src/trading_research/storage/research_cycle_repositories.py
src/trading_research/storage/research_repositories.py
src/trading_research/storage/trading_repositories.py
src/trading_research/storage/paper_books_repositories.py
src/trading_research/storage/shadow_alerts_repositories.py

src/trading_research/shadow/readiness.py
src/trading_research/shadow/alerts.py

src/trading_research/cli.py
```

Use repository code as the source of truth.

---

# Scratchpad

Create:

```text
.claude/scratchpads/milestone9-2-progress.md
```

Use only:

```markdown
# Milestone 9.2 Progress

## Baseline
## Provider-provenance sources
## Cross-book verification
## Alert operations
## Readiness diagnostics
## Implementation
## Tests
## Documentation
## Safety review
## Known limitations
## Final status
```

Record summarized commands and results only.

Never include credentials, `.env`, raw prompts, raw model responses, account identifiers, or chain-of-thought.

---

# Baseline

Run:

```bash
pytest tests/ -q --tb=short
```

Expected:

```text
1486 passed, 14 skipped
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

---

# Hard boundaries

Do not:

* activate launchd, cron, or recurring execution;
* add external paper-broker submission;
* add Robinhood or Alpaca order mutation;
* add live trading;
* add a `--live` flag;
* add margin, shorting, or options;
* make network or paid API calls;
* infer provider provenance from cost alone;
* fabricate a cross-book verification pass;
* automatically resolve alerts;
* overwrite an existing alert-resolution audit trail;
* automatically clear pause or kill state;
* weaken readiness thresholds;
* reduce minimum history requirements;
* automatically promote the enhanced arm;
* share state between books;
* modify `real_orders`;
* redesign the paper-book subsystem;
* implement partial fills or trailing stops;
* fix unrelated Milestone 7 metric issues.

---

# Primary objectives

Complete only:

1. Replace cost-based real-provider classification with explicit provider provenance.
2. Add authoritative cross-book verification events.
3. Add alert listing and audited alert-resolution CLI commands.
4. Expose every failed readiness check while preserving one deterministic primary status.
5. Feed the new authoritative signals into controlled-soak readiness.
6. Keep recurring activation advisory and disabled.

---

# 1. Inventory authoritative provider provenance

Trace persisted provider identity for:

```text
market-data provider
news provider
Reddit provider
SEC/corporate-status provider
Claude/model provider
fixture or scripted providers
```

Determine which existing records already contain:

```text
provider name
provider mode
fixture/scripted/real classification
request ID
source-record ID
model name
research run ID
cycle ID
attempt ID
```

Classify every candidate source:

```text
AUTHORITATIVE
DERIVED
AMBIGUOUS
MISSING
```

Do not use `cost_usd` as provider identity.

Cost may remain a separate pricing-readiness signal.

---

# 2. Define provider-provenance classification

Add a typed classification such as:

```python
class ProviderProvenanceClassification(str, Enum):
    FIXTURE_ONLY = "FIXTURE_ONLY"
    REAL_EVIDENCE_ONLY = "REAL_EVIDENCE_ONLY"
    REAL_CLAUDE_ONLY = "REAL_CLAUDE_ONLY"
    REAL_EVIDENCE_AND_CLAUDE = "REAL_EVIDENCE_AND_CLAUDE"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"
```

A cycle may count toward real-provider history only when persisted evidence proves at least one required real provider was invoked successfully.

Requirements:

* fixture providers never count as real;
* positive cost does not prove a real provider;
* zero cost does not prove a fixture provider;
* real SEC/evidence with fixture Claude is classified accurately;
* real Claude with fixture evidence is classified accurately;
* mixed provider modes remain explicit;
* unknown metadata remains `UNKNOWN`;
* classification is deterministic;
* classification reasons and contributing provider records are bounded and queryable.

---

# 3. Persist provenance when necessary

Prefer existing provider-request/source/attempt records.

Add new persistence only when existing data cannot authoritatively associate provider mode with a cycle.

Potential additive table:

```text
research_cycle_provider_provenance
```

Suggested fields:

```text
cycle_id
research_run_id
symbol
provider_category
provider_name
provider_mode
is_fixture
is_real
request_or_source_id
status
observed_at
classification_version
```

Requirements:

* additive schema;
* deterministic key;
* immutable;
* idempotent;
* no credentials;
* no raw provider payload;
* no raw Claude output;
* historical records with insufficient metadata remain `UNKNOWN`, not retroactively guessed.

Do not retrofit large existing schemas when a small additive record is safer.

---

# 4. Replace real-provider readiness counting

Update controlled-soak readiness so:

```text
real_provider_cycle_count
```

comes from explicit provider-provenance classification.

Count a cycle once, even when it contains several real providers.

Also expose:

```text
fixture_only_cycle_count
real_evidence_only_cycle_count
real_claude_only_cycle_count
real_evidence_and_claude_cycle_count
mixed_cycle_count
unknown_cycle_count
```

Requirements:

* remove cost-based identity inference from this readiness path;
* retain pricing/cost validation as a separate readiness check;
* unknown cycles do not satisfy real-provider minimums;
* fixture-only cycles do not satisfy real-provider minimums;
* no provider calls occur during readiness evaluation.

---

# 5. Add authoritative cross-book verification

Create a focused module such as:

```text
src/trading_research/paper_books/cross_book_verification.py
```

Conceptual entry point:

```python
verify_cross_book_integrity(
    conn,
    *,
    as_of,
    operator_run_id=None,
    lifecycle_run_id=None,
) -> CrossBookVerificationResult
```

Suggested result:

```python
@dataclass(frozen=True)
class CrossBookVerificationResult:
    verification_id: str
    as_of: datetime
    status: str
    checks: tuple[CrossBookCheck, ...]
    violation_count: int
    policy_version: str
```

Suggested statuses:

```text
PASSED
FAILED
INSUFFICIENT_DATA
```

Do not treat absence of an exception as a persisted pass.

---

# 6. Required cross-book checks

Verify at minimum:

## Book and arm identity

* `BASELINE` book maps only to baseline experiment arm.
* `ENHANCED` book maps only to enhanced experiment arm.
* experiment assignments do not map an arm to the wrong book.

## Orders and fills

* every fill references an order in the same book;
* every order’s experiment arm matches its book;
* no order is persisted under a foreign book;
* no fill is applied to another book’s position.

## Cash

* cash-ledger order/fill references belong to the same book;
* no settlement references a foreign-book fill or order.

## Positions and lots

* every lot belongs to the same book as its position;
* quantities recomputed from fills remain book-scoped;
* no foreign-book fill contributes to a position.

## Lifecycle and reconciliation

* exit decisions and SELL intents target the same book;
* lifecycle symbol results do not reference foreign-book orders;
* reconciliation records refer to their own book.

## Idempotency

Identical identifier strings may legally exist in different books when the schema is book-scoped.

Do not flag an identifier merely because the same text appears in both books.

Flag only an actual foreign reference, mismatched arm, shared mutable state, or cross-book contribution.

---

# 7. Persist verification results

Add additive storage such as:

```text
paper_book_cross_book_verifications
paper_book_cross_book_verification_checks
```

Persist:

```text
verification_id
as_of
operator_run_id
lifecycle_run_id
status
check_name
observed
expected
source
reason
policy_version
created_at
```

Requirements:

* immutable;
* idempotent;
* bounded check output;
* explicit `PASSED`, `FAILED`, or `INSUFFICIENT_DATA`;
* zero violations plus insufficient source data must not become `PASSED`;
* verification can be rerun deterministically for the same frozen database state.

---

# 8. Feed cross-book verification into readiness

Remove the hardcoded missing-signal constant.

Controlled readiness must use the latest applicable persisted verification at or before `as_of`.

Behavior:

```text
FAILED
    → blocks readiness

INSUFFICIENT_DATA
    → permits manual soak if otherwise safe
    → blocks recurring-activation-review status

PASSED
    → satisfies the cross-book readiness gate
```

Expose:

```text
verification_id
status
violation_count
verification_as_of
policy_version
```

Do not automatically trigger activation when the result is `PASSED`.

---

# 9. Add alert-list CLI

Add:

```bash
python -m trading_research.cli shadow-alert-list \
  [--severity CRITICAL] \
  [--unresolved-only] \
  [--limit 50]
```

Requirements:

* read-only;
* bounded output;
* deterministic ordering;
* sanitized;
* include:

  * alert ID;
  * type;
  * severity;
  * created time;
  * resolved status;
  * resolved time;
  * bounded reason/message;
* no raw provider payloads;
* no credentials.

Default to a conservative bounded limit.

---

# 10. Add audited alert-resolution CLI

Add:

```bash
python -m trading_research.cli shadow-alert-resolve \
  --alert-id <id> \
  --operator <name> \
  --reason "<reason>"
```

Requirements:

* operator required;
* reason required;
* unknown alert fails closed;
* resolution is idempotent;
* first resolution is immutable;
* repeated resolution does not overwrite:

  * original operator;
  * original reason;
  * original resolved time;
* return sanitized JSON;
* do not clear pause or kill state;
* resolving an alert does not imply the underlying incident is repaired;
* no bulk “resolve all” command in this milestone.

---

# 11. Improve readiness diagnostics

Preserve one deterministic primary status.

Also expose:

```text
all_failed_checks
blocking_checks
advisory_checks
missing_checks
```

Each check should contain:

```text
name
classification
passed
observed
threshold
source
reason
```

Requirements:

* collect all failed checks, not only the first;
* primary status still follows documented priority;
* missing data remains distinct from failure;
* deterministic ordering;
* bounded output;
* no weakening of fail-closed behavior.

Avoid rewriting Milestone 9’s paper-soak evaluator unless a small additive detailed-result function is required.

---

# 12. Update operator workflow

Update `paper-soak-run` so that after lifecycle and reconciliation it:

```text
1. Runs cross-book verification
2. Persists the verification
3. Builds the soak report
4. Evaluates combined readiness using that verification
5. Persists the operator-run summary
```

Add the verification ID/status to the operator-run record and output.

Requirements:

* no activation side effect;
* one verification failure does not erase lifecycle evidence;
* failed verification blocks activation-review readiness;
* replay remains idempotent;
* no duplicate verification rows for identical frozen inputs.

---

# 13. Read-only verification CLI

Add:

```bash
python -m trading_research.cli paper-book-cross-check \
  --as-of <ISO-8601>
```

Optional:

```text
--operator-run-id
--lifecycle-run-id
```

Requirements:

* no network call;
* deterministic;
* persists or loads the deterministic verification result according to repository conventions;
* sanitized JSON;
* disabled paper-book configuration fails closed when appropriate.

---

# 14. Tests

Add focused tests for:

## Provider provenance

* fixture evidence + fixture Claude;
* real evidence + fixture Claude;
* fixture evidence + real Claude;
* real evidence + real Claude;
* mixed providers;
* missing metadata;
* positive cost does not imply real;
* zero cost does not imply fixture;
* one cycle counted only once.

## Cross-book verification

* clean books pass;
* assignment arm/book mismatch fails;
* fill/order book mismatch fails;
* cash foreign-reference mismatch fails;
* lot/position mismatch fails;
* lifecycle exit/order mismatch fails;
* insufficient data remains explicit;
* same identifier text in two correctly isolated books does not fail;
* deterministic verification ID;
* idempotent persistence.

## Alert operations

* list unresolved critical alerts;
* resolved alerts excluded when requested;
* resolution requires operator and reason;
* unknown alert fails;
* first resolution immutable;
* repeated resolution idempotent;
* alert resolution does not change pause/kill state.

## Readiness diagnostics

* all simultaneous failures returned;
* deterministic primary status;
* missing checks separated;
* failed cross-book verification blocks;
* insufficient cross-book evidence caps readiness;
* passed verification allows activation-review status when every other gate passes;
* unknown provider history blocks real-provider minimum;
* cost remains a separate pricing signal.

## Operator workflow

* verification runs after lifecycle;
* verification ID persisted on operator run;
* replay idempotent;
* verification failure preserved in output;
* no activation side effect.

---

# 15. Offline integration test

Add one small deterministic integration test:

```text
persistent test database
→ fixture-only cycle
→ explicit provider provenance
→ controlled paper lifecycle
→ clean isolated books
→ persisted cross-book verification PASSED
→ resolved historical CRITICAL alert
→ paper-soak-run
→ all readiness checks returned
→ provider minimum remains unmet for fixture-only history
→ replay is idempotent
→ no network or live execution
```

Add a second case:

```text
inject an arm/book or foreign-reference violation
→ cross-book verification FAILED
→ readiness blocked
→ lifecycle evidence remains persisted
→ no activation side effect
```

Add a third minimal provider case proving:

```text
real-provider metadata with zero cost
→ counts as real provider
```

Do not make a real provider call.

---

# 16. Documentation

Create:

```text
docs/milestone9-2-soak-evidence-integrity.md
docs/runbooks/soak-evidence-and-alert-operations.md
```

Update `docs/milestone9-1-controlled-soak-readiness.md` with a short pointer only.

Document:

* provider-provenance classification;
* why cost is not provider identity;
* cross-book checks;
* `PASSED`/`FAILED`/`INSUFFICIENT_DATA`;
* alert-list and alert-resolution commands;
* immutable resolution semantics;
* detailed readiness output;
* operator workflow;
* activation remains advisory;
* no recurring deployment.

Do not rewrite prior milestone documents.

---

# Deferred items

Keep out of Milestone 9.2:

```text
unattended recurring activation
launchd installation
external paper broker
per-book paper_runtime subprocess pool
partial fills
trailing stops
live trading
automated promotion
remaining corporate actions
dividend entitlement correction
unsupported_claim_rate correction
general duplicate-scheduler detection
```

---

# Required test execution

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

Milestone 9.2 is complete when:

1. Existing 1,486 tests still pass.
2. Existing 33 paper-runtime tests still pass.
3. Real-provider history no longer uses cost as provider identity.
4. Fixture-only cycles never satisfy real-provider minimums.
5. Zero-cost real-provider metadata can satisfy real-provider classification.
6. Unknown provider provenance remains explicit.
7. Cross-book integrity is verified and persisted.
8. A clean verification can return `PASSED`.
9. Violations return `FAILED`.
10. Insufficient evidence remains `INSUFFICIENT_DATA`.
11. Same scoped identifier text in different books is not falsely flagged.
12. Cross-book verification feeds readiness.
13. Failed verification blocks activation-review readiness.
14. Insufficient verification evidence caps readiness.
15. Alert listing is bounded and sanitized.
16. Alert resolution requires operator and reason.
17. Alert-resolution audit fields are immutable.
18. Alert resolution never changes pause or kill state.
19. Readiness returns all failed checks.
20. Primary readiness status remains deterministic.
21. Operator runs persist verification identity and status.
22. Reprocessing remains idempotent.
23. No network or broker call occurs.
24. No automatic activation occurs.
25. No live execution path exists.
26. Documentation matches implementation.


---

# Final response

Keep the final response concise.

Report only:

1. Baseline and final tests.
2. Files created and modified.
3. Provider-provenance classification.
4. Cross-book verification checks and status.
5. Alert CLI commands.
6. Readiness diagnostic changes.
7. Operator workflow changes.
8. Idempotency proof.
9. Current readiness result.
10. Safety confirmation.
11. Deferred items.

Include a compact table:

```text
Requirement → implementation → test
```

Use labels:

```text
SOAK-EVIDENCE-INTEGRITY
AUTHORITATIVE-PROVIDER-PROVENANCE
CROSS-BOOK-VERIFIED
ALERT-AUDITABLE
ALL-READINESS-FAILURES-VISIBLE
IDEMPOTENT
PAPER-BOOK-ISOLATED
ADVISORY-ONLY
LIVE-TRADING-NOT-IMPLEMENTED
RECURRING-DEPLOYMENT-NOT-ACTIVATED
```

Commit and push the changes
