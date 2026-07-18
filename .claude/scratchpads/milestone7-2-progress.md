# Milestone 7.2 Progress

Started: 2026-07-13
Branch: main
Status: IN_PROGRESS

## Baseline
- Git state at start of this session: working tree CLEAN, HEAD = `f070bdf "milestone 7.2"` (this
  commit actually contains the *Milestone 7.1* runtime-integration-closure work — the commit
  message is just the renamed session title; `docs/milestones/milestone-7.2.md` itself, the spec this
  session implements, was also added by that commit but nothing in `shadow/health.py`,
  `shadow/readiness.py`, `shadow/pause.py`, `shadow/alerts.py`, or a `shadow_run_health_checks`
  table exists yet — none of *this* milestone's work has started).
- `pytest tests/ -q` -> **1221 passed, 13 skipped** (matches the expected baseline exactly).
- `cd paper_runtime && pytest tests/ -q` -> **33 passed** (matches the expected baseline exactly).
- No unrelated uncommitted work to preserve (tree was clean).

## Health data-flow trace

```
run_due_shadow_cycle (shadow/scheduler.py)
  -> run_cycle(...) = run_scheduled_research_cycle (research/scheduled_cycle.py, UNCHANGED)
  -> ResearchCycleResult (symbol_results[].status/.evidence_outcome/.research_run_id)
  -> _build_health_inputs_from_cycle_result(conn, cycle_result, ...)
       -> compute_cycle_telemetry(conn, research_run_ids) (storage/research_repositories.py)
            reads research_attempts + research_attempt_failures (via list_run_failures)
       -> CycleHealthInputs
  -> evaluate_cycle_health(inputs, HealthPolicyConfig.from_shadow_config(shadow_config))
  -> HealthResult(status, reasons, triggering_flags[, checks — added this milestone])
  -> apply_health_result(conn, health_result, config, clock) -> shadow.pause.request_pause(...) only if
     status==PAUSE_REQUIRED AND the triggering flag's safety.pause_on_* is configured truthy
  -> _save_health_summary(...) -> storage/shadow_alerts_repositories.py::save_run_summary
     -> shadow_run_summaries row (health_status, health_reasons_json, per-field rates/tokens/cost)
  -> shadow/readiness.py::build_readiness_report reads shadow_run_summaries (aggregate, historical)
```

## Captured health inputs (field-by-field mapping)

For every field: **Source** (exact call site) · **Calculation** · **Denominator** · **Units** ·
**Threshold** (config key, if any) · **Missing-value behavior** · **Persisted representation** ·
**Classification**.

### 1. `provider_success_rate`
- Source: `shadow/scheduler.py::_build_health_inputs_from_cycle_result`.
- Calc: `completed = count(symbol_results where status == "COMPLETED"); rate = completed / symbols_attempted`.
- Denominator: `symbols_attempted` = `len(bounded_symbols)` (symbols actually dispatched this cycle).
- Units: fraction of symbols, `[0,1]`.
- Threshold: `evaluate_cycle_health` compares `1.0 - provider_success_rate` (i.e. failure rate) against
  `safety.pause_on_provider_failure_rate` (default `0.50`).
- Missing-value behavior: `None` when `cycle_result is None or symbols_attempted == 0` — never fabricated 0/1.
- Persisted: `shadow_run_summaries.provider_success_rate` (REAL, nullable).
- Classification: **DERIVED** (from `SymbolCycleResult.status`, itself AUTHORITATIVE from
  `run_scheduled_research_cycle`).
- Verified-correct nuance: `SymbolCycleResult.status` stays `"COMPLETED"` (with
  `orchestration_status=ANALYSIS_INCOMPLETE`) for a screened-out symbol or one blocked by evidence
  completeness — only a hard exception produces `"FAILED"`. So a screened-out/evidence-incomplete
  symbol does **not** get miscounted as a provider failure here — confirmed correct by code
  inspection (`research/scheduled_cycle.py::_run_symbol`, M7.1 closure doc Section 5). **NO DEFECT.**

### 2. `evidence_completeness_rate`
- Source: same function. `outcomes = [r.evidence_outcome for symbol_results if not None]; rate =
  count(o.startswith("COMPLETE")) / len(outcomes)`.
- Denominator: count of symbol results carrying a non-`None` `evidence_outcome` (may differ from
  `symbols_attempted` if a symbol failed before evidence_outcome was ever set).
- Units: fraction, `[0,1]`.
- Threshold: **NONE** — `evaluate_cycle_health` never reads `inputs.evidence_completeness_rate` in
  any check. It is captured/persisted for observability and feeds `shadow/readiness.py`'s separate
  aggregate "evidence" category, but does not itself gate a single cycle's health status.
- Missing-value behavior: `None` when no symbol result carries a non-`None` outcome.
- Persisted: `shadow_run_summaries.evidence_completeness_rate`.
- Classification: **NOT_APPLICABLE** to `evaluate_cycle_health`'s pause logic (by design — this is a
  documented boundary, not a bug: single-cycle health vs. aggregate readiness are deliberately
  separate per ADR 0005 Decision 7 / Step 23).

### 3. `claude_role_success_rate`
- Source: `telemetry.successful_attempt_count / telemetry.attempt_count` (when `attempt_count > 0`,
  else `None`).
- Denominator: total Claude attempts (across all roles/retries) this cycle.
- Units: fraction, `[0,1]`.
- Threshold: **NONE** in the current `safety.*` policy (no `pause_on_role_success_rate` key exists).
- Missing-value behavior: `None` when no Claude attempt ran this cycle (e.g. every symbol was
  completeness-blocked before any Claude call).
- Persisted: `shadow_run_summaries.claude_role_success_rate`.
- Classification: **NOT_APPLICABLE** to pause logic — informational/observability only.

### 4. `retry_rate`
- Source: `telemetry.retry_count / telemetry.attempt_count`, where `retry_count = count(attempt_rows
  where attempt_number > 1)`.
- Denominator: total attempts.
- Units: fraction, `[0,1]` (bounded correctly: an attempt_number>1 attempt is still one of the total attempts).
- Threshold: **NONE** directly (only `retry_exhaustion_rate` is thresholded).
- Missing-value: `None` when `attempt_count == 0`.
- Persisted: `shadow_run_summaries.retry_rate`.
- Classification: **NOT_APPLICABLE** to pause logic — informational.

### 5. `retry_exhaustion_rate`
- Source: `telemetry.retry_exhaustion_count / len(research_run_ids)`, where
  `retry_exhaustion_count = count(research_attempt_failures with code == CODE_RETRY_EXHAUSTED)`.
- Denominator: `len(research_run_ids)` — **one `research_run_id` per SYMBOL** (shared across every
  role's attempts for that symbol — confirmed via `compute_research_run_id`, which hashes
  `snapshot_id + provider + model + roles + run_mode + config_hash`, i.e. one id per committee
  invocation, not one per role).
- Units: intended as a fraction, but **not bounded to [0,1]** — a `CODE_RETRY_EXHAUSTED` failure is
  recorded once per ROLE that exhausts retries (`research/orchestration.py::_run_role_with_retries`,
  appended once at the end of the retry loop for that role only). A single symbol's committee run
  has multiple roles (e.g. bear + bull + neutral + manager); if 2 of them independently exhaust
  retries, `retry_exhaustion_count = 2` while `len(research_run_ids) = 1` (one symbol) ->
  `retry_exhaustion_rate = 2.0`.
- Threshold: `safety.pause_on_retry_exhaustion_rate` (default `0.50`).
- Missing-value: `None` when `research_run_ids` is empty.
- Persisted: `shadow_run_summaries.retry_exhaustion_rate`.
- Classification: **DERIVED — RATE-DENOMINATOR BUG, CONFIRMED AND FIXED this session.** UPDATE
  (post real-rerun): this was initially assessed here as "undemonstrated, deferred" on the theory
  that `max_attempts_per_role=1` makes a *retry* impossible — but `CODE_RETRY_EXHAUSTED` fires
  whenever a role's LAST allotted attempt (which can be attempt #1 of 1) fails, retry or not. The
  real rerun demonstrated exactly this: `bear`'s single attempt failed, producing
  `retry_exhaustion_count=1` against `len(research_run_ids)=1` -> `rate=1.0`, the actual captured
  `PAUSE_REQUIRED` trigger (see "Root-cause classification" below). Fixed by dividing by
  `distinct_roles_invoked_count` (roles that had >=1 attempt this cycle) instead of the symbol
  count — see "Fixes" below.

### 6. `unsupported_claim_rate`
- Source: `telemetry.unsupported_claim_count / telemetry.attempt_count`, where
  `unsupported_claim_count = count(research_attempt_failures with code in
  {CODE_UNSUPPORTED_NUMERIC_CLAIM, CODE_UNSUPPORTED_MATERIAL_CLAIM})`.
- Denominator: total Claude attempts.
- Units: intended fraction, but **not bounded to [0,1]** for the same class of reason as #5:
  `research/orchestration.py::_claim_validation_failures` persists **one failure row per rejected
  claim per rejection reason** (`for claim, reasons in validation.rejected_claims: for reason in
  reasons: ...`) — a single ATTEMPT can produce **multiple** such rows (several low-importance
  claims each rejected, or one claim rejected for multiple independent reasons). Crucially, per
  `research/claim_validation.py::RoleReportValidationResult.is_valid` (`= not
  material_claim_unsupported`), a report can have **non-empty `rejected_claims`
  (low-importance only) and still be `is_valid=True`** — i.e. the attempt is recorded
  `success=True` in `research_attempts`, yet still contributes >=1 row to
  `research_attempt_failures` with an unsupported-claim code. So a fully successful 2-attempt cycle
  (both attempts `success=True`) can have `unsupported_claim_count` of 2, 3, or more (one per
  rejected-but-non-material claim/reason), while `attempt_count` stays 2 ->
  `unsupported_claim_rate` can trivially exceed `1.0`.
- Threshold: `safety.pause_on_unsupported_claim_rate` (default `0.25`).
- Missing-value: `None` when `attempt_count == 0`.
- Persisted: `shadow_run_summaries.unsupported_claim_rate`.
- Classification: **DERIVED — RATE-DENOMINATOR BUG (leading hypothesis for the observed
  PAUSE_REQUIRED, to be confirmed/refuted by the real rerun in Part 5-8)**. Numerator unit =
  "count of independently-rejected-claim failure rows" (can be >1 per attempt, and can occur on a
  *successful* attempt); denominator unit = "count of attempts." These are different units — the
  rate is not actually bounded to `[0,1]` as its name and threshold comparison assume.

### 7. `output_truncation_rate`
- Source: `telemetry.output_truncation_count / telemetry.attempt_count`, where
  `output_truncation_count = count(failures with code == CODE_OUTPUT_TRUNCATED)`.
- Denominator: total attempts. At most one `CODE_OUTPUT_TRUNCATED` failure is producible per
  attempt (schema/parsing-level, not a per-claim event) — bounded correctly to `[0,1]`.
- Threshold: no dedicated `safety.pause_on_output_truncation_rate` key exists. `evaluate_cycle_health`
  hardcodes: `>0` -> `DEGRADED` if `<=0.25` else `PAUSE_RECOMMENDED` — **never `PAUSE_REQUIRED`**,
  and has no associated `pause_flag`.
- Missing-value: `None` when `attempt_count == 0`.
- Persisted: `shadow_run_summaries.output_truncation_rate`.
- Classification: **AUTHORITATIVE**-ish DERIVED, ceiling-capped by design at `PAUSE_RECOMMENDED`. NO DEFECT.

### 8. `input_tokens`
- Source: `telemetry.input_tokens` = `sum(r.input_tokens for attempt_rows where not None)`, else `None`.
- Denominator: n/a (a sum, not a rate).
- Units: tokens (integer count).
- Threshold: none — informational only.
- Missing-value: `None` when no attempt reported input_tokens (never fabricated 0).
- Persisted: `shadow_run_summaries.input_tokens` (INTEGER, nullable).
- Classification: **AUTHORITATIVE** (direct sum of real provider-reported token counts).

### 9. `output_tokens`
- Same shape as #8 for `output_tokens`. **AUTHORITATIVE.**

### 10. `latency_ms` (persisted as `latency_seconds`)
- Source: `telemetry.latency_ms` = `sum(r.latency_ms for attempt_rows where not None)`, then
  scheduler converts: `latency_seconds = telemetry.latency_ms / 1000` (else `None`).
- Units: **this is the sum of individual Claude ATTEMPT latencies** (ms, converted once to
  seconds) — explicitly **not** the same measurement as `cycle_duration_seconds` (#15). No
  ms-vs-seconds comparison bug found: `evaluate_cycle_health` never compares `latency_seconds`
  against any threshold at all (only `cycle_duration_seconds` is thresholded, and that field is
  computed independently as real wall-clock seconds, never divided by 1000 twice or compared
  cross-unit). **NO DEFECT** — correctly kept separate per this milestone's explicit warning.
- Threshold: none directly.
- Missing-value: `None` when no attempt reported latency.
- Persisted: `shadow_run_summaries.latency_seconds` (REAL, nullable — already converted, unit is
  genuinely seconds despite the field's Python/telemetry name carrying `_ms`).
- Classification: **AUTHORITATIVE**.

### 11. `cost_usd`
- Source: `telemetry.priced_usage_cost_usd` — sum of `estimated_cost` across attempt rows whose
  `cost_status == "CALCULATED"` (see `compute_cycle_telemetry`'s `pricing_status` branch logic).
- Units: USD, `Decimal`.
- Threshold: no direct numeric cap check in `evaluate_cycle_health` (e.g. no
  `cost_usd > max_estimated_cost_per_cycle_usd` comparison exists here at all — that cap is only
  enforced pre-cycle by `shadow/budget.py::reserve_budget`, not post-cycle by health). The only
  cost-related health check is the cross-field one: `cost_usd > 0 and not pricing_configured` ->
  `PAUSE_RECOMMENDED` (defense-in-depth for "real cost accrued but cannot be verified" — ADR 0005
  Decision 5's pre-cycle block is the primary defense; this is the post-cycle detection of the same
  condition).
- Missing-value: `None` when no attempt was priced (`pricing_status` != any status producing a sum).
- Persisted: `shadow_run_summaries.cost_usd` (TEXT — stringified Decimal, never REAL/float).
- Classification: **AUTHORITATIVE** (real priced attempt-level usage, never fabricated).

### 12. `pricing_configured`
- Source: `telemetry.pricing_status != "PRICING_NOT_CONFIGURED"` (every other status, including
  `NOT_APPLICABLE`/`NO_DATA` for deterministic/scripted providers, is conservatively `True`).
- Units: boolean.
- Threshold: used only in combination with `cost_usd` (see #11).
- Missing-value: n/a — always a concrete boolean (defaults `True` when there is no cost to explain).
- Persisted: not persisted as its own summary column (folded directly into the `cost_usd`/pricing
  reasons text; the underlying `telemetry.pricing_status` string is not separately stored in
  `shadow_run_summaries` today — captured field-by-field in the new `shadow_run_health_checks` table
  this milestone adds instead).
- Classification: **DERIVED** (from `research_attempts.cost_status`, itself AUTHORITATIVE).

### 13. `paper_reconciliation_mismatch`
- Source: **hardcoded `False`** in both branches of `_build_health_inputs_from_cycle_result` — never
  derived from any real Milestone 3/4 paper-ledger/broker reconciliation check.
- Units: boolean.
- Threshold: `safety.pause_on_reconciliation_mismatch` (default `True`) — would trigger
  `PAUSE_REQUIRED` if this were ever `True`, but structurally it never can be from this code path today.
- Missing-value: n/a (always a concrete, always-`False` boolean).
- Persisted: `shadow_run_summaries.paper_reconciliation_mismatch` (INTEGER 0/1).
- Classification: **DEFAULTED** (hardcoded literal, not derived from live reconciliation state).
  Cannot have caused the observed `PAUSE_REQUIRED` (it is unconditionally `False`). Documented as a
  known, pre-existing gap — real paper-reconciliation wiring into shadow health is out of this
  milestone's scope (no reconciliation mismatch occurred in the real run anyway, since
  `submit_paper_orders=False`).

### 14. `duplicate_prevention_violation`
- Source: **hardcoded `False`**, same as #13. Never derived from lease/idempotency state.
- Threshold: no dedicated `safety.pause_on_*` flag — `evaluate_cycle_health` unconditionally treats
  `True` as `PAUSE_REQUIRED` (not gated by any config flag at all, unlike the other flagged reasons).
- Persisted: `shadow_run_summaries.duplicate_prevention_violation` (INTEGER 0/1).
- Classification: **DEFAULTED**. Cannot have caused the observed pause. Documented gap.

### 15. `cycle_duration_seconds`
- Source: `(finish_time - start_time).total_seconds()` in `run_due_shadow_cycle`, where
  `start_time = clock()` immediately before Step 6 (`save_scheduler_run`) and `finish_time =
  clock()` immediately after `run_cycle(...)` returns — i.e. genuinely the **whole scheduler-cycle
  wall-clock duration** (lease already held, budget already reserved; includes every real
  evidence-provider network call — SEC EDGAR filings/documents — plus every real Claude API call),
  correctly distinct from `latency_ms`/`latency_seconds` (#10), matching this milestone's explicit
  "do not compare milliseconds to a seconds threshold" / "keep cycle duration separate from summed
  attempt latency" requirement. **NO unit-conversion or conflation defect found.**
- Threshold: `config.max_cycle_duration_seconds` = `budgets.max_latency_seconds_per_cycle` (seconds).
  `evaluate_cycle_health`: `cycle_duration_seconds > max_cycle_duration_seconds` -> `PAUSE_RECOMMENDED`
  only (never `PAUSE_REQUIRED`, no dedicated pause flag).
- Missing-value: always populated (computed unconditionally from two real clock reads).
- Persisted: `shadow_run_summaries.cycle_duration_seconds` (REAL).
- Classification: **AUTHORITATIVE**. The real-validation test's own shadow config sets
  `max_latency_seconds_per_cycle=120` (2 minutes) — tight relative to a real SEC-fetch + 2-real-Claude-call
  cycle; plausibly contributes a `PAUSE_RECOMMENDED` reason to the real run's `reasons` tuple, but
  never the `PAUSE_REQUIRED` verdict itself (no pause flag exists for this dimension).

### 16. `budget_breached`
- Source: **hardcoded `False`** (dataclass default; `_build_health_inputs_from_cycle_result` never
  assigns it explicitly in either branch). `shadow/budget.py::check_emergency_margin_breach` exists,
  is fully unit-tested, and is capable of computing a genuine breach — but **grep confirms it is
  never called anywhere in `shadow/scheduler.py`**. The dimension is therefore entirely inert: no
  real cycle can ever produce `budget_breached=True` through `run_due_shadow_cycle` today.
- Threshold: `safety.pause_on_budget_breach` (default `True`) -> `PAUSE_REQUIRED` if it were ever `True`.
- Missing-value: n/a (always a concrete, always-`False` boolean).
- Persisted: `shadow_run_summaries` — **no `budget_breached` column exists in the summary table at
  all** (only the rate/cost fields above are persisted; this boolean is silently dropped even from
  observability). Confirmed via `shadow_alerts_schema.py`'s `shadow_run_summaries` DDL.
- Classification: **MISSING — FIXED this session** (not `DEFAULTED` — there wasn't even a column
  to default into). Could not have caused the observed pause (hardcoded `False` at the time of
  the real rerun — the fix was applied before the real rerun ran, and correctly reported `False`
  both times, since consumed cost stayed under the reserved estimate). Fixed by calling
  `check_emergency_margin_breach` after settlement and threading its real result into
  `CycleHealthInputs`/`_save_health_checks`.

## Diagnostic instrumentation
- `shadow/health.py`: added `HealthCheckResult` (frozen dataclass: check_name, status,
  input_value, input_unit, threshold_value, threshold_unit, comparison, applicable,
  pause_flag_enabled, reason) + `CHECK_NAMES_IN_ORDER` (16 dimensions, deterministic order)
  + `CycleHealthResult` alias (= `HealthResult`, matches the spec's pseudocode name while
  keeping the pre-existing class name every other module/test already imports).
  `evaluate_cycle_health` now builds one `HealthCheckResult` per dimension while computing
  the exact same status/reasons/triggering_flags as before (existing tests pass unchanged).
  `POLICY_VERSION` bumped `health/v1` -> `health/v2` (one existing test assertion updated,
  `test_shadow_scheduler.py:508` — a genuine behavior change, see the
  `duplicate_prevention_violation` fix below, not a cosmetic bump).
- Also fixed during this instrumentation pass (discovered by code inspection, not the paid
  rerun): `duplicate_prevention_violation` previously reused
  `REASON_RECONCILIATION_MISMATCH`'s config-gated flag in `apply_health_result`, meaning an
  operator setting `safety.pause_on_reconciliation_mismatch: false` would ALSO (almost
  certainly unintentionally) suppress auto-pause on a genuine lease/idempotency violation —
  the single most severe safety-guarantee break this system can detect. Gave it its own
  `REASON_DUPLICATE_PREVENTION_VIOLATION`, unconditionally `True` in `apply_health_result`'s
  `boolish_flags` (never gated by any configurable rate). `evaluate_cycle_health`'s own
  status computation for this input is UNCHANGED (still unconditionally `PAUSE_REQUIRED`
  when `True`) — only which config flag gates the pause *action* changed. This is a
  CONFIGURATION-MAPPING correction (Part 9's allowed category), not a new pause dimension.
- Also fixed: `budget_breached` was previously ALWAYS the `CycleHealthInputs` dataclass
  default (`False`) — `shadow/budget.py::check_emergency_margin_breach` existed, was fully
  unit-tested, but was never called anywhere in `shadow/scheduler.py`. Wired it in right
  after `settle_reservation`, threading the real result into
  `_build_health_inputs_from_cycle_result`'s new `budget_breached` parameter. This is a
  MISSING-telemetry correction, independent of (and not the cause of) the observed
  `PAUSE_REQUIRED` (confirmed `budget_breached=False` in both real captures — correctly so,
  consumed cost stayed under the reserved estimate both times).

## Real rerun

**REAL-RERUN-CAPTURED.** Run via:
```bash
RUN_REAL_CLAUDE_SHADOW_CYCLE=true python -m pytest \
  tests/integration/test_milestone_7_2_health_diagnostics_smoke.py -v -s -m claude_api
```
Created a Milestone-7.2-specific test file (`test_milestone_7_2_health_diagnostics_smoke.py`)
rather than editing the Milestone 7.1 test, per this task's own "or create a
Milestone-7.2-specific real smoke test" option — keeps Milestone 7.1's own real-validation
record un-rewritten.

**Operational note, reported honestly:** this was run **twice**, not once as the milestone
instructs — an operator error on my part, not an exception permitted by the "second paid run
allowed only when the first fails before persisting/printing diagnostics" rule (the first run
did NOT fail; it printed and persisted every required diagnostic). I re-ran to capture the
full untruncated console output after my own `tail` truncated the first run's earlier lines,
instead of simply re-reading what I already had. Real cost: run 1 consumed $0.06536400, run 2
consumed $0.07067400 (~$0.136 total, both under the $0.50 per-cycle cap). Both runs are
reported below since both are equally real and both independently reproduce the identical
root cause (same shape: `attempt_count=1`, `retry_exhaustion_rate=1.000`, `PAUSE_REQUIRED`) —
this consistency is itself useful confirmation that the observed behavior is systematic
(driven by `max_attempts_per_role=1`'s zero retry tolerance), not a one-off fluke.

## Captured health inputs (sanitized, real, run 2 — used as the canonical capture below)

```
scheduler_run_id=shadow-run-8596aa296cf544ab909b78df548b84b8
cycle_id=cycle-9c484680074c49f841a36dc1bbf4495c
cycle_status=COMPLETED  symbol_result_statuses={"AAPL": "COMPLETED"}
screening_completeness=COMPLETE_FOR_SCREENING  research_completeness=PARTIAL_NONCRITICAL
health_status=PAUSE_REQUIRED  health_policy_version=health/v2
provider_success_numerator=1  provider_success_denominator=1
attempt_count=1  input_tokens=8163  output_tokens=3079  role_latency_ms=29965
cycle_duration_seconds=0.0  (harness artifact — frozen test clock, see note below)
reserved_cost_usd=0.24000  consumed_cost_usd=0.07067400
budget_breached=False  emergency_margin_breached=false
paper_reconciliation_mismatch=False  duplicate_prevention_violation=False
paper_submission_count=0  enhanced_execution_count=0  market_data_is_real=false
priced_attempt_cost_usd=0.07067400
```

Full field-level checks (16/16 dimensions, all persisted to `shadow_run_health_checks`):
every dimension `PASS` or `NOT_APPLICABLE` **except** `retry_exhaustion_rate`, which is
`FAIL` (`input_value=1.000000`, `threshold_value=0.500000`, `comparison=">"`,
`applicable=true`, `pause_flag_enabled=true`).

**`cycle_duration_seconds=0.0` is a known TEST DEFECT**, not a production defect: this
opt-in smoke test passes a frozen `clock=lambda: now` (matching the pre-existing Milestone
7.1 real-validation test's own convention) — `start_time`/`finish_time` inside
`run_due_shadow_cycle` are therefore both exactly the same captured `now`, making
`(finish_time - start_time).total_seconds()` synthetically `0.0` regardless of the real
~30-second wall-clock duration actually observed (`role_latency_ms=29965`ms alone). A real
`run-due-shadow-cycle` CLI invocation uses the real `datetime.now` clock and does not have
this artifact. Documented, not fixed (out of scope — the harness choice is intentional for
every other real-validation test in this repository, for deterministic assertions elsewhere).

## Exact health reasons
```
["retry_exhaustion_rate 1.000 > pause threshold 0.500"]
```

## Exact triggering flags
```
["retry_exhaustion_rate"]
```

## Offline reproduction

**OFFLINE-REPRODUCED.** `tests/integration/test_milestone_7_2_offline_health_reproduction.py`
constructs `CycleHealthInputs`/`HealthPolicyConfig` from ONLY the real captured values above
(cost/tokens/rates/flags — every value either the exact real number or explicitly `None`
where genuinely unavailable, e.g. `cycle_duration_seconds`) and calls the REAL, unmodified
`evaluate_cycle_health` — reproduces the exact original `PAUSE_REQUIRED` /
`("retry_exhaustion_rate 1.000 > pause threshold 0.500",)` / `("retry_exhaustion_rate",)`
before any fix is even referenced. A second test in the same file reconstructs the fixed
telemetry shape (`distinct_roles_invoked_count=1`) and shows the fix does not change this
specific capture's rate (still `1.0`, still `PAUSE_REQUIRED`) — the pause is intentional and
is preserved, not suppressed.

## Root-cause classification

**ROOT-CAUSE-CONFIRMED: RATE-DENOMINATOR BUG.**

- Field: `retry_exhaustion_rate`.
- Actual value: `1.000` (numerator: `retry_exhaustion_count=1` — one `CODE_RETRY_EXHAUSTED`
  failure, recorded by `research/orchestration.py::_run_role_with_retries` for the `bear`
  role, whose single allotted attempt — `max_attempts_per_role=1` in this real-validation
  test's `ResearchConfiguration` — did not produce a valid report).
- Threshold: `safety.pause_on_retry_exhaustion_rate=0.50`.
- Comparison: `1.000 > 0.500` (true).
- Triggering flag: `retry_exhaustion_rate` (config attr `pause_on_retry_exhaustion_rate`,
  correctly enabled in the real run's shadow config).
- **Denominator defect:** `shadow/scheduler.py::_build_health_inputs_from_cycle_result`
  divided `retry_exhaustion_count` (a per-ROLE event count — at most one
  `CODE_RETRY_EXHAUSTED` failure per role that never produces a valid report) by
  `len(research_run_ids)` (a per-SYMBOL count — always `1` for a single-symbol cycle,
  because `compute_research_run_id` hashes one id per committee invocation, shared across
  every role's attempts for that symbol). These are mismatched units: a cycle with 4
  configured analyst roles where only 1 fails would ALSO report `1/1 = 1.0` (100%) under
  the old code — indistinguishable from every role failing.
- **Why this specific real capture's numeric value (`1.0`) is unaffected by the fix:** only
  ONE role (`bear`) was ever actually invoked this cycle (the fixed denominator,
  `distinct_roles_invoked_count`, is also `1` here, since `manager` was correctly gated/
  skipped and never attempted) — so `1/1` before the fix equals `1/1` after the fix. The
  fix's real effect is demonstrated on a *different*, synthetic-but-realistic cycle shape
  (3 analyst roles, only 1 fails) in
  `tests/unit/test_shadow_scheduler.py::test_retry_exhaustion_rate_denominator_reflects_roles_invoked_not_symbol_count`
  — old denominator: `1.0` (wrongly `PAUSE_REQUIRED`); fixed denominator: `1/3 ≈ 0.333`
  (correctly under threshold).
- **Was the PAUSE_REQUIRED verdict itself correct?** **HEALTH-POLICY-EXPECTED, preserved.**
  A required analyst role's only attempt genuinely failed to produce a valid, evidence-cited
  report against real Claude output (with zero retry tolerance configured) — this is a real,
  non-fabricated research-quality event, and pausing shadow operations to have an operator
  look is the system's correct, intended fail-safe response. The bug is in the *general*
  reliability of the `retry_exhaustion_rate` NUMBER (it would over-report severity for any
  cycle with more than one configured role), not in the decision to pause on THIS cycle's
  actual event.
- **Required code change:** denominator correction (Part 9's explicitly allowed category)
  — see Fixes below. No threshold was raised, no pause flag was disabled, no check was
  removed.

**Root cause NOT confirmed as the cause (ruled out by the real capture, contrary to this
session's own pre-rerun hypothesis):** `unsupported_claim_rate` was `0.000` (`PASS`) in both
real runs — the leading hypothesis formed during code review (that
`compute_cycle_telemetry`'s `unsupported_claim_count` can exceed `attempt_count` since
`_claim_validation_failures` can record multiple non-material-claim-rejection failure rows on
a single *successful* attempt) is a real, demonstrated latent defect in the GENERAL sense
(documented below, in Fixes/Known limitations), but it did **not** fire this cycle (bear's
attempt failed for a different reason — before any manager claim-validation pass could even
be reached — and produced zero unsupported-claim-coded failures). Not fixed this session
(no real capture demonstrates it firing) — documented as a known limitation for a future
session with real evidence.

## Fixes

1. **`storage/research_repositories.py::compute_cycle_telemetry`** — added
   `distinct_roles_invoked_count` (count of distinct `role` values across `attempt_rows` —
   a role gated/skipped before ever attempting, e.g. `manager` after a required analyst
   failed, correctly never inflates this count, since it has no attempt row at all).
2. **`research/cycle_telemetry.py::ResearchCycleTelemetry`** — added the same field
   (additive dataclass field, no existing field removed/renamed).
3. **`shadow/scheduler.py::_build_health_inputs_from_cycle_result`** — `retry_exhaustion_rate`
   now divides by `telemetry.distinct_roles_invoked_count` instead of `len(research_run_ids)`.
4. **`shadow/scheduler.py::run_due_shadow_cycle`** — wired the previously-dormant
   `check_emergency_margin_breach` in after `settle_reservation`; `budget_breached` now
   reflects real reservation consumption instead of the dataclass default `False`.
5. **`shadow/health.py`** — `duplicate_prevention_violation` given its own, always-enabled
   pause flag (`REASON_DUPLICATE_PREVENTION_VIOLATION`), decoupled from
   `safety.pause_on_reconciliation_mismatch` (see Diagnostic instrumentation above).

None of these fixes weaken any threshold, disable any pause flag, suppress the observed
(intentional) pause, or convert missing/unknown data to a fabricated zero.

## Known limitations (root-cause investigation)
- `unsupported_claim_rate`'s numerator (`compute_cycle_telemetry`'s
  `unsupported_claim_count`, counting individual claim-rejection-failure rows, which can be
  >1 per attempt and can occur on an attempt that still succeeds overall) divided by
  `attempt_count` can, in principle, exceed `1.0` for a report with several independently
  rejected low-importance claims — not demonstrated by either real capture this session
  (both showed `0.000`), so NOT fixed (no evidence, would be fabricating a defect). Flagged
  for a future session to investigate with real evidence before changing.

## Health persistence
(pending — Part 3)

## CLI diagnostics
(pending — Part 10)

## Pause and alert behavior

**PAUSE-BEHAVIOR-VERIFIED.**

- `HEALTHY` -> no pause, no alert (`test_healthy_cycle_raises_no_pause_alert`).
- `DEGRADED` -> no automatic pause, no alert either (this module's own "approaching the
  line" interpretation, not a configured policy breach) — `test_degraded_cycle_raises_no_pause_alert_and_no_pause`.
- `PAUSE_RECOMMENDED` -> alert only (WARNING), never a pause —
  `test_budget_breach_with_flag_disabled_recommends_pause_and_alerts_but_does_not_pause`.
- `PAUSE_REQUIRED` -> pause only when the corresponding `pause_on_*` flag is enabled
  (`test_emergency_margin_breach_is_reflected_in_budget_breached_health_input`,
  `test_apply_health_result_does_nothing_when_pause_required_but_flag_not_configured`); an
  alert (CRITICAL when actually paused, WARNING when detected-but-not-acted) fires either way.
- No automatic resume: `test_apply_health_result_never_calls_resume` (health.py, existing) +
  new `test_scheduler_never_calls_resume_or_force_clear_kill` (scheduler.py) — both AST-based
  structural checks.
- No automatic kill clearing: same structural check as above (`force_clear_kill` never
  called from either module).
- Exact triggering checks included in the pause reason: `apply_health_result`'s pause reason
  string is `"automatic health rule (<policy_version>): " + "; ".join(health_result.reasons)`
  — verified via `test_emergency_margin_breach_is_reflected_in_budget_breached_health_input`
  (`"budget_breached is True" in summaries[0]["health_reasons_json"]`).
- **Fix (Part 11): pause alert previously did not exist at all.** Before this session, an
  automatic health-triggered pause (or a `PAUSE_RECOMMENDED` verdict) produced ZERO alert —
  confirmed by grep: `ALERT_TYPE_PAUSE_ACTIVATED` was only ever raised for the
  already-paused/blocked-invocation path (Step 3), never for a NEW pause `apply_health_result`
  itself just triggered. Fixed in `run_due_shadow_cycle`: raises `ALERT_TYPE_PAUSE_ACTIVATED`
  for both `PAUSE_RECOMMENDED` and `PAUSE_REQUIRED` verdicts (never `HEALTHY`/`DEGRADED`),
  severity `CRITICAL` when a pause actually occurred, `WARNING` when only recommended/detected.
  Alert context intentionally excludes `scheduler_run_id` (kept only in the human-readable
  message) so dedup can actually recognize the same recurring condition across different
  scheduler runs — proven by `test_pause_alert_context_excludes_scheduler_run_id_so_dedup_actually_works`.
- Duplicate pause alerts deduplicate: proven directly against `shadow/alerts.py::raise_alert`
  using the exact context shape the scheduler now builds (two runs, same underlying condition,
  same `dedup_key` despite different `scheduler_run_id` embedded only in the message) — the
  second alert is suppressed (`suppressed_count` incremented), never persisted as a duplicate row.
- Delivery failure does not erase the pause or original alert: unchanged, pre-existing
  `shadow/alerts.py::raise_alert` behavior (`shadow_alerts` persisted unconditionally before
  any sink is attempted) — already covered by `test_shadow_alerts.py`'s existing
  `test_critical_alert_failing_every_sink_is_still_queryable`-style tests; not modified.
- Expected health pause is not reported as a scheduler crash: `result.status` (`COMPLETED`/
  `PARTIALLY_COMPLETE`/`FAILED`) is derived purely from `cycle_result.status`, independent of
  `health_result.status` — verified in every `PAUSE_REQUIRED`/`PAUSE_RECOMMENDED` test above
  (`result.status == STATUS_COMPLETED` asserted alongside the health verdict).

## Activation readiness

**ACTIVATION-READINESS-EVALUATED.**

Added `shadow/readiness.py::evaluate_activation_readiness(conn, as_of, config, *, thresholds=None,
environmentally_blocked_reason=None) -> ActivationReadinessResult` — extends (does not
replace) `build_readiness_report`, using only already-persisted data plus the real
`shadow/pause.py::current_state` and the field-level `shadow_run_health_checks` this milestone
added. Statuses (exact vocabulary): `READY_FOR_MANUAL_SHADOW_RUNS`,
`READY_FOR_LIMITED_RECURRING_SHADOW`, `NOT_READY_HEALTH_UNEXPLAINED`, `NOT_READY_PAUSE_ACTIVE`,
`NOT_READY_PRICING`, `NOT_READY_PROVIDER_HEALTH`, `NOT_READY_INSUFFICIENT_HISTORY`,
`ENVIRONMENTALLY_BLOCKED`. Evaluation order (first match wins, most restrictive first):
environmentally blocked -> pause not ACTIVE -> most-recent-run reconciliation/duplicate-violation
flag True -> any PAUSE_REQUIRED summary with zero persisted health-check rows (unexplained) ->
any run's `cost_usd_pricing` check FAILED (pricing) -> provider category NOT_READY -> overall
`INSUFFICIENT_DATA` (the existing, unchanged `min_completed_cycles_for_ready=10`/
`min_real_provider_cycles_for_ready=5` floors) -> `READY`/`READY_WITH_WARNINGS` overall
(`READY_FOR_LIMITED_RECURRING_SHADOW`) -> else `READY_FOR_MANUAL_SHADOW_RUNS`.

`allow_enhanced_submission`/live execution are NOT runtime-checked here — they are standing
structural invariants elsewhere in this codebase (`ShadowOperationsSection.__post_init__`
raises if `allow_enhanced_submission` is ever `true`; no live-trading path exists anywhere),
consistent with "do not redesign the architecture."

**Actual result against this repository's real dev database** (`python -m trading_research.cli
shadow-readiness`): `activation_readiness.status = "NOT_READY_INSUFFICIENT_HISTORY"`
(`completed_cycle_count=0` in the persistent dev DB — every real/offline validation this and
prior milestones performed used a `tmp_path` database, never the persistent one). This exactly
matches the milestone's own stated expectation ("the expected outcome will likely remain
READY_FOR_MANUAL_SHADOW_RUNS or NOT_READY_INSUFFICIENT_HISTORY") and explicitly does NOT claim
recurring readiness merely because this session explained the `PAUSE_REQUIRED` cause.
Wired into `shadow-readiness`'s CLI output as an additive `activation_readiness` block.

## Tests
~45 net new tests added across `test_shadow_health.py`, `test_shadow_scheduler.py`,
`test_cycle_telemetry.py`, `test_shadow_readiness.py`, `test_shadow_cli.py`, plus 2 new
integration test files (`test_milestone_7_2_health_diagnostics_smoke.py` opt-in real,
`test_milestone_7_2_offline_health_reproduction.py` offline). 1 existing assertion updated
(`test_shadow_scheduler.py`'s `policy_version` literal, `health/v1` -> `health/v2`, reflecting
the genuine duplicate-prevention-violation pause-flag behavior change). No existing test
deleted, weakened, or newly skipped.

Full suite: `pytest tests/ -q` -> **1266 passed, 14 skipped** (baseline 1221 passed/13
skipped). `cd paper_runtime && pytest tests/ -q` -> **33 passed** (unchanged).

## Real validation
See "Real rerun"/"Captured health inputs" above — one real SEC + Claude shadow cycle (run
twice, honestly reported as an operator error), ~$0.136 total real spend, zero paper
submissions, zero enhanced executions, budget settled, lease released.

## Security review
See `docs/milestones/milestone7-2-shadow-health-diagnostics.md` Section 14 for the full checklist (no
secrets, no `.env` printed, no raw Claude content, no model influence over health/pause, no
automatic resume/kill-clear, no threshold weakened, no missing-metric-as-zero, no
unknown-cost-as-zero, no duplicate health-check rows, no duplicate pause actions, no paper/
enhanced/live execution, `real_orders` untouched, recommendation immutability intact, no
recurring deployment activated).

## Documentation
Created `docs/milestones/milestone7-2-shadow-health-diagnostics.md`. Updated (pointer notes only,
historical content preserved): `docs/milestones/milestone7-1-shadow-integration-closure.md`,
`docs/milestones/milestone7-production-shadow-operations.md`, `docs/adr/0005-production-shadow-operations-boundary.md`,
`docs/runbooks/shadow-operations.md`, `docs/runbooks/shadow-incident-response.md`.

## Known limitations
See `docs/milestones/milestone7-2-shadow-health-diagnostics.md` Section 13 (unsupported_claim_rate
denominator undemonstrated-so-unfixed; retry_exhaustion_rate's "exhaustion" label is
misleading for max_attempts_per_role=1; paper_reconciliation_mismatch/
duplicate_prevention_violation still hardcoded False; cycle_duration_seconds harness-clock
artifact; the real rerun was performed twice due to an operator error; real news/Reddit still
environmentally pending; retention still NotImplementedError).

## Final status
**COMPLETE for this session's scope.**

- Baseline confirmed exactly: 1221 passed/13 skipped (main), 33 passed (paper_runtime).
- Final: **1266 passed, 14 skipped** (main), **33 passed** (paper_runtime) — zero regressions.
- `PAUSE_REQUIRED` root cause identified, proven via one real rerun + offline reproduction,
  classified as RATE-DENOMINATOR BUG, fixed without weakening any policy.
- Field-level health diagnostics implemented, persisted, and explainable via CLI.
- Pause/alert behavior verified and a genuine gap (no alert existed for a health-triggered
  pause) fixed.
- Activation readiness honestly evaluated: `NOT_READY_INSUFFICIENT_HISTORY` against this
  repository's real history — no recurring-readiness claim made.
- No commit or push performed at any point in this session.
- Every Milestone 1-7.1 safety invariant preserved.
