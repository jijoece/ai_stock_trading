# Runbook: Shadow operations

Operator-facing procedures for the Milestone 7 shadow-operations control layer. See
`docs/milestone7-production-shadow-operations.md` for the full architecture and design
rationale, and `docs/adr/0005-production-shadow-operations-boundary.md` for why each
boundary exists.

**Before you start:** shadow operations ships fully disabled. `config/shadow_operations.yaml`
has `shadow_operations.enabled: false` and `schedule.enabled: false` out of the box. No
recurring schedule has ever been activated on any machine this code has run on — read
"Activate the launchd artifact" below carefully before doing so for the first time.

**Milestone 7.1 update:** `run-due-shadow-cycle` now accepts `--provider-mode {fixture,real}`
(default `fixture`) and repeatable `--symbol`. Corporate-status evidence, evidence-completeness
gating, and per-role budget enforcement are now active in the real scheduled-cycle path — see
`docs/milestone7-1-shadow-integration-closure.md` for what changed and
`docs/adr/0005-production-shadow-operations-boundary.md`'s "Milestone 7.1 closure" section.
A real-mode invocation still requires `research.yaml`'s `provider`/`model` to be set and,
for `provider: anthropic`, a matching `config/research_pricing.yaml` entry and
`ANTHROPIC_API_KEY` — missing any of these fails closed before any lease/DB work, reported
as `MISSING_CREDENTIALS`/`PRICING_NOT_CONFIGURED` in the CLI's JSON output.

**Milestone 8 update:** shadow operations still only ever submits baseline-arm paper orders
through the legacy global `paper/ledger.py` path (unchanged) — isolated baseline/enhanced
paper books are a separate, additive subsystem (`config/paper_books.yaml`,
`docs/runbooks/paper-book-operations.md`) not yet wired into the shadow scheduler. Wiring
`paper_books/` into `run_due_shadow_cycle` is recommended future work (see
`docs/milestone8-isolated-paper-portfolios.md` Section 21).

**Milestone 7.2 update:** every `run-due-shadow-cycle` invocation that reaches health
evaluation now persists one field-level diagnostic row per health dimension. Explain any
run's verdict directly:

```bash
python -m trading_research.cli shadow-health-explain --scheduler-run-id <id>
python -m trading_research.cli shadow-health-explain --cycle-id <id>
```

`shadow-readiness` also now reports an `activation_readiness` block (`READY_FOR_MANUAL_SHADOW_RUNS`
/ `READY_FOR_LIMITED_RECURRING_SHADOW` / `NOT_READY_*` / `ENVIRONMENTALLY_BLOCKED`) — see
`docs/milestone7-2-shadow-health-diagnostics.md` for the full vocabulary and evaluation order.
An automatic health-triggered pause (or `PAUSE_RECOMMENDED` verdict) now also raises a
`shadow-alerts` entry, which it previously did not.

## Enable shadow operations

1. Open `config/shadow_operations.yaml`. The shipped defaults:

   ```yaml
   shadow_operations:
     enabled: false
     mode: SHADOW_ENHANCED
     allow_baseline_paper_submission: false
     allow_enhanced_submission: false   # cannot be set true — config load fails closed
   schedule:
     enabled: false
     cadence: DAILY_MARKET_DAY
     intended_local_time: "06:45"
   ```

2. To allow manual/scheduled cycles to run at all, set `shadow_operations.enabled: true`.
3. To allow the baseline arm to actually submit paper orders (as opposed to running
   research-only), set `allow_baseline_paper_submission: true`. Leave `false` while
   validating.
4. To allow a recurring schedule to be treated as due (as opposed to only manual
   `run-due-shadow-cycle` invocations), set `schedule.enabled: true` and adjust
   `intended_local_time`/`run_window_start`/`run_window_end`/`run_window_timezone` under
   `shadow_operations` as needed.
5. Do **not** attempt to set `allow_enhanced_submission: true` — config loading will raise
   `ShadowOperationsConfigError` immediately. The enhanced (Claude-informed) arm is
   structurally forbidden from ever submitting an order in this milestone.
6. Review `budgets.*` (per-cycle/daily/monthly caps, token/latency limits) and `safety.*`
   (pause thresholds) before enabling — the shipped defaults are reasonable starting points,
   not validated production values.
7. If you intend real-Claude cycles, ensure `config/research_pricing.yaml` has a matching
   pricing entry for your model — an unpriced `anthropic`-provider cycle fails closed
   (`BudgetConfigError`) before any API call.

Restart or re-invoke the CLI after editing the YAML — configuration is loaded fresh on every
invocation, there is no running process to restart.

## Run a manual cycle

```bash
python -m trading_research.cli run-due-shadow-cycle
```

This performs **at most one** intended scheduled cycle and exits — there is no loop, no
daemon. Expected outcomes:

- `shadow_operations.enabled: false` → exit 0, `{"status": "DISABLED", "is_successful_no_op": true, ...}`.
- Not yet due (before the intended local time, or a market holiday, or outside the run
  window) → exit 0, an explicit `NOT_DUE`/`MARKET_HOLIDAY`/`OUTSIDE_RUN_WINDOW` status —
  still a successful no-op, not an error.
- Already completed for today's intended slot → exit 0, `ALREADY_COMPLETED` — safe to
  re-run any number of times (idempotent).
- System paused or killed → non-error blocked status naming the current pause state; no
  lease is attempted.
- Due and clear → runs the cycle, settles budget, releases the lease, returns a structured
  `ShadowCycleRunResult` as JSON.

Re-running the command is always safe. It is the intended way to both trigger a fresh cycle
and to recover after a missed/crashed invocation (the scheduler resolves catch-up status
automatically, bounded by `max_catch_up_cycles`).

## Check status

```bash
python -m trading_research.cli shadow-status          # pause/kill state + recent runs
python -m trading_research.cli shadow-readiness        # 8-category readiness report
python -m trading_research.cli shadow-budget-status     # daily/monthly usage vs caps
python -m trading_research.cli shadow-alerts            # recent alerts + delivery status
python -m trading_research.cli shadow-lease-status       # current lease holder/expiry
python -m trading_research.cli shadow-run-history        # recent scheduler runs/summaries
```

Run these before *and* after any pause/resume/kill action, and always before resuming from
a paused or killed state (see the incident-response runbook's "first response" checklist).

`shadow-readiness` will not report `READY`/`READY_WITH_WARNINGS` from a small number of
cycles — it requires (by default) at least 10 completed cycles and 5 real-provider cycles
before the overall status can leave `INSUFFICIENT_DATA`, regardless of how healthy each
individual category looks. This is intentional; do not interpret `INSUFFICIENT_DATA` as a
problem to be worked around.

## Pause, resume, and kill

```bash
python -m trading_research.cli shadow-pause \
  --reason "operator maintenance"

python -m trading_research.cli shadow-resume \
  --reason "maintenance complete" --operator "your-name"

python -m trading_research.cli shadow-kill \
  --reason "critical safety issue" --operator "your-name"

python -m trading_research.cli shadow-force-clear-kill \
  --reason "root cause identified and fixed" --operator "your-name"
```

- `shadow-pause` blocks all new scheduled work (`shadow-status` will show a `PAUSED_*`
  state). It requires `--reason` but not `--operator`.
- `shadow-resume` clears any `PAUSED_*` state back to `ACTIVE`. **`shadow-resume` cannot
  clear a `KILLED` state** — it will refuse with exit code 2 and the message
  `"cannot resume from KILLED — use force_clear_kill (requires operator + reason)"`. This is
  a deliberate, non-bypassable safety boundary: `pause.py::resume()` raises
  `PauseStateError` while the current state is `KILLED`, and there is no flag or option to
  override it.
- `shadow-kill` is the emergency stop. It requires both `--reason` and `--operator`.
  `KILLED` blocks every scheduled cycle before any provider or Claude call — checked before
  lease acquisition.
- `shadow-force-clear-kill` is the **only** way out of `KILLED`. It is a separate, explicit
  command (never an implicit side effect of `shadow-resume`) and requires both `--reason`
  and `--operator`. Every use is recorded as a `KILL_FORCE_CLEARED` operator action —
  investigate the root cause before running this (see the incident-response runbook).

`shadow-resume`/`shadow-kill`/`shadow-force-clear-kill` all require `--operator` even though
the milestone's own suggested-CLI shorthand only shows `--reason` — this is deliberate:
`pause.py` structurally requires a non-empty operator string for these three actions, and
defaulting it silently would weaken the audit trail for the highest-stakes commands.

Every pause/resume/kill/force-clear action is persisted to `shadow_pause_state` (current
state + full history) and `shadow_operator_actions` (append-only audit trail) — nothing is
ever silently overwritten.

## Activate the launchd artifact

Full step-by-step detail lives in `deploy/launchd/README.md` — read it before doing this for
the first time. Summary:

1. Copy both example files, dropping the `.example` suffix:
   ```bash
   cp deploy/launchd/com.agentic-trading-desk.shadow.plist.example \
      ~/Library/LaunchAgents/com.agentic-trading-desk.shadow.plist
   cp deploy/launchd/run_shadow_cycle.sh.example \
      deploy/launchd/run_shadow_cycle.sh
   chmod +x deploy/launchd/run_shadow_cycle.sh
   ```
2. Edit `run_shadow_cycle.sh`: set `REPO_DIR` (absolute path to this repo) and `LOG_DIR`
   (where per-invocation logs go).
3. Edit the copied plist: point `ProgramArguments` at your edited wrapper script's absolute
   path, set real `StandardOutPath`/`StandardErrorPath`, and uncomment + fill in
   `StartCalendarInterval` with your desired firing time. This does not need to exactly
   match `config/shadow_operations.yaml`'s `schedule.intended_local_time` — the scheduler
   re-evaluates due-ness fresh on every invocation and tolerates launchd firing a few
   minutes early or late.
4. **This step activates recurring execution — confirm shadow operations is genuinely ready
   before running it:**
   ```bash
   launchctl load ~/Library/LaunchAgents/com.agentic-trading-desk.shadow.plist
   ```
5. Verify it loaded: `launchctl list | grep com.agentic-trading-desk` should show the label.

Validate without activating (safe at any time):

- `plutil -lint path/to/the.plist` — syntax check only, no side effects.
- `bash deploy/launchd/run_shadow_cycle.sh` — run the wrapper manually in the foreground,
  once, outside launchd entirely. This is the fastest way to confirm paths and environment
  loading before trusting launchd with it.
- `launchctl list | grep com.agentic-trading-desk` — confirm nothing is loaded yet.

As of this milestone, no `launchctl load` has ever been run against these artifacts in this
repository's history — activating it is a deliberate, separate operator decision, not
something any prior implementation session performed.

## Deactivate

```bash
launchctl unload ~/Library/LaunchAgents/com.agentic-trading-desk.shadow.plist
```

Unloading stops future scheduled firings. It does not interrupt a currently in-flight
invocation, which runs to completion and respects its own lease TTL as usual. To stop
shadow operations immediately regardless of what launchd is doing, use `shadow-kill` (see
above) — this is the faster, safer path if you need scheduled work to stop *right now*
rather than merely stop being triggered in the future.

## Retention

```bash
python -m trading_research.cli retention-plan            # read-only classification + counts
python -m trading_research.cli retention-apply --dry-run  # read-only diff of what WOULD change
python -m trading_research.cli retention-apply            # raises NotImplementedError
```

- `retention-plan` and `retention-apply --dry-run` are both strictly read-only — neither
  ever executes a `DELETE`/`UPDATE`/`INSERT` statement.
- `retention-apply` **without** `--dry-run` always raises `NotImplementedError`. This is
  intentional (ADR 0005 Decision 11), not a bug: real destructive cleanup has not been built
  in this milestone, and will not be added without a dedicated, separately-reviewed task
  with its own tests.
- Every table is classified into one of four tiers: `PERMANENT_AUDIT` (never eligible for
  deletion), `RETAIN_N_DAYS`, `RETAIN_N_DAYS_THEN_HASH_ONLY`, or
  `RETAIN_INDEFINITELY_ACTIVE_EVALUATION`.

## FAQ

**Q: I ran `run-due-shadow-cycle` twice in a row. Did it run two cycles?**
No. The second invocation resolves the same intended schedule slot as `ALREADY_COMPLETED`
and is a successful no-op. This is enforced independently of any lease (idempotency, not
mutual exclusion).

**Q: Two operators/machines ran `run-due-shadow-cycle` at the same moment. What happened?**
Exactly one acquired the lease; the other received `LEASE_HELD` and made zero provider or
Claude calls. See the incident-response runbook for how to investigate a persistent lease
conflict.

**Q: I want to increase the daily Claude budget cap. Where?**
`config/shadow_operations.yaml`'s `budgets.max_actual_cost_per_day_usd` /
`max_actual_cost_per_month_usd`. Changes take effect on the next invocation — there is
nothing to restart.

**Q: Can I make `shadow-resume` clear a `KILLED` state by passing some flag?**
No, by design. Use `shadow-force-clear-kill` explicitly, and only after investigating why
the system was killed (see the incident-response runbook).

**Q: Does enabling `shadow_operations.enabled: true` submit real trades?**
No. It only allows the *baseline* arm to submit *paper* orders, and only if
`allow_baseline_paper_submission: true` is also set. The enhanced (Claude-informed) arm can
never submit anything — `allow_enhanced_submission` cannot be set to `true` at all. There is
no live-trading path anywhere in this system.

**Q: How do I know if shadow operations is "ready" for unattended recurring operation?**
Run `shadow-readiness`. It will not report `READY` until real, sustained history exists
(minimum sample-size floors apply on top of individual category health) — a handful of
manual or smoke-test cycles is not sufficient, and the report will say so honestly via
`INSUFFICIENT_DATA` rather than a premature `READY`.

**Q: Where do I see what a real Claude cycle actually cost?**
`shadow-budget-status` for the reservation/settlement view, or query `research_attempts`
directly for authoritative per-attempt token/cost/latency — the scheduler's own
`budget_consumed_usd` field is not yet wired to real per-cycle cost data (see the milestone
doc's "Known limitations").
