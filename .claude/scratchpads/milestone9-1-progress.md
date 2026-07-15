# Milestone 9.1 Progress

## Baseline
- `pytest tests/ -q --tb=short` -> 1454 passed, 14 skipped (matches expected exactly).
- `cd paper_runtime && pytest tests/ -q --tb=short` -> 33 passed (matches expected exactly).
- Git status at start: clean except untracked `docs/milestone-9.1.md` (spec) and this scratchpad.

## Readiness inputs
Traced all 13 combined-readiness inputs (Section 1 of spec) against real repository code:
paper-soak side reuses Milestone 9's own `evaluate_paper_soak_readiness` (extracted, not
copied); shadow side reuses Milestone 7.2's `evaluate_activation_readiness` wholesale. Two
inputs had no persisted signal: unresolved critical alerts (no `resolved` concept existed at
all in `shadow_alerts` before this session) and cross-book violations (never persisted anywhere,
distinct from each book's own reconciliation status). Fixed the first additively (new nullable
columns + `resolve_alert`); left the second honestly `MISSING` (documented gap, caps final
status at `READY_FOR_EXTENDED_MANUAL_SOAK`, never `READY_FOR_RECURRING_ACTIVATION_REVIEW`).

## Manual workflow
`paper-soak-run` (new): validate config -> validate shadow pause/kill -> optional explicit
cycle integration -> lifecycle (reconciles internally) -> soak report -> combined readiness ->
persist operator-run summary -> sanitized JSON. `paper-soak-readiness` (new): read-only wrapper
around the combined readiness module.

## Clock correction
Confirmed the real bug: `paper_book_lifecycle_run_cli` always passed `clock=_utc_now`, so a
pending order's `created_at` becomes real wall time even for a historical `--as-of` replay —
this later order reads as created in the future relative to a subsequent historical `as_of`,
and `market_days_held` raises. `run_paper_book_lifecycle`'s own default (`clock=None` ->
anchored to `as_of`) was always correct; only the CLI's override was wrong. Fixed: default
`clock=None`, added `--audit-time-now` opt-in for a genuine wall-clock audit stamp. Regression
test forces the old behavior and proves it breaks; two new tests prove the fixed default
doesn't.

## Implementation
- `storage/shadow_alerts_schema.py` / `shadow_alerts_repositories.py`: additive
  `resolved_at`/`resolved_by`/`resolved_reason` columns (mirrors `trading_schema.py`'s own
  `_ensure_columns` upgrade pattern) + `resolve_alert()` + `list_alerts(unresolved_only=...)`.
- `storage/paper_books_schema.py` / `paper_books_repositories.py`: additive
  `paper_soak_operator_runs` table (immutable, insert-or-ignore on deterministic
  `operator_run_id`) + `save_operator_run`/`load_operator_run`/`list_operator_runs`.
- `paper_books/controlled_soak_readiness.py` (new): `ControlledSoakReadinessResult`,
  `ReadinessCheck`, `evaluate_controlled_soak_readiness()` — combines both existing readiness
  functions, adds the two new checks, fixed fail-closed order per spec Section 3.
- `paper_books/cli_support.py`: extracted `_build_soak_report`/`evaluate_paper_soak_readiness`
  (behavior-preserving refactor, existing CLI functions now thin wrappers); fixed clock default
  in `paper_book_lifecycle_run_cli` (+ `audit_time_now` param); added `paper_soak_run_cli` /
  `paper_soak_readiness_cli`.
- `cli.py`: `--audit-time-now` on `paper-book-lifecycle-run`; new `paper-soak-run` /
  `paper-soak-readiness` subcommands.

## Tests
- `test_shadow_alerts.py`: +3 tests (unresolved-by-default, resolve excludes from
  unresolved_only, idempotent resolution).
- `test_controlled_soak_readiness.py` (new): 15 tests — every NOT_READY_* branch, both READY_*
  tiers, cross-book MISSING-signal cap, status always in documented vocabulary.
- `test_paper_books_lifecycle_cli.py`: +14 tests — clock anchoring (default + audit-time-now +
  forced-wallclock regression), paper-soak-run (zero-cycle, unknown-cycle fails-closed,
  persistence, idempotent replay, sanitized JSON, shadow pause/kill fail-closed),
  paper-soak-readiness (status vocabulary, fail-closed when disabled).
- `test_milestone_9_1_offline_end_to_end.py` (new): 2 tests — full offline pipeline with a
  resolved CRITICAL alert (never blocks) + idempotent replay + no cross-book contamination + no
  network call; and unresolved CRITICAL alert / active pause blocking with zero activation side
  effect (no operator-run row, no lifecycle-run row persisted).
- Net new: 34 tests. All targeted suites green throughout development.

## Documentation
Created `docs/milestone9-1-controlled-soak-readiness.md` (architecture record) and
`docs/runbooks/controlled-paper-soak.md` (operator runbook). Added a short pointer to
`docs/milestone9-manual-paper-soak-and-lifecycle.md`. No prior milestone doc rewritten.

## Safety review
- No forbidden imports (lumibot/alpaca/robinhood/anthropic/trading_paper_runtime) in any new
  file — grep-confirmed.
- No `--live` flag anywhere in CLI help — grep-confirmed.
- No `os.environ`/`getenv` read in `controlled_soak_readiness.py` — only config objects.
- `real_orders`/`execution/`/`paper/ledger.py` untouched — `git diff --stat` empty.
- `config/` untouched — no threshold weakened, no minimum reduced (`git diff --stat` empty).
- No pause/kill automatically cleared — `paper_soak_run_cli` only *reads* `pause_mod.current_state`,
  never calls `resume`/`force_clear_kill`/`kill`.
- No automatic enhanced-arm promotion — `comparison.py`/`promotion_evidence.py` untouched.
- `READY_FOR_RECURRING_ACTIVATION_REVIEW` structurally unreachable today
  (`_CROSS_BOOK_SIGNAL_AVAILABLE = False`), proven by
  `test_never_returns_recurring_activation_review_today`.
- Missing data never becomes zero: unresolved-alert count/cross-book signal both explicit
  MISSING/AUTHORITATIVE classifications, never a fabricated `False`/`0`.
- Fixture vs real-provider cycles: reused Milestone 7.2's own `cost_usd > 0` real-provider
  filter verbatim — no new "is real" heuristic invented.
- Operator-run summary is immutable (insert-or-ignore + no-UPDATE/DELETE triggers), no raw model
  output, no credentials.
- Existing tests not weakened: only new files + additive appends to 2 existing test files (new
  tests only, zero existing test line changed/deleted).

## Known limitations
- Cross-book violation signal is permanently `MISSING` this session (Section 4 of the doc) —
  `READY_FOR_RECURRING_ACTIVATION_REVIEW` is structurally unreachable until a future milestone
  adds real detection.
- When multiple Milestone-9-owned paper-soak conditions are simultaneously false (e.g.
  insufficient cycles AND reconciliation mismatch at once), the combined module surfaces
  whichever one Milestone 9's own `evaluate_paper_soak_readiness` reports first (its own
  pre-existing internal order: cycles -> market-days -> lifecycle-failures -> reconciliation ->
  valuation) rather than strictly the Milestone 9.1 spec's listed order. Every condition tested
  in isolation (per the spec's own test list) surfaces correctly.
- `ACTIVATION_NOT_READY_PAUSE_ACTIVE` (Milestone 7.2's shared status for both real pause AND the
  latest run's own reconciliation/duplicate-violation safety flags) is mapped to
  `NOT_READY_RECONCILIATION` here when triggered by the latter — a reasonable but not perfectly
  distinct mapping, since Milestone 7.2 itself conflates the two triggers under one status.
- Alert resolution has no CLI command in this session (only a repository function) — matches
  the milestone's own CLI command list (only `paper-soak-run`/`paper-soak-readiness` were
  required); a future session can add an operator-facing `shadow-alert-resolve` command.

## Final status
**COMPLETE for this session's scope.**
- Baseline confirmed exactly: 1454 passed/14 skipped (main), 33 passed (paper_runtime).
- Final: **1486 passed, 14 skipped** (main) — 32 net new tests, zero regressions, zero existing
  test weakened. **33 passed** (paper_runtime, untouched).
- No commit or push performed.
