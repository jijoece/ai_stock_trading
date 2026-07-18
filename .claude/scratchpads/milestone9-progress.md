# Milestone 9 Progress

## Baseline
- `pytest tests/ -q --tb=short` -> 1394 passed, 14 skipped (matches expected exactly).
- `cd paper_runtime && pytest tests/ -q --tb=short` -> 33 passed (matches expected exactly).
- Git status at start: clean except untracked `docs/milestones/milestone-9.md` (spec) and this scratchpad.

## Existing lifecycle gaps
Milestone 8/8.1 built isolated books, deterministic risk/execution/reconciliation, and real
scheduled-cycle entry integration, but no SELL/exit path, no pending-order re-evaluation across
days, and no persistent multi-day soak tracking/reporting — all recorded as Milestone 9
candidates in `docs/milestones/milestone8-isolated-paper-portfolios.md` Section 21.

## Lifecycle design
New `paper_books/lifecycle.py::run_paper_book_lifecycle` — fixed processing order (validate ->
integrate cycles -> pending orders -> exits -> snapshots -> reconcile -> metrics -> persist run
summary). Reuses `execution.submit_and_simulate` unmodified for both pending-order reprocessing
and new SELL exit intents — no second fill simulator. Default `clock` anchored to `as_of` (not
wall-clock `now()`) so order/decision timestamps stay consistent with a historical `as_of` —
found and fixed during development (see Tests below).

## Exit policy
New `paper_books/exit_policy.py::evaluate_exit_decision` — fixed check order: no-position ->
missing/unsafe/stale price -> manual request -> stop loss -> profit target -> max holding
(market days via `evaluation/market_calendar.py`) -> reversal -> HOLD. Reversal defined as: a
newer, in-window, frozen recommendation with `side in (screened_out, no_action)` and
`status=active` — never a missing/watch/incomplete recommendation.

## Pending-order handling
`_process_pending_orders` re-derives a fresh point-in-time-safe market-simulation input at each
lifecycle `as_of` and re-attempts `submit_and_simulate` (idempotent by construction). Expires
via `market_days_held(created_at, as_of) >= expire_after_market_days`, releasing BUY
reservations exactly once (`execution.expire_pending_intent`, reused).

## Daily processing
Snapshot -> reconcile -> metrics per enabled book, one book's exception caught and recorded in
`failure_reasons` without preventing the other book's processing.

## Reporting
`paper-book-soak-report` / `paper-book-soak-readiness` CLI commands — read-only,
deterministic, advisory-only. Soak report never recomputes/duplicates promotion evidence
(points to the existing `paper-promotion-status` command instead).

## Tests
- `pytest tests/unit/test_paper_books_exit_policy.py` -> 25 passed.
- `pytest tests/unit/test_paper_books_lifecycle.py` -> 17 passed.
- `pytest tests/unit/test_paper_books_lifecycle_cli.py` -> 12 passed.
- `pytest tests/unit/test_shadow_scheduler.py` -> 40 passed (36 existing + 4 new lifecycle-hook
  tests).
- `pytest tests/integration/test_milestone_9_offline_end_to_end.py` -> 2 passed.
- Bug found and fixed during development: `run_paper_book_lifecycle`'s default `clock`
  originally used `datetime.now(timezone.utc)` (real wall time), which desynchronized from a
  historical `as_of` in the offline e2e test and caused `market_days_held` to raise
  ("as_of_date must not precede opened_on") when a pending exit order created on one lifecycle
  day was re-processed on a later one — silently absorbed into `failure_reasons` by the
  "one book failure never mutates the other" try/except, never surfaced as a raised exception.
  Fixed by anchoring the default clock to `as_of`; added `failure_reasons == ()` assertions to
  every multi-day unit test afterward to prevent this class of bug from hiding again. Also fixed
  a real schema bug: `paper_book_lifecycle_symbol_results.lifecycle_run_id` had a `REFERENCES
  paper_book_lifecycle_runs` FK, but symbol results are written *during* processing, before the
  run-summary row exists (written last) — removed the FK.

## Documentation
Created `docs/milestones/milestone9-manual-paper-soak-and-lifecycle.md` (architecture record) and
`docs/runbooks/manual-paper-trading-soak.md` (operator runbook). Added a short pointer to
`docs/milestones/milestone8-1-scheduled-paper-book-integration.md`.

## Safety review
- No forbidden imports in `lifecycle.py`/`exit_policy.py` (grep + AST-scan test confirmed:
  no lumibot/alpaca/robinhood/anthropic/trading_paper_runtime).
- No `--live` flag anywhere in `python -m trading_research.cli --help` (grep-confirmed).
- No `os.environ`/`getenv` read in either new module — only `config/paper_books.yaml`'s
  `lifecycle.enabled`/`lifecycle.exits.enabled` (both default `false`) can enable this path.
- `real_orders`/`paper/ledger.py`/`execution/models.py` untouched (`git diff --stat` empty).
- No launchd/deploy changes.
- SELL quantity capped at `min(exit_decision.quantity, available_quantity)` — never oversells;
  `execution.submit_and_simulate`'s existing oversell check (unmodified) is the actual enforcer.
- No cross-book state: every new repo function/query is `book_id`-scoped; e2e test asserts
  independent cash/positions/quantities across BASELINE/ENHANCED throughout.
- Claude never sees/produces an exit decision — `exit_policy.py` is a pure function over
  typed, already-persisted inputs.
- No automatic promotion / no live promotion path touched (`comparison.py`/
  `promotion_evidence.py` not modified).
- Config fails closed at two independent levels (`paper_books.enabled` AND
  `paper_books.lifecycle.enabled`), each checked directly inside `run_paper_book_lifecycle`.
- Manual exit requests require non-empty `operator`+`reason`, checked in both `exit_policy.py`
  (dataclass-level via the caller-supplied dict) and `cli_support.py` (CLI-level validation).
- No recurring deployment: `paper_book_lifecycle_hook` on `run_due_shadow_cycle` defaults
  `None`; no caller in this session wires a real `run_paper_book_lifecycle` into it.
- Existing tests not weakened: only new files + one append to `test_shadow_scheduler.py` (new
  tests only, zero existing test line changed).

## Known limitations
- A SELL exit's limit price equals its own trigger-day reference price exactly (mirrors
  Milestone 8's BUY convention) — it typically does not fill same-day; resolves via
  pending-order reprocessing once a later day's price actually crosses. Directly exercised
  (not just documented) by the offline e2e test.
- `paper-book-soak-report`'s promotion-evidence field is a pointer to the existing
  `paper-promotion-status` command, not a recomputation.
- Full-position exits only; FIFO-only lot consumption (unchanged from Milestone 8).
- Manual exit requests are picked FIFO when more than one exists for the same book/symbol.

## Final status
**COMPLETE for this session's scope.**
- Baseline confirmed exactly: 1394 passed/14 skipped (main), 33 passed (paper_runtime).
- Final: **1454 passed, 14 skipped** (main) — 60 net new tests (25+17+12+4+2), zero
  regressions, zero existing test weakened. **33 passed** (paper_runtime, untouched).
- Lifecycle processing (and exits within it) both ship disabled by default; manual CLI is the
  only invocation path; no scheduler/launchd deployment was activated; no commit or push
  performed.
