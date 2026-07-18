# Streamlit Dashboard Handoff

## Repository state

- Starting commit: `5af1923e1d468c84b819d6a30eebed82937ae7f3`
- Current branch: `agent/milestone-13-risk-controls`
- Working tree at Phase 3 start: Phase 1-2 dashboard changes were present;
  `docs/milestones/20-ui.md` was already untracked.
- Python version: `3.14.5rc1` (`.venv/bin/python`)
- Database configuration source: `src/trading_research/config.py` reads
  `RESEARCH_DATABASE_PATH`, defaulting under `RESEARCH_DATA_DIR` to
  `research.sqlite3`; the existing `storage/database.py::connect()` is
  read-write and applies schemas, so the dashboard must not use it.

## Confirmed tables and repositories

| Purpose | Table or repository | Important fields |
|---|---|---|
| Shadow scheduler runs | `shadow_scheduler_runs`; `shadow_operations_repositories` | `scheduler_run_id`, `cycle_id`, `status`, counts, budget, timestamps |
| Recurring-paper scheduler | `paper_recurring_scheduler_runs`; `paper_books_repositories` | IDs, processed cycles, readiness, `status`, failure codes, timestamps |
| Research cycles | `research_cycles`; `SQLiteResearchCycleRepository` | `cycle_id`, `as_of`, policy/mode, `status`, timestamps |
| Candidate results | `research_cycle_symbol_results` | symbol, status, snapshot/run/recommendation IDs, `baseline_paper_submitted`, failure reason |
| Screening and scoring | `screening_runs`, `candidate_scores` | run totals; per-symbol component score, total, rank |
| Recommendation disposition | `recommendations`; `trading_repositories` | symbol, side, status, score, confidence, acted, timestamp |
| Research decisions | `research_decisions`, `research_overlay_decisions` | run/symbol, rating, overlay action, policy version |
| Research attempts | `research_committee_runs`, `research_attempts`, `research_attempt_failures` | status, success, structured failure code/stage, usage, latency |
| Evidence completeness | `research_cycle_symbol_evidence_status`, `evidence_completeness_results` | screening/research completeness, blocking categories, policy |
| Provider provenance | `research_cycle_provider_provenance` and links | category/name/mode, fixture/real, status, normalized outcome |
| Provider telemetry | `evidence_provider_requests`, `evidence_provider_health_snapshots` | success/error code, retry/rate-limit/latency; aggregate status |
| Paper books and cash | `paper_books`, `paper_book_cash_ledger` | arm/status/starting cash; exact cash events and reservations |
| Allocation/risk decisions | `paper_book_risk_decisions` | cycle/recommendation/symbol, decision, notionals, stable reason codes |
| Paper orders and fills | `paper_book_orders`, `paper_book_fills` | cycle/recommendation/symbol, order status, quantities, fill price/time |
| External paper evidence | `paper_external_order_events`, `paper_external_broker_fills`, `paper_external_submission_queue` | stable state transitions, error code, fill, queue status |
| Positions and valuation | `paper_book_positions`, `paper_book_snapshots`, snapshot positions | exact quantities/cost/P&L; cash, equity, valuation status |
| Skip/exit history | `paper_book_lifecycle_symbol_results`, `paper_book_exit_decisions` | stage, stable outcome, reason codes, intent/fill IDs |
| Budget decisions | `shadow_budget_reservations`, `shadow_budget_usage`, `shadow_role_budget_checks` | status/usage; role decision and stable reason |
| System health | `shadow_run_summaries`, `shadow_run_health_checks` | health status/reasons, rates, reconciliation/duplicate flags |
| Health hysteresis | `shadow_health_hysteresis_state` and evaluations | decision/effective status, streaks, provider metrics, reasons |
| Pause and kill state | `shadow_pause_state`; `shadow_operations_repositories` | current `state` (including kill), source, expiry, timestamp |
| Paper safety/activation | `paper_book_safety_events`, `paper_recurring_activation_events` | book pause reason; recurring state transition and timestamp |

## Decision taxonomy

Mapping precedence is implemented in `dashboard/models/view_models.py` and
uses stable persisted fields only. Narrative rationale/failure text is never
used to infer an outcome.

| Dashboard outcome | Source fields or codes |
|---|---|
| `BOUGHT_OR_SUBMITTED` | `baseline_paper_submitted=1`; order status `SUBMITTED`, `PARTIALLY_FILLED`, or `FILLED` |
| `BUY_CANDIDATE_NOT_SUBMITTED` | recommendation side `buy_candidate`; pending/preview/requested order state |
| `REJECTED` | stable `paper_book_risk_decisions.decision` beginning `REJECTED_`, except mapped budget/policy codes |
| `SCREENED_OUT` | recommendation side `screened_out` |
| `EVIDENCE_INCOMPLETE` | known non-complete screening/research completeness code |
| `RESEARCH_INCOMPLETE` | run/recommendation status `ANALYSIS_INCOMPLETE`, `PARTIALLY_COMPLETE`, or `FAILED` |
| `POLICY_BLOCKED` | stable paused/arm/risk-state/daily-loss/drawdown/economic-blackout risk code |
| `BUDGET_BLOCKED` | `BUDGET_REJECTED`, `SKIPPED_BUDGET_EXHAUSTED`, insufficient-cash or daily-notional risk code |
| `PROVIDER_FAILURE` | persisted provider `error_code`/structured failure code |
| `DUPLICATE_PREVENTED` | No candidate-level stable persisted success code confirmed; map to `UNKNOWN` |
| `PRICE_CONDITION_NOT_MET` | `STILL_PENDING` is ambiguous (price unavailable or operator pending); map to `UNKNOWN` |
| `NO_ACTION` | recommendation side `no_action` or `watch` |
| `UNKNOWN` | missing, legacy, ambiguous, or unrecognized stable-code combination; explanation sanitized and capped at 240 characters |

## Data contracts

All contracts are immutable frozen dataclasses in
`dashboard/models/view_models.py`.

| View | Service | Status |
|---|---|---|
| `DashboardOverview` | `overview_service` | Phase 2 complete; latest persisted aggregate |
| `CandidateDecisionSummary` | `decision_service` | Phase 2 complete; bounded filtered query and mapper |
| `CandidateDecisionDetail` | `decision_service` | Phase 2 complete; whitelisted structured detail |
| `ResearchCycleSummary`, `ResearchCycleDetail`, `ResearchCycleFunnel` | `cycle_service` | Phase 3 complete; bounded history, funnel, ticker decisions |
| `PortfolioSummary`, `PositionSummary`, order/fill summaries | `portfolio_service` | Phase 3 complete; bounded book, position, order, and fill queries |
| `ProviderHealthSummary`, `SystemStatusSummary`, `SystemHealthView` | `health_service` | Phase 3 complete; operational and provider-partitioned health |

## Phase status

- [x] Phase 1 — Discovery and contracts
- [x] Phase 2 — Foundation and decision UI
- [x] Phase 3 — Portfolio, cycles, and health
- [x] Phase 4 — Tailscale and final validation

## Files changed

- `dashboard/models/view_models.py` — immutable view contracts, dashboard
  outcome taxonomy, deterministic mapper, bounded unknown explanation, and
  structured decision-detail fields.
- `dashboard/services/database.py` — sanitized configuration and short-lived
  SQLite URI `mode=ro` connections with `query_only` enabled.
- `dashboard/services/decision_service.py` — parameterized, bounded decision
  summary/detail queries and whitelisted structured payload parsing.
- `dashboard/services/overview_service.py` — latest persisted paper-book,
  cycle, scheduler, pause, and decision aggregates.
- `dashboard/services/cycle_service.py` — bounded research-cycle history,
  persisted funnel stages, provider/model partitions, and ticker decisions.
- `dashboard/services/portfolio_service.py` — filtered paper books, positions,
  persisted price provenance, orders, and fills, capped at 200 rows per book.
- `dashboard/services/health_service.py` — pause/scheduler/budget/hysteresis
  state plus separate evidence- and model-provider health partitions.
- `dashboard/streamlit_app.py` — five-page navigation with unique URL paths.
- `dashboard/pages/overview.py` — persisted overview metrics with explicit
  `Not available` handling.
- `dashboard/pages/decisions.py` — bounded filters, result table, and bought
  and not-bought decision-path details shared by cycle drill-down.
- `dashboard/pages/research_cycles.py` — cycle table, funnel, and ticker
  decision detail.
- `dashboard/pages/portfolio.py` — book/position/activity filters with
  positions, price source/timestamp, orders, and fills.
- `dashboard/pages/system_health.py` — operational state and distinct
  evidence/model provider tables; non-production recovery is labeled.
- `.streamlit/config.toml` — loopback-only, headless, CORS/XSRF-protected
  Streamlit configuration with detailed errors and telemetry disabled.
- `pyproject.toml` — adds Streamlit and Pandas runtime dependencies.
- `tests/dashboard/test_view_models.py` — focused mapper and immutability tests.
- `tests/dashboard/conftest.py` — temporary persisted SQLite fixture.
- `tests/dashboard/test_database.py` — missing-path, row, and write-rejection tests.
- `tests/dashboard/test_decision_service.py` — filter, bound, mapping, and detail tests.
- `tests/dashboard/test_overview_service.py` — persisted overview aggregation test.
- `tests/dashboard/test_cycle_service.py`, `test_portfolio_service.py`, and
  `test_health_service.py` — Phase 3 read-only service coverage.
- `tests/dashboard/test_dashboard_smoke.py` — side-effect-free imports and all
  five page renders, including cycle drill-down.
- `docs/ui/streamlit-dashboard-handoff.md` — persistence inventory and phase handoff.
- `scripts/run_dashboard.sh` — verifies env/database/Streamlit, starts the
  dashboard bound to `127.0.0.1:8501`, writes a PID file under
  `data/.dashboard_runtime/`, never prints the full database path.
- `scripts/stop_dashboard.sh` — stops only the PID-file process; no `pkill`.
- `scripts/tailscale_serve_dashboard.sh` — verifies `tailscale` CLI,
  connection, and local health, then runs `tailscale serve --bg
  127.0.0.1:8501`; never invokes Funnel.
- `scripts/dashboard_status.sh` — reports process, health, DB existence and
  mtime, and Tailscale connection/Serve status without secrets or full paths.
- `docs/ui/streamlit-dashboard.md` — architecture, install, env var, startup,
  Tailscale prerequisites/setup, remote access, status/shutdown/Serve
  disablement, security boundaries, limitations, troubleshooting.

## Focused tests completed

- `.venv/bin/python -m pytest tests/dashboard/test_database.py tests/dashboard/test_decision_service.py tests/dashboard/test_view_models.py tests/dashboard/test_overview_service.py tests/dashboard/test_dashboard_smoke.py -q`
- Result: `30 passed in 0.80s`.
- `.venv/bin/streamlit config show` validated all committed options with
  Streamlit `1.59.2`, including `address = "127.0.0.1"`.
- Local server smoke: loopback server started and `/_stcore/health` returned
  `ok`; the server was then stopped.
- Phase 3 service focus: `.venv/bin/python -m pytest
  tests/dashboard/test_cycle_service.py tests/dashboard/test_portfolio_service.py
  tests/dashboard/test_health_service.py -q` — `7 passed in 0.14s`.
- Phase 3 dashboard suite: `.venv/bin/python -m pytest tests/dashboard -q` —
  `40 passed in 1.16s`.
- `git diff --check` passed; targeted write-statement scan found no write or
  direct SQLite connection in the Phase 3 services/pages.
- Phase 4 full offline suite: `.venv/bin/python -m pytest tests/dashboard/ -q`
  — `40 passed`; `.venv/bin/python -m pytest tests/ -q` — `2448 passed, 18
  skipped`; `(cd paper_runtime && ../.venv/bin/python -m pytest tests/ -q)` —
  `53 passed, 1 skipped`.
- `.venv/bin/python -m compileall -q dashboard src` — clean; `pyright
  --project pyright-safety.json` — `0 errors, 0 warnings`; `git diff --check`
  — clean.
- Local smoke: `scripts/run_dashboard.sh` against a temporary throwaway
  SQLite file started the server; `curl .../_stcore/health` returned `ok`;
  `lsof -nP -iTCP:8501 -sTCP:LISTEN` confirmed `127.0.0.1:8501` only;
  `scripts/dashboard_status.sh` and `scripts/stop_dashboard.sh` verified
  end-to-end; the listener was confirmed gone after stop.
- Tailscale CLI is not installed on this development machine, so
  `tailscale_serve_dashboard.sh` and the connection/Serve status sections of
  `dashboard_status.sh` could not be exercised live; both scripts fail
  closed with a clear message when `tailscale` is absent. Remote Tailscale
  verification remains a manual operator step per the milestone.

## Known limitations

- Decision outcome and reason filters scan at most 1,000 persisted candidates
  before returning at most 200 results; there is no unbounded load.
- Overview outcome counts display unavailable when the latest cycle exceeds
  the 200-candidate service bound instead of showing partial or fabricated data.
- Portfolio activity is bounded to the latest 200 orders and fills per book;
  cycle history is bounded to 200 cycles and ticker drill-down to 200 decisions.
- Portfolio valuations use only the latest persisted snapshot/current-position
  price. Missing price source/timestamp remains `Not available`; no live price
  lookup is performed.
- Provider failure/recovery streaks use the latest 2,000 persisted events.
  Fixture, deterministic, and scripted model partitions are labeled
  `NON_PRODUCTION` and never contribute a production recovery streak.
- No stable candidate-level persisted code distinguishes successful duplicate
  prevention, and `STILL_PENDING` does not distinguish a price condition from
  an external order awaiting an operator. Both remain `UNKNOWN` as required.
- The dashboard uses only isolated paper-book evidence; legacy execution data is not
  shown.

## Exact next task

All four phases are complete. Remaining work is operator-only and outside
Claude Code's scope: on the target MacBook, run `tailscale up` if not
already connected, run `scripts/run_dashboard.sh`, run
`scripts/tailscale_serve_dashboard.sh`, and verify remote access from an
authorized tailnet device. No further code changes are required unless new
functionality is requested.
