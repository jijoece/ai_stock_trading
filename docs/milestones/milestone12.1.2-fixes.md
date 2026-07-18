# Milestone 12.1.2 — Model-Provider Ownership and Fail-Closed Retry Closure

Work directly in the latest `main` branch of:

```text
jijoece/ai_stock_trading
```

Validate and fix only the five high-priority safety issues listed below.

Inspect the current implementation before modifying it. Reproduce each issue with a focused offline regression test. Do not make unrelated refactors.

Do not commit or push unless explicitly requested.

---

## Token-efficiency requirements

Keep the investigation narrow:

* inspect only files involved in research attempts, attempt control, provider-health policy, model-provider health, scheduler correlation, and migrations;
* use targeted searches instead of reading broad directory trees;
* run focused tests while developing;
* run the full suite only once after focused tests pass;
* create only a concise scratchpad and implementation report;
* stop investigating a finding once it is proven already fixed with adequate regression coverage;
* do not rewrite unrelated documentation.

Prefer the smallest safe patch.

---

# Safety boundaries

Preserve all existing boundaries:

```text
research-only operation
local simulation by default
scheduled research never submits orders
external paper execution disabled
live trading unavailable
```

Do not:

* enable any broker submission;
* invoke real Codex, Claude Code, Anthropic, Alpaca, SEC, Reddit, or other services;
* use real credentials;
* add provider fallback;
* weaken fail-closed behavior;
* persist prompts, raw provider output, credentials, environment values, or unrestricted exception text.

All tests must remain offline.

---

# Minimal scratchpad

Create:

```text
.codex/scratchpads/milestone12-1-2-model-provider-ownership.md
```

Track only:

```markdown
# Milestone 12.1.2

## Baseline
- Commit:
- Branch:
- Main tests:
- Paper-runtime tests:
- Safety Pyright:

## Findings
| ID | Classification | Root cause | Fix | Tests |
|---|---|---|---|---|

## Schema changes

## Final results

## Remaining blockers
```

Use:

```text
CONFIRMED
PARTIALLY_FIXED
ALREADY_FIXED
NOT_REPRODUCIBLE
FIXED
```

---

# 1. Persist exact scheduler ownership on research attempts

## Problem

Model-provider health currently associates attempts with a scheduler run by joining:

```text
research_attempts
→ shadow_role_budget_checks
```

using:

```text
research_run_id
role
attempt_number
```

The scheduled research run ID is deterministic from:

```text
research_cycle_id + symbol
```

Two scheduler invocations revisiting the same cycle can therefore reuse the same:

```text
research_run_id
role
attempt_number
```

A later scheduler run may incorrectly inherit model attempts from an earlier run.

## Required fix

Persist immutable scheduler ownership directly on every scheduled research attempt.

Preferred additive fields:

```text
scheduler_run_id
research_cycle_id
attempt_control_check_id
correlation_mode
```

At minimum, `scheduler_run_id` must be persisted directly on the attempt.

For scheduled attempts:

```text
scheduler_run_id must be nonempty
correlation_mode = SCHEDULED
```

For manual attempts:

```text
scheduler_run_id may be null
correlation_mode = MANUAL or RESEARCH_RUN
```

Do not infer scheduler ownership through a join on reusable research identifiers.

## Query correction

Replace the model-health ownership query with an exact query such as:

```sql
SELECT ...
FROM research_attempts
WHERE correlation_mode = 'SCHEDULED'
  AND scheduler_run_id = ?
ORDER BY created_at, attempt_id
```

The query must not depend on:

```text
research_run_id
role
attempt_number
time window
symbol
```

for ownership.

Those fields may be retained as validation or provenance.

## Migration

Use an additive migration.

For existing attempts:

* leave `scheduler_run_id` null;
* mark correlation as legacy or unknown;
* do not attempt to infer scheduler ownership from historical budget-check rows;
* exclude uncorrelated legacy rows from run-specific operational health.

## Tests

Prove:

1. scheduler run A sees only A’s attempts;
2. scheduler run B sees only B’s attempts;
3. A and B may share the same research-cycle ID;
4. A and B may share the same deterministic research-run ID;
5. a manual attempt with the same research-run ID does not enter scheduled health;
6. legacy attempts with null scheduler ownership are excluded;
7. retry attempts retain the correct scheduler-run ID;
8. budget-gated attempts are not counted as provider invocations;
9. migration preserves existing attempt rows;
10. ordering is deterministic.

The critical regression test must use the same:

```text
research_run_id
role
attempt_number
```

for two different scheduler runs.

---

# 2. Partition model-provider health by provider and model

## Problem

Current model-provider health aggregates all research attempts in one scheduler run and persists one global hysteresis scope.

This can allow one provider’s success to recover another provider’s failure streak.

Example:

```text
Codex fails repeatedly
→ configuration switches to deterministic or Anthropic
→ successful attempts enter the same model-provider dimension
→ Codex failure streak recovers incorrectly
```

## Required fix

Model-provider health must be evaluated for the exact expected provider and model used by the scheduled cycle.

Pass explicit expected identity:

```text
expected_provider
expected_model
provider_configuration_hash
```

Filter attempts before calculating health:

```text
attempt.provider == expected_provider
attempt.model_name == expected_model
```

When model aliases or resolved-model fields exist, follow the repository’s existing provenance policy and document which identifier controls health partitioning.

Do not combine:

```text
codex
claude_code
anthropic
deterministic
scripted
```

in the same operational health evidence.

## Hysteresis scope

Use a provider-specific scope such as:

```text
MODEL_PROVIDER_FAILURE:<provider>:<model>:<configuration-hash>
```

Generate it deterministically and safely.

Normalize values before using them in a scope.

Do not place credentials or executable paths in the scope.

A configuration or model change must create an explicit health-policy boundary instead of recovering an old provider’s streak.

## Fixture providers

For:

```text
deterministic
scripted
fixture-only providers
```

production model-provider health should be:

```text
NOT_APPLICABLE
```

or use a separate non-production scope.

Fixture success must never recover Codex, Claude Code, or Anthropic failure state.

## Tests

Prove:

1. Codex attempts affect only Codex health;
2. Claude Code attempts affect only Claude Code health;
3. Anthropic attempts affect only Anthropic health;
4. deterministic success cannot recover Codex failure;
5. changing models creates a separate health scope;
6. changing the provider configuration hash creates a separate policy boundary;
7. attempts from another provider are excluded rather than counted as success;
8. an empty expected-provider sample is insufficient, not healthy;
9. scheduler replay remains idempotent within the exact provider scope.

---

# 3. Use the centralized classifier for immediate pausing

## Problem

The attempt controller currently checks only whether a code appears in the named structural-code allowlist, and only for selected CLI providers.

It does not consistently call:

```python
classify_model_provider_failure(
    failure_code,
    failure_retryable,
)
```

Consequences include:

* Anthropic non-retryable provider failures may not pause immediately;
* unknown non-retryable Codex or Claude failures may not pause immediately;
* generic non-retryable provider client errors may continue to later roles or symbols;
* behavior differs between immediate attempt handling and end-of-cycle model health.

## Required fix

Use the centralized classifier in `after_attempt()` for every real model provider.

Conceptually:

```python
classification = classify_model_provider_failure(
    attempt.failure_code,
    attempt.failure_retryable,
)

if classification == MODEL_PROVIDER_FAILURE_STRUCTURAL:
    request persistent provider-health pause
```

Apply this to:

```text
codex
claude_code
anthropic
```

Use the repository’s provider registry rather than duplicating provider-name lists where possible.

Do not immediately pause for deterministic or scripted providers unless they are explicitly running under a production provider policy.

## Required behavior

Structural failure:

```text
persistent pause requested immediately
next provider attempt blocked by before_attempt
no later role or symbol provider call permitted
```

Transient failure:

```text
no immediate pause
bounded retry remains possible
persistent model-provider hysteresis handles repeated cycles
```

Do not classify using messages in the attempt controller.

## Tests

Prove:

1. unknown non-retryable Codex failure pauses;
2. unknown non-retryable Claude Code failure pauses;
3. Anthropic `PROVIDER_CLIENT_ERROR` pauses;
4. authentication, quota, invalid configuration, unsupported model, and protocol failures pause;
5. timeout does not immediately pause;
6. rate limiting does not immediately pause;
7. transient server failure does not immediately pause;
8. the next role is blocked after a structural failure;
9. the next symbol is blocked after a structural failure;
10. changing human-readable messages does not change the result.

---

# 4. Treat code-less non-retryable failures as structural

## Problem

The centralized classifier treats:

```text
failure_retryable = false or null
```

as structural.

But model-health aggregation currently sets `structural_failure=True` only when a structural failure also has a nonempty code.

Example:

```text
failure_code = null
failure_retryable = false

classifier → STRUCTURAL
aggregator → structural_failure false
```

## Required fix

Calculate structural status independently of whether a code exists.

Conceptually:

```python
classification = classify_model_provider_failure(code, retryable)

if classification == STRUCTURAL:
    structural_failure_count += 1
    structural_codes.append(
        code or "UNCLASSIFIED_STRUCTURAL_FAILURE"
    )
```

Add bounded evidence fields such as:

```text
structural_failure_count
transient_failure_count
unclassified_structural_failure_count
structural_failure_codes
```

The placeholder must be a stable taxonomy value, not free text.

Suggested code:

```text
UNCLASSIFIED_STRUCTURAL_FAILURE
```

Add it to the central taxonomy if the repository requires registered failure codes.

Do not fabricate a provider-specific code.

## Tests

Prove:

1. null code plus `retryable=false` is structural;
2. null code plus `retryable=null` fails closed to structural;
3. unknown code plus `retryable=false` is structural;
4. unknown code plus `retryable=true` is transient;
5. structural Boolean does not depend on a truthy code;
6. the placeholder is persisted safely;
7. raw failure messages remain excluded.

---

# 5. Retry authorization must consider every failure

## Problem

The orchestration now selects one deterministic primary failure and uses that primary failure’s `retryable` value to decide whether to retry.

Primary selection is useful for attempt summaries, but it must not be the sole retry authority.

An attempt can potentially contain:

```text
unknown-stage non-retryable failure
+
known retryable claim or schema failure
```

If the retryable failure becomes primary, another provider call may occur despite the non-retryable failure.

## Required fix

Separate:

```text
primary failure for reporting
```

from:

```text
retry eligibility for control flow
```

Retry eligibility must be conservative:

```python
attempt_retryable = bool(failures) and all(
    failure.retryable is True
    for failure in failures
)
```

Any failure with:

```text
retryable = false
retryable = null
```

must stop the retry loop.

Do not treat an empty failure set as retryable.

Persist:

```text
primary failure code
attempt-level retryable decision
```

if the existing attempt schema supports both. If not, keep `failure_retryable` as the conservative attempt-level decision rather than only the selected primary failure’s value.

## Primary selector correction

Unknown non-retryable failures must rank conservatively.

Do not place them below known retryable claim-validation failures.

A safe ordering is:

```text
known structural provider failure
unknown non-retryable failure
known non-retryable contract failure
budget or kill gate
retryable provider failure
retryable schema failure
claim validation
diagnostic
```

## Tests

Prove:

1. one non-retryable failure plus one retryable failure does not retry;
2. list ordering does not change retry eligibility;
3. unknown non-retryable failure outranks retryable claim failure;
4. all retryable failures permit retry;
5. null retryability prevents retry;
6. persisted attempt retryability matches actual control flow;
7. primary reporting selection remains deterministic;
8. automatic pause still blocks the next provider call.

---

# Cross-cutting integration scenarios

## Scenario A — Same research run reused

```text
scheduler run A
  research_cycle_id = cycle-X
  research_run_id = cycle-X-AAPL
  Codex succeeds

scheduler run B
  research_cycle_id = cycle-X
  research_run_id = cycle-X-AAPL
  Codex fails structurally

Expected:
  run B health sees only run B attempt
  run A success cannot hide run B failure
```

## Scenario B — Provider switch

```text
run A uses Codex and fails
run B uses deterministic provider and succeeds
run C uses Anthropic and succeeds

Expected:
  Codex streak remains unchanged by B and C
```

## Scenario C — Immediate Anthropic failure

```text
Anthropic returns non-retryable client/configuration error
→ centralized classifier says STRUCTURAL
→ pause requested immediately
→ no next role or symbol model call
```

## Scenario D — Missing failure code

```text
failed attempt
failure_code = null
failure_retryable = false
→ structural model-provider failure
→ immediate pause
→ audit uses bounded placeholder
```

## Scenario E — Mixed failures

```text
attempt has:
  retryable claim failure
  unknown non-retryable provider failure

Expected:
  deterministic primary = non-retryable failure
  attempt_retryable = false
  no second provider call
```

---

# Minimal migrations

Add only the migrations required for exact attempt ownership.

Likely additions to `research_attempts`:

```text
scheduler_run_id TEXT
research_cycle_id TEXT
correlation_mode TEXT
attempt_control_check_id TEXT
```

Add an index supporting:

```text
scheduler_run_id
provider
model_name
created_at
```

Requirements:

* additive and idempotent;
* preserve all existing rows;
* legacy rows remain null or explicitly legacy;
* do not infer historical scheduler ownership;
* fresh and upgraded databases produce the same final schema;
* no provider call occurs inside a database transaction.

---

# Focused files

Inspect only as needed:

```text
src/trading_research/research/orchestration.py
src/trading_research/research/failure_taxonomy.py
src/trading_research/research/model_provider_health_policy.py
src/trading_research/shadow/attempt_controller.py
src/trading_research/shadow/model_provider_health.py
src/trading_research/shadow/health.py
src/trading_research/shadow/health_hysteresis.py
src/trading_research/shadow/scheduler.py
src/trading_research/storage/research_schema.py
src/trading_research/storage/research_repositories.py
src/trading_research/storage/shadow_operations_repositories.py
src/trading_research/storage/schema_version.py
```

Do not inspect unrelated broker or trading-engine modules unless a test failure demonstrates a direct dependency.

---

# Validation

Run focused tests first.

After focused tests pass:

```bash
pytest tests/ -q --tb=short
```

Run credential-free:

```bash
env \
  -u OPENAI_API_KEY \
  -u CODEX_API_KEY \
  -u CODEX_ACCESS_TOKEN \
  -u OPENAI_ACCESS_TOKEN \
  -u ANTHROPIC_API_KEY \
  -u ANTHROPIC_AUTH_TOKEN \
  -u CLAUDE_CODE_OAUTH_TOKEN \
  -u ALPACA_API_KEY \
  -u ALPACA_API_SECRET \
  pytest tests/ -q --tb=short
```

Then:

```bash
(
  cd paper_runtime
  pytest tests/ -q --tb=short
)

python -m compileall -q src paper_runtime
pyright --project pyright-safety.json
git diff --check
git status --short
```

Expand `pyright-safety.json` only for production modules directly changed by this patch.

Run focused tests repeatedly:

```bash
for i in {1..10}; do
  pytest \
    tests/unit/test_*attempt_control*.py \
    tests/unit/test_*model_provider_health*.py \
    tests/unit/test_*select_primary_failure*.py \
    tests/unit/test_*scheduler_run*.py \
    tests/unit/test_*migration*.py \
    -q || exit 1
done
```

Use actual filenames in the repository.

No real provider or broker call may occur.

---

# Minimal documentation

Create or update only:

```text
docs/milestone12-1-2-model-provider-ownership.md
docs/INDEX.md
```

Report:

```text
starting commit
finding classifications
schema changes
files changed
tests
remaining blockers
operational readiness
```

Do not rewrite unrelated milestones or runbooks.

---

# Acceptance criteria

Complete only when:

1. every scheduled attempt has exact scheduler-run ownership;
2. operational model health no longer infers ownership through reusable research IDs;
3. two scheduler runs sharing a research-run ID remain isolated;
4. model health is partitioned by provider and model;
5. deterministic or scripted success cannot recover a real provider’s failure streak;
6. configuration changes create explicit health boundaries;
7. immediate pause uses the centralized classifier;
8. Anthropic structural failures pause immediately;
9. unknown non-retryable failures pause immediately;
10. code-less non-retryable failures are structural;
11. structural status does not depend on the presence of a failure code;
12. retry eligibility considers every failure;
13. any non-retryable or unknown-retryability failure stops retries;
14. primary failure selection remains deterministic;
15. migrations preserve existing data;
16. main and paper-runtime tests pass;
17. safety Pyright passes;
18. no real provider or broker call occurs;
19. execution and scheduling defaults remain disabled;
20. no commit or push occurs unless explicitly requested.

---

# Final response

Report only:

1. starting commit and branch;
2. finding classifications;
3. schema and migration changes;
4. exact scheduler-ownership changes;
5. provider/model health partitioning;
6. centralized immediate-pause changes;
7. code-less structural-failure handling;
8. retry-authorization changes;
9. tests and results;
10. remaining limitations;
11. confirmation that no real provider or broker call occurred;
12. confirmation that execution defaults remain disabled;
13. confirmation that no commit or push occurred.

End with:

| Capability                      | Status                              |
| ------------------------------- | ----------------------------------- |
| Deterministic research          | READY / LIMITED                     |
| Manual Codex research           | READY / SUPERVISED_ONLY / NOT_READY |
| Manual Claude Code research     | READY / SUPERVISED_ONLY / NOT_READY |
| Manual Anthropic research       | READY / SUPERVISED_ONLY / NOT_READY |
| Unattended scheduled research   | READY / KEEP_DISABLED               |
| External Alpaca paper execution | KEEP_DISABLED                       |
| Live trading                    | NOT_IMPLEMENTED                     |
