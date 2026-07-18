# ADR 0005: Shadow operations are a bounded, single-invocation control layer around the existing scheduled-cycle service — not a new authority, not a daemon

**Status:** Accepted
**Date:** 2026-07-13 (Milestone 7)

## Context

Milestones 1-6.1 built a deterministic, evidence-backed research pipeline with a manually invoked scheduled-cycle service (`research/scheduled_cycle.py::run_scheduled_research_cycle`). It is idempotent and resumable per cycle, but nothing currently:

* invokes it on a schedule;
* prevents two concurrent invocations from racing the same intended cycle;
* enforces a spending limit on Claude usage before or during a run;
* can pause or kill recurring operation;
* alerts an operator when something goes wrong;
* reports whether the system is stable enough to keep running unattended.

`docs/milestones/milestone-7.md` asks for all of this while explicitly forbidding an always-running daemon, silent scheduler activation, and any weakening of the Milestone 1-6.1 safety invariants (paper-only execution, no enhanced-arm submission, no live trading, Claude as research-provider-only).

## Decision 1: The scheduler is an external-invocation-compatible single-run entry point, not a daemon

`trading_research.cli run-due-shadow-cycle` performs **at most one intended scheduled cycle** per process invocation and exits. There is no `while True` loop, no background thread, and no self-installing OS schedule anywhere in this repository. Recurring behavior is entirely the responsibility of whatever invokes the process (cron, launchd, a CI scheduler, or an operator typing the command). This mirrors the existing paper-runtime boundary precedent (ADR 0002: the main process never runs the risky part unsupervised) applied to *time* instead of *credentials*: the repository controls what a scheduled invocation is allowed to do, never when or whether one happens automatically. A `deploy/launchd/*.plist.example` artifact is provided as a documented, disabled-by-default template; creating the file does not install or activate a schedule (see Decision 9).

## Decision 2: The scheduler wraps `run_scheduled_research_cycle`; it does not reimplement cycle logic

`shadow/scheduler.py::run_due_shadow_cycle` performs preflight (enabled check, pause/kill check, market-calendar/window check, lease acquisition, budget reservation, provider-health check) and then calls the *existing, unmodified* `run_scheduled_research_cycle` for exactly the one due symbol batch, exactly like a human operator would invoke it manually today. `derive_cycle_id`'s existing determinism is preserved and extended one level: the scheduler additionally computes a deterministic `intended_schedule_id` (from the intended trading-day/local-time slot, not wall-clock invocation time) so that re-running the same command for the same missed/due slot is a no-op against `shadow_scheduler_runs`, independent of whatever cycle-level idempotency `research_cycles` already provides.

## Decision 3: Lease and idempotency are two separate, complementary mechanisms

A **lease** (`shadow_run_leases`, `shadow/lease.py`) prevents two *concurrent* processes from starting the same intended cycle at the same time — it is a mutex with a bounded TTL, owner identity, and stale-recovery, modeled after standard lease patterns rather than SQLite `BEGIN IMMEDIATE` alone, because a lease additionally needs to survive across the acquire → run → release sequence spanning possibly-fatal work (Claude calls), not just a single transaction. **Idempotency** (`derive_cycle_id`, `intended_schedule_id`) prevents two *sequential* invocations (e.g., a crash-recovered rerun) from re-doing completed work — it is a content-addressed identity check, independent of whether a lease was ever held. Both are required: a lease alone does not survive a crash between "lease released" and "cycle marked complete"; idempotency alone does not prevent two processes racing to acquire before either persists anything.

## Decision 4: Pause/kill state is a single global row with full history, checked before any provider or Claude call

`shadow_pause_state` holds exactly one current-state row (`ACTIVE`, `PAUSED_MANUAL`, `PAUSED_BUDGET`, `PAUSED_PROVIDER_HEALTH`, `PAUSED_RESEARCH_QUALITY`, `PAUSED_RECONCILIATION`, `KILLED`) plus an append-only history in the same table (previous-state, reason, source, operator, timestamp). `run_due_shadow_cycle` checks this **before** lease acquisition, so a paused/killed system never even attempts to acquire a lease or touch a provider. Only deterministic application code writes this table — `shadow/pause.py` exposes `request_pause(reason, source)` / `resume(reason, operator)` / `kill(reason, operator)`, called either by an operator CLI command or by the health-rule evaluator (Decision 7) after a cycle completes; no model output can reach these functions, and `resume()` explicitly refuses to clear a `KILLED` state (a separate, explicitly-named `force_clear_kill` path is required, and it still requires an operator reason).

## Decision 5: Budget is reserved before spending and settled after, using the same "no fabricated data" posture as the rest of the repository

`shadow/budget.py` estimates a worst-case cost for the about-to-run cycle (bounded by `max_symbols_per_cycle * max_roles_per_symbol * max_attempts_per_role * max_output_tokens_per_cycle`, priced via the existing `config/research_pricing.yaml` versioned pricing table — unchanged from Milestone 6's "unknown pricing blocks scheduled real-Claude operation" posture, now enforced structurally instead of just documented), reserves that estimate against daily/monthly caps in `shadow_budget_reservations`, and only after the cycle finishes records **actual** usage (from real Claude response metadata, exactly like `research/orchestration.py` already captures token counts — never fabricated) into `shadow_budget_usage`, releasing the unused portion of the reservation. If pricing is not configured, a scheduled cycle configured for the `anthropic` provider fails closed before any Claude call; the existing `deterministic`/`scripted` providers (used by the offline test suite and diagnostic runs) are exempt, matching Milestone 6's existing cost-tracking scope (`research/cost_tracking.py` already only prices real Claude usage).

## Decision 6: Role/token/attempt/latency/cost enforcement happens once per role call, immediately before that call, not only in aggregate

Before each role invocation inside a shadow cycle, `shadow/role_budget.py::check_role_budget` computes the *maximum possible* remaining cost of that role call (using the role's configured max output tokens and the manager's own estimate when applicable) against the cycle's remaining reservation. A call that could theoretically exceed the remaining budget is skipped with `SKIPPED_BUDGET_EXHAUSTED` — a distinct outcome from a provider failure, so it never pollutes provider-failure-rate health metrics or research-failure-taxonomy provider-failure codes. This is a pre-call gate, matching the existing `_run_role_with_retries` pattern of deciding role eligibility before invoking a provider, and does not touch `research/orchestration.py` itself; the scheduler supplies a role-count-limited `configuration.roles` tuple to `analyze_with_research_committee` rather than the module gaining new internal budget awareness.

## Decision 7: Health evaluation is deterministic, config-driven, and produces a request to pause — it does not pause anything by itself

`shadow/health.py::evaluate_cycle_health` is a pure function: cycle-level counters (provider failure rate, evidence completeness rate, retry-exhaustion rate, unsupported-claim rate, latency, reconciliation mismatches) in, one of `HEALTHY` / `DEGRADED` / `PAUSE_RECOMMENDED` / `PAUSE_REQUIRED` out, versioned via `config/shadow_operations.yaml`'s `safety.*` thresholds. Only the scheduler's post-cycle step calls `shadow.pause.request_pause(...)` when the result is `PAUSE_REQUIRED` **and** `safety.pause_on_*` is configured to auto-pause — the health function itself never touches `shadow_pause_state`. This separation keeps the same "deterministic application code decides, nothing else does" boundary the rest of the repository already uses for screening/promotion.

## Decision 8: Alerts are persisted before delivery is attempted, and delivery failure never erases the underlying event

`shadow/alerts.py::OperationalAlert` (severity, alert_type, message, context — no secrets, no raw prompts, no raw Claude responses) is written to `shadow_alerts` synchronously as part of the same operation that detected the condition (budget breach, lease conflict, pause activation, etc.), before any `AlertSink.send()` is attempted. `AlertSink` is a `Protocol`; this milestone ships two concrete sinks — `PersistenceOnlyAlertSink` (satisfies the protocol trivially, since persistence already happened) and `LogAlertSink` (structured `logging_config.py`-style log line). No webhook/email sink is added — the repository has no existing outbound-HTTP-notification dependency, and adding one is out of scope without a concrete, already-authorized target (mirrors the news-provider decision's "don't add a vendor relationship without a documented, already-authorized need"). `shadow_alert_deliveries` records each delivery attempt (sink name, success/failure, bounded response) separately from `shadow_alerts`, so a delivery failure is visible without ever discarding the original alert.

## Decision 9: `DEPLOYABLE SCHEDULER ARTIFACT` and `ACTUAL RECURRING DEPLOYMENT ACTIVATED` are different, explicitly labeled claims

`deploy/launchd/com.agentic-trading-desk.shadow.plist.example` is a documented, disabled-by-default (`.example` suffix, never loaded by launchd until an operator explicitly copies and `launchctl load`s it) template invoking only `run-due-shadow-cycle`, with no embedded credentials (it invokes a wrapper script that sources `.env` at runtime). Creating or validating this file's syntax and paths is **not** the same claim as an actual recurring schedule being active on this machine; this milestone's scratchpad and final report distinguish "code-complete scheduler support," "deployable scheduler artifact," and "actual recurring deployment activated" as three separate, independently-falsifiable statements, and only claims the third if `launchctl load` was actually run with the user's explicit authorization.

## Decision 10: Evidence-completeness policy is a new, versioned classification layer sitting beside `evidence_providers/normalization.py::classify_snapshot_outcome`, not a replacement for it

`classify_snapshot_outcome` (Milestone 6, unchanged) already classifies a single `EvidenceSnapshot`'s outcome (e.g. `COMPLETE`, `INCOMPLETE_REQUIRED_DATA`, `PROVIDER_UNAVAILABLE`, `POINT_IN_TIME_UNSAFE`) from evidence-item-level data. `research/evidence_completeness.py::evaluate_completeness` (new) consumes that outcome *plus* the new `CorporateStatusEvidence` result and produces a distinct, explicitly screening-vs-research pair of statuses per `docs/milestones/milestone-7.md` Step 11's status vocabulary. It does not duplicate snapshot-level classification logic — it composes the existing snapshot outcome with the new corporate-status result under a versioned policy (`policy_version` field, persisted), and its result is what the scheduler checks before allowing enhanced-arm Claude calls to run (extending, not replacing, the existing `evidence_blocks_enhanced` check in `research/scheduled_cycle.py::_run_symbol`).

## Decision 11: Retention is dry-run-first and never deletes audit-relevant rows in this milestone

`shadow/retention.py` classifies every Milestone 7 table (and revisits the Milestone 1-6.1 tables the spec names) into a retention tier and prints a plan (`retention-plan`) or a dry-run diff (`retention-apply --dry-run`). No code path in this milestone actually deletes a row — `retention-apply` without `--dry-run` is deliberately unimplemented (raises `NotImplementedError` with a clear message) rather than half-built, because the milestone's own acceptance criteria require tests and explicit configuration before any destructive path exists, and this milestone's budget does not include that additional design-and-test surface. This is recorded as **deferred work**, not a silent gap.

## What remains explicitly outside Milestone 7

* live trading, margin, options, short selling — never implemented, unchanged from every prior milestone;
* enhanced-arm paper/live execution — `may_submit_enhanced()` remains unconditionally `False`;
* separate paper-book namespaces (`ENHANCED_ONLY`, `BOTH_SEPARATE_PAPER_BOOKS` experiment policies) — still explicitly unsupported, per ADR 0004 Decision 6, unchanged by this milestone;
* an actual outbound webhook/email alert sink;
* destructive retention execution;
* activating any real recurring OS-level schedule without a separate, explicit operator step outside this document.

## Consequences

* Every money-affecting or Claude-cost-affecting decision in the shadow path is deterministic application code (lease, pause/kill, budget, health, alerts) — Claude's role is unchanged from ADR 0003: it analyzes supplied evidence and returns a decision object that deterministic code may or may not act on, and in the shadow-enhanced arm, never acts on for execution.
* The known, accepted limitation this ADR records: real news and real Reddit sentiment remain code-complete but environmentally pending in this session (no Alpaca market-data credential pair, no Reddit app credentials configured) — a future session with those credentials present validates them without further code changes, per Decisions in `docs/adr/0004`'s own precedent for optional providers.

## Milestone 7.1 closure (2026-07-13)

When this ADR was accepted, Decision 6 ("role/token/attempt/latency/cost enforcement
happens once per role call") and Decision 10's completeness-gates-Claude claim described
**target** behavior only — Milestone 7's own scratchpad honestly recorded both as not yet
wired into the running scheduler. Milestone 7.1 (`docs/milestones/milestone-7.1.md`,
`docs/milestones/milestone7-1-shadow-integration-closure.md`) closed this gap:

* Decision 6 is now RUNTIME-INTEGRATED: `shadow/attempt_controller.py::
  ShadowResearchAttemptController` (a shadow-specific adapter to a new, framework-neutral
  `research/orchestration.py::ResearchAttemptController` hook) checks
  `shadow/role_budget.py::check_role_budget` before every analyst and manager attempt,
  including retries, when the scheduler caller supplies `research_roles` — real-validated
  against a live Claude API call (2 role-budget checks persisted, both `PROCEED`, before
  the corresponding real attempts).
* Decision 10 is now RUNTIME-INTEGRATED: `research/scheduled_cycle.py::_run_symbol` calls
  `evaluate_completeness` automatically, before any Claude call, using a real corporate-status
  fetch — real-validated against live SEC EDGAR data for AAPL.
* Decision 5's own "actual usage... never fabricated" posture is now also
  RUNTIME-INTEGRATED at attempt granularity, not just cycle granularity:
  `shadow/budget.py::record_actual_usage_for_attempt` charges the reservation once per
  real attempt (idempotent on `attempt_id`), settled and reconciled — real-validated
  (`consumed_cost_usd` exactly equals the sum of persisted attempt-level priced usage
  after a real Claude run).
* `CycleIntent.model_name`/`.provider` are no longer derived from a
  `cycle_configuration.provider_mode` guess — `run_due_shadow_cycle` now takes explicit
  `research_provider_name`/`research_model_name` parameters, the single source of truth
  for both budget-pricing lookup and the attempt-controller's role-budget checks.

**What remains explicitly pending, honestly, after Milestone 7.1** (see that document's
own "Known limitations"/"Deferred work" sections for full detail — not restated here to
avoid this ADR drifting out of sync with the closure document over time):
real Alpaca news/market-data, real Reddit sentiment, destructive retention, full-schema
retention coverage, corporate-action evidence-registry wiring, and — unchanged from
Decision 9 — no actual recurring deployment has ever been activated.

This section does not rewrite the history above: every Decision 1-11 paragraph describes
what was true and accepted at Milestone 7's own completion, including the target-vs-active
distinction Milestone 7 itself documented honestly at the time.

## Milestone 7.2 closure (2026-07-13)

Decision 7's "health evaluation detects, a separate caller acts" boundary is unchanged, but
`evaluate_cycle_health` now additionally returns one `HealthCheckResult` per evaluated
dimension (`shadow/health.py`), persisted to a new, additive `shadow_run_health_checks` table
and explainable via a new `shadow-health-explain` CLI command — the summary verdict Decision 7
already produced is no longer opaque. Milestone 7.1's own real-validation run returned an
unexplained `health_status=PAUSE_REQUIRED`; Milestone 7.2 root-caused it as a
**RATE-DENOMINATOR BUG** (`retry_exhaustion_rate`'s denominator conflated a symbol count with
a role-invocation count) and fixed the denominator — the pause itself was, and remains,
intentional (a required role's only attempt genuinely failed to produce a valid report).
Decision 8's alerting boundary gained one addition: an automatic health-triggered pause (or a
`PAUSE_RECOMMENDED` verdict) now raises an alert where previously it raised none — the
underlying "persist before any sink is attempted, delivery failure never erases the alert"
guarantee is unchanged. See `docs/milestones/milestone7-2-shadow-health-diagnostics.md` for full detail,
including a demonstrated (but session-undemonstrated-by-real-evidence, hence unfixed)
`unsupported_claim_rate` denominator concern, and an honest activation-readiness decision
(`shadow/readiness.py::evaluate_activation_readiness`) that still reports
`NOT_READY_INSUFFICIENT_HISTORY` against this repository's real history.
