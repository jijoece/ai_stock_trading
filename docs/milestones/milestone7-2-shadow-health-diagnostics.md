# Milestone 7.2 — Shadow health diagnostics and activation readiness

**Status:** Complete for this session's scope.
**Date:** 2026-07-13.
**Scope:** Instruments field-level health diagnostics, performs one bounded real rerun,
proves the exact cause of Milestone 7.1's unexplained `health_status=PAUSE_REQUIRED`,
fixes the demonstrated defect, and adds an honest activation-readiness decision. Does not
redesign the shadow-operations architecture, does not begin Milestone 8, does not activate a
recurring deployment.

## 1. The original, unexplained pause

Milestone 7.1's own real-validation run (`docs/milestones/milestone7-1-shadow-integration-closure.md`
Section 24) completed a real SEC + real Claude shadow cycle successfully — 2/2 attempts
succeeded, cost reconciled exactly — but returned `health_status=PAUSE_REQUIRED` with no
captured reasons or triggering flags. That session explicitly deferred investigating it to
avoid further real spend.

## 2. Health data-flow mapping

```
run_due_shadow_cycle -> run_scheduled_research_cycle (unmodified)
  -> ResearchCycleResult -> _build_health_inputs_from_cycle_result
       -> compute_cycle_telemetry (research_attempts / research_attempt_failures)
       -> CycleHealthInputs
  -> evaluate_cycle_health -> HealthResult (status, reasons, triggering_flags, checks)
  -> apply_health_result -> shadow.pause.request_pause (only if PAUSE_REQUIRED and flag enabled)
  -> shadow_run_summaries + shadow_run_health_checks (persisted)
  -> shadow-alerts (new: pause-triggered alert) / shadow-readiness / shadow-health-explain
```

Every one of the 16 health-input fields (`provider_success_rate`, `evidence_completeness_rate`,
`claude_role_success_rate`, `retry_rate`, `retry_exhaustion_rate`, `unsupported_claim_rate`,
`output_truncation_rate`, `input_tokens`, `output_tokens`, `latency_ms`, `cost_usd`,
`pricing_configured`, `paper_reconciliation_mismatch`, `duplicate_prevention_violation`,
`cycle_duration_seconds`, `budget_breached`) is documented field-by-field — source, calculation,
denominator, units, threshold, missing-value behavior, persisted representation, and
classification (`AUTHORITATIVE`/`DERIVED`/`NOT_APPLICABLE`/`MISSING`/`DEFAULTED`) — in
`.claude/scratchpads/milestone7-2-progress.md`'s "Captured health inputs" section. Two
dimensions (`paper_reconciliation_mismatch`, `duplicate_prevention_violation`) are still
hardcoded `False` (never derived from live reconciliation/lease state) — a known,
pre-existing, out-of-scope gap, not touched this session (confirmed not the cause, since they
are unconditionally `False`).

## 3. Field-level health diagnostics

`src/trading_research/shadow/health.py` — `HealthCheckResult` (frozen dataclass: check_name,
status, input_value, input_unit, threshold_value, threshold_unit, comparison, applicable,
pause_flag_enabled, reason), one per dimension, in deterministic order
(`CHECK_NAMES_IN_ORDER`). `HealthResult.checks` (also aliased `CycleHealthResult`, matching
this milestone's own spec pseudocode while keeping the pre-existing class name every caller
already imports) carries all 16. Every check reports exactly one of `PASS`/`WARNING`/`FAIL`/
`NOT_APPLICABLE`/`INSUFFICIENT_DATA` — missing telemetry is always `INSUFFICIENT_DATA` with a
`None` `input_value`, never a fabricated `0.0`. `evaluate_cycle_health`'s existing
`status`/`reasons`/`triggering_flags` computation is unchanged (byte-for-byte identical logic,
now additionally emitting the per-dimension explanation alongside it) — confirmed by the full
existing `test_shadow_health.py` suite passing unchanged.

## 4. Persistence

New additive table `shadow_run_health_checks` (`storage/shadow_alerts_schema.py`, co-located
with `shadow_run_summaries`): `check_id` (deterministic — sha256 of
`scheduler_run_id|check_name|policy_version`), `scheduler_run_id`, `cycle_id`, `check_name`,
`check_status`, `input_value`, `input_unit`, `threshold_value`, `threshold_unit`, `comparison`,
`applicable`, `pause_flag_enabled`, `reason`, `policy_version`, `evaluated_at`. `INSERT OR
IGNORE` — idempotent, no duplicate rows on resume/re-evaluation. Queryable by scheduler run,
by cycle, and by check name. No destructive migration; no change to any existing table.

## 5. Real rerun

`RUN_REAL_CLAUDE_SHADOW_CYCLE=true pytest tests/integration/test_milestone_7_2_health_diagnostics_smoke.py
-v -s -m claude_api` — a new Milestone-7.2-specific test (kept separate from the Milestone 7.1
test so that session's own record is not rewritten). One symbol (AAPL), real SEC EDGAR
corporate status, real Claude (`bear`+`manager`, `max_attempts_per_role=1`), a strict $0.50
cost cap, a temporary database, no paper submission, no enhanced execution.

**Honestly reported: this was run twice, not once** — an operator error (re-running to see
console output already captured, rather than re-reading it), not an exception permitted by
this milestone's own "second paid run only if the first fails before capturing diagnostics"
rule. The first run did not fail. Total real cost across both runs: ~$0.136 (well under the
$0.50 per-cycle cap each time). Both runs independently reproduced the identical result shape.

Captured result (sanitized): `cycle_status=COMPLETED`, `attempt_count=1` (only `bear`
attempted — its single, no-retry-possible attempt failed to produce a valid report; `manager`
was consequently never invoked), `health_status=PAUSE_REQUIRED`,
`health_reasons=["retry_exhaustion_rate 1.000 > pause threshold 0.500"]`,
`triggering_flags=["retry_exhaustion_rate"]`. Every other field-level check `PASS` or
`NOT_APPLICABLE` (including `unsupported_claim_rate=0.000`, ruling out this session's own
pre-rerun hypothesis — see Section 7). No paper submission, no enhanced execution, budget
settled, lease released.

## 6. Offline reproduction

`tests/integration/test_milestone_7_2_offline_health_reproduction.py` constructs
`CycleHealthInputs`/`HealthPolicyConfig` from ONLY the real captured values above and calls the
real, unmodified `evaluate_cycle_health` — reproduces the exact original
`PAUSE_REQUIRED`/reasons/triggering_flags before any fix. A second test shows the fix (Section
8) does not change this specific capture's numeric result (the pause remains intentional and
correctly preserved).

## 7. Root cause: RATE-DENOMINATOR BUG

`shadow/scheduler.py::_build_health_inputs_from_cycle_result` computed
`retry_exhaustion_rate = retry_exhaustion_count / len(research_run_ids)` — a per-ROLE event
count (`research/orchestration.py::_run_role_with_retries` records one `CODE_RETRY_EXHAUSTED`
failure whenever a role's LAST allotted attempt, which can be attempt #1 of 1, fails to
produce a valid report — this fires on a genuine single-attempt failure, not only after
multiple retries) divided by a per-SYMBOL count (always `1` for a single-symbol cycle, since
one `research_run_id` is shared across every role's attempts for that symbol). A cycle with 4
configured analyst roles where only 1 fails would ALSO report `1/1 = 1.0` (100%) under this
denominator — indistinguishable from every role failing.

For the specific real capture, only one role (`bear`) was ever invoked, so the fixed
denominator (roles actually invoked) is also `1` — the numeric result and the `PAUSE_REQUIRED`
verdict are unchanged and correctly preserved (`HEALTH-POLICY-EXPECTED`: a required role
genuinely failed to produce a valid, evidence-cited report with zero retry tolerance
configured — pausing to have an operator look is the correct, intended fail-safe response).
The bug's general severity — misreporting a partial (e.g. 1-of-4) role failure as 100% — is
separately, additionally demonstrated with a synthetic-but-realistic 3-analyst-role scenario
in `tests/unit/test_shadow_scheduler.py::
test_retry_exhaustion_rate_denominator_reflects_roles_invoked_not_symbol_count` (old
denominator: `1.0`/100%, wrongly `PAUSE_REQUIRED`; fixed: `1/3`≈33%, correctly under threshold).

**Ruled out (this session's own pre-rerun hypothesis, real-captured `unsupported_claim_rate`
was `0.000`):** a latent denominator/counting issue exists in `unsupported_claim_rate` too
(`compute_cycle_telemetry`'s `unsupported_claim_count` counts individual claim-rejection-failure
rows, which can exceed 1 per attempt — including on an attempt that still succeeds overall —
divided by `attempt_count`) but it did not fire this cycle and was NOT fixed, since no real
evidence demonstrates it. Documented as a known limitation for a future session.

## 8. Fixes (demonstrated defects only)

1. **`research/cycle_telemetry.py`/`storage/research_repositories.py::compute_cycle_telemetry`**
   — added `distinct_roles_invoked_count` (count of distinct roles with >=1 attempt row this
   cycle; a gated/skipped role, e.g. `manager` after a required analyst fails, correctly never
   inflates this count, since it never gets an attempt row).
2. **`shadow/scheduler.py::_build_health_inputs_from_cycle_result`** —
   `retry_exhaustion_rate` now divides by `distinct_roles_invoked_count` instead of the symbol
   count.
3. **`shadow/scheduler.py::run_due_shadow_cycle`** — wired the previously-dormant
   `shadow/budget.py::check_emergency_margin_breach` in after settlement; `budget_breached`
   now reflects real reservation consumption instead of an always-`False` dataclass default
   (a demonstrable gap discovered by code inspection — the function existed, fully
   unit-tested, but was never called; confirmed `False` correctly in both real captures, since
   consumed cost stayed under the reserved estimate).
4. **`shadow/health.py`** — `duplicate_prevention_violation` (a lease/idempotency guarantee
   break) previously reused `paper_reconciliation_mismatch`'s configurable
   `safety.pause_on_reconciliation_mismatch` flag to decide whether to auto-pause — meaning
   disabling that unrelated rate flag would also silently suppress auto-pause on the single
   most severe safety violation this system can detect. Given its own, dedicated,
   unconditionally-enabled flag (`REASON_DUPLICATE_PREVENTION_VIOLATION`). The verdict
   `evaluate_cycle_health` itself computes for this input is unchanged (still unconditionally
   `PAUSE_REQUIRED` when `True`) — only which flag gates the pause *action* changed.

None of these fixes raises a threshold, disables a pause flag, suppresses the observed
(intentional) pause, removes a check, or converts missing/unknown data to a fabricated value.
`POLICY_VERSION` bumped `health/v1` -> `health/v2` to reflect the Fix 4 behavior change (one
existing test assertion updated accordingly).

## 9. Diagnostic CLI

```bash
python -m trading_research.cli shadow-health-explain --scheduler-run-id <id>
python -m trading_research.cli shadow-health-explain --cycle-id <id>
```

Returns `scheduler_run_id`, `cycle_id`, `health_status`, `policy_version`, `reasons`,
`triggering_flags`, and `checks` (all 16, in the same deterministic
`CHECK_NAMES_IN_ORDER` as `evaluate_cycle_health` itself uses — not SQL's own alphabetical
order). An unknown run/cycle returns `{"error": ...}` with exit code 2. No credentials, no raw
model content — every persisted field is already a bounded, structured diagnostic value.

## 10. Pause and alert behavior

`HEALTHY`/`DEGRADED` -> no pause, no alert. `PAUSE_RECOMMENDED` -> alert only (`WARNING`),
never a pause. `PAUSE_REQUIRED` -> pause only when the corresponding `safety.pause_on_*` flag
is enabled; an alert (`CRITICAL` if actually paused, `WARNING` if only detected) fires either
way. **Fix:** previously an automatic health-triggered pause (or a `PAUSE_RECOMMENDED`
verdict) produced **zero** alert at all — `run_due_shadow_cycle` now raises
`ALERT_TYPE_PAUSE_ACTIVATED` for both statuses, with `health_reasons`/`triggering_flags` in its
context (deliberately excluding `scheduler_run_id` from the context so recurring identical
conditions across different runs still share a `dedup_key` and can actually deduplicate — kept
only in the human-readable message). No automatic resume, no automatic kill-clearing
(structurally verified, AST-based, for both `health.py` and `scheduler.py`). An expected
health-triggered pause is never reported as a scheduler crash — `result.status` is derived
purely from the underlying cycle's own outcome, independent of the health verdict.

## 11. Activation readiness

`shadow/readiness.py::evaluate_activation_readiness` extends `build_readiness_report` with an
honest manual-vs-recurring decision: `READY_FOR_MANUAL_SHADOW_RUNS`,
`READY_FOR_LIMITED_RECURRING_SHADOW`, `NOT_READY_HEALTH_UNEXPLAINED`, `NOT_READY_PAUSE_ACTIVE`,
`NOT_READY_PRICING`, `NOT_READY_PROVIDER_HEALTH`, `NOT_READY_INSUFFICIENT_HISTORY`,
`ENVIRONMENTALLY_BLOCKED`. Never claims recurring readiness merely because a `PAUSE_REQUIRED`
is now explained — the existing (unchanged) minimum completed-cycle (10) and real-provider-cycle
(5) floors are independently required. Wired into `shadow-readiness`'s CLI output as an
additive `activation_readiness` block.

**Actual result against this repository's real dev database:**
`activation_readiness.status = "NOT_READY_INSUFFICIENT_HISTORY"` (`completed_cycle_count=0` —
every real validation this and prior milestones performed used a temporary database, never the
persistent one). Matches this milestone's own stated expectation exactly.

## 12. Test summary

Targeted new/changed tests this session: ~45 new unit/integration tests (health-check
diagnostics, persistence, CLI, pause/alert behavior, activation readiness, the
retry-exhaustion-rate denominator fix and regression, offline reproduction) plus 1 new opt-in
real-validation test and 1 existing test-assertion update (`policy_version`). Full suite:
`pytest tests/ -q` -> **1266 passed, 14 skipped** (baseline 1221 passed/13 skipped). Zero
regressions, zero existing test weakened/deleted/newly-skipped. `paper_runtime`: **33 passed**,
unchanged.

## 13. Known limitations

1. `unsupported_claim_rate`'s numerator (individual claim-rejection-failure rows, which can
   exceed 1 per attempt, including on an overall-successful attempt) divided by `attempt_count`
   can, in principle, exceed `1.0` — not demonstrated by either real capture this session
   (both `0.000`), so not fixed. Flagged for a future session with real evidence.
2. `retry_exhaustion_rate`'s numerator (`CODE_RETRY_EXHAUSTED`) fires on a role's LAST attempt
   failing regardless of whether more than one attempt was actually configured — the label
   "retry exhaustion" is misleading for a `max_attempts_per_role=1` configuration where no
   retry was ever structurally possible. Not renamed/relabeled this session (would touch
   `research/failure_taxonomy.py`'s allowlisted codes, a broader surface than this milestone's
   scope) — the denominator fix (Section 8) addresses the demonstrated health-metric defect
   without needing this relabeling.
3. `paper_reconciliation_mismatch`/`duplicate_prevention_violation` remain hardcoded `False` in
   `_build_health_inputs_from_cycle_result` — never derived from live reconciliation/lease
   state. Pre-existing, out-of-scope gap (confirmed not the cause of this session's
   investigation, since both are unconditionally `False`).
4. The real-validation smoke test's `cycle_duration_seconds=0.0` is a harness artifact (a
   frozen `clock=lambda: now`, matching the Milestone 7.1 real-validation test's own
   convention) — a real `run-due-shadow-cycle` CLI invocation uses the real clock and does not
   have this artifact.
5. This session's own real rerun was performed twice (an operator error, not a rule
   exception) — reported honestly in Section 5 and the scratchpad, not concealed.
6. Real news/Reddit sentiment remain `ENVIRONMENTALLY_PENDING`, unchanged from Milestone 7/7.1.
7. `shadow/retention.py::apply_retention(dry_run=False)` still unconditionally raises
   `NotImplementedError`, unchanged.

## 14. Safety review

No secrets committed, no `.env` printed, no raw Claude output persisted anywhere (every new
persisted field is a bounded structured value — check_name/status/value/unit/threshold/
comparison/reason string, never a raw response). No model influence over health or pause
decisions (`evaluate_cycle_health`/`apply_health_result` remain pure functions over
deterministic counters). No automatic resume, no automatic kill-clearing (AST-verified). No
threshold weakened merely to obtain a pass — no `config/shadow_operations.yaml` value was
touched this session (confirmed via `git diff -- config/`, empty). No missing metric converted
to a fabricated zero (`INSUFFICIENT_DATA` with `input_value=None` throughout). No unknown cost
converted to zero (`cost_usd_pricing` check reports `INSUFFICIENT_DATA` when genuinely
unpriced). No duplicate health-check rows (`INSERT OR IGNORE` on a deterministic `check_id`,
tested). No duplicate pause actions (alert dedup tested directly; `request_pause`'s own
pre-existing idempotency untouched). No paper submission, no enhanced execution (asserted in
both new real-validation and offline tests; `may_submit_enhanced` structurally `False`). No
live trading, no Robinhood mutation — no new code path anywhere near `execution/`/
`paper/ledger.py`'s mutation surface. No recurring deployment activated — `deploy/launchd/*`
untouched, no `launchctl` invocation this session. `real_orders` remains write-blocked
(untouched, confirmed via `git diff --stat`). Recommendation immutability remains intact (no
`UPDATE`/`DELETE` against `recommendations` in any new/modified file).

## 15. Requirement → implementation → verifying test

| Requirement | Implementation | Verifying test |
|---|---|---|
| Field-level health diagnostics | `shadow/health.py::HealthCheckResult`/`CHECK_NAMES_IN_ORDER` | `tests/unit/test_shadow_health.py` (field-level section) |
| Diagnostics persisted | `storage/shadow_alerts_schema.py::shadow_run_health_checks` + repositories | `tests/unit/test_shadow_scheduler.py::test_completed_cycle_persists_one_health_check_per_dimension` et al. |
| Diagnostic CLI | `cli.py::shadow_health_explain_cli` | `tests/unit/test_shadow_cli.py` (health-explain section) |
| Real rerun captured | `tests/integration/test_milestone_7_2_health_diagnostics_smoke.py` | itself (opt-in, real) |
| Offline reproduction | `tests/integration/test_milestone_7_2_offline_health_reproduction.py` | itself |
| Retry-exhaustion denominator fix | `research/cycle_telemetry.py` + `storage/research_repositories.py` + `shadow/scheduler.py` | `tests/unit/test_shadow_scheduler.py::test_retry_exhaustion_rate_denominator_reflects_roles_invoked_not_symbol_count`, `tests/unit/test_cycle_telemetry.py::test_distinct_roles_invoked_count_excludes_never_attempted_manager` |
| Emergency-margin/budget-breach wiring | `shadow/scheduler.py::run_due_shadow_cycle` | `tests/unit/test_shadow_scheduler.py::test_emergency_margin_breach_is_reflected_in_budget_breached_health_input` |
| Duplicate-prevention pause-flag fix | `shadow/health.py` | `tests/unit/test_shadow_health.py::test_duplicate_prevention_violation_always_pause_flag_enabled_regardless_of_reconciliation_flag` |
| Pause alert (new) | `shadow/scheduler.py::run_due_shadow_cycle` | `tests/unit/test_shadow_scheduler.py` (pause/alert section) |
| Alert dedup across runs | context excludes `scheduler_run_id` | `tests/unit/test_shadow_scheduler.py::test_pause_alert_context_excludes_scheduler_run_id_so_dedup_actually_works` |
| Activation readiness | `shadow/readiness.py::evaluate_activation_readiness` | `tests/unit/test_shadow_readiness.py` (activation readiness section) |

## 16. Recommended next milestone

Investigate `unsupported_claim_rate`'s own denominator with real evidence (Known limitation 1)
and consider relabeling `CODE_RETRY_EXHAUSTED` for the single-attempt-configured case (Known
limitation 2); wire real paper-reconciliation/duplicate-prevention detection into
`_build_health_inputs_from_cycle_result` instead of hardcoded `False`; accumulate real,
sustained shadow-cycle history (the `min_completed_cycles_for_ready=10`/
`min_real_provider_cycles_for_ready=5` floors) before any future session claims
`READY_FOR_LIMITED_RECURRING_SHADOW`.
