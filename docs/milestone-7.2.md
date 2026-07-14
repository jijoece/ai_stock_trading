Continue implementation of my existing AI-driven trading-desk repository.

Implement:

# Milestone 7.2 — Shadow health diagnostics and activation readiness

Milestones 1 through 7.1 are complete. Do not redesign the architecture, restart Milestone 7, or begin broad Milestone 8 work.

Milestone 7.1 successfully completed:

* corporate-status integration;
* completeness gating;
* per-attempt role-budget enforcement;
* actual token/cost settlement;
* model-specific pricing;
* cycle telemetry;
* health/readiness telemetry;
* real SEC validation;
* real Claude shadow validation;
* zero paper submissions;
* zero enhanced executions.

The remaining issue is that the successful real SEC + Claude shadow cycle returned:

```text
health_status=PAUSE_REQUIRED
```

The exact health reasons and triggering flags were not captured.

Your task is to instrument health diagnostics, perform exactly one bounded real rerun, identify the exact cause, reproduce it offline, fix only demonstrated defects, and determine activation readiness.

Do not weaken health policy merely to produce `HEALTHY`.

---

## Mandatory source review

Before editing, read:

```text
.claude/scratchpads/milestone7-progress.md
.claude/scratchpads/milestone7-1-progress.md

docs/milestone7-production-shadow-operations.md
docs/milestone7-1-shadow-integration-closure.md
docs/adr/0005-production-shadow-operations-boundary.md
docs/runbooks/shadow-operations.md
docs/runbooks/shadow-incident-response.md
```

Inspect at minimum:

```text
src/trading_research/shadow/health.py
src/trading_research/shadow/readiness.py
src/trading_research/shadow/scheduler.py
src/trading_research/shadow/config.py
src/trading_research/shadow/pause.py
src/trading_research/shadow/alerts.py
src/trading_research/shadow/budget.py
src/trading_research/shadow/attempt_controller.py

src/trading_research/research/cycle_telemetry.py
src/trading_research/research/scheduled_cycle.py
src/trading_research/research/orchestration.py

src/trading_research/storage/shadow_operations_repositories.py
src/trading_research/storage/shadow_alerts_schema.py
src/trading_research/storage/research_repositories.py

src/trading_research/cli.py

config/shadow_operations.yaml
config/research.yaml
config/research_pricing.yaml

tests/integration/test_milestone_7_1_real_validation_smoke.py
```

Use the current repository as the source of truth.

---

## Mandatory scratchpad

Before implementation edits, create:

```text
.claude/scratchpads/milestone7-2-progress.md
```

Use:

```markdown
# Milestone 7.2 Progress

Started:
Branch:
Status: STARTING

## Baseline
## Health data-flow trace
## Diagnostic instrumentation
## Real rerun
## Captured health inputs
## Exact health reasons
## Exact triggering flags
## Offline reproduction
## Root-cause classification
## Fixes
## Health persistence
## CLI diagnostics
## Pause and alert behavior
## Activation readiness
## Tests
## Real validation
## Security review
## Documentation
## Known limitations
## Final status
```

Never include credentials, `.env` contents, raw prompts, raw Claude responses, authorization headers, account IDs, or chain-of-thought.

Do not commit or push unless explicitly asked.

---

## Baseline

Verify before editing:

```text
pytest tests/ -q
Expected: 1221 passed, 13 skipped

cd paper_runtime
pytest tests/ -q
Expected: 33 passed
```

Check Git status and preserve all unrelated work.

---

# Part 1 — Trace the health calculation

Trace:

```text
run_due_shadow_cycle
→ ResearchCycleResult
→ ResearchCycleTelemetry
→ CycleHealthInputs
→ evaluate_cycle_health
→ CycleHealthResult
→ apply_health_result
→ shadow_run_summaries
→ shadow_pause_state
→ alerts
→ readiness
```

For every health input, document its source, calculation, denominator, units, threshold, missing-value behavior, and persisted representation:

```text
provider_success_rate
evidence_completeness_rate
claude_role_success_rate
retry_rate
retry_exhaustion_rate
unsupported_claim_rate
output_truncation_rate
input_tokens
output_tokens
latency_ms
cost_usd
pricing_configured
paper_reconciliation_mismatch
duplicate_prevention_violation
cycle_duration_seconds
budget_breached
```

Classify each field as:

```text
AUTHORITATIVE
DERIVED
NOT_APPLICABLE
MISSING
DEFAULTED
```

Do not change code before recording this mapping.

---

# Part 2 — Add field-level health diagnostics

Extend the health result so every evaluated dimension is explainable.

Use an immutable model equivalent to:

```python
@dataclass(frozen=True)
class HealthCheckResult:
    check_name: str
    status: str
    input_value: str | None
    input_unit: str | None
    threshold_value: str | None
    threshold_unit: str | None
    comparison: str
    applicable: bool
    pause_flag_enabled: bool
    reason: str


@dataclass(frozen=True)
class CycleHealthResult:
    status: str
    reasons: tuple[str, ...]
    triggering_flags: tuple[str, ...]
    checks: tuple[HealthCheckResult, ...]
    policy_version: str
```

Use existing conventions where possible.

Every health dimension must report one of:

```text
PASS
WARNING
FAIL
NOT_APPLICABLE
INSUFFICIENT_DATA
```

Requirements:

* explicit units;
* exact input value;
* exact threshold;
* exact comparison;
* exact pause flag;
* deterministic ordering;
* stable serialization;
* no secrets;
* no raw model content;
* missing telemetry must remain missing;
* missing telemetry must never silently become zero.

---

# Part 3 — Persist health diagnostics

Persist field-level checks.

Prefer an additive table such as:

```text
shadow_run_health_checks
```

Suggested columns:

```text
check_id
scheduler_run_id
cycle_id
check_name
check_status
input_value
input_unit
threshold_value
threshold_unit
comparison
applicable
pause_flag_enabled
reason
policy_version
evaluated_at
```

Requirements:

* stable or deterministic check ID;
* idempotent persistence;
* query by scheduler run;
* query by cycle;
* query by check name;
* no duplicate rows on resume;
* no destructive migration;
* no secrets or raw Claude content.

Continue storing the summary health status, reasons, and triggering flags in the existing run-summary path.

---

# Part 4 — Update the real smoke test before rerunning

Update:

```text
tests/integration/test_milestone_7_1_real_validation_smoke.py
```

or create a Milestone 7.2-specific real smoke test.

Before the paid rerun, ensure it captures and prints only sanitized values:

```text
scheduler_run_id
cycle_id
cycle_status
symbol_result_statuses
screening_completeness
research_completeness

health_status
health_policy_version
health_reasons
triggering_flags

all field-level health checks

provider-success numerator
provider-success denominator
evidence-completeness numerator
evidence-completeness denominator
role-success numerator
role-success denominator
retry numerator
retry denominator

attempt_count
input_tokens
output_tokens
role_latency_ms
cycle_duration_seconds

reserved_cost_usd
consumed_cost_usd
budget_breached
emergency_margin_breached

paper_reconciliation_mismatch
duplicate_prevention_violation

paper_submission_count
enhanced_execution_count
```

Never print:

* raw prompts;
* raw Claude responses;
* credentials;
* `.env`;
* authorization headers;
* raw SEC filing documents.

---

# Part 5 — Perform exactly one bounded real rerun

After diagnostic instrumentation and targeted tests pass, run exactly one real validation:

```bash
RUN_REAL_CLAUDE_SHADOW_CYCLE=true \
pytest tests/integration/test_milestone_7_1_real_validation_smoke.py \
-v -s -m claude_api
```

Use:

* one symbol;
* real SEC;
* Claude roles `bear` and `manager`;
* one attempt per role;
* configured model-specific pricing;
* explicit cost cap;
* temporary database;
* no paper submission;
* no enhanced execution;
* no launchd activation.

Do not make repeated paid calls.

A second paid run is allowed only when the first rerun fails before persisting or printing the required health diagnostics.

Record the actual sanitized output in the scratchpad.

---

# Part 6 — Identify the exact trigger

Use the captured health checks to determine precisely why the status became `PAUSE_REQUIRED`.

Investigate at minimum:

## Provider success

Verify the numerator and denominator.

Disabled providers must not enter the denominator.

A symbol that:

* screened out;
* produced no paper order;
* used shadow-only enhancement;
* lacked optional news or sentiment;

must not automatically count as a provider failure.

## Evidence completeness

Verify:

```text
COMPLETE_FOR_SCREENING
```

maps correctly.

Verify missing noncritical news or sentiment does not trigger a critical pause.

Do not confuse:

```text
screening completeness
```

with:

```text
research completeness
```

## Role success

For two successful required roles:

```text
2 / 2 = 1.0
```

Ensure the manager and analyst are not double-counted.

## Retry metrics

No retry must produce:

```text
retry_rate=0.0
retry_exhaustion_rate=0.0
```

when attempts exist.

## Latency and duration

Verify milliseconds versus seconds.

Keep separate:

```text
sum of Claude attempt latency
```

and:

```text
whole scheduler cycle duration
```

Do not compare milliseconds to a seconds threshold.

## Cost and budget

Verify:

```text
consumed cost < reserved cost
```

does not become a budget breach.

Check:

* actual cost;
* reservation;
* emergency margin;
* daily cap;
* monthly cap;
* configured safety flags.

## Configuration mapping

Ensure numeric threshold fields are not accidentally treated as Boolean enable flags.

## Missing telemetry

Missing optional data may produce `INSUFFICIENT_DATA` or `DEGRADED`, but must not become a critical failure unless explicitly required by policy.

## Duplicate/reconciliation state

Confirm the captured values rather than assuming they are false.

---

# Part 7 — Root-cause classification

Use one or more exact labels:

```text
HEALTH-INPUT BUG
RATE-DENOMINATOR BUG
UNIT-CONVERSION BUG
STATUS-MAPPING BUG
CONFIGURATION-MAPPING BUG
PERSISTENCE BUG
EXPECTED POLICY PAUSE
MISSING TELEMETRY
TEST DEFECT
DOCUMENTATION DEFECT
NO DEFECT
```

For every cause record:

* field;
* actual value;
* threshold;
* comparison;
* triggering flag;
* why the pause was correct or incorrect;
* required code or configuration change.

Do not call it a false positive without evidence.

---

# Part 8 — Reproduce the captured result offline

After the real rerun, create an offline fixture using the captured sanitized values.

The offline test must call the real production health evaluator.

It must reproduce:

```text
health_status
reasons
triggering_flags
field-level checks
```

before applying any production fix.

Then add regression tests for the demonstrated issue.

Do not fabricate uncaptured values. Mark unavailable fields as `None`.

---

# Part 9 — Fix only demonstrated defects

Valid corrections may include:

* denominator correction;
* status mapping correction;
* milliseconds/seconds correction;
* screening-versus-research completeness correction;
* disabled-provider exclusion;
* missing telemetry preservation;
* budget-breach correction;
* numeric threshold versus Boolean flag correction;
* role-success calculation correction.

Do not:

* increase thresholds merely to pass;
* disable pause flags;
* suppress a legitimate pause;
* convert missing evidence to complete;
* remove checks;
* weaken safety policy.

If the pause is proven intentional, preserve it and only improve explanation and persistence.

---

# Part 10 — Diagnostic CLI

Add:

```bash
python -m trading_research.cli shadow-health-explain \
  --scheduler-run-id <id>
```

Optionally support:

```bash
python -m trading_research.cli shadow-health-explain \
  --cycle-id <id>
```

Return structured JSON:

```text
scheduler_run_id
cycle_id
health_status
policy_version
reasons
triggering_flags
checks:
  - check_name
  - status
  - input_value
  - input_unit
  - threshold_value
  - threshold_unit
  - comparison
  - applicable
  - pause_flag_enabled
  - reason
```

Requirements:

* deterministic ordering;
* sanitized output;
* no credentials;
* unknown run returns an error and nonzero exit code;
* no arbitrary SQL or Python input.

---

# Part 11 — Verify pause and alert behavior

Verify:

```text
HEALTHY
→ no pause

DEGRADED
→ no automatic pause

PAUSE_RECOMMENDED
→ alert only unless explicitly configured otherwise

PAUSE_REQUIRED
→ pause only when the corresponding pause_on_* policy is enabled
```

Requirements:

* no automatic resume;
* no automatic kill clearing;
* exact triggering checks included in the pause reason;
* pause alert includes health reasons;
* duplicate pause alerts deduplicate;
* delivery failure does not erase the pause or original alert;
* expected health pause is not reported as a scheduler crash.

---

# Part 12 — Activation-readiness result

Add or extend an activation-readiness decision with statuses such as:

```text
READY_FOR_MANUAL_SHADOW_RUNS
READY_FOR_LIMITED_RECURRING_SHADOW
NOT_READY_HEALTH_UNEXPLAINED
NOT_READY_PAUSE_ACTIVE
NOT_READY_PRICING
NOT_READY_PROVIDER_HEALTH
NOT_READY_INSUFFICIENT_HISTORY
ENVIRONMENTALLY_BLOCKED
```

Requirements for limited recurring readiness:

* no unexplained `PAUSE_REQUIRED`;
* pause state `ACTIVE`;
* pricing configured;
* persisted health explanations;
* leases validated;
* budget controls validated;
* no reconciliation mismatch;
* no duplicate-prevention violation;
* existing minimum completed-cycle history satisfied;
* existing minimum real-provider-cycle history satisfied;
* no enhanced or live execution.

Do not reduce readiness minimums.

Given the current limited run history, the expected outcome will likely remain:

```text
READY_FOR_MANUAL_SHADOW_RUNS
```

or:

```text
NOT_READY_INSUFFICIENT_HISTORY
```

Do not claim recurring readiness solely because the health issue is explained.

---

# Tests

Add tests for:

## Health explanation

* one check per dimension;
* exact value;
* unit;
* threshold;
* comparison;
* applicability;
* pause flag;
* deterministic order;
* missing telemetry;
* policy version.

## Provider-success rates

* all providers successful;
* failed provider;
* disabled provider excluded;
* no provider activity;
* no paper order;
* screened-out symbol;
* optional provider unavailable.

## Evidence completeness

* complete for screening;
* partial noncritical;
* missing news;
* missing sentiment;
* critical corporate-status missing;
* unsafe evidence;
* conflicting evidence.

## Role and retry rates

* two successful roles;
* required-role failure;
* retry success;
* retry exhaustion;
* no role because completeness blocked;
* analyst-only diagnostic run.

## Units

* milliseconds versus seconds;
* role latency;
* cycle duration;
* threshold boundary;
* below;
* equal;
* above.

## Budget

* consumed below reservation;
* consumed equal reservation;
* emergency-margin breach;
* pricing missing;
* deterministic zero-cost provider;
* real priced usage.

## Persistence

* every health check persisted;
* idempotent save;
* run query;
* cycle query;
* check-name query;
* no duplicates on resume.

## CLI

* explain by run;
* explain by cycle;
* missing run;
* deterministic JSON;
* sanitized output.

## Pause and alert behavior

* healthy no pause;
* degraded no pause;
* recommended alert;
* required pause with enabled flag;
* required no pause with disabled flag;
* killed state unchanged;
* no automatic resume;
* alert deduplication.

## Readiness

* unexplained pause;
* explained expected pause;
* pause active;
* pricing missing;
* insufficient history;
* manual readiness;
* recurring readiness minimums.

## End-to-end offline regression

Using the sanitized real-run values:

```text
captured values
→ production health evaluator
→ exact original status reproduced
→ demonstrated fix applied
→ corrected or intentional status asserted
→ checks persisted
→ CLI explains result
→ readiness remains history-limited
→ no paper submission
→ no enhanced execution
```

---

# Required test execution

Run targeted tests, then:

```bash
pytest tests/ -q
```

Then:

```bash
cd paper_runtime
pytest tests/ -q
```

Default tests must remain offline and deterministic.

The real test must remain opt-in and skipped by default.

Do not weaken, delete, or newly skip existing tests to obtain a pass.

---

# Documentation

Create:

```text
docs/milestone7-2-shadow-health-diagnostics.md
```

Update:

```text
docs/milestone7-1-shadow-integration-closure.md
docs/milestone7-production-shadow-operations.md
docs/adr/0005-production-shadow-operations-boundary.md
docs/runbooks/shadow-operations.md
docs/runbooks/shadow-incident-response.md
```

Document:

* original unexplained pause;
* real rerun values;
* exact reasons and flags;
* offline reproduction;
* root-cause classification;
* fixes;
* persisted health-check model;
* diagnostic CLI;
* pause and alert behavior;
* activation-readiness decision;
* remaining limitations.

Do not rewrite history to imply the cause was known during Milestone 7.1.

---

# Safety review

Before completion verify:

* no secrets committed;
* no `.env` printed;
* no raw Claude output persisted;
* no model influence over health;
* no model influence over pause;
* no automatic resume;
* no automatic kill clearing;
* no threshold weakened merely for a pass;
* no missing metric converted to zero;
* no unknown cost converted to zero;
* no duplicate health-check rows;
* no duplicate pause actions;
* no paper submission;
* no enhanced execution;
* no live trading;
* no Robinhood mutation;
* no recurring deployment activated;
* `real_orders` remains write-blocked;
* recommendation immutability remains intact.

---

# Acceptance criteria

Milestone 7.2 is complete only when:

1. Existing 1,221 main tests still pass.
2. Existing 33 paper-runtime tests still pass.
3. Field-level health diagnostics exist.
4. Health diagnostics are persisted.
5. The diagnostic CLI explains every check.
6. Exactly one bounded real rerun captures the missing reasons and flags.
7. The rerun result is reproduced offline.
8. The exact `PAUSE_REQUIRED` cause is proven.
9. Any demonstrated code defect is fixed.
10. Intentional policy pauses remain intact.
11. Units and denominators are verified.
12. Missing telemetry remains missing.
13. Optional-provider absence does not become provider failure.
14. Missing noncritical evidence does not become critical failure.
15. Cost below reservation does not become budget breach.
16. Pause behavior matches policy flags.
17. No automatic resume exists.
18. Activation readiness is evaluated honestly.
19. Recurring readiness is not claimed without minimum history.
20. No paper submission occurs.
21. No enhanced execution occurs.
22. No scheduler is activated.
23. Documentation matches the implementation.
24. No commit or push occurs unless explicitly requested.

---

# Required final response

Provide:

1. Baseline and Git state.
2. Scratchpad path.
3. Health data-flow mapping.
4. Real rerun result.
5. Captured health inputs.
6. Exact reasons.
7. Exact triggering flags.
8. Offline reproduction.
9. Root-cause classification.
10. Whether the pause was correct.
11. Code fixes.
12. Configuration changes, if justified.
13. Field-level health model.
14. Persistence changes.
15. Diagnostic CLI.
16. Pause behavior.
17. Alert behavior.
18. Activation-readiness result.
19. Tests added.
20. Main-suite result.
21. Paper-runtime result.
22. Proof of no paper/enhanced execution.
23. Documentation changes.
24. Safety review.
25. Known limitations.
26. Recommended next milestone.

Include:

```text
Requirement → implementation file → verifying test
```

Use labels:

```text
REAL-RERUN-CAPTURED
OFFLINE-REPRODUCED
ROOT-CAUSE-CONFIRMED
HEALTH-INPUT-BUG
HEALTH-POLICY-EXPECTED
HEALTH-DIAGNOSTICS-PERSISTED
PAUSE-BEHAVIOR-VERIFIED
ACTIVATION-READINESS-EVALUATED
ENHANCED-SHADOW-ONLY
ACTUAL-RECURRING-DEPLOYMENT-NOT-ACTIVATED
```

Do not claim completion until the `PAUSE_REQUIRED` result is explained.

Do not commit or push unless explicitly asked.
