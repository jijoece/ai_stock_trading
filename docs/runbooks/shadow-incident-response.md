# Runbook: Shadow-operations incident response

Playbooks for operational incidents in the Milestone 7 shadow-operations layer. Pair with
`docs/runbooks/shadow-operations.md` for routine commands and
`docs/milestone7-production-shadow-operations.md` for architecture detail.

**Milestone 7.1 update:** `shadow_role_budget_checks` is now a real, queryable audit trail
(one row per pre-attempt budget decision, before every real Claude call) — when
investigating an unexpected `BUDGET_REJECTED`/`SKIPPED_BUDGET_EXHAUSTED` outcome, query it
directly (`SELECT * FROM shadow_role_budget_checks WHERE scheduler_run_id = ?`) rather than
only inferring budget state from `shadow-budget-status`. See
`docs/milestone7-1-shadow-integration-closure.md` for the full closure detail.

**Milestone 7.2 update:** for ANY unexplained `health_status` (`DEGRADED`/`PAUSE_RECOMMENDED`/
`PAUSE_REQUIRED`), run `shadow-health-explain --scheduler-run-id <id>` first — it returns the
exact input value, threshold, comparison, and pause-flag state for every one of the 16 health
dimensions, not just the summary reasons string. A health-triggered pause/recommendation now
also appears in `shadow-alerts` (`alert_type=PAUSE_ACTIVATED`), so `shadow-alerts` is no longer
silent about an automatic pause the way it was before this update. If `retry_exhaustion_rate`
is the triggering dimension, remember its denominator is "roles actually invoked this cycle,"
not "symbols" — a single required role failing its only attempt can legitimately produce a
100% rate when only that one role ever ran (see
`docs/milestone7-2-shadow-health-diagnostics.md` Section 7 for the real, investigated example).

## First response — always start here

Before taking any action (resuming, force-clearing a kill, force-releasing a lease), run
all three of these and read the output:

```bash
python -m trading_research.cli shadow-status
python -m trading_research.cli shadow-alerts
python -m trading_research.cli shadow-readiness
```

- `shadow-status` tells you the current pause/kill state and recent run outcomes — this is
  the single most important signal for "is something actively wrong right now."
- `shadow-alerts` tells you what was actually detected (alert type, severity, message,
  delivery status) — read this before assuming you know the cause.
- `shadow-readiness` tells you whether the system's recent history looks healthy across all
  8 categories — useful context for whether an incident is isolated or part of a pattern.

Do not resume, force-clear a kill, or force-release a lease until you understand *why* the
system entered its current state. Every override action is permanently audited
(`shadow_operator_actions`) — there is no harm in taking an extra minute to look first.

## Budget breach

**What alert fires:** `BUDGET_EXCEEDED` (or `BUDGET_NEAR_LIMIT` as an earlier warning),
severity `ERROR`/`CRITICAL`. If `safety.pause_on_budget_breach: true` (the shipped default),
the system also auto-transitions to `PAUSED_BUDGET`.

**What to check:**
```bash
python -m trading_research.cli shadow-budget-status
python -m trading_research.cli shadow-alerts
```
- Confirm which reservation breached (`reservation_id`, `estimated_cost_usd` vs
  `consumed_cost_usd`), and whether it was within the configured `emergency_margin_fraction`
  (a small overage is expected and tolerated; check
  `check_emergency_margin_breach`'s `emergency_margin_breached` flag on the reservation) or
  a genuine runaway.
- Check whether this is a single-cycle spike (one unusually expensive symbol/role) or a
  sustained trend across `shadow-run-history` — the latter suggests a config problem
  (`budgets.max_output_tokens_per_cycle` too generous, a retry loop) rather than a one-off.
- Confirm daily/monthly totals against `budgets.max_actual_cost_per_day_usd`/
  `max_actual_cost_per_month_usd` in `config/shadow_operations.yaml`.

**How to resume safely:**
1. Identify root cause (see above) before doing anything else.
2. If it was a one-off within tolerance: `shadow-resume --reason "..." --operator "..."`.
3. If it was a genuine config problem: fix `config/shadow_operations.yaml`'s budget caps or
   the underlying cause (e.g. a role generating excessive output) **first**, then resume.
4. Never raise a budget cap merely to make an alert stop firing without understanding why
   the cap was hit — that defeats the purpose of the cap.

## Lease conflict / stuck lease

**What it looks like:** `LEASE_CONFLICT` alert, or a scheduled invocation repeatedly
returning `LEASE_HELD` without ever running a cycle.

**How to diagnose:**
```bash
python -m trading_research.cli shadow-lease-status
```
Check `owner`, `acquired_at`, `expires_at`, and current wall-clock time.

- **If `expires_at` is in the future:** a legitimate concurrent invocation is genuinely
  in-flight, or a very recent one hasn't finished yet. This is normal — wait for it to
  finish or expire naturally. Do not force-release.
- **If `expires_at` is in the past** (the lease has expired but still shows `HELD`): the
  **next scheduled or manual invocation will automatically reclaim it** via stale recovery
  — no operator action is required. Stale recovery always writes a `LEASE_STALE_RECOVERED`
  operator action, visible in `shadow-status`'s recent-actions view. This is the normal,
  automatic crash-recovery path (e.g. a process that acquired the lease and then crashed
  before releasing it).
- **If you need the lease released immediately, before the next invocation would naturally
  reclaim it** (e.g. you need to run a manual cycle right now and can't wait out the TTL):
  this requires an explicit operator action — `force_release` is not exposed as a standalone
  top-level CLI subcommand in this milestone; it is invoked via the `shadow/lease.py` module
  directly by an operator with repository access, and **always** requires both `reason` and
  `operator`, and **always** writes a `LEASE_FORCE_RELEASED` operator action. Only do this
  when you are confident the lease holder is genuinely dead (e.g. you know the process
  crashed), not merely slow — force-releasing a lease held by a still-running process breaks
  the exclusion guarantee and can cause duplicate Claude calls or duplicate paper intents.

**When stale recovery kicks in automatically vs. when an operator must act:** automatically,
whenever `expires_at` has passed and a new invocation attempts `acquire()` — no waiting
period beyond the configured `lease_ttl_seconds` (default 3600s) and no operator action
needed. An operator only needs to act when you cannot wait for the TTL to elapse.

## Provider outage

**What it looks like:** `PROVIDER_UNAVAILABLE` or `PROVIDER_FAILURE_RATE_HIGH` alerts;
`shadow-readiness`'s provider category reporting `NOT_READY`; a `CYCLE_FAILED` or
`CYCLE_PARTIALLY_COMPLETE` alert whose `failure_reason` names a provider (SEC EDGAR, Alpaca
market data, Alpaca news, Anthropic).

**What to check:**
```bash
python -m trading_research.cli shadow-alerts
python -m trading_research.cli provider-health          # existing Milestone 6 command
python -m trading_research.cli evidence-provider-usage   # existing Milestone 6 command
```
- `provider-health` shows recent request success/failure/latency per provider — confirm
  whether the outage is provider-side (timeouts, 5xx, rate-limit 429s) or a local
  configuration/credential problem (401/403, which will not resolve by waiting).
- Check whether `safety.pause_on_provider_failure_rate` (default 0.50) has already
  triggered an automatic `PAUSED_PROVIDER_HEALTH` transition.

**Safe response:**
- If the provider is SEC EDGAR or Alpaca and this is a transient outage: no action needed
  beyond monitoring — the existing bounded-retry logic (`HttpJsonClient`, default
  `max_attempts=2`) and the scheduler's own catch-up semantics
  (`MISSED_WITHIN_CATCHUP`/`MISSED_TOO_OLD`) handle a short outage without operator
  intervention. Do not force-resume before the provider is confirmed healthy again —
  resuming into a still-down provider just produces more failed cycles and more alerts.
- If the failure is a credential/configuration problem (401/403, or a provider that should
  be `enabled: true` reporting as excluded from the registry): fix the underlying
  `.env`/`config/evidence_providers.yaml` issue first. Never work around a missing
  credential by disabling a safety check.
- Corporate-status derivation failing (`derive_corporate_status` returning
  `SOURCE_UNAVAILABLE`) correctly blocks screening completeness — this is expected,
  fail-closed behavior, not a bug to route around.

## Unsupported-claim-rate spike

**What it looks like:** `UNSUPPORTED_CLAIM_RATE_HIGH` alert; if
`safety.pause_on_unsupported_claim_rate` (default 0.25) is configured, an automatic
`PAUSED_RESEARCH_QUALITY` transition.

**What to check:**
```bash
python -m trading_research.cli research-failure-metrics
python -m trading_research.cli research-failures --research-run-id <id>
```
- This indicates Claude-generated claims are being rejected by the deterministic
  claim-to-evidence validator (`claim_validation.py`) at an elevated rate — check whether
  this correlates with a recent prompt-version change (`prompt_version` field on
  `research_attempts`) or a change in evidence quality/completeness feeding the committee.
- Cross-reference with `shadow-readiness`'s research category and `evidence-completeness`
  results for the affected symbols — degraded evidence quality (e.g. a provider partially
  down) can itself increase unsupported-claim rates, since the model has less to cite.

**Safe response:** investigate root cause (prompt regression vs. evidence-quality
regression) before resuming. The validator itself is never weakened as a remediation — if
claims are genuinely unsupported, rejecting them is correct behavior, not a defect. Consider
reverting a recent prompt-version change if that correlates with the spike.

## Reconciliation mismatch

**What it looks like:** `RECONCILIATION_MISMATCH` alert; if
`safety.pause_on_reconciliation_mismatch: true` (the shipped default), an automatic
`PAUSED_RECONCILIATION` transition.

**What to check:**
- This means the baseline paper-execution ledger and the broker's own reported state
  disagree — a serious signal, since it touches the one path in this system that actually
  submits orders (paper-only, never live).
- Review the affected paper intents/orders directly via the existing paper-execution
  reconciliation tooling (Milestone 3/4) before resuming shadow operations — this alert
  intentionally blocks *new* scheduled work while the mismatch is unresolved, it does not
  attempt to auto-correct the ledger.

**Safe response:** treat this as the highest-priority incident type in this list, since it
is the only one that touches money-adjacent state (even though paper-only). Resolve the
underlying reconciliation discrepancy using existing paper-execution tooling first; only
resume shadow operations after confirming the ledger and broker state agree again.

## Kill-switch activation

**What it means:** `KILLED` is the most severe state. It blocks every scheduled cycle before
any provider or Claude call is made — checked before lease acquisition. It can only be
reached via an explicit `shadow-kill` command (an operator or an automated health rule never
calls `kill()` implicitly — only `request_pause()` is ever called automatically; `kill()` is
always an explicit, deliberate action requiring both `--reason` and `--operator`).

**How to safely investigate before ever using `shadow-force-clear-kill`:**
1. `shadow-status` — find the `KILL_ACTIVATED` entry in the operator-action history: who
   killed it, when, and the recorded reason.
2. `shadow-alerts` — a `KILL_SWITCH_ACTIVATED` alert will have been raised with context.
3. Read the reason string. If it references a specific incident (budget, provider,
   reconciliation, research quality), work through that incident's own playbook above
   **first** — the kill switch does not diagnose anything, it only stops further activity.
4. Confirm the underlying cause is actually resolved, not merely no-longer-actively-firing.
   A transient provider outage that "went away" is not the same as "confirmed root-caused."
5. Only then: `shadow-force-clear-kill --reason "<what you found and fixed>" --operator
   "<you>"`. This is a separate, explicit command from `shadow-resume` by design —
   `shadow-resume` structurally cannot clear `KILLED` (it raises `PauseStateError`), so
   there is no accidental path back to `ACTIVE` from `KILLED`.
6. After force-clearing, the state returns to `ACTIVE` directly (not through a `PAUSED_*`
   intermediate state) — run one manual `run-due-shadow-cycle` and watch it closely before
   trusting the recurring schedule again, if one is active.

## Escalation notes

- Every action described in this document (pause, resume, kill, force-clear-kill,
  force-release-lease) is permanently recorded in `shadow_operator_actions` — when in doubt
  about "what happened and who did it," this table (surfaced via `shadow-status`) is the
  source of truth.
- No override in this system can ever enable live trading, enhanced-arm execution, or bypass
  `real_orders`' write-blocked status — those boundaries are structural (config-load-time or
  construction-time failures), not policy choices an incident response could accidentally
  weaken.
- If an incident reveals a genuine code defect (not just a configuration/threshold tuning
  issue), file it against the relevant module and reference the milestone doc's "Known
  limitations"/"Deferred work" sections — several structural gaps (e.g. per-role budget
  enforcement not wired end-to-end, scheduler pricing lookup not model-aware) are already
  documented there and may be the root cause.
