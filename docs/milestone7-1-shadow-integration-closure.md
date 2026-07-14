# Milestone 7.1 — Shadow-control runtime integration closure

**Status:** Complete for this session's scope.
**Date:** 2026-07-13.
**Scope:** Closes the specific runtime-integration gaps Milestone 7 left open. Does not
redesign the shadow-operations architecture, does not begin Milestone 8, does not activate
a recurring deployment.

## 1. Original Milestone 7 integration gaps (confirmed from code before editing)

All 13 gaps named in `docs/milestone-7.1.md` were confirmed present in the actual code
before any edit (see the scratchpad's "Current integration gaps confirmed" section for the
exact file/line evidence gathered during Step 2 of this session):

| # | Gap | Confirmed in |
|---|-----|---------------|
| 1 | Corporate-status evidence not in the primary `EvidenceSnapshot` | `research/scheduled_cycle.py::build_real_evidence_snapshot` (no corporate-status provider slot) |
| 2 | No corporate-status provider connected to `build_evidence_snapshot` | `research/evidence.py` (no such Protocol/provider existed) |
| 3 | `evaluate_completeness` not called from the scheduled-cycle path | `research/scheduled_cycle.py::_run_symbol` (no call site) |
| 4 | Corporate completeness does not gate Claude | follows from #3 |
| 5 | `check_role_budget` not called before actual role attempts | `shadow/scheduler.py` docstring itself: "does NOT wire per-role runtime budget checks" |
| 6 | Actual Claude usage not incrementally charged to the reservation | `shadow/scheduler.py::run_due_shadow_cycle` hardcoded `record_actual_usage(..., actual_cost_usd=Decimal("0"), ...)` |
| 7 | Final consumption does not reflect persisted Claude usage | follows from #6 |
| 8 | Scheduler health receives `None` for several fields | `_build_health_inputs_from_cycle_result`'s own docstring: "no per-role/per-attempt retry or token counters" |
| 9 | `CycleIntent.model_name` not populated correctly | `shadow/scheduler.py:469` hardcoded `model_name=None` |
| 10 | Model-specific pricing lookup incomplete | follows from #9 |
| 11 | `FixtureSecClient.list_filings()` returns no filings | `evidence_providers/fixture_clients.py`: `return ()` unconditionally |
| 12 | Real scheduler CLI path not assembled for explicit real-provider mode | `run_due_shadow_cycle_cli` only ever drove `provider_mode="fixture"`, no `--provider-mode` flag existed |
| 13 | ADR 0005 describes some target behaviors as active that were not wired | ADR 0005 Decisions 6/10 describe completeness gating and role-level budget checks in the present tense; Milestone 7's own scratchpad recorded them as explicitly deferred |

## 2. Corporate-status provider design

- **`CorporateStatusEvidenceProvider`** (Protocol, `evidence_providers/corporate_status_adapters.py`):
  `.fetch(symbol, as_of) -> CorporateStatusEvidence` — the typed result, not a lossy
  `EvidenceBundle` round-trip, so `evaluate_completeness`/persistence get the full result.
- **`SecCorporateStatusProvider`** (concrete): wraps any `SecEdgarClient`-shaped client
  (real or `FixtureSecClient`) plus an optional `FilingDocumentClient` for Step 6's
  text-level composition. `filing_document_client=None` → metadata-only, deterministic,
  offline (fixture mode). A real `FilingDocumentClient` → bounded text-level upgrade.
- Wired into `EvidenceProviderRegistry.corporate_status` (new optional field, default
  `None` — every Milestone 1-7 construction site remains valid) and into both
  `cli.py::_build_evidence_provider_registry`'s fixture and real branches.

## 3. Corporate-status snapshot representation

`evidence_providers/evidence_adapters.py::corporate_status_to_evidence_bundle` converts
`CorporateStatusEvidence` into bounded `EvidenceItem`s: reporting status, earliest reliable
filing date (explicitly labeled a *public-reporting-history proxy*, never company age),
latest annual/quarterly filing, late-filing notices, and one item per risk-signal category
(bankruptcy/delisting/registration-termination/shell-company/going-concern) carrying
`status`+`basis`+provenance. One shared `SourceRecord` per symbol/`as_of`
(`corporate-status-{symbol}-{as_of_date}`), `point_in_time_safe=True` (the retrieval
methodology is point-in-time safe regardless of content certainty — `derive_corporate_status`
already filters filings `<= as_of`). A tiny `PrefetchedEvidenceProvider` adapter
(`evidence_adapters.py`) lets this pre-built bundle plug into `build_evidence_snapshot`'s
existing `providers` list unchanged — the smallest safe extension, reusing the existing
`EvidenceBundle` provider shape per the milestone's own instruction. Corporate-status
evidence therefore participates in canonical snapshot hashing (`snapshot_id` changes when
corporate-status content changes — proven by
`test_corporate_status_provider_boundary.py::test_snapshot_hashing_includes_corporate_status`).

## 4. Disclosure-composition behavior

`evidence_providers/corporate_status_adapters.py::build_corporate_status_with_disclosures`
composes the existing metadata-only `derive_corporate_status` with Step 6's bounded
text-level extraction: at most 2 filing documents fetched total (the latest annual filing's
text serves both the going-concern and shell-company searches; the most recent 8-K named
in the bankruptcy signal's own metadata serves the bankruptcy search).
`disclosure_extraction.py::extract_disclosure`'s `EXPLICIT_DISCLOSURE_FOUND` outcome
upgrades a signal to `CONFIRMED`; every other outcome (not-found / ambiguous /
search-incomplete / document-unavailable) leaves the metadata-only status unchanged and
only appends an audit note — never downgrades, never fabricates `SOURCE_UNAVAILABLE` from
one unavailable document, never converts "not found" into a confirmed negative.

**Real-validation bug found and fixed:** the shell-company regex matched the *standard SEC
10-K/10-Q cover-page checkbox question* ("Indicate by check mark whether the registrant is
a shell company...") regardless of whether the real answer was Yes or No — confirmed
against AAPL's actual, real 10-K text, which produced a false `CONFIRMED` for a company
that is obviously not a shell company. Fixed with a context filter
(`_looks_like_cover_page_checkbox_context`) that skips any match preceded within ~200
characters by checkbox-question framing, continuing to search for a genuine affirmative
statement elsewhere in the document. Two regression tests added
(`tests/unit/test_disclosure_extraction.py`). This is exactly the class of defect real
validation exists to catch — no offline fixture exercised real 10-K cover-page boilerplate.

## 5. Completeness persistence and gate behavior

- New table `research_cycle_symbol_evidence_status` (appended to
  `storage/research_cycle_schema.py`, PK `(cycle_id, symbol)`, `INSERT OR REPLACE`)
  associates `snapshot_id`/`corporate_status_evidence_id`/`completeness_result_id`/
  `screening_completeness`/`research_completeness`/`blocking_categories_json`/
  `policy_version` with the cycle/symbol that produced them.
- `research/scheduled_cycle.py::_run_symbol` now: builds the snapshot (with corporate
  status merged in) → persists corporate-status evidence → calls
  `evaluate_completeness(...)` → persists the completeness result → persists the
  association row → **before any Claude call**.
- Gate: `evidence_blocks_enhanced = configuration.require_complete_evidence and
  (outcome in BLOCKING_OUTCOMES or completeness_result.screening_blocked)` — extends,
  does not replace, the pre-existing snapshot-outcome gate.
- `news_present`/`sentiment_present` are computed from whether `snapshot.evidence_items`
  actually contains a `news`/`sentiment` category item — an *enabled-but-empty* provider
  is correctly "not present," matching news/sentiment's non-blocking status.
- `SymbolCycleResult`'s status/shape is unchanged (still `COMPLETED` with
  `orchestration_status=ANALYSIS_INCOMPLETE`/`research_run_id=None` when blocked) — the
  completeness reason is authoritatively queryable via the new association table, not
  overloaded onto `failure_reason` (which existing consumers treat as a hard-failure signal).

## 6. Fixture SEC correction

`FixtureSecClient.list_filings` now returns deterministic, point-in-time-filtered
`FilingRecord`s anchored relative to `available_by` (same anchoring convention
`get_company_facts` already uses): an annual filing (10-K), a quarterly filing (10-Q), an
amendment (10-K/A), a late-filing notice (NT 10-Q), a historical earliest filing (5 years
back), one risk-signal fixture (8-K, exercising the bankruptcy metadata search), and one
future filing that point-in-time filtering excludes. No network, stable deterministic
ordering, exercises the complete offline corporate-status path.

## 7. Operating-history semantic decision

Unchanged: `operating_history.py::derive_operating_history` remains unwired from
`CandidateInput`/`FundamentalSnapshot.operating_history_years` — actual operating history
stays `None`/unknown, and deterministic screening remains fail-closed on it. The
corporate-status `EvidenceBundle` labels `earliest_reliable_filing_date` explicitly as a
"public-reporting-history proxy — NOT company age or years of actual operating history" in
both its evidence-item title and summary text (verified by
`test_corporate_status_bundle_labels_earliest_filing_as_proxy_not_operating_history`).

## 8. Provider/model propagation

`shadow/scheduler.py::run_due_shadow_cycle` gained `research_provider_name`/
`research_model_name`/`research_roles` parameters (all optional, defaults preserve every
prior caller's exact behavior). `CycleIntent.provider`/`.model_name` now come directly from
these — the old `cycle_configuration.provider_mode`-based guess (which conflated the
evidence-provider mode "fixture"/"real" with the Claude-provider taxonomy
"deterministic"/"scripted"/"anthropic") was **deleted**, not layered on top.
`cli.py::run_due_shadow_cycle_cli` supplies both from `research.yaml`'s `provider`/`model`
fields — the same single source of truth every other Claude call path in this repository
already uses (no new/duplicate model config surface). `estimate_cycle_cost`'s
`select_pricing` call now correctly keys on the real model name, so an anthropic scheduled
run with no matching `research_pricing.yaml` entry raises `BudgetConfigError` before any
lease/Claude work — real-validated (see Section 13).

## 9. Attempt-controller design

`research/orchestration.py` gained `AttemptControlRequest`/`AttemptControlDecision`
(frozen dataclasses) + `ResearchAttemptController` (Protocol). `analyze_with_research_committee`
and `_run_role_with_retries` gained an optional `attempt_controller: ResearchAttemptController
| None = None` parameter (default `None` = every prior milestone's exact behavior, zero
existing call sites need updating). Before each `provider.generate_structured(request)`
call, `before_attempt(...)` is consulted; a denial skips the provider call entirely and
records a `ResearchAttemptRecord(success=False, ...)` tagged with a new, structurally
distinct failure taxonomy entry (`STAGE_BUDGET_GATED`/`CODE_BUDGET_EXHAUSTED`) — never
conflated with any provider-failure code. `after_attempt(...)` runs after every completed
attempt (valid output, schema rejection, claim rejection, provider error with a record).
`research/orchestration.py` imports nothing from `shadow` (structurally verified by
`test_attempt_control_hooks.py::test_orchestration_module_never_imports_shadow`, an
AST-based guard).

## 10. Role-budget integration

`shadow/attempt_controller.py::ShadowResearchAttemptController` adapts the Protocol to
`shadow/role_budget.py::check_role_budget` + `shadow/budget.py::record_actual_usage_for_attempt`,
using the **same** `PricingEntry` already selected for the cycle's own reservation (never a
second, potentially-inconsistent lookup) and a per-symbol role-index counter (fresh
instance per symbol via `research/scheduled_cycle.py`'s new
`attempt_controller_factory: Callable[[str], ...] | None` parameter on
`run_scheduled_research_cycle`/`_run_symbol`). `shadow/scheduler.py::run_due_shadow_cycle`
builds this factory only when the new `research_roles` parameter is supplied; `None`
(every existing caller) preserves the exact prior "cycle-level reservation only" behavior.

## 11. Budget-check persistence

New append-only `shadow_role_budget_checks` table (`storage/shadow_operations_schema.py`),
one row per pre-attempt check: reservation/scheduler-run/cycle/research-run/symbol/role/
attempt identity, provider/model, decision, reason, remaining vs. maximum-possible
tokens/latency/cost. `check_id` is deterministic (sha256 of
`reservation_id|research_run_id|role|attempt_number`), persisted via `INSERT OR IGNORE` —
a resumed cycle's identical check never creates a duplicate row (verified by
`test_shadow_attempt_controller.py::test_check_id_deterministic_and_idempotent_on_resume`).

## 12. Actual usage accounting

New companion table `shadow_budget_usage_attempts` (`attempt_id` PK) gives the pre-existing
append-only `shadow_budget_usage` table an idempotency key without an `ALTER TABLE` on it.
`shadow/budget.py::record_actual_usage_for_attempt` checks this table first; a duplicate
`attempt_id` is a pure no-op. `ShadowResearchAttemptController.after_attempt` only charges
usage when `usage.input_tokens`/`.output_tokens`/`.latency_ms` are all genuinely present —
never fabricates a zero for a provider error with no token data. Deterministic/scripted
providers with real (non-`None`) token counts but no pricing entry are charged `$0`
(honest, matching existing convention); an anthropic attempt with genuinely missing cost
data (which the cycle-level preflight should have already prevented) is also never charged
a fabricated `$0` — usage recording is simply skipped for that attempt.

## 13. Retry accounting

Verified by `test_milestone_7_1_shadow_integration.py::
test_retry_attempt_budget_checked_with_reduced_balance_and_usage_not_double_charged`: a
first (schema-rejected) attempt is charged its real usage; the second attempt's role-budget
check reads the reservation's *reduced* remaining balance (proving the first attempt's
charge landed before the second attempt's check ran); both attempts' usage rows are
retained; `shadow_budget_usage_attempts` never contains a duplicate `attempt_id`.

## 14. Cycle telemetry model

`research/cycle_telemetry.py::ResearchCycleTelemetry` + `storage/research_repositories.py::
compute_cycle_telemetry(conn, research_run_ids)` derive attempt/success/retry/
retry-exhaustion/required-role-failure/provider-failure/unsupported-claim/
output-truncation/budget-skip counts plus token/latency/cost sums directly from persisted
`research_attempts`/`research_attempt_failures` rows — the one authoritative query, never a
duplicated in-memory counter. `status` is `UNAVAILABLE` (no research_run_ids at all),
`PARTIAL` (any required-role-failure/retry-exhaustion/budget-skip), or `COMPLETE`
otherwise. Token/latency/cost fields stay `None` when genuinely absent from every attempt
row (never a fabricated `0`); `pricing_status` distinguishes `CALCULATED` /
`PRICING_NOT_CONFIGURED` / `NOT_APPLICABLE` / `USAGE_NOT_RETURNED` / `MIXED` / `NO_DATA`.

## 15. Budget reservation and settlement result

`shadow/scheduler.py::_build_health_inputs_from_cycle_result` now joins
`ResearchCycleTelemetry` against `cycle_result.symbol_results[].research_run_id`. The
scheduler's own post-cycle `record_actual_usage` call (recording the cycle's own
non-Claude wall-clock overhead at `$0`/`0` tokens) was already conceptually correct — the
gap was that no *attempt-level* charging existed until Steps 12-15 closed it. Real
validation (Section 17) proved `consumed_cost_usd` exactly equals the sum of persisted
attempt-level priced usage (`$0.16562400 == $0.16562400`), not a fabricated figure.

## 16. Health telemetry result

Verified end-to-end (offline and real): `shadow_run_summaries` rows now show real
`claude_role_success_rate`/`retry_rate`/`retry_exhaustion_rate`/`unsupported_claim_rate`/
`output_truncation_rate` instead of always `None`; `input_tokens`/`output_tokens`/`cost_usd`
populate whenever the underlying attempts reported them (still correctly `None` for a
deterministic-provider cycle, which genuinely reports no tokens — not a regression).

## 17. Readiness integration

`shadow/readiness.py::build_readiness_report` required **no code change** — it already
reads `shadow_run_summaries` unmodified, so it automatically benefits from Section 16's
now-populated summaries. `ReadinessThresholds.min_completed_cycles_for_ready=10` (unchanged)
still means one successful real cycle is never sufficient for `READY`/`READY_WITH_WARNINGS`
by itself — not weakened by this milestone.

## 18. CLI real-mode behavior

`run-due-shadow-cycle` gained `--provider-mode {fixture,real}` (default `fixture`) and
repeatable `--symbol`. `--provider-mode real` builds the real SEC/corporate-status
provider (and market/news/sentiment only when explicitly enabled in
`config/evidence_providers.yaml`) and, when `research.yaml`'s `provider: anthropic`, the
real `AnthropicResearchProvider` — but only after every preflight below passes:

* missing/unset `research.model` → `ResearchConfigError` (via `require_ready()`);
* missing `ANTHROPIC_API_KEY` → `{"status": "MISSING_CREDENTIALS"}`, before any DB session;
* no matching pricing entry for the real model/date → `{"status": "PRICING_NOT_CONFIGURED"}`,
  before any DB session (fails closed strictly earlier than the scheduler's own internal
  preflight — belt-and-suspenders, verified by
  `test_shadow_cli_provider_mode.py::test_real_mode_anthropic_missing_pricing_fails_closed`
  asserting `not db_path.exists()`).

Structured JSON output includes `provider_mode`, `research_provider`, `research_model`,
budget outcome, and every existing field — no credentials ever printed. No live-trading
flag exists on this or any other subcommand (structurally verified). Shipped
`config/shadow_operations.yaml` remains `enabled: false` by default — real mode never
activates from credential presence alone.

## 19. Schema changes

All additive, no destructive migration, no change to any Milestone 1-7 table:

* `research_cycle_symbol_evidence_status` (`storage/research_cycle_schema.py`) — Section 5.
* `shadow_role_budget_checks` (`storage/shadow_operations_schema.py`) — Section 11.
* `shadow_budget_usage_attempts` (`storage/shadow_operations_schema.py`) — Section 12.

## 20. Files created

* `src/trading_research/research/cycle_telemetry.py`
* `src/trading_research/shadow/attempt_controller.py`
* `tests/integration/test_milestone_7_1_shadow_integration.py` (7 tests)
* `tests/integration/test_milestone_7_1_real_validation_smoke.py` (1 opt-in test)
* `tests/unit/test_corporate_status_provider_boundary.py` (12 tests)
* `tests/unit/test_attempt_control_hooks.py` (7 tests)
* `tests/unit/test_shadow_attempt_controller.py` (8 tests)
* `tests/unit/test_cycle_telemetry.py` (5 tests)
* `tests/unit/test_shadow_cli_provider_mode.py` (6 tests)
* `docs/milestone7-1-shadow-integration-closure.md` (this file)

## 21. Files modified

* `src/trading_research/evidence_providers/corporate_status_adapters.py` — provider Protocol, `SecCorporateStatusProvider`, `build_corporate_status_with_disclosures`.
* `src/trading_research/evidence_providers/evidence_adapters.py` — `corporate_status_to_evidence_bundle`, `PrefetchedEvidenceProvider`.
* `src/trading_research/evidence_providers/disclosure_extraction.py` — cover-page checkbox false-positive fix.
* `src/trading_research/evidence_providers/fixture_clients.py` — `FixtureSecClient.list_filings` correction.
* `src/trading_research/research/scheduled_cycle.py` — `EvidenceProviderRegistry.corporate_status`, `build_real_evidence_snapshot` return-shape change, completeness persistence/gating in `_run_symbol`, `attempt_controller_factory` threading.
* `src/trading_research/research/orchestration.py` — attempt-control hooks.
* `src/trading_research/research/failure_taxonomy.py` — `STAGE_BUDGET_GATED`/`CODE_BUDGET_EXHAUSTED`.
* `src/trading_research/shadow/scheduler.py` — provider/model/role propagation, attempt-controller-factory wiring, real telemetry-based health inputs.
* `src/trading_research/shadow/budget.py` — `record_actual_usage_for_attempt`.
* `src/trading_research/storage/research_cycle_schema.py` / `research_cycle_repositories.py` — association table.
* `src/trading_research/storage/shadow_operations_schema.py` / `shadow_operations_repositories.py` — role-budget-check and usage-idempotency tables.
* `src/trading_research/storage/research_repositories.py` — `compute_cycle_telemetry`.
* `src/trading_research/cli.py` — corporate-status/real-mode registry wiring, `run_due_shadow_cycle_cli` provider-mode rewrite.
* `tests/unit/test_shadow_scheduler.py` — 2 tests corrected for the deleted provider_mode guess.
* `tests/integration/test_shadow_end_to_end.py` — 1 stale docstring corrected.
* `tests/unit/test_disclosure_extraction.py` — 2 regression tests for the shell-company fix.

## 22. Test results

* **Targeted new/changed tests:** 46 new + 2 regression = 48 new, all passing; 2 existing tests corrected for the Step 11 design change.
* **Full main suite:** `pytest tests/ -q` → **1221 passed, 13 skipped** (baseline 1174 passed/12 skipped + 47 net new default-run tests + 1 new opt-in-skipped real-validation test). Zero regressions, zero existing test weakened/deleted/newly-skipped.
* **Paper-runtime suite:** `cd paper_runtime && pytest tests/ -q` → **33 passed** (unchanged, directory untouched).

## 23. Real SEC validation — REAL-SEC-VALIDATED

Real `SecEdgarClient`/`FilingDocumentClient` against the live `data.sec.gov`/`www.sec.gov`
endpoints for AAPL, `as_of=2026-07-11T13:00Z`. Found and fixed a genuine false-positive bug
(Section 4) before proceeding. See scratchpad "Real validation" section for full sanitized
detail.

## 24. Real Claude validation — REAL-CLAUDE-VALIDATED

Real `AnthropicResearchProvider` (bear + manager, one attempt each, model `claude-sonnet-5`)
driven through the real, unmodified `run_due_shadow_cycle` → `run_scheduled_research_cycle`
→ `analyze_with_research_committee` chain. See Section 15/16 and the scratchpad for the
full sanitized result set: 2 role-budget checks (both `PROCEED`), 2 real attempts, 18,833
input / 7,275 output tokens, 68,135ms latency, `$0.24` reserved / `$0.1656` consumed
(exact match to persisted attempt-level priced usage), zero paper submissions, lease
released, reservation settled. `health_status=PAUSE_REQUIRED` on this otherwise fully
successful run is recorded as an open, un-investigated (to avoid further real spend this
session) diagnostic item — flagged in Section 26 "Known limitations," not silently omitted.

## 25. Reserved versus consumed cost

`reserved_cost_usd=0.24000000` (worst-case: 1 symbol × 2 roles × 1 attempt × 4000 tokens ×
$15/M) versus `consumed_cost_usd=0.16562400` (real, priced, attempt-level usage) —
reservation released the unused ~31% correctly on settlement. Consumed cost is never
silently zero after a real Claude run (the specific defect this milestone was required not
to still exhibit).

## 26. Known limitations

1. `health_status=PAUSE_REQUIRED` on the real validation run's otherwise-successful cycle
   was not root-caused this session (avoided a third paid Claude call purely to
   investigate); the test now prints `health_reasons_json`/`cycle_status` for a future
   session to diagnose from a fresh real run.
2. `shadow/retention.py::apply_retention(dry_run=False)` still unconditionally raises
   `NotImplementedError` (unchanged from Milestone 7 — real destructive retention remains
   explicitly out of scope for this and every prior milestone).
3. Real Alpaca news/market-data and real Reddit sentiment remain ENVIRONMENTALLY_PENDING —
   `ALPACA_MARKET_DATA_API_KEY`/`_SECRET` and `REDDIT_CLIENT_ID`/`_SECRET` are absent from
   `.env` this session, unchanged from Milestone 7's own session.
4. `_looks_like_cover_page_checkbox_context`'s 200-character lookback window is a
   reasonable, tested bound, not derived from a formal specification of SEC cover-page
   layout — a filing with unusually long intervening boilerplate between the checkbox
   question and the "shell company" phrase could theoretically still produce a false
   positive; not observed against real AAPL text, but not exhaustively proven for every
   filer's exact cover-page wording.
5. `shadow/retention.py`'s table classification still does not cover the repository's
   entire schema (unchanged limitation from Milestone 7, not touched by this milestone).
6. Corporate-action evidence (`AlpacaCorporateActionsClient`) remains a standalone client
   with no `EvidenceProviderRegistry` wiring — unchanged from Milestone 7, out of this
   milestone's explicit scope (Steps 4-6 named corporate *status*, not corporate *actions*).

## 27. Deferred work (explicitly out of this milestone's scope)

* Additional news vendors, real Reddit app registration, remaining deferred corporate-action types.
* Destructive retention, separate baseline/enhanced paper books, MFE/MAE, live promotion.
* Actual recurring scheduler activation (`launchctl load`) — **not performed**, not requested.
* A new broker, a new research model, a replacement for SQLite, an LLM-based disclosure extractor.

## 28. ACTUAL RECURRING DEPLOYMENT ACTIVATED: **FALSE**

No `launchctl load` was run. `deploy/launchd/*.plist.example` remains exactly as Milestone
7 left it (inert, `.example` suffix, `RunAtLoad: false`, `StartCalendarInterval` commented
out) — this milestone did not touch it.
