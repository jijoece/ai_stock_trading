# AI Stock Trading — Current-Main Remediation Plan

**Repository:** `jijoece/ai_stock_trading`  
**Audit baseline:** `e76029062083f47e8e6a21d0e24b3103d70ac38a`  
**Current main reviewed:** `83e9217dc36862f0dd60ecf5fbc902613dd5f8b3`  
**Purpose:** Implement the major confirmed audit issues in small, reviewable Claude Code sessions without enabling live trading or making real provider/broker calls.

## 1. Current disposition

The uploaded audit contains 96 findings, but many are duplicates reported through different review lenses. Treat them as root-cause clusters rather than 96 independent changes.

### Already addressed or materially improved after the audit

The commits after the audit baseline primarily close the persistent research-token-budget gap:

- `PersistentResearchTokenBudgetController` is now wired into configured research-cycle paths.
- A configured daily token cap fails closed when no controller is supplied.
- Ambiguous reservations block retries across UTC dates.
- Reservation payload mismatches and reconciliation evidence conflicts fail closed.
- Missing authoritative reasoning-token usage no longer silently becomes zero.
- Production-path and migration regression tests were added.

Do **not** reopen these changes unless a regression test demonstrates a remaining defect.

### Partially addressed

The pre-research shortlist and daily candidate/fresh-cycle gates remain separate from token accounting. `decide_research_action`, strategy shortlist selection, and the daily candidate limits still need production wiring. Treat this as a later operational-wiring session, not as a reason to rewrite the completed token-budget work.

### Highest-priority open root causes

1. External-paper approval phrase is documentation-only.
2. External daily-notional reservation and `SUBMISSION_REQUESTED` are not one transaction.
3. Retry can strand a superseded reservation if the process dies between commits.
4. Prior-day `RESERVED` rows can carry forward without charging the current UTC-day cap.
5. Safety pause/critical-alert checks are outside the order lease and fenced transaction.
6. Local paper-book daily new notional is always passed as zero.
7. Event-catalyst content identity changes as wall-clock time advances.
8. Backtest daily-loss, R-multiple, slippage, benchmark, event-input identity, and DST handling are incorrect or incomplete.
9. Research-run claiming and attempt persistence are not crash-safe or concurrency-safe.
10. Capability flags such as `research.enabled`, `scheduled_research.enabled`, and `shadow.schedule.enabled` are not authoritative.
11. Legacy paper synchronization has non-atomic writes and aborts on unknown broker status.
12. CI, migration, and security hardening remain incomplete.

## 2. Required safety posture during the remediation

Until Sessions 1–3 are merged:

- Keep external Alpaca paper submission disabled.
- Do not run `external-paper-submit` or `external-paper-retry-submit` against a credentialed runtime.
- Use fake runtimes, temporary SQLite databases, and deterministic fixtures only.

Until Sessions 4–5 are merged:

- Keep unattended research and paper scheduling disabled.
- Manual deterministic/offline research may continue.

At all times:

- Do not add live-trading capability.
- Keep `live_trading_enabled`, `allow_live_promotion`, `allow_enhanced_submission`, and equivalent controls false.
- Do not use real broker, model-provider, Reddit, SEC, or market-data credentials during implementation.
- Do not weaken an invariant merely to make a test pass.

## 3. Session workflow

Each Claude Code session should:

1. Pull the latest `main` and record the starting SHA.
2. Read this plan and the previous session’s handoff.
3. Revalidate the cited finding against current code before editing.
4. Create one focused branch.
5. Add failing regression tests before or alongside the implementation.
6. Keep database changes additive and migration-safe.
7. Run focused tests, the full offline suite, and the blocking safety Pyright project.
8. Update `docs/remediation/current-main-audit-progress.md`.
9. Commit and provide a handoff for the next session.
10. Never call a real provider or broker.

Suggested validation baseline:

```bash
pytest tests/ -q --tb=short
(
  cd paper_runtime
  pytest tests/ -q --tb=short
)
pyright --project pyright-safety.json
```

Add focused commands for the files modified in each session.

---

# Session 0 — Establish the authoritative disposition

**Branch:** `audit/current-main-disposition`

## Goal

Create a durable mapping from the 96 audit findings to consolidated root causes and current-main status.

## Steps

1. Confirm `main` SHA and compare it with audit SHA `e760290...`.
2. Review PR #39 and the token-budget commits after the audit.
3. Create `docs/remediation/current-main-audit-progress.md`.
4. Record every finding as one of:
   - `RESOLVED_ON_CURRENT_MAIN`
   - `PARTIALLY_RESOLVED`
   - `CONFIRMED_OPEN`
   - `NEEDS_REPRODUCTION`
   - `DEFERRED_LEGACY_PATH`
5. Consolidate duplicate findings under one root-cause ID.
6. Add a “do not implement blindly” list for findings whose reachability verifier refuted the production scenario, especially F-027/F-032/F-056 and legacy-only reconciliation concerns.
7. Make no production code changes.

## Acceptance criteria

- Every High finding has a current-main disposition.
- F-001/F-008/F-009 are not incorrectly treated as fully open; persistent token budgeting is marked resolved while candidate-cap wiring remains open.
- Each remaining root cause points to a future session in this plan.
- The document records exact test commands and current results.

## Claude Code prompt

> Review the uploaded audit against the latest `main` of `jijoece/ai_stock_trading`. Do not change production code. Create `docs/remediation/current-main-audit-progress.md`, consolidate duplicate findings into root causes, and classify each finding as resolved, partial, open, needs reproduction, or legacy/deferred. Pay special attention to commits after `e760290...`, especially PR #39. Run only offline tests. End with a session handoff listing the next branch, open risks, and exact validation results.

---

# Session 1 — Enforce external-paper human approval and fix the runbook

**Branch:** `fix/external-paper-approval-gate`

**Findings:** F-002, F-006, F-007, F-038, F-044, F-051, F-054, F-068, F-083

## Goal

Make the documented human approval requirement executable at the domain boundary and document the required baseline activation step.

## Implementation steps

1. Add a canonical constant and policy version, for example:
   - `EXTERNAL_PAPER_APPROVAL_PHRASE`
   - `EXTERNAL_PAPER_APPROVAL_POLICY_VERSION`
2. Add a dedicated `approval_phrase` parameter. Do not overload `reason`.
3. Enforce the exact phrase inside `submit_external_paper_order`.
4. Enforce it inside `retry_external_paper_order`, because retry can create broker exposure.
5. Do not require the exposure-creation phrase for account check, preview, reconciliation, or cancellation.
6. Add CLI `--approval-phrase`; keep `--reason` as free-form audit context.
7. Fail before lease acquisition, reservation, runtime creation, or broker contact.
8. Return a stable error code such as `EXTERNAL_PAPER_APPROVAL_REQUIRED`.
9. Do not store the raw phrase as a secret-like free-text artifact. Persist the policy version and verified status only if a schema field already cleanly supports it; otherwise defer persistence to a separate additive migration.
10. Update:
    - `docs/runbooks/alpaca-paper-operations.md`
    - README external-paper sequence
    - relevant milestone/current-state documentation
11. Insert `external-paper-activate-baseline` between account check and preview/submit and explain when reactivation is required.

## Tests

- Missing phrase is rejected.
- Approximate phrase is rejected.
- Exact phrase succeeds with a fake runtime.
- Runtime methods are never called when approval fails.
- Retry requires the phrase.
- Cancel/reconcile remain available without the exposure-creation phrase.
- CLI passes `reason` and `approval_phrase` separately.
- Runbook command sequence contains baseline activation.

## Acceptance criteria

- No external submit/retry path can reach a runtime with an invalid phrase.
- The documented operator sequence is executable as written.
- No live capability is added.

## Claude Code prompt

> Implement Session 1 from `docs/remediation/current-main-audit-plan.md`. Enforce the exact external-paper approval phrase at the domain boundary for initial submission and retry, using a dedicated `approval_phrase` argument separate from `reason`. Fail before any lease, reservation, or runtime call. Update CLI tests and the Alpaca paper runbook, including the missing baseline-activation command. Use fake runtimes only. Do not call Alpaca or add live trading.

---

# Session 2 — Make external reservation and submission checkpoint co-atomic

**Branch:** `fix/external-paper-atomic-submit-checkpoint`

**Findings:** F-004, F-005, F-014, F-025, F-034, F-037, F-039

## Goal

Commit the attempt-scoped daily-notional reservation, retry supersede transition, cash/share reservation, and `SUBMISSION_REQUESTED` event in one `BEGIN IMMEDIATE` transaction.

## Design requirement

The order lease’s `fenced_write()` already provides the transaction boundary. Refactor the daily-notional helper so it can run inside that transaction rather than opening and committing its own transaction.

## Implementation steps

1. Split `_reserve_daily_notional` into:
   - a transaction-owning compatibility wrapper only if still needed; and
   - an internal `commit=False`/“transaction required” helper.
2. Move initial attempt reservation creation into `_submit_once`’s first fenced write.
3. For retry, move:
   - prior attempt transition to `SUPERSEDED_BY_RETRY`;
   - new attempt reservation insert;
   - cash/share reservation;
   - `SUBMISSION_REQUESTED` append;
   into the same fenced write.
4. Recalculate the account/day total inside this transaction.
5. Remove the prior-day `RESERVED` carry-forward shortcut.
6. For new code, a crash before commit must leave none of the above rows changed.
7. For legacy orphan rows already present in databases, fail closed with a specific repair-required error rather than silently reusing them.
8. Preserve idempotent same-attempt replay by validating every immutable reservation field.
9. Keep the broker call strictly after the atomic checkpoint commit.

## Crash-injection tests

Add failpoints or monkeypatches for:

- after daily reservation calculation but before insert;
- after reservation insert but before event append;
- after retry supersede but before new reservation;
- after new reservation but before event append;
- after event append but before commit;
- immediately after commit but before runtime call.

Expected result:

- Before-commit failures roll back everything.
- After-commit/before-runtime failures leave a recoverable `SUBMISSION_REQUESTED` chain.
- No orphan `RESERVED` attempt consumes daily capacity without a matching event.
- A retry can always proceed through a supported recovery path.

## Acceptance criteria

- One transaction owns the complete pre-broker state transition.
- Cross-day attempts charge the current UTC day.
- Daily cap remains account-wide under two real SQLite connections.
- Existing recovery behavior for a committed `SUBMISSION_REQUESTED` checkpoint remains intact.

## Claude Code prompt

> Refactor the external-paper pre-broker write path so the attempt daily-notional reservation, any retry supersede transition, local cash/share reservation, and `SUBMISSION_REQUESTED` event are committed in one `lease.fenced_write()` transaction. Remove prior-day reservation carry-forward. Add crash-injection and two-connection race tests before changing behavior. Preserve fail-closed idempotency and keep the broker call after the durable checkpoint. Do not use a real runtime.

---

# Session 3 — Close the external-paper safety race and add orphan repair

**Branch:** `fix/external-paper-safety-fencing-repair`

**Findings:** F-013, F-040, F-067, F-077, F-082 plus legacy orphan recovery from Session 2

## Goal

Make pause/alert/reconciliation checks authoritative at the final checkpoint and provide a supported repair path for pre-existing orphan reservations.

## Implementation steps

1. Re-run `_safety_checks` inside the first fenced write immediately before the checkpoint.
2. Keep the early preflight check for fast failure, but treat the in-transaction check as authoritative.
3. Confirm lease ownership inside the same transaction.
4. Persist critical reconciliation evidence before raising `ORDER_MISSING_AT_BROKER`.
5. Add a read-only detector for orphan attempt reservations:
   - active reservation;
   - no matching attempt event;
   - chain still at `PREVIEWED` or prior `UNKNOWN`;
   - no broker evidence proving submission.
6. Add an explicit operator repair command. It may transition only a provably unsubmitted orphan to a terminal budget state. Never delete immutable audit rows.
7. Require operator, reason, and exact scope identity.
8. Add concurrency tests that create a pause/critical alert while a submit waits for the lease.

## Acceptance criteria

- A safety state raised before the checkpoint prevents submission.
- A safety state raised after checkpoint commit does not cause a blind retry; normal recovery/reconciliation owns that state.
- Legacy orphan reservations can be repaired without direct SQL.
- Repair cannot release a reservation with broker acceptance evidence.

## Claude Code prompt

> Make the external-paper safety gate authoritative inside the fenced pre-broker transaction. Persist reconciliation evidence for missing broker orders. Add an operator-only, fail-closed orphan-reservation detector and repair action for legacy states that cannot arise after Session 2. Use immutable state transitions, never deletes. Add real two-connection tests and fake runtime assertions.

---

# Session 4 — Enforce local paper-book daily notional atomically

**Branch:** `fix/local-paper-daily-notional`

**Findings:** F-010 and related concentration-race concerns

## Goal

Stop passing `Decimal("0")` as the accumulated daily new notional and make the per-book daily limit concurrency-safe.

## Implementation steps

1. Define which BUY states consume daily new-notional capacity.
2. Add a repository query for the per-book UTC-day total, or introduce an append-only local daily reservation table if query-only accounting cannot cover pre-fill/pending orders.
3. Run the read-cap-decide-persist flow under `BEGIN IMMEDIATE`.
4. Update both scheduled integration and direct paper-book cycle paths.
5. Count each intent once using deterministic identity.
6. Correctly handle:
   - multiple symbols in one cycle;
   - retries/replays;
   - rejected intents;
   - cancelled/unfilled intents according to the documented policy;
   - two concurrent processes.
7. Keep external-account reservation accounting separate.

## Tests

- Second order is capped after the first consumes capacity.
- Same intent replay does not double count.
- Two connections cannot jointly exceed the cap.
- Existing cash, position-weight, symbol-concentration, and open-position gates still apply.

## Claude Code prompt

> Implement authoritative per-book, per-UTC-day new-BUY notional accounting for local paper books. Replace every production `daily_new_notional_usd=0` call with persisted accounting and make check-plus-persist atomic under SQLite `BEGIN IMMEDIATE`. Add multi-symbol, replay, and two-connection race tests. Do not modify the external Alpaca reservation subsystem.

---

# Session 5 — Make research runs claimable, resumable, and capability-gated

**Branch:** `fix/research-run-recovery-activation`

**Findings:** F-023, F-024, F-028, F-029, F-030, F-031, F-045, F-053, F-058

## Goal

Prevent duplicate provider calls, recover crashed runs safely, and make enablement flags authoritative.

## Implementation steps

1. Add an atomic research-run claim/lease keyed by `research_run_id`.
2. On entry:
   - terminal run: reuse;
   - active unexpired run: return in-progress/fail closed;
   - expired `RUNNING`: mark failed/recovering and resume safely.
3. Ensure `RUN_STATUS_FAILED` is actually persisted.
4. Replace blind `save_attempt` insertion with idempotent conflict validation:
   - identical attempt payload: reuse/no-op;
   - different payload under same ID: explicit integrity error.
5. Decide whether attempt + role report should be one transaction or whether a persisted valid attempt can be promoted to a role report on resume without another provider call.
6. Add two-worker tests proving only one provider invocation occurs.
7. Enforce flags at the correct boundaries:
   - `run-research` → `research.enabled`;
   - scheduled cycle → `scheduled_research.enabled`;
   - due shadow scheduler → `shadow_operations.enabled` and `schedule.enabled`.
8. Avoid ambiguous bypasses. Any manual override must be explicit, separately named, and tested.
9. Use deterministic/fake providers only.

## Acceptance criteria

- Crash after attempt persistence does not require another provider call.
- Concurrent identical runs do not double-consume tokens.
- Disabled capability flags stop work before provider initialization or state mutation.
- Token-budget changes from PR #39 remain intact.

## Claude Code prompt

> Add atomic research-run claiming and crash-safe resume semantics. Prevent duplicate provider calls for the same deterministic run ID, make attempt persistence idempotent with immutable-payload validation, and persist failed/orphaned states. Enforce `research.enabled`, `scheduled_research.enabled`, and `shadow.schedule.enabled` at their authoritative entry points. Preserve the completed token-budget work and use fake providers only.

---

# Session 6 — Wire shortlist and daily candidate gates into scheduled research

**Branch:** `feat/strategy-shortlist-research-gate`

**Findings:** remaining portion of F-001 plus F-042, F-043, F-058, F-073, F-075

## Goal

Wire the deterministic strategy shortlist and pre-research candidate/fresh-cycle caps into production scheduled research without conflating them with token accounting.

## Implementation steps

1. Confirm `strategy_candidate_selection.enabled` and activation stage semantics.
2. Build or reuse a deterministic shortlist before research orchestration.
3. Invoke `decide_research_action` for each candidate.
4. Persist:
   - shortlist identity;
   - decision;
   - reason;
   - daily counters;
   - whether the orchestrator reused or ran fresh research.
5. Make daily candidate/fresh-cycle counters atomic across workers.
6. Keep manual one-symbol diagnostic research available only through an explicitly separate path.
7. Update documentation that currently describes the target flow as current behavior.

## Acceptance criteria

- Disabled selector produces no strategy scan or provider call.
- Candidate and fresh-cycle caps are enforced across restarts.
- Reused research does not incorrectly consume a fresh-cycle slot.
- Token reservations still wrap every configured provider call.

## Claude Code prompt

> Wire the deterministic strategy shortlist and `decide_research_action` into scheduled research behind the existing activation flag. Persist every pre-research decision and enforce daily symbol/fresh-cycle caps atomically. Keep this separate from the already-completed persistent token budget. Add restart and two-worker tests; use no real providers.

---

# Session 7 — Correct backtest risk and execution semantics

**Branch:** `fix/backtest-risk-execution-correctness`

**Findings:** F-003, F-015, F-016, F-017, F-047, F-048, F-061, F-062

## Goal

Correct the backtest controls that can materially distort strategy results.

## Implementation steps

1. Daily loss:
   - anchor day-start equity to the prior session’s close-mark equity;
   - include unrealized overnight gaps;
   - define first-session behavior.
2. R-multiple:
   - store `entry_risk_per_share = entry_price - actual_stop`;
   - use it for partial-profit triggers and any R-based trailing logic.
3. Slippage:
   - ensure a fill never falls outside the bar’s observed range;
   - pass bar bounds into sell-fill calculation.
4. Benchmark:
   - validate/fetch the benchmark before the session loop.
5. Backtest identity:
   - include normalized economic events and any other result-affecting external input in `input_hash`.
6. Entry timestamp:
   - derive US market-open time with `America/New_York`, then convert to UTC; do not hard-code 14:30 UTC year-round.
7. Add a migration-safe policy for legacy rows with null/old input hashes.

## Tests

- Gap-down held position blocks a new entry under daily-loss policy.
- Strategy stop defines 1R.
- Stop-gap fill remains at or above bar low.
- Benchmark misconfiguration fails before work.
- Economic-event changes create a new run identity.
- EDT and EST dates use correct UTC market-open time.

## Claude Code prompt

> Fix the backtest daily-loss anchor, actual-stop R-multiple calculation, bar-bounded slippage, early benchmark validation, economic-event input hashing, and DST-aware market-open timestamp. Add focused deterministic fixtures for every defect before implementation. Do not change strategy policy beyond correcting the stated semantics.

---

# Session 8 — Stabilize strategy identity and validation

**Branch:** `fix/strategy-signal-identity-validation`

**Findings:** F-011, F-052, F-093, F-094, F-095

## Goal

Make content identity stable across re-evaluation while changing when the selected economic event or strategy inputs change.

## Implementation steps

1. Remove wall-clock-derived event ages from the content payload, or derive them from stable `data_as_of`.
2. Add stable selected-event identity to the signal content.
3. Remove duplicate factor keys.
4. Verify content ID:
   - unchanged data, later evaluation time → same ID;
   - different selected event → different ID;
   - changed economically meaningful factor → different ID.
5. Fail closed on non-positive, inverted, or otherwise invalid ATR-derived stop levels in momentum and mean-reversion strategies.
6. Document relative-strength scale assumptions or normalize them.

## Claude Code prompt

> Make strategy signal content IDs stable across wall-clock re-evaluation and sensitive to selected-event identity. Remove time-varying age fields from the hashed content, eliminate duplicate factor keys, and add fail-closed stop validation for momentum and mean reversion. Add property-style tests for identity stability and change sensitivity.

---

# Session 9 — Retire or harden the legacy paper execution path

**Branch:** `refactor/legacy-paper-execution-boundary`

**Findings:** F-012, F-022, F-033, F-055, F-070, F-077, F-081, F-087

## Goal

Choose one deliberate path: remove an orphaned legacy subsystem, or make it transactionally safe.

## Decision gate

Before coding, enumerate every production caller of:

- `submit_credentialed_paper_order`
- `sync_paper_orders`
- legacy market-intent builder
- legacy reconciliation commands

### Preferred outcome

If no supported operational workflow uses them, retire or hard-disable the CLI surface and document migration to `paper_books/external_broker.py`.

### Retention outcome

If retained:

1. Wrap event save, ledger fill application, event-applied marker, submission update, and final result in one transaction.
2. Use insert-with-conflict-validation for deterministic event IDs.
3. Persist unknown broker statuses as bounded failure/reconciliation evidence and continue the sweep.
4. Make account/position reconciliation snapshots atomic.
5. Align or explicitly quarantine MARKET intents from the limit-only authoritative path.

## Claude Code prompt

> Investigate the legacy paper execution/sync path before editing. If it has no supported production caller, retire or hard-disable it and update docs. If it must remain, make `_sync_one` and reconciliation atomic, validate deterministic-event conflicts, persist unknown statuses, and continue the sweep. Do not create a second authoritative execution model.

---

# Session 10 — Harden migrations and CI/security

**Branch:** `chore/migration-ci-security-hardening`

**Findings:** F-018–F-021, F-035, F-036, F-050, F-063–F-066, F-071, F-072, F-089, F-091

## Migration steps

1. Replace O(N×4) row-by-row migration probes with set-based SQL or bounded batches.
2. Add forward-progress checkpoints.
3. Quarantine irreconcilable legacy rows in a persistent migration-conflict table rather than bricking every `connect()`.
4. Make trigger replacement transactional.
5. Add fixture databases representing previous schema versions and conflicting legacy rows.
6. Test concurrent startup during migration.

## CI/security steps

1. Add a Python matrix covering the declared minimum and current supported versions.
2. Expand the blocking Pyright safety set to external broker, risk, execution services, and migrations.
3. Add Ruff.
4. Add `pip check` and a dependency audit.
5. Add a focused security scan where signal quality is acceptable.
6. Pin GitHub Actions to immutable SHAs.
7. Add least-privilege `permissions`, job timeouts, and concurrency cancellation.
8. Enable strict pytest markers so broker/provider test marker typos fail collection.
9. Ensure all credentialed tests remain explicitly excluded in CI.

## Claude Code prompt

> Harden schema upgrades and CI in one infrastructure-focused branch. Make legacy backfills bounded and forward-progressing, quarantine conflicts instead of bricking `connect()`, and add previous-version database fixtures. Then add Python-version coverage, blocking safety Pyright coverage, Ruff, dependency checks, immutable action pins, least-privilege permissions, timeouts, concurrency, and strict pytest markers. Keep CI entirely offline and credential-free.

---

# Session 11 — Final documentation, regression audit, and go/no-go

**Branch:** `docs/final-remediation-audit`

## Goal

Re-run the consolidated audit against the final main candidate and publish an honest capability assessment.

## Steps

1. Re-run all targeted regression tests and full offline suites.
2. Search for each invariant at its authoritative boundary.
3. Update the disposition document with commit/PR evidence.
4. Reconcile README, runbooks, config comments, milestone docs, and actual code.
5. Publish a go/no-go table for:
   - deterministic research;
   - historical backtesting;
   - local paper books;
   - unattended local paper scheduling;
   - supervised external Alpaca paper;
   - unattended external paper;
   - live trading.
6. Keep live trading `NOT IMPLEMENTED / DISABLED`.
7. Do not claim CI is green until GitHub Actions completes.

## Exit criteria

External paper may be reconsidered for supervised use only when:

- exact approval is code-enforced;
- reservation + checkpoint is co-atomic;
- retry crash recovery is tested;
- cross-day cap bypass is closed;
- safety checks are authoritative inside the fence;
- orphan repair is available;
- all focused and full offline tests pass.

Unattended scheduling may be reconsidered only when:

- local daily notional is atomic;
- research run claiming/recovery is safe;
- capability flags are authoritative;
- shortlist/candidate/token gates are all wired;
- CI safety checks are blocking.

## Claude Code prompt

> Perform the final current-main remediation audit. Do not add features. Verify each consolidated invariant against code and tests, update all documentation, and publish an evidence-based go/no-go table. Run full offline test suites and blocking static checks. Confirm no real provider or broker call occurred and that live trading remains structurally unavailable.

---

## 4. Standard handoff format for every session

```markdown
# Session Handoff

## Starting point
- Base branch:
- Starting SHA:
- Session branch:

## Root causes addressed
- Root-cause IDs:
- Audit finding IDs:

## Changes
- Files changed:
- Schema version/migrations:
- Behavioral changes:

## Safety
- Live trading added: No
- Real provider called: No
- Real broker called: No
- Credentials used: No

## Validation
- Focused tests:
- Full tests:
- Paper-runtime tests:
- Pyright safety:
- Other checks:

## Remaining risks
- Confirmed open:
- Needs reproduction:
- Deferred legacy:

## Next session
- Recommended branch:
- Required starting documents:
- First test to write:
```

## 5. Implementation order

```text
Session 0
  ├─ Session 1
  ├─ Session 2 ─ Session 3
  ├─ Session 4
  ├─ Session 5 ─ Session 6
  ├─ Session 7
  ├─ Session 8
  ├─ Session 9
  └─ Session 10
All completed ─ Session 11
```

Sessions 1–3 are the external-paper safety gate and should be merged before any credentialed paper execution. Sessions 4–6 are required before unattended scheduling. Sessions 7–10 may proceed in parallel after Session 0, provided branches do not overlap heavily.
