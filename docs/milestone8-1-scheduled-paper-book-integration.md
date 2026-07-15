# Milestone 8.1 — Scheduled research to isolated paper-book integration

**Status:** Complete for this session's scope.
**Date:** 2026-07-13
**Applies to:** `src/trading_research/paper_books/scheduled_integration.py`,
`src/trading_research/paper_books/config.py`, `src/trading_research/shadow/scheduler.py`,
`config/paper_books.yaml`, `docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md`.

See `.claude/scratchpads/milestone8-1-progress.md` for the full session log. This document is
the durable architecture record for the integration closure Milestone 8 left deferred:

```text
real scheduled research cycle
-> frozen baseline/enhanced recommendations
-> shared EvidenceSnapshot and as_of
-> isolated per-book portfolio valuation
-> deterministic risk decisions
-> book-aware paper intents
-> local simulated fills
-> book-specific reconciliation and evaluation
```

## 1. What this milestone built

A single new module, `paper_books/scheduled_integration.py`, that reads an already-persisted,
already-frozen scheduled research cycle (`research/scheduled_cycle.py::
run_scheduled_research_cycle`'s own output — never a fixture recommendation) and drives it
through the existing Milestone 8 paper-book primitives (`valuation`, `risk`, `order_intent`,
`execution`, `reconciliation`) exactly the way `paper_books/cli_support.py::
paper_book_run_cycle_cli` already does for its fixture-mode, CLI-supplied inputs. It is the
missing glue between Milestone 6's scheduled cycle and Milestone 8's isolated books — no
Milestone 8 module was rewritten, only the new integration layer was added.

## 2. Entry point

```python
integrate_scheduled_cycle_into_paper_books(
    conn, *, cycle_id, experiment_policy, paper_books_config, clock, price_provider=None,
) -> PaperBookCycleIntegrationResult
```

Reads exclusively via `SQLiteResearchCycleRepository` (`get_cycle`/`list_symbol_results`),
`storage/trading_repositories.py::load_recommendation`, and
`storage/research_repositories.py::load_evidence_snapshot` — never from an in-memory
`ResearchCycleResult` object, so it also works from the CLI against an old persisted cycle.

`experiment_policy` is a required, explicit parameter — **not** read from the cycle's own
recorded `research_cycles.experiment_policy` column. That column is always drawn from the
*legacy* supported set (`OBSERVE_ONLY`/`BASELINE_ONLY`/`SHADOW_ENHANCED`) because
`ScheduledResearchConfiguration.__post_init__` rejects `ENHANCED_ONLY`/
`BOTH_SEPARATE_PAPER_BOOKS` at cycle-creation time (unchanged, Milestone 6 behavior). Paper-book
routing is a separate policy surface (`research/experiment_policy.py`'s additive
`may_submit_*_to_paper_book` functions), so the caller supplies it explicitly.

Fails closed with `ScheduledIntegrationError` when `paper_books_config.enabled` or
`paper_books_config.scheduled_integration.enabled` is false, or `cycle_id` has no persisted
`research_cycles` row. Every other failure mode is a bounded `SymbolArmOutcome`, never an
unclassified exception.

## 3. Scheduled-cycle output mapping

| Field | Source | Class |
|---|---|---|
| cycle_id | `research_cycles.cycle_id` | AUTHORITATIVE |
| research_run_id | `research_cycle_symbol_results.research_run_id` | AUTHORITATIVE |
| symbol | `research_cycle_symbol_results.symbol` | AUTHORITATIVE |
| as_of | `research_cycles.as_of` | AUTHORITATIVE |
| evidence_snapshot_id | `research_cycle_symbol_results.snapshot_id` | AUTHORITATIVE |
| baseline/enhanced recommendation_id | `research_cycle_symbol_results.*` | AUTHORITATIVE |
| baseline/enhanced status+side | `load_recommendation(conn, rec_id)["status"/"side"]` | DERIVED |
| evidence completeness | `research_cycle_symbol_evidence_status` row | AUTHORITATIVE |
| experiment policy (cycle-recorded) | `research_cycles.experiment_policy` | AUTHORITATIVE (legacy-scoped only — see above) |

## 4. Eligibility (fail-closed, per symbol/arm)

Order: book enabled -> policy permits this arm -> recommendation exists/frozen ->
recommendation is an active `buy_candidate` with `risk_plan.shares > 0` -> recommendation
symbol matches the cycle symbol and `ts <= as_of` -> evidence snapshot exists and matches
symbol -> evidence completeness (`screening_completeness == COMPLETE_FOR_SCREENING`) ->
portfolio valuation builds -> deterministic risk evaluation -> order intent -> market
simulation input -> local-simulated fill.

Outcome vocabulary: `EXECUTED`, `INTENT_CREATED_PENDING_FILL`, `SKIPPED_BOOK_DISABLED`,
`SKIPPED_POLICY`, `SKIPPED_RECOMMENDATION_MISSING`, `SKIPPED_RECOMMENDATION_INVALID`,
`SKIPPED_EVIDENCE_INCOMPLETE`, `SKIPPED_SNAPSHOT_MISMATCH`, `SKIPPED_VALUATION_UNAVAILABLE`,
`REJECTED_BY_RISK`, `FAILED`.

## 5. Shared experiment assignment

One immutable `PaperBookExperimentAssignment` row per `(cycle_id, symbol)` is persisted
**first**, before either arm's eligibility is evaluated — both arms' book IDs are fixed
(`BASELINE`/`ENHANCED`), and both arms' intent IDs are precomputed deterministically via the
existing `derive_paper_order_intent_id`/`order_intent.EXECUTION_VERSION` (no risk/execution has
run yet at that point). A missing recommendation for either arm is still explicitly recorded in
this same row (`None` for that arm's fields) rather than omitted. Reprocessing the same cycle
resolves to the same row (idempotent, immutable, insert-only).

## 6. Market-simulation input (never a live quote)

`_build_market_simulation_input` tries, in order:

1. A bid/ask pair already present in the shared `EvidenceSnapshot`'s own "market" evidence item
   (not populated by any existing evidence provider today, checked generically/future-proof).
   Labeled `OBSERVED`.
2. The same point-in-time reference price `valuation.select_valuation_price` already selected
   for risk sizing, converted to a synthetic symmetric bid/ask using the existing
   `execution.py::DEFAULT_SLIPPAGE_BPS` as the half-spread proxy — the only existing configured
   spread/slippage numeric model in the paper-books execution module (no dedicated "spread"
   config field exists anywhere in this repository). Labeled `SIMULATED`, versioned
   `paper-books-scheduled-market-sim-v1`.
3. Neither available: the order intent is still created and persisted, but stays
   `PENDING_SUBMISSION` with reason `MARKET_SIMULATION_INPUT_UNAVAILABLE` — never a fabricated
   fill.

Never uses a price dated after `as_of`. Reuses the existing Milestone 8 fill simulator
(`execution.py::submit_and_simulate`) — no second fill engine was created.

## 7. Reconciliation

Only for a book this invocation actually opened (`cash_ledger.open_book` was reached — i.e. the
arm reached `EXECUTED`/`INTENT_CREATED_PENDING_FILL`/`REJECTED_BY_RISK`). Every other outcome
returns before the book row exists, so it is never reconciled. Uses the existing
`reconciliation.py::reconcile_book`, once per book per invocation — never cross-book.

## 8. Configuration

`config/paper_books.yaml` gained an OPTIONAL `paper_books.scheduled_integration.enabled`
section (default `false` when present, and absence of the section entirely also defaults
closed — kept optional specifically so all 13 pre-existing `test_paper_books_config.py` tests,
none of which include this section, keep loading unchanged). Both `paper_books.enabled` AND
`paper_books.scheduled_integration.enabled` must be `true` for
`integrate_scheduled_cycle_into_paper_books` to run — checked directly inside the function
itself (defense in depth, independent of caller diligence). No environment variable can enable
either flag.

## 9. Scheduler wiring (disabled by default, manual invocation only)

`shadow/scheduler.py::run_due_shadow_cycle` gained an optional `paper_book_integrator:
Callable[[ResearchCycleResult, datetime], Any] | None = None` keyword — default `None` is zero
behavior change for every pre-existing caller/test. When supplied, it is invoked once, only
after `run_cycle` returns without raising (i.e. only after frozen recommendations actually
exist), wrapped in its own try/except. A raised exception is recorded on
`ShadowCycleRunResult.paper_book_integration_status` (`"FAILED"`)/`.paper_book_integration_reason`
and never re-raised, never mutates `cycle_result`, and is never folded into `failure_reason`
(reserved for the Claude/cycle-crash path) — so a paper-book integration failure can never be
mislabeled as a provider failure. `shadow/scheduler.py` itself does not import `paper_books` at
all — the real wiring (injecting `integrate_scheduled_cycle_into_paper_books`) is the CLI
wrapper's responsibility, and only when explicitly configured to do so. No launchd activation,
no recurring process, no automatic wiring was added in this session.

## 10. CLI

```bash
python -m trading_research.cli paper-book-integrate-cycle \
  --cycle-id <cycle-id> [--experiment-policy BOTH_SEPARATE_PAPER_BOOKS]
```

Loads the actual persisted cycle (never fabricates a recommendation), fails closed with
`{"error": ...}` + exit code 2 when scheduled integration is disabled or the cycle is unknown,
and returns sanitized, deterministic JSON (bounded per-symbol/per-arm outcomes plus
reconciliation status — no raw Claude prompt/response content). The existing fixture-oriented
`paper-book-run-cycle` command is unchanged.

## 11. ADR correction

`docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md` Decision 9 previously implied
Milestone 8's execution path calls the isolated `paper_runtime` subprocess boundary. Corrected:
Milestone 8/8.1 uses an in-process, book-aware, deterministic local simulator;
`OrderIntentPayload`'s additive optional `book_id` field exists for a possible future
subprocess-per-book integration; per-book `paper_runtime` subprocess execution is deferred. The
ADR's Section 5 table-count wording ("ten") is also corrected to 14 in
`docs/milestone8-isolated-paper-portfolios.md`.

## 12. Tests

Added `tests/unit/test_paper_books_scheduled_integration.py` (28 tests: mapping, policy
routing, portfolio isolation, market simulation, idempotency, failure handling, reconciliation),
`tests/unit/test_paper_books_scheduled_integration_cli.py` (4 tests: actual persisted cycle,
disabled integration, missing cycle, sanitized JSON), 4 new tests appended to
`tests/unit/test_shadow_scheduler.py` (optional hook: not-supplied, invoked-and-recorded,
exception-recorded-not-raised, never-invoked-on-cycle-crash), and
`tests/integration/test_milestone_8_1_scheduled_integration_e2e.py` (3 tests: a real
`run_scheduled_research_cycle` run feeding the mapping correctly, a full dual-book
execute/reconcile/idempotent pipeline, and a structural no-live-execution-path proof).

## 13. Deferred (recorded, not silently dropped)

* Per-book `paper_runtime` subprocess pool (the additive `book_id` field remains unused by any
  Milestone 8/8.1 code path).
* External paper broker.
* Automated exits / partial fills (unchanged from Milestone 8's own deferred list).
* Dividend record-date entitlement.
* Recurring/launchd activation of the scheduler-to-paper-books wiring — the
  `paper_book_integrator` hook exists and is tested, but no caller wires a real
  `integrate_scheduled_cycle_into_paper_books` into `run_due_shadow_cycle` by default; that
  remains a manual CLI-driven flow (`paper-book-integrate-cycle`) plus an opt-in hook for a
  future scheduled-deployment task.
