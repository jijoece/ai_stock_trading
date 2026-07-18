# Milestone 9.2 Progress

## Baseline
`pytest tests/ -q --tb=short` -> 1486 passed, 14 skipped (matches expected).
`cd paper_runtime && pytest tests/ -q --tb=short` -> 33 passed (matches expected).
Git status at start: clean except untracked `docs/milestones/milestone-9.2.md` (spec) and pre-existing
unrelated untracked files (`batch/prompts/milestone-10*`, `milestone-11*`, `milestone-9-3*`,
`docs/batch_creation.md`) — left untouched.

## Provider-provenance sources
Claude: AUTHORITATIVE via `research_committee_runs.provider`/`research_attempts.provider`,
joinable by `research_run_id` (reachable from `shadow_scheduler_runs.cycle_id`). Evidence
categories (market/news/sentiment/fundamentals/filing/corporate_status): MISSING at the
persistence layer (fixture/real distinction decided in `cli.py::_build_evidence_provider_registry`
but discarded before `SourceRecord` persistence) — closed additively via
`research_cycle_provider_provenance`, using the already-authoritative whole-cycle
`research_cycles.provider_mode` combined with which categories the snapshot actually populated
(real mode never mixes a fixture client per category — confirmed by tracing the wiring).

## Cross-book verification
No dedicated signal existed (Milestone 9.1's own `_CROSS_BOOK_SIGNAL_AVAILABLE = False`).
`paper_books` tables are already book_id-scoped by PK, so every check joins by (book_id, id)
tuples — same identifier text in two isolated books never compared cross-book.

## Alert operations
`shadow_alerts_repositories.list_alerts`/`resolve_alert` already existed (Milestone 9.1) with the
exact idempotency/immutability semantics needed — only added `limit` param + two thin CLI wrappers.

## Readiness diagnostics
Added derived views (all_failed_checks/blocking_checks/advisory_checks/missing_checks) computed
from the existing `ControlledSoakReadinessResult.checks` list — no change to evaluation ordering.

## Implementation
- `storage/research_cycle_schema.py`: additive `research_cycle_provider_provenance` table.
- `storage/research_cycle_repositories.py`: `save_provider_provenance`/
  `list_provider_provenance_for_cycle`/`list_provider_provenance_upto`.
- `research/provider_provenance.py` (new): classification enum, row builders, `classify_cycle`,
  `compute_real_provider_history`. Deferred storage import (circular-import avoidance with
  `scheduled_cycle.py`).
- `research/scheduled_cycle.py`: `_run_symbol` persists evidence-category provenance right after
  `save_evidence_snapshot`, and the Claude row right after `research_run_id` is assigned.
- `storage/paper_books_schema.py`: additive `paper_book_cross_book_verifications`/
  `_checks` tables + `_ensure_columns` upgrade (`cross_book_verification_id`/`_status` on
  `paper_soak_operator_runs`, mirrors `shadow_alerts_schema.py`'s own ALTER TABLE pattern).
- `storage/paper_books_repositories.py`: `list_all_experiment_assignments_upto`,
  `save_cross_book_verification`/`load_cross_book_verification`/
  `list_cross_book_verification_checks`/`latest_cross_book_verification_upto`; `save_operator_run`
  carries the two new columns.
- `paper_books/cross_book_verification.py` (new): 7 checks (book/arm identity, orders-arm,
  fills-order, cash-ledger foreign-reference, lots-fill, lifecycle-symbol-results scope,
  reconciliations-own-book); `verify_cross_book_integrity`/`persist_verification`.
- `paper_books/controlled_soak_readiness.py`: removed `_CROSS_BOOK_SIGNAL_AVAILABLE`; reads
  `pb_repo.latest_cross_book_verification_upto`; new `STATUS_NOT_READY_CROSS_BOOK`; real-provider
  check now sourced from `provider_provenance.compute_real_provider_history` + 6 informational
  breakdown checks.
- `paper_books/cli_support.py`: `paper_soak_run_cli` now runs+persists cross-book verification
  after lifecycle, before readiness; `_controlled_readiness_to_json` (diagnostics fields);
  `paper_book_cross_check_cli` (new).
- `storage/shadow_alerts_repositories.py`: `list_alerts` gained optional `limit`.
- `cli.py`: `shadow_alert_list_cli`/`shadow_alert_resolve_cli` (new) + `shadow-alert-list`/
  `shadow-alert-resolve`/`paper-book-cross-check` subcommands.

## Tests
- `test_provider_provenance.py` (new): 9 tests — every classification combination, missing
  metadata, cost-independence, one-cycle-once, idempotent persistence.
- `test_cross_book_verification.py` (new): 10 tests — insufficient-data, clean pass, same
  identifier text across books, 3 injected-violation cases, deterministic ID, idempotent persist,
  never-fabricates-PASSED.
- `test_controlled_soak_readiness.py`: +2 new tests (cross-book FAILED blocks in isolation, PASSED
  allows activation-review tier); 2 pre-existing tests updated to zero the real-provider threshold
  (their own focus is market-days tiering, not provider history — a genuine, correct behavior
  change, not a regression).
- `test_shadow_cli.py`: +9 tests for `shadow_alert_list_cli`/`shadow_alert_resolve_cli`.
- `test_paper_books_lifecycle_cli.py`: +4 tests (cross-check CLI disabled/bounded, verification
  persisted+threaded onto operator run, readiness diagnostics fields present).
- `test_milestone_9_2_offline_end_to_end.py` (new): 3 tests — fixture-only history + clean
  cross-book PASSED + resolved alert + idempotent replay; injected foreign-reference violation ->
  cross-book FAILED + readiness blocked + lifecycle evidence preserved; zero-cost real-provider
  metadata counts as real (no cost_usd read anywhere).
- Net new: 63 tests. All targeted suites green throughout development.

## Documentation
Created `docs/milestones/milestone9-2-soak-evidence-integrity.md` and
`docs/runbooks/soak-evidence-and-alert-operations.md`. Added a short pointer to
`docs/milestones/milestone9-1-controlled-soak-readiness.md`. No prior milestone doc rewritten.

## Safety review
- No forbidden imports (lumibot/alpaca_trade_api/robinhood order mutation/anthropic) in any new
  file — grep-confirmed; `cli.py` hits are pre-existing, unrelated (mock adapter, comments).
- No `--live` flag anywhere — grep-confirmed.
- No `os.environ`/`getenv` read in `provider_provenance.py`/`cross_book_verification.py`.
- `execution/`, `paper/ledger.py`, `config/` untouched — `git diff --stat` empty.
- No pause/kill automatically cleared — `shadow_alert_resolve_cli` only writes
  `shadow_alerts.resolved_*`, never touches `shadow_pause_state`; proven by a dedicated test.
- No automatic enhanced-arm promotion — `comparison.py`/`promotion_evidence.py` untouched.
- Cross-book verification never fabricates PASSED from an absent exception — every check reports
  its own explicit status; overall PASSED requires at least one observed, non-failed check.
- `research_cycle_provider_provenance` never retrofits historical rows — cycles with no persisted
  provenance read back as absent from the count entirely (not a fabricated UNKNOWN row).
- Real-provider counting never reads `cost_usd` anywhere in `provider_provenance.py` — grep-confirmed.
- Alert resolution audit fields immutable — `resolve_alert`'s pre-existing `WHERE resolved_at IS
  NULL` guard (Milestone 9.1, unchanged) proven by a dedicated repeat-resolution test.
- Existing tests not weakened: only 2 existing test bodies changed (both adding an explicit
  threshold override to isolate their own original assertion from the new, correct provider-history
  gate), zero existing assertion deleted or loosened.

## Known limitations
- Cross-book checks are bounded to the 7 listed in Section 6 of the spec (book/arm identity,
  orders/fills, cash, lots, lifecycle scope, reconciliation-ownership) — a hypothetical violation
  outside those categories (e.g. a corrupted snapshot row) is not detected by this milestone.
- `real_provider_cycle_count`'s minimum-threshold is still sourced from
  `shadow_readiness.ReadinessThresholds`/`DEFAULT_MIN_REAL_PROVIDER_CYCLES_FOR_READY` (Milestone
  7.2, unchanged) — only the numerator (what counts as "real") changed in this milestone, not the
  threshold itself.
- `MIXED` classification is proven correct by direct unit test but is not reachable through
  today's real cycle-wiring (real mode never mixes fixture clients per category) — documented as
  a structural, not a functional, gap.

## Final status
**COMPLETE for this session's scope.**
- Baseline confirmed exactly: 1486 passed/14 skipped (main), 33 passed (paper_runtime).
- Final: **1549 passed, 14 skipped** (main) — 63 net new tests, zero regressions, zero existing
  test weakened. **33 passed** (paper_runtime, untouched).
- Committed and pushed (see final commit).
