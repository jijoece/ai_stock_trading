# Milestone 12.1.1 — Remaining Provider and Health Safety Fixes

Work in the current `jijoece/ai_stock_trading` repository from the latest `main` containing PR #19.

Validate and fix only the high-priority issues listed below.

Do not assume every finding still exists. Inspect the current implementation, reproduce each issue with a focused test, and modify code only when confirmed.

Do not commit or push unless explicitly requested.

---

## Token-efficiency requirements

Minimize context and tool usage:

* inspect only files directly involved in each finding;
* use targeted searches instead of reading large directories;
* batch related file reads and test runs;
* do not regenerate or rewrite unrelated documentation;
* do not repeat repository architecture already documented;
* do not perform broad refactoring;
* run focused tests during implementation, then one final full suite;
* keep the scratchpad and final report concise;
* stop investigating a finding once it is proven already fixed with an adequate regression test.

Prefer the smallest safe patch.

---

## Safety boundaries

Preserve all existing safety controls:

```text
research only
local simulation by default
scheduled research does not submit orders
external paper execution disabled
live trading unavailable
```

Do not:

* enable paper or live order submission;
* invoke real Codex, Claude Code, Anthropic, Alpaca, SEC, Reddit, or other network services;
* use real credentials;
* weaken fail-closed behavior;
* add provider fallback;
* persist prompts, raw subprocess output, credentials, or unrestricted exception messages.

All tests must be offline using fake providers, fake executables, fixtures, and temporary SQLite databases.

---

## Minimal scratchpad

Create:

```text
.codex/scratchpads/milestone12-1-1-safety-fixes.md
```

Track only:

```markdown
# Milestone 12.1.1 Safety Fixes

## Baseline
- Commit:
- Branch:
- Main tests:
- Paper-runtime tests:
- Safety Pyright:

## Findings
| ID | Classification | Root cause | Fix | Tests |
|---|---|---|---|---|

## Files changed

## Final results

## Remaining blockers
```

Classifications:

```text
CONFIRMED
PARTIALLY_FIXED
ALREADY_FIXED
NOT_REPRODUCIBLE
FIXED
```

---

# 1. Honor provider error retryability

## Problem

Orchestration currently catches several error classes as retryable and may continue even when:

```python
exc.retryable is False
```

Examples include:

```text
CODEX_USAGE_METADATA_MISSING
CODEX_REASONING_TOKENS_INVALID
non-retryable malformed output
non-retryable provider contract failures
```

This may cause another provider invocation after the attempt controller has already paused operations.

## Required fix

Retry based on the structured `retryable` value, not only the exception class.

Conceptually:

```python
except POTENTIALLY_RETRYABLE_ERRORS as exc:
    persist_failure(exc)
    attempt_controller.after_attempt(...)

    if not exc.retryable:
        break

    continue
```

Before every provider call, `before_attempt()` must verify the current persistent:

```text
pause state
kill state
budget state
```

An automatic pause created after attempt one must block attempt two.

## Tests

Prove:

1. non-retryable failure produces exactly one provider call;
2. retryable timeout can retry;
3. missing usage metadata does not retry;
4. invalid reasoning-token metadata does not retry;
5. authentication or quota failure does not retry;
6. automatic pause after attempt one blocks attempt two;
7. manual pause and kill block the first call;
8. persisted `failure_retryable` matches actual behavior.

---

# 2. Select structured failures deterministically

## Problem

An attempt can have multiple structured failures, but the attempt-level operational code may currently use whichever failure appears first.

Operational behavior must not depend on list ordering.

## Required fix

Create a deterministic selector such as:

```python
select_primary_failure(failures)
```

Use an explicit priority policy:

```text
structural provider failure
non-retryable provider contract failure
budget or kill failure
retryable provider failure
schema validation
claim validation
diagnostic failure
```

The selected failure must be stable when the input list is reordered.

Do not prioritize using human-readable messages.

Persist the policy version if the repository already versions similar control policies.

## Tests

Prove:

1. reversing failure order produces the same primary code;
2. structural provider failure outranks claim validation;
3. non-retryable protocol failure outranks retryable validation;
4. unknown provider codes fail closed;
5. raw messages do not affect selection.

---

# 3. Exclude `NOT_APPLICABLE` from hysteresis recovery

## Problem

Fixture-only provider health can produce:

```text
NOT_APPLICABLE
```

The current generic qualification helper may treat every status except `INSUFFICIENT_DATA` as qualified, allowing fixture cycles to count as healthy recovery evidence.

## Required fix

Only these statuses are qualified:

```text
PASS
WARNING
FAIL
```

These must move neither failure nor recovery streak:

```text
INSUFFICIENT_DATA
NOT_APPLICABLE
UNKNOWN
DISABLED
```

A fixture-only cycle must never clear a production provider-failure streak.

## Tests

Prove:

1. `PASS`, `WARNING`, and `FAIL` are qualified;
2. `INSUFFICIENT_DATA` is unqualified;
3. `NOT_APPLICABLE` is unqualified;
4. repeated fixture cycles do not advance recovery;
5. fixture cycles do not advance failure;
6. idempotent replay remains unchanged.

---

# 4. Use scheduler-run identity for health hysteresis

## Problem

Provider telemetry is scoped by:

```text
research_cycle_id + scheduler_run_id
```

but hysteresis idempotency may still use only `research_cycle_id`.

Two scheduler runs for the same deterministic cycle can therefore be treated as one replay.

## Required fix

For scheduled operational health:

```text
evaluation identity = scheduler_run_id
grouping provenance = research_cycle_id
```

Hysteresis idempotency must use:

```text
scope
scheduler_run_id
policy hash
```

Store `research_cycle_id` separately for reporting.

Do not use one field for both operational-attempt identity and research-cycle grouping.

Use an additive migration only if required.

## Tests

Prove:

1. run A advances a failure streak;
2. run B for the same research cycle advances it again;
3. replaying run B does not double-count;
4. a later successful run advances recovery;
5. manual and scheduled evaluations cannot collide;
6. migration preserves existing history.

---

# 5. Required-category insufficient data must not pass

## Problem

A required provider category can be:

```text
INSUFFICIENT_DATA
```

while aggregate provider success is 100%, allowing the overall provider-health dimension to pass.

Example:

```text
SEC: enough successful requests
Alpaca: one successful request
market-data minimum_requests: three
```

The market-data category is insufficient and must not become healthy.

## Required fix

Apply this combined policy:

```text
required category MISSING → FAIL
required category FAIL → FAIL
required category INSUFFICIENT_DATA → overall provider dimension INSUFFICIENT_DATA
all required categories PASS → evaluate remaining provider metrics
```

`INSUFFICIENT_DATA` must advance neither failure nor recovery streak.

Required-category sample floors and success thresholds must come from strict frozen configuration, not only Python defaults.

A minimal configuration shape is sufficient:

```yaml
provider_health:
  policy_version: provider-coverage/v2
  required_categories:
    market_data:
      providers: [alpaca-data]
      minimum_requests: 1
      minimum_success_rate: 1.0
    corporate_filings:
      providers: [sec-edgar]
      minimum_requests: 1
      minimum_success_rate: 1.0
```

Reject invalid numbers, unknown categories, empty provider lists, and unknown fields.

## Tests

Prove:

1. required category PASS;
2. required category FAIL;
3. required category MISSING;
4. required category INSUFFICIENT_DATA;
5. insufficient category prevents overall PASS;
6. optional-provider failures do not dilute required health;
7. SEC success cannot hide Alpaca failure;
8. policy changes alter the policy hash.

---

# 6. Enforce configured Codex minimum version

## Problem

Codex preflight may validate only the repository’s supported adapter range while ignoring:

```text
codex.minimum_version
```

Example:

```text
configured minimum = 0.144.9
installed version = 0.144.5
adapter supports 0.144.5
```

This must fail.

## Required fix

Codex is ready only when:

```text
installed version >= configured minimum
AND
installed version matches an explicitly supported adapter contract
```

Prefer the narrowest validated compatibility policy.

If only `0.144.5` was actually validated, accept only that version unless fixtures or runtime evidence justify additional versions.

Fail before inference.

Persist safe provenance:

```text
installed version
configured minimum
adapter version
```

## Tests

Prove:

1. installed version below configured minimum fails;
2. installed version equal to supported minimum passes;
3. installed version outside the adapter policy fails;
4. malformed and prerelease versions fail;
5. no inference occurs after version-preflight failure.

---

# 7. Add independent model-provider health

## Problem

Current hysteresis dimensions cover evidence providers, retry exhaustion, and unsupported claims, but not the model provider itself.

Add:

```text
MODEL_PROVIDER_FAILURE
```

Model-provider health must come from persisted research attempts for the current scheduler run, not evidence-provider request rows.

Track:

```text
attempt count
success count
failure count
retryable failures
non-retryable failures
timeouts
rate limits
authentication failures
quota failures
configuration failures
protocol failures
missing usage failures
```

## Policy

Immediate structural failures should include verified codes for:

```text
authentication failure
quota or credit exhaustion
unsupported version
unsupported model
invalid configuration
schema or CLI contract rejection
missing required usage metadata
invalid reasoning-token contract
```

Transient failures should use hysteresis:

```text
timeout
rate limit
network failure
temporary service failure
retryable malformed output
```

Centralize this code-to-health policy in one module. Do not duplicate allowlists across the attempt controller and scheduler.

Unknown non-retryable provider errors must fail closed.

Add strict hysteresis configuration for the model-provider dimension.

## Tests

Prove:

1. authentication and unsupported model pause immediately;
2. invalid configuration and usage-contract failures pause immediately;
3. one transient timeout does not immediately pause;
4. repeated transient failures reach the configured pause threshold;
5. healthy model attempts advance only model-provider recovery;
6. evidence-provider success does not clear model-provider failures;
7. scheduler runs see only their own model attempts;
8. replay is idempotent.

---

# Minimal persistence changes

Add only migrations required for:

```text
scheduler-run health evaluation identity
model-provider health evidence
deterministic primary failure, if not already representable
```

Requirements:

* additive and idempotent;
* preserve PR #19 data;
* do not infer typed codes from legacy free text;
* state and history updates are atomic;
* no network or provider call inside a database transaction.

Add one prior-schema migration test using the PR #19 schema.

---

# Validation

Run focused tests while implementing.

After all fixes:

```bash
pytest tests/ -q --tb=short
```

Run with credentials removed:

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
  -u APCA_API_KEY_ID \
  -u APCA_API_SECRET_KEY \
  pytest tests/ -q --tb=short
```

Run:

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

Do not attempt broad whole-project type cleanup.

Run the focused retry and health tests repeatedly:

```bash
for i in {1..10}; do
  pytest \
    tests/unit/test_*retry*.py \
    tests/unit/test_*attempt_controller*.py \
    tests/unit/test_*health*.py \
    tests/unit/test_*scheduler_run*.py \
    tests/unit/test_*codex_version*.py \
    -q || exit 1
done
```

Use the actual filenames present after implementation.

---

# Minimal documentation

Create or update only:

```text
docs/milestone12-1-1-provider-health-closure.md
docs/INDEX.md
```

The report should contain:

```text
starting commit
finding classifications
files changed
migrations
tests
remaining blockers
operational readiness
```

Do not rewrite unrelated milestones or runbooks.

---

# Acceptance criteria

Complete only when:

1. non-retryable provider failures never retry;
2. persistent pause and kill state block provider calls;
3. primary failure selection is deterministic;
4. `NOT_APPLICABLE` and `INSUFFICIENT_DATA` move neither hysteresis streak;
5. fixture cycles cannot satisfy production recovery;
6. scheduler-run identity controls operational hysteresis;
7. two runs for one cycle can each advance health once;
8. required-category insufficient data cannot pass;
9. required-category policy is configuration-driven;
10. configured Codex minimum is enforced;
11. unvalidated Codex versions fail before inference;
12. model-provider health is independent;
13. structural model-provider failures pause immediately;
14. transient model-provider failures use hysteresis;
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
3. files changed;
4. migrations;
5. retry and pause-barrier changes;
6. hysteresis identity and qualification changes;
7. required-provider policy changes;
8. Codex version changes;
9. model-provider health changes;
10. tests and results;
11. remaining limitations;
12. confirmation that no real provider or broker call occurred;
13. confirmation that execution defaults remain disabled;
14. confirmation that no commit or push occurred.

End with:

| Capability                      | Status                              |
| ------------------------------- | ----------------------------------- |
| Deterministic research          | READY / LIMITED                     |
| Manual Codex research           | READY / SUPERVISED_ONLY / NOT_READY |
| Manual Claude Code research     | READY / SUPERVISED_ONLY / NOT_READY |
| Local simulated paper trading   | READY / LIMITED                     |
| Unattended scheduled research   | READY / KEEP_DISABLED               |
| External Alpaca paper execution | KEEP_DISABLED                       |
| Live trading                    | NOT_IMPLEMENTED                     |
