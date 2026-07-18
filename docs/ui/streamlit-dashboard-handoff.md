# Streamlit Dashboard Handoff

## Repository state

- Starting commit: `5af1923e1d468c84b819d6a30eebed82937ae7f3`
- Current branch: `agent/milestone-13-risk-controls`
- Working tree at start: `docs/milestones/20-ui.md` was already untracked.
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
| `DashboardOverview` | `overview_service` | Contract complete; service is Phase 2 |
| `CandidateDecisionSummary` | `decision_service` | Contract and outcome mapper complete; service is Phase 2 |
| `CandidateDecisionDetail` | `decision_service` | Contract complete; service is Phase 2 |
| `ResearchCycleSummary` | `cycle_service` | Contract complete; service is Phase 3 |
| `PortfolioSummary`, `PositionSummary` | `portfolio_service` | Contracts complete; service is Phase 3 |
| `ProviderHealthSummary`, `SystemStatusSummary` | `health_service` | Contracts complete; service is Phase 3 |

## Phase status

- [x] Phase 1 — Discovery and contracts
- [ ] Phase 2 — Foundation and decision UI
- [ ] Phase 3 — Portfolio, cycles, and health
- [ ] Phase 4 — Tailscale and final validation

## Files changed

- `dashboard/models/view_models.py` — immutable view contracts, dashboard
  outcome taxonomy, deterministic mapper, bounded unknown explanation.
- `tests/dashboard/test_view_models.py` — focused mapper and immutability tests.
- `docs/ui/streamlit-dashboard-handoff.md` — persistence inventory and phase handoff.

## Focused tests completed

- `.venv/bin/python -m pytest tests/dashboard/test_view_models.py -q`
- Result: `17 passed in 0.02s`.

## Known limitations

- Phase 1 contains no database query services or UI.
- Scheduled paper integration returns detailed per-arm outcomes but does not
  persist that return object directly; later services must join the persisted
  assignment, risk, order, fill, and lifecycle tables.
- No stable candidate-level persisted code distinguishes successful duplicate
  prevention, and `STILL_PENDING` does not distinguish a price condition from
  an external order awaiting an operator. Both remain `UNKNOWN` as required.
- Legacy simulated/execution tables coexist with isolated paper-book tables;
  Phase 2 should prefer isolated paper-book evidence and label legacy data.

## Exact next task

Start a fresh session and execute Phase 2 only: read this handoff, add the
read-only SQLite helper, Streamlit configuration and navigation, Overview and
Decisions services/pages/detail, and Phase 2 focused tests. Do not implement
Phase 3 views or Phase 4 startup/Tailscale work.
