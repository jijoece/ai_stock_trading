## Milestone 7.2 assessment

Milestone 7.2 delivered substantial improvements, but its documentation **overstates one conclusion**.

### What is genuinely complete

The following work is well supported:

* field-level diagnostics for all 16 health dimensions;
* deterministic health-check ordering and serialization;
* persistence in `shadow_run_health_checks`;
* the `shadow-health-explain` CLI;
* correct handling of missing telemetry as `INSUFFICIENT_DATA`;
* a valid fix to the retry-exhaustion denominator;
* real emergency-margin breach wiring;
* a dedicated, always-enabled duplicate-prevention pause trigger;
* alerts for health-triggered pauses and recommendations;
* activation-readiness evaluation;
* **1,266 passed, 14 skipped**, with the paper runtime unchanged at **33 passed**.  

The new real runs were also honestly reported:

```text
Real runs: 2
Run-two attempts: 1
Invoked role: bear
Bear result: failed validation
Manager: not invoked
retry_exhaustion_rate: 1.0
Health result: PAUSE_REQUIRED
Combined cost: approximately $0.136
Paper submissions: 0
Enhanced executions: 0
```

For **that new run**, the pause was correct: the only invoked required role failed its only allowed attempt, producing a terminal `RETRY_EXHAUSTED` record.

## Important unresolved discrepancy

Milestone 7.2 did **not prove the cause of the original Milestone 7.1 pause**.

The original Milestone 7.1 run had:

```text
attempt_count: 2
roles: bear + manager
role-budget decisions: PROCEED + PROCEED
input tokens: 18,833
output tokens: 7,275
consumed cost: $0.165624
```

It was described as an otherwise successful two-role run.

The Milestone 7.2 rerun instead had:

```text
attempt_count: 1
bear failed
manager never invoked
retry_exhaustion_rate: 1.0
```

These are materially different executions. The second run explains **its own** `PAUSE_REQUIRED`, but it cannot establish why the earlier two-successful-role run paused. The original temporary database was unavailable, and its field-level checks were never captured. 

Therefore, the trustworthy conclusion is:

```text
Milestone 7.2 diagnostics: COMPLETE
Retry-exhaustion denominator fix: COMPLETE
New real-run pause: EXPLAINED AND EXPECTED
Original Milestone 7.1 pause: NOT RECONSTRUCTIBLE FROM AVAILABLE DATA
```

The closure document should replace claims such as “proves the exact cause of Milestone 7.1’s unexplained pause” with:

> The original Milestone 7.1 health result is not reconstructible because its temporary database and field-level diagnostics are unavailable. Milestone 7.2 captured and explained a subsequent real `PAUSE_REQUIRED` result caused by a terminal bear-role failure.

## Most plausible unresolved candidate

The documented `unsupported_claim_rate` issue remains a credible explanation for the original successful run.

Currently, the metric is effectively:

```text
number of unsupported-claim failure rows
÷
number of attempts
```

One successful attempt can produce several non-material rejected-claim rows. Therefore:

* the numerator may exceed the number of attempts;
* the rate can exceed `1.0`;
* a successful report can contribute to the failure numerator;
* the configured `0.25` threshold may trigger from a single otherwise valid attempt.

The new reruns showing `unsupported_claim_rate=0` only prove it was not the cause of those new runs. They do not rule it out for the unavailable Milestone 7.1 run. 

This can be corrected without another paid API call. First decide what the metric means:

```text
Option A:
attempts containing at least one unsupported claim
÷
total attempts

Option B:
unsupported claims
÷
total validated claims
```

Option A is the smallest compatible fix. It requires counting distinct `attempt_id` values containing an unsupported-claim failure rather than counting every failure row.

## Other operational gaps

Before recurring activation, these should also be addressed:

### Reconciliation status is still hardcoded

`paper_reconciliation_mismatch=False` is not derived from actual paper reconciliation.

For cycles with no submitted paper orders, the correct diagnostic state should probably be:

```text
NOT_APPLICABLE
```

rather than an authoritative `False`.

For cycles with paper activity, it should be sourced from the existing reconciliation records.

### Duplicate-prevention status is still hardcoded

The pause behavior for `duplicate_prevention_violation=True` is now correct, but the scheduler never derives that value from actual lease, idempotency, or duplicate-cycle events.

The detector and the response mechanism are therefore not yet connected.

### Real test duration is not measured

The smoke test’s frozen clock produces:

```text
cycle_duration_seconds = 0.0
```

even though the Claude call alone took about 30 seconds. This did not cause the health result, but it prevents the real validation from testing duration-related health behavior.

An injected advancing clock or a separate monotonic duration measurement would make this field meaningful.

### Retry-exhaustion terminology remains confusing

With `max_attempts_per_role=1`, a first-attempt terminal failure is labeled `RETRY_EXHAUSTED`, even though no retry was possible.

The behavior is fail-safe, but the terminology makes diagnostics misleading. A future additive code such as:

```text
ROLE_ATTEMPTS_EXHAUSTED
```

or:

```text
ROLE_TERMINAL_FAILURE
```

would better cover both one-attempt and multi-attempt configurations.

## Activation decision

The current activation result is correctly:

```text
NOT_READY_INSUFFICIENT_HISTORY
```

The persistent database has no completed production-like shadow history because validations used temporary databases. Existing readiness floors still require:

```text
minimum completed cycles: 10
minimum real-provider cycles: 5
```

Those thresholds should not be reduced.

No recurring scheduler should be activated yet. 

## Recommended next step

Use a narrow:

# Milestone 7.3 — Health-metric semantics and operational-signal integration

Scope it to:

1. Correct `unsupported_claim_rate` using distinct affected attempts or a real claim denominator.
2. Add bounded tests where a successful attempt has multiple non-material unsupported claims.
3. Rename or clarify terminal role-attempt exhaustion for one-attempt configurations.
4. Derive reconciliation health from actual reconciliation records, with `NOT_APPLICABLE` when no paper activity exists.
5. Derive duplicate-prevention health from real lease/idempotency events.
6. Fix real-smoke-test duration measurement.
7. Correct Milestone 7.2 documentation so the original and subsequent real runs are not conflated.
8. Run several manual shadow cycles into a persistent evaluation database before considering recurring activation.

The implementation is safer and more observable after 7.2, but the precise status should be:

> **Milestone 7.2 diagnostics are complete; the subsequent real pause is explained, while the original Milestone 7.1 pause remains historically unresolvable. Recurring activation remains blocked by insufficient history and incomplete operational-signal wiring.**
