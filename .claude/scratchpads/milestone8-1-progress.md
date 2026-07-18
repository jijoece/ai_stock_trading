# Milestone 8.1 Progress

## Baseline
- `pytest tests/ -q --tb=short` -> 1355 passed, 14 skipped (matches expected exactly).
- `cd paper_runtime && pytest tests/ -q --tb=short` -> 33 passed (matches expected exactly).
- Git status at start: clean except untracked `docs/milestones/milestone-8.1.md` (this spec) and this
  scratchpad. No unrelated in-flight work.

## Scheduled-cycle output mapping
Source of truth: `research/scheduled_cycle.py` + `storage/research_cycle_repositories.py::
SQLiteResearchCycleRepository` (`get_cycle`, `list_symbol_results`) +
`storage/trading_repositories.py::load_recommendation` +
`storage/research_repositories.py::load_evidence_snapshot` +
`storage/research_cycle_repositories.py::load_symbol_evidence_status`.

| Field | Source | Class |
|---|---|---|
| cycle_id | `research_cycles.cycle_id` (`cycle_repo.get_cycle`) | AUTHORITATIVE |
| research_run_id | `research_cycle_symbol_results.research_run_id` (None when evidence blocked Claude) | AUTHORITATIVE |
| symbol | `research_cycle_symbol_results.symbol` | AUTHORITATIVE |
| as_of | `research_cycles.as_of` (cycle-level; same value used to freeze both arms) | AUTHORITATIVE |
| evidence_snapshot_id | `research_cycle_symbol_results.snapshot_id` | AUTHORITATIVE |
| baseline_recommendation_id / enhanced_recommendation_id | `research_cycle_symbol_results.*` | AUTHORITATIVE |
| baseline/enhanced status+side | `load_recommendation(conn, rec_id)["status"/"side"]` (frozen payload) | DERIVED |
| evidence completeness | `research_cycle_symbol_evidence_status` row (`load_symbol_evidence_status`) | AUTHORITATIVE |
| experiment policy (cycle-recorded) | `research_cycles.experiment_policy` | AUTHORITATIVE — NOTE: this is always one of the *legacy*-supported values (OBSERVE_ONLY/BASELINE_ONLY/SHADOW_ENHANCED) because `ScheduledResearchConfiguration.__post_init__` validates against `experiment_policy.validate_experiment_policy`, which rejects ENHANCED_ONLY/BOTH_SEPARATE_PAPER_BOOKS at cycle-creation time. The **paper-book routing policy** is therefore a separate, explicitly-supplied parameter to the new integration entry point (matches the milestone's own pseudocode signature), not reused from this column. |
| SymbolCycleResult.evidence_outcome/baseline_side/enhanced_side (in-memory dataclass) | not persisted in `research_cycle_symbol_results` table | NOT_AVAILABLE after reload — reconstructed via DERIVED path above instead |

No change made to `ResearchCycleResult`/`SymbolCycleResult`. Integration reads exclusively via
`SQLiteResearchCycleRepository` + `load_recommendation` + `load_evidence_snapshot` +
`load_symbol_evidence_status`, never from an in-memory `ResearchCycleResult` object (fresher,
authoritative, and works when invoked from the CLI against an old persisted cycle).

## Integration design
(see module docstring in `paper_books/scheduled_integration.py` for full detail)
- New module `paper_books/scheduled_integration.py`:
  `integrate_scheduled_cycle_into_paper_books(conn, *, cycle_id, experiment_policy,
  paper_books_config, clock, price_provider=None) -> PaperBookCycleIntegrationResult`.
- Per symbol: persist one immutable `PaperBookExperimentAssignment` FIRST (both arms, both
  book_ids fixed BASELINE/ENHANCED, intent IDs precomputed deterministically via the existing
  `derive_paper_order_intent_id`/`order_intent.EXECUTION_VERSION` before any risk/execution
  runs) — satisfies "no assignment after observing fills."
- Per arm eligibility order: book enabled -> policy permits arm -> recommendation
  exists/frozen -> recommendation is an active buy_candidate with risk_plan.shares>0 ->
  recommendation.symbol matches cycle symbol and ts<=as_of -> evidence snapshot exists and
  matches symbol -> evidence completeness (`screening_completeness ==
  COMPLETE_FOR_SCREENING`) -> portfolio valuation builds without exception -> deterministic
  risk evaluation -> order intent -> market-simulation input -> local-simulated fill.
- New config section `paper_books.scheduled_integration.enabled` (default `false`, OPTIONAL
  key so all 13 pre-existing `test_paper_books_config.py` tests — which never include this
  section — keep loading with it defaulting closed; unknown keys inside it still fail closed).
  `integrate_scheduled_cycle_into_paper_books` itself checks `paper_books_config.enabled` AND
  `paper_books_config.scheduled_integration.enabled`, raising `ScheduledIntegrationError`
  closed if either is false — defense in depth regardless of caller diligence.
- Market-simulation input builder (Section 6): tier 1 = bid/ask literally present in the
  shared EvidenceSnapshot's own "market" category item (never currently populated by any
  existing provider — checked generically, future-proof, never fabricated); tier 2 = the same
  point-in-time reference price `valuation.select_valuation_price` already selects, converted
  to a synthetic symmetric bid/ask using the *existing* `execution.py::DEFAULT_SLIPPAGE_BPS`
  as the half-spread proxy (the only existing configured spread/slippage numeric model in the
  paper-books execution module) — labeled `SIMULATED`, versioned
  `paper-books-scheduled-market-sim-v1`, documented explicitly as a deliberate reuse decision
  since no dedicated "spread" config field exists anywhere in the repo; tier 3 = intent
  created but left `PENDING_SUBMISSION` with reason `MARKET_SIMULATION_INPUT_UNAVAILABLE`,
  never a fabricated fill.
- Reconciliation: once per book actually touched by this integration call (not once per
  symbol) via existing `reconciliation.reconcile_book`.
- Shadow-scheduler wiring: `shadow/scheduler.py::run_due_shadow_cycle` gains an optional
  `paper_book_integrator: Callable[[ResearchCycleResult, datetime], Any] | None = None`
  keyword (default `None` = zero behavior change for all existing callers/tests). Invoked only
  after `cycle_result` is obtained and only when non-None, wrapped in its own try/except so a
  paper-book integration exception is recorded on the new `ShadowCycleRunResult` fields
  (`paper_book_integration_status`/`paper_book_integration_reason`, both default `None`) and
  never re-raised, never mutates `cycle_result`, never mislabeled as a Claude-provider failure.

## Implementation
Files created:
- `src/trading_research/paper_books/scheduled_integration.py` — entry point
  `integrate_scheduled_cycle_into_paper_books`, `SymbolArmOutcome`/`PaperBookCycleIntegrationResult`,
  `_process_arm` (per-arm eligibility+execution), `_build_market_simulation_input` (3-tier),
  `_resolve_may_submit` (policy try/except wrapper).
- `docs/milestones/milestone8-1-scheduled-paper-book-integration.md`.

Files modified:
- `src/trading_research/paper_books/config.py` — new `ScheduledIntegrationSection` (optional,
  defaults closed, unknown-key fail-closed), threaded into `PaperBooksConfiguration`.
- `config/paper_books.yaml` — added `scheduled_integration.enabled: false`.
- `src/trading_research/paper_books/cli_support.py` — `paper_book_integrate_cycle_cli`.
- `src/trading_research/cli.py` — `paper-book-integrate-cycle` subcommand wired.
- `src/trading_research/shadow/scheduler.py` — optional `paper_book_integrator` param on
  `run_due_shadow_cycle` (default `None`), two new optional `ShadowCycleRunResult` fields
  (`paper_book_integration_status`/`_reason`, both default `None`).
- `docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md` — Decision 1/9 corrected
  (in-process local simulator, not a `paper_runtime` subprocess call); status line updated.
- `docs/milestones/milestone8-isolated-paper-portfolios.md` — "Ten new tables" corrected to "Fourteen";
  pointer note to this milestone added.

Bugs found and fixed during development (caught by targeted tests, fixed before final run):
1. Reconciliation was attempted for every "touched" book, including books that were never
   actually opened (arms that skip before reaching `cash_ledger.open_book`, e.g.
   `SKIPPED_SNAPSHOT_MISMATCH`) — raised `ValueError: unknown book_id`. Fixed by restricting
   reconciliation to the 3 outcomes that actually open a book
   (`EXECUTED`/`INTENT_CREATED_PENDING_FILL`/`REJECTED_BY_RISK`).
2. Test assumption that `PaperBookExperimentAssignment` persists 2 rows per (cycle,symbol) —
   wrong; it is 1 row carrying both arms (unlike the legacy per-arm `ExperimentAssignment`).
   Fixed the test assertion, not the implementation (implementation was correct).

## Tests
- `tests/unit/test_paper_books_scheduled_integration.py` — 28 tests: disabled-config/unknown-
  cycle fail-closed (3), scheduled-cycle output mapping (6: found/missing-baseline/missing-
  enhanced/missing-snapshot/symbol-mismatch/timestamp-mismatch), policy routing (6: baseline-
  only/enhanced-only/both/observe-only/disabled-baseline-no-fallback/disabled-enhanced-no-
  fallback), evidence/recommendation validity (3), portfolio isolation (2), market simulation
  (4: observed/simulated-spread/unavailable-rejected-by-risk/future-price-never-observed),
  idempotency (1), failure handling (3: risk-rejection-persisted/research-result-immutable/
  reconciliation-persisted-per-book).
- `tests/unit/test_paper_books_scheduled_integration_cli.py` — 4 tests: actual persisted cycle
  -> sanitized deterministic JSON, disabled scheduled_integration fails closed, shipped default
  config fails closed (no monkeypatch), missing cycle fails closed.
- `tests/unit/test_shadow_scheduler.py` — 4 new tests appended: hook not supplied = zero
  behavior change, hook invoked-and-recorded with the real cycle_result, hook exception
  recorded on dedicated fields (never raised, never folds into `failure_reason`), hook never
  invoked when the cycle itself crashes (no frozen recommendations exist yet).
- `tests/integration/test_milestone_8_1_scheduled_integration_e2e.py` — 3 tests: (A) a REAL
  unmodified `run_scheduled_research_cycle` run (fixture/deterministic providers, same harness
  as `test_scheduled_research_cycle.py`) feeding a real cycle_id into the integration, proving
  correct AUTHORITATIVE-field mapping and shared evidence_snapshot_id/as_of persisted before
  either arm's eligibility was evaluated (AAPL's real $300 fixture price is correctly
  screened_out by `config/screening.yaml`'s unrelated `max_share_price: 25.0` gate — both arms
  correctly SKIPPED_RECOMMENDATION_INVALID, never fabricated as executable); (B) a full
  executable dual-book pipeline built via the same persistence primitives the real cycle uses
  (different pre-existing ENHANCED position -> different approved quantities -> distinct
  intents/fills -> separate cash/positions -> separate MATCHED reconciliation -> idempotent
  reprocessing); (C) structural no-live-execution-path proof (AST import scan + `--live` flag
  absence).

Idempotency proof (test `test_reprocessing_same_cycle_is_idempotent` +
`test_full_isolated_dual_book_pipeline_via_scheduled_integration`'s second-call assertions):
calling `integrate_scheduled_cycle_into_paper_books` twice on the same cycle_id leaves cash,
position, fill count, and assignment count byte-identical after the second call — the second
call's own `fill_id` in the returned outcome is `None` (the idempotent-replay path in
`execution.py::submit_and_simulate` recognizes the fill already exists and never re-applies it,
matching Milestone 8's own existing idempotency contract exactly).

Isolation proof: `test_one_books_existing_position_does_not_affect_the_other` and the e2e
pipeline test both show ENHANCED's pre-existing MSFT position never appears in BASELINE, and
the two books' AAPL fills/intents/cash are provably distinct.

## Test run log
- 2026-07-13 — targeted: `pytest tests/unit/test_paper_books_scheduled_integration.py
  tests/unit/test_paper_books_scheduled_integration_cli.py tests/unit/test_shadow_scheduler.py
  tests/integration/test_milestone_8_1_scheduled_integration_e2e.py
  tests/unit/test_paper_books_config.py -q` -> 84 passed.
- 2026-07-13 — `pytest tests/ -q --tb=short` -> **1394 passed, 14 skipped** (1355 baseline + 39
  net new tests — 28 + 4 + 4 + 3 — zero regressions, zero existing test modified/weakened).
- 2026-07-13 — `cd paper_runtime && pytest tests/ -q --tb=short` -> **33 passed** (unchanged,
  directory not touched this session at all).

## Documentation
Created `docs/milestones/milestone8-1-scheduled-paper-book-integration.md` (full architecture record: entry
point, mapping table, eligibility order, assignment design, market-simulation tiers,
reconciliation scope, config gate, scheduler wiring, CLI, ADR correction, tests, deferred list).
Updated `docs/milestones/milestone8-isolated-paper-portfolios.md` with a one-paragraph pointer (table-count
correction "ten" -> "fourteen"). Corrected `docs/adr/0006-isolated-paper-books-and-portfolio-
evaluation.md` Decision 1/9 (in-process local simulator, not a `paper_runtime` subprocess call;
`OrderIntentPayload.book_id` is for a possible future integration, currently unused) and its
status line.

## Safety review
All checks performed directly against the actual working-tree diff:
- No forbidden imports anywhere in `scheduled_integration.py` (grep-confirmed: no
  `lumibot`/`alpaca`/`robinhood`/`anthropic`), reused by the AST-based structural test
  `test_no_live_execution_path_in_scheduled_integration`.
- No `--live` flag: `python -m trading_research.cli --help` contains zero `--live` occurrences
  (grep count = 0).
- No `os.environ`/`getenv` read anywhere in `scheduled_integration.py` — no credential or env
  var can enable this path; only `config/paper_books.yaml`'s two explicit booleans
  (`enabled`/`scheduled_integration.enabled`) can, and both default `false`.
- `real_orders`/`paper/ledger.py`/`execution/models.py` untouched (`git diff --stat` empty for
  all three).
- No launchd/deploy changes (`git diff --stat -- deploy/` empty).
- No enhanced-to-baseline fallback / no baseline-to-enhanced fallback: each arm's `book_id` is
  a fixed constant (`cfg.baseline.book_id`/`cfg.enhanced.book_id`) throughout — never inferred
  or substituted, tested directly (`test_disabled_baseline_book_fails_closed_no_fallback`/
  `test_disabled_enhanced_book_fails_closed_no_fallback`).
- No fabricated recommendation: `_process_arm` only ever reads an already-persisted
  `load_recommendation` payload; it never constructs one. No fabricated fill: `simulate_fill`
  is reused unmodified from Milestone 8; the market-simulation-input tier-3 (`None`) path
  leaves the intent `PENDING_SUBMISSION`, never fabricating a cross.
- No mutation of frozen recommendations: `test_research_result_is_never_mutated_by_paper_book_failure`
  proves a skipped/invalid recommendation's persisted payload is byte-identical before/after.
- No automatic promotion / no live promotion path touched — this milestone does not modify
  `comparison.py`/`promotion_evidence.py` at all (`git diff --stat` confirms).
- Config fails closed: `paper_books.enabled` AND `paper_books.scheduled_integration.enabled`
  both required `true`; checked twice (inside `integrate_scheduled_cycle_into_paper_books`
  itself, and again in `cli_support.py` before even opening a DB session) — defense in depth.
- Paper-book failure never mislabeled as a Claude-provider failure: the new
  `ShadowCycleRunResult.paper_book_integration_status`/`_reason` fields are entirely separate
  from `failure_reason` (reserved for the cycle/Claude path) — proven directly by
  `test_paper_book_integrator_exception_is_recorded_not_raised_not_misclassified`.
- No recurring deployment activation: `run_due_shadow_cycle`'s new `paper_book_integrator`
  parameter defaults `None` and is not wired to any real integrator by any existing caller in
  this session — `shadow/scheduler.py` does not import `paper_books` at all (grep-confirmed).
- Existing tests not weakened: `git status --short tests/` shows only new files + one append to
  `test_shadow_scheduler.py` (new tests only, zero existing test line changed); full suite went
  1355/14 -> 1394/14, zero regressions, zero new skips.

## Known limitations
- `MARKET_SIMULATION_INPUT_UNAVAILABLE` (tier 3) is structurally reachable but not naturally
  exercised in the normal flow: by the time market-simulation-input building runs, risk
  evaluation has already required a valid, non-stale, non-unsafe reference price to approve at
  all (using the *same* `PriceSelection` object), so tier 2 (`SIMULATED`) always succeeds
  whenever an order is actually approved. This is a deliberate, documented consequence of
  reusing one price-selection call for both risk and market-simulation input (never diverging),
  not an oversight — "never fabricate a fill" is still fully proven via the
  `REJECTED_BY_RISK`-before-market-sim path (`test_unavailable_price_leaves_intent_pending_never_fabricates_fill`).
- The one required offline end-to-end test's Part A intentionally does not force AAPL through
  the full deterministic screener into an executable `buy_candidate` (the repository's
  `config/screening.yaml` `max_share_price: 25.0` gate rejects any triple-digit fixture price,
  and tuning the screener/scorer fixture inputs further is Milestone 2 territory, out of this
  milestone's integration-boundary scope) — Part A instead proves real-cycle-output mapping
  correctness on the actually-produced `screened_out` result (a legitimate, correctly-classified
  `SKIPPED_RECOMMENDATION_INVALID` outcome), and Part B separately proves the full
  execute/reconcile/idempotent chain using the same persistence primitives the real cycle uses.
- No wiring of a real `paper_book_integrator` into any production `run_due_shadow_cycle`
  caller — the hook exists, is tested, and is documented, but activating it end-to-end through
  `cli.py::run_due_shadow_cycle_cli` was not requested and would move toward recurring
  deployment, which is explicitly out of scope this session.
- Matches every Milestone 8 known limitation (no PARTIALLY_FILLED, no automated SELL path, etc.
  — unchanged, not revisited this session).

## Final status
**COMPLETE for this session's scope.**
- Baseline confirmed exactly: 1355 passed/14 skipped (main), 33 passed (paper_runtime).
- Final: **1394 passed, 14 skipped** (main) — 39 net new tests, zero regressions. **33 passed**
  (paper_runtime, untouched this session).
- Real persisted scheduled-cycle recommendations now feed isolated books through a single new
  entry point; no fixture recommendation is ever constructed by that path.
- Both arms share one evidence snapshot/as_of, persisted in one immutable assignment row before
  either arm's eligibility runs. Baseline maps only to BASELINE, enhanced only to ENHANCED — no
  fallback either direction, proven directly.
- Per-book valuation/risk/execution/reconciliation are fully independent; reprocessing is
  idempotent; no live or external-broker path exists anywhere; configuration remains disabled
  by default at two independent levels; the manual CLI command works from a real persisted
  cycle; ADR 0006 now matches the actual execution architecture; no scheduler/recurring
  deployment was activated.
- No commit or push performed at any point in this session.
