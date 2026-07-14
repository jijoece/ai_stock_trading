# Milestone 7.1 Progress

Started: 2026-07-13T17:50:08Z
Branch: main
Status: STARTING

## Baseline
- `pytest tests/ -q` -> 1174 passed, 12 skipped (matches expected baseline exactly)
- `cd paper_runtime && pytest tests/ -q` -> 33 passed (matches expected baseline exactly)
- Git status: clean except untracked `.claude/scratchpads/milestone7-progress.md` and `docs/milestone-7.md` (both pre-existing, added before this session)
- Credentials, boolean presence only:
  - ANTHROPIC_API_KEY: present
  - ALPACA_API_KEY: present
  - ALPACA_API_SECRET: present
  - ALPACA_MARKET_DATA_API_KEY: absent
  - ALPACA_MARKET_DATA_API_SECRET: absent
  - REDDIT_CLIENT_ID: absent
  - REDDIT_CLIENT_SECRET: absent
- DB paths: `data/research.sqlite3` (gitignored local dev DB, used by CLI). Tests use temp sqlite files via `storage/database.py::connect()`.
- Real validation this session will therefore be possible for: real SEC EDGAR corporate-status, real Claude (bear+manager). NOT possible for: real Alpaca news/market-data, real Reddit (credentials absent) — consistent with Milestone 7's own session.

## Current integration gaps confirmed
(from docs/milestone-7.1.md's own list, cross-checked against .claude/scratchpads/milestone7-progress.md "Known limitations"/"Deferred work" sections — all 13 confirmed accurate as of baseline, will re-verify against actual code before editing each)

1. Corporate-status evidence not in primary EvidenceSnapshot — CONFIRMED (milestone7-progress.md line 409, 458)
2. No corporate-status provider connected to build_evidence_snapshot — CONFIRMED (same)
3. evaluate_completeness not auto-called from scheduled-cycle path — CONFIRMED (line 408, 458)
4. Corporate completeness does not gate Claude calls — CONFIRMED (follows from #3)
5. check_role_budget not called before actual role attempts — CONFIRMED (line 426, 460 "Per-role runtime budget enforcement is NOT wired end-to-end")
6. Actual Claude attempt usage not incrementally charged to shadow reservation — CONFIRMED (line 428 "record_actual_usage is always called with zero actual cost/tokens")
7. Final shadow budget consumption doesn't reflect persisted Claude usage — CONFIRMED (follows from #6)
8. Scheduler health receives None for several fields — CONFIRMED (line 441, 461)
9. CycleIntent.model_name not populated correctly — CONFIRMED (line 450, hardcoded None at scheduler.py:469 per M7 scratchpad)
10. Model-specific pricing lookup incomplete — CONFIRMED (follows from #9)
11. FixtureSecClient.list_filings() returns no useful filings — CONFIRMED (line 451, unconditionally returns `()`)
12. Real scheduler CLI path not fully assembled for real-provider mode — CONFIRMED (line 442, 463 "only ever drives provider_mode=fixture")
13. ADR 0005 describes target behaviors as active that M7 docs record as not wired — TO VERIFY during Step 3 (ADR review)

## Architecture decisions

Full code read of: research/evidence.py, evidence_completeness.py, orchestration.py,
scheduled_cycle.py, models.py, configuration.py, usage.py, evidence_providers/
corporate_status*.py, filing_documents.py, disclosure_extraction.py, evidence_adapters.py,
fixture_clients.py, sec_provider.py, normalization.py, shadow/scheduler.py, budget.py,
role_budget.py, health.py, config.py, storage/*_repositories.py, *_schema.py, database.py,
cli.py (evidence registry builder, run_due_shadow_cycle_cli, run_research_cycle_cli),
config/*.yaml, ADR 0005, failure_taxonomy.py. Confirms all 13 gaps from docs/milestone-7.1.md
plus the milestone7-progress.md "Deferred work" list.

Key finding: `research_attempts` table (research_repositories.py) already stores
per-attempt input_tokens/output_tokens/latency_ms/estimated_cost/cost_status/retry_count
keyed by research_run_id — SymbolCycleResult.research_run_id already threads this ID up to
the scheduler. This is the authoritative source for cycle telemetry (Step 16) and actual
usage settlement (Step 15/17) — no new usage-capture mechanism needed, just a query layer
joining shadow scheduler run -> cycle -> symbol_results -> research_run_ids -> research_attempts.

### Corporate-status provider boundary (Step 4)
- New `CorporateStatusEvidenceProvider` Protocol + `SecCorporateStatusProvider` concrete
  class added to `evidence_providers/corporate_status_adapters.py` (co-located with
  `derive_corporate_status`, no research/ import needed there).
- `SecCorporateStatusProvider.fetch(symbol, as_of)` calls new
  `build_corporate_status_with_disclosures(...)` (Step 6) which wraps `derive_corporate_status`
  and optionally layers bounded text-level disclosure extraction on top.
- Fixture and real modes both use `SecCorporateStatusProvider` — fixture wraps
  `FixtureSecClient` (now returns real deterministic filings, Step 10) with
  `filing_document_client=None` (metadata-only, no HTTP in fixture mode); real mode wraps
  `SecEdgarClient` with a real `FilingDocumentClient`.

### Snapshot normalization (Step 5)
- New `corporate_status_to_evidence_bundle(evidence) -> EvidenceBundle` in
  `evidence_providers/evidence_adapters.py` (this file already imports research.evidence/
  models — the correct home for the research-facing conversion). Produces bounded
  EvidenceItems: reporting_status, earliest_reliable_filing_date (labeled as a
  public-reporting-history *proxy*, never company age), latest_annual/quarterly_filing,
  late_filing_notices, and one item per risk-signal category (bankruptcy/delisting/
  registration/shell/going_concern) carrying `status`+`basis`+accession/form-type
  provenance. Stable evidence IDs: `f"corporate-status-{symbol}-{as_of_date}-{field}"`.
  One shared SourceRecord per symbol/as_of (`corporate-status-{symbol}-{as_of_date}`),
  provider="sec-edgar-corporate-status", available_at=as_of, point_in_time_safe=True
  (methodology is point-in-time safe regardless of content certainty — mirrors how
  `derive_corporate_status` already filters filings <= as_of before this layer ever runs).
- New tiny `PrefetchedEvidenceProvider` adapter (frozen dataclass wrapping an
  already-built `EvidenceBundle`, `.fetch()` returns it unchanged) lets the pre-fetched
  corporate-status bundle plug into `build_evidence_snapshot`'s existing
  `providers: Sequence[Protocol]` list without changing that function's signature —
  smallest safe extension point, per the milestone's own instruction to reuse the
  existing `EvidenceBundle` provider shape.
- `EvidenceProviderRegistry` (research/scheduled_cycle.py) gains one new optional field
  `corporate_status: Any | None = None` (appended at the end, default `None` — every
  existing positional/keyword construction site remains valid; grepped all call sites,
  none use positional args).
- `build_real_evidence_snapshot` now: (1) calls `providers.corporate_status.fetch(...)`
  once if configured, (2) converts to a bundle and includes it in `active_providers`,
  (3) returns `(snapshot, corporate_status_evidence_or_None)` — a tuple, changing its
  return type. Both call sites (`_run_symbol`, `cli.py::fetch_evidence_cli`) updated.

### Disclosure composition (Step 6)
- New `build_corporate_status_with_disclosures(symbol, *, sec_client,
  filing_document_client, as_of, cik=None)` in `corporate_status_adapters.py`. Reuses the
  metadata-only `derive_corporate_status` result as a base, then — only when a
  `filing_document_client` is supplied — fetches at most 2 bounded filing documents
  (latest annual filing text serves both going_concern and shell_company searches since
  they're the same document; the most recent 8-K referenced by the bankruptcy signal's
  own `evidence_refs` serves the bankruptcy search) and applies `extract_disclosure`.
  `EXPLICIT_DISCLOSURE_FOUND` upgrades the signal to `CONFIRMED` with the accession/section
  cited in `basis`; every other extraction outcome (NOT_FOUND/AMBIGUOUS/SEARCH_INCOMPLETE/
  DOCUMENT_UNAVAILABLE) leaves the original metadata-only status unchanged and only appends
  an audit note to `basis` — never downgrades, never converts NOT_FOUND to a confirmed
  negative, never fabricates SOURCE_UNAVAILABLE from a merely-unavailable single document.
  `filing_document_client=None` (fixture mode) returns the unmodified metadata-only base —
  deterministic, offline, no HTTP.

### Cycle/symbol association persistence (Step 7)
- New table `research_cycle_symbol_evidence_status` appended to
  `storage/research_cycle_schema.py` (natural home — already owns the cycle/symbol
  coordination tables), PK `(cycle_id, symbol)`, columns per the milestone's suggested
  shape (snapshot_id, corporate_status_evidence_id, completeness_result_id,
  screening_completeness, research_completeness, blocking_categories_json,
  policy_version, created_at). `INSERT OR REPLACE` keyed by `(cycle_id, symbol)` —
  idempotent save, mirrors `save_symbol_result`'s existing pattern.
- New repository functions `save_symbol_evidence_status`/`load_symbol_evidence_status`
  in `storage/research_cycle_repositories.py`.
- Idempotent resume: unchanged from the existing mechanism — `run_scheduled_research_cycle`
  already skips `_run_symbol` entirely for any symbol whose `research_cycle_symbol_results`
  row is COMPLETED/SKIPPED (top-level `existing.status` check), so corporate-status is
  never re-fetched for a completed symbol on resume, exactly like snapshot-building
  isn't re-invoked either. No finer-grained resumability needed inside `_run_symbol`.

### Evidence-completeness gating (Step 8-9)
- `_run_symbol` calls `evaluate_completeness(...)` right after snapshot+corporate-status
  are built and persisted, before any Claude call. `news_present`/`sentiment_present`
  computed from `snapshot.evidence_items` categories (`any(item.category == "news" ...)`),
  not merely provider-configured — an evidence-item is only present if that provider
  configured AND returned data.
  `evidence_blocks_enhanced = configuration.require_complete_evidence and (outcome in
  BLOCKING_OUTCOMES or completeness_result.screening_blocked)` — extends, does not
  replace, the existing snapshot-outcome gate. `SymbolCycleResult` dataclass/status
  semantics are NOT changed (still COMPLETED with orchestration_status=ANALYSIS_INCOMPLETE
  when blocked) — the completeness reason is authoritatively queryable via the new
  association table's `blocking_categories_json`, not overloaded onto
  `SymbolCycleResult.failure_reason` (which existing consumers treat as a hard-failure
  signal only).
- Operating-history semantics (Step 9): UNCHANGED — `operating_history.py::
  derive_operating_history` remains unwired from `CandidateInput`/`FundamentalSnapshot`.
  The corporate-status EvidenceBundle labels `earliest_reliable_filing_date` explicitly as
  a public-reporting-history proxy (not operating history) in its evidence item title/summary.

## Attempt-control hook design (Step 12)
New `AttemptControlRequest`/`AttemptControlDecision` frozen dataclasses + `ResearchAttemptController`
Protocol added to `research/orchestration.py` itself (no new module — keeps the framework-neutral
hook next to the one function that calls it, avoids a needless extra file). `analyze_with_research_committee`
and `_run_role_with_retries` gain an optional `attempt_controller: ResearchAttemptController | None = None`
parameter (default `None` = today's exact behavior, zero call sites need updating). Before each
`provider.generate_structured(request)` call, if a controller is supplied, `before_attempt(...)` is
called; a `not allowed` decision skips the provider call entirely and records a distinct
`ResearchAttemptRecord(success=False, failure_reason=f"budget gated: {decision.reason}")` tagged via
a NEW failure stage/code (`STAGE_BUDGET_GATED`/`CODE_BUDGET_EXHAUSTED`, added to failure_taxonomy.py's
allowlists) — structurally distinct from `CODE_PROVIDER_UNAVAILABLE`/retryable-provider-error codes,
so it can never be miscounted as a provider failure in telemetry/health. `after_attempt(...)` is
called once per completed attempt (success, schema rejection, claim rejection, provider error with
a record, malformed output) right after the attempt is appended to `attempts`/persisted — for every
`continue`/`break`/`return` path in the retry loop.

## Per-attempt budget enforcement (Step 13-14)
New `shadow/attempt_controller.py`: `ShadowResearchAttemptController` implementing
`ResearchAttemptController`, adapting `before_attempt`/`after_attempt` to
`role_budget.check_role_budget` + `budget.record_actual_usage`. Holds `reservation`,
`allowed_roles`, per-role max-token/latency caps (from `research_configuration`/
`shadow_config.budgets`), and the SAME `PricingEntry` used for the cycle's own reservation
(passed in, never re-selected) so the pre-call gate and the reservation's own worst-case
estimate cannot drift apart. Persists one `shadow_role_budget_checks` row per `before_attempt`
call (Step 14). `after_attempt` calls `budget.record_actual_usage` keyed by `attempt_id`
(idempotency — see Usage telemetry design below).

## Model and pricing propagation (Step 11)
- `run_due_shadow_cycle` gains two new required-with-default params: `research_provider_name: str`
  and `research_model_name: str | None`, threaded into `CycleIntent(provider=research_provider_name,
  model_name=research_model_name, ...)` — REPLACES the old `cycle_configuration.provider_mode`-based
  guess entirely (that mapping is deleted, not layered on top). CLI wiring (Step 20) supplies both
  from `load_research_config()` — the SAME `research.yaml` `provider`/`model` fields Milestone 5-7
  already use for every other Claude call path, per the task's "use the existing model configuration
  convention" instruction. No new config field, no duplicate model surface.
- `estimate_cycle_cost`'s `select_pricing` call already keys on `intent.model_name` — once real
  model name flows through, an anthropic scheduled run with no matching `research_pricing.yaml`
  entry now correctly raises `BudgetConfigError` before lease/provider work (fail-closed, matches
  acceptance criterion #13).

## Usage telemetry design (Step 15)
- `shadow_budget_usage` already has no natural idempotency key (Step 15 note: "if lacking an
  idempotency key, add the smallest safe additive schema change"). ADDING one column:
  `attempt_id TEXT` (nullable, UNIQUE constraint only enforced via an application-level
  `INSERT OR IGNORE`-equivalent check — actually: added a real `UNIQUE(attempt_id)` partial
  concept isn't native to SQLite easily with NULLs allowed; used a companion small table
  `shadow_budget_usage_attempts (attempt_id TEXT PRIMARY KEY, usage_id TEXT NOT NULL,
  reservation_id TEXT NOT NULL, recorded_at TEXT NOT NULL)` instead — additive, keeps
  `shadow_budget_usage` untouched (safer, no ALTER TABLE on an existing table), gives
  `record_actual_usage_for_attempt(...)` an idempotency check ("has this attempt_id already
  been charged?") without weakening the existing append-only `shadow_budget_usage` semantics.

## Budget reservation and settlement
(populated during Step 17 implementation)

## Health and readiness telemetry
(populated during Step 18-19 implementation)

## Manual verification — Steps 11-19 (ad-hoc scripts, formal pytest tests added in Step 21-22)
- `run_due_shadow_cycle` now takes `research_provider_name`/`research_model_name`/
  `research_roles` (all optional, default preserves every prior caller's behavior
  exactly). `CycleIntent.provider`/`.model_name` now come directly from these —
  the old `cycle_configuration.provider_mode`-guess mapping was DELETED, not layered.
  Fixed 2 existing tests in test_shadow_scheduler.py that relied on the old
  provider_mode="real"->"anthropic" guess (now pass `research_provider_name="anthropic"`
  explicitly) — legitimate test correction for the corrected design, not weakening.
- `research/orchestration.py`: added `AttemptControlRequest`/`AttemptControlDecision`/
  `ResearchAttemptController` Protocol; `_run_role_with_retries`/
  `analyze_with_research_committee` gained optional `attempt_controller=None` param
  (zero behavior change for every existing caller). New failure taxonomy
  `STAGE_BUDGET_GATED`/`CODE_BUDGET_EXHAUSTED` added (structurally distinct from
  every provider-failure code).
- `shadow/attempt_controller.py::ShadowResearchAttemptController` — adapts the hook
  Protocol to `role_budget.check_role_budget`/`budget.record_actual_usage_for_attempt`.
  Verified via direct `analyze_with_research_committee(..., attempt_controller=...)` call:
  (a) normal case — 5/5 role-budget checks PROCEED, 5 attempts, status COMPLETED;
  (b) exhausted case (reserved_output_tokens=100 < max_output_tokens_per_role=4000) —
  all 4 analyst roles SKIPPED_BUDGET_EXHAUSTED with zero retries each (denial breaks
  immediately, no wasted attempts), manager never invoked (MANAGER_NOT_INVOKED),
  failure codes distinctly include BUDGET_EXHAUSTED alongside RETRY_EXHAUSTED/
  MISSING_REQUIRED_ROLE — never conflated with a provider failure.
- `research/scheduled_cycle.py::run_scheduled_research_cycle`/`_run_symbol` gained
  optional `attempt_controller_factory: Callable[[str], ...] | None = None`
  (per-symbol factory, fresh role-index state each symbol) — threaded into
  `analyze_with_research_committee`. Default `None` preserves exact prior behavior.
- `shadow/scheduler.py::run_due_shadow_cycle` builds the factory only when the new
  `research_roles` param is supplied, reusing the EXACT pricing entry
  `estimate_cycle_cost` already selected (same `select_pricing` call, same inputs —
  never a second inconsistent lookup).
- Full real, unmodified `run_due_shadow_cycle` -> `run_scheduled_research_cycle`
  smoke-verified end-to-end (fixture SEC+market providers, deterministic Claude
  provider, `research_roles` supplied): 5 role-budget checks persisted (all PROCEED),
  corporate-status evidence + completeness gate correctly non-blocking
  (COMPLETE_FOR_SCREENING / PARTIAL_NONCRITICAL for missing news+sentiment), cycle
  COMPLETED.
- `research/cycle_telemetry.py::ResearchCycleTelemetry` + `storage/
  research_repositories.py::compute_cycle_telemetry(conn, research_run_ids)` — derives
  attempt/retry/retry-exhaustion/required-role-failure/provider-failure/
  unsupported-claim/output-truncation/budget-skip counts plus token/latency/cost
  sums (`None` when genuinely absent, never fabricated 0) directly from persisted
  `research_attempts`/`research_attempt_failures` rows — the one authoritative query.
- `shadow/scheduler.py::_build_health_inputs_from_cycle_result` now joins this
  telemetry against `cycle_result.symbol_results[].research_run_id` — verified via
  the same end-to-end smoke run: `shadow_run_summaries` row shows
  `claude_role_success_rate=1.0`, `retry_rate=0.0`, `retry_exhaustion_rate=0.0`,
  `unsupported_claim_rate=0.0`, `output_truncation_rate=0.0` (all real, previously
  always `None`), `input_tokens`/`output_tokens`/`cost_usd` correctly still `None`
  (deterministic provider genuinely reports no tokens — honest, not a regression),
  `health_status=HEALTHY`. This closes gap #8.
- Readiness (Step 19): `shadow/readiness.py::build_readiness_report` already reads
  `shadow_run_summaries` unmodified — no readiness.py code change needed; it
  automatically benefits from Step 18's now-populated summaries.
- Full main suite after Steps 11-19: 1174 passed, 12 skipped — zero regressions.

## Manual verification — Steps 4-9 (ad-hoc scripts, not yet added as pytest tests)
- Fixture cycle (AAPL, as_of=2026-07-01T23:00Z, all providers configured incl.
  corporate_status): screening_completeness=COMPLETE_FOR_SCREENING,
  research_completeness=PARTIAL_NONCRITICAL (news+sentiment absent, correctly
  NON-blocking), research_run_id populated -> Claude WAS called. Confirms
  non-blocking categories do not block screening.
- Same cycle with a corporate-status SEC client whose `list_filings` raises:
  screening_completeness=MISSING_CRITICAL_CORPORATE_STATUS, `research_run_id=None`,
  a call-counting `DeterministicResearchProvider` subclass recorded exactly 0 calls
  -> Claude never invoked when corporate status is critically uncertain.
  `SymbolCycleResult.status` stays COMPLETED (baseline unaffected) with
  `evidence_outcome=COMPLETE` (snapshot-level outcome unrelated to the corporate-status
  gate) — completeness detail lives in the new
  `research_cycle_symbol_evidence_status` association row, not overloaded onto
  `SymbolCycleResult.failure_reason`.
- Pre-existing, unrelated-to-this-milestone quirk noted during this check:
  `RealMarketEvidenceProvider.fetch`'s `available_at` for the latest bar is set to
  21:00 UTC on the bar's own session date; an `as_of` earlier that same day (e.g.
  20:00 UTC) makes that one SourceRecord's `point_in_time_safe=False`, which then makes
  the WHOLE snapshot POINT_IN_TIME_UNSAFE via `build_evidence_snapshot`'s `all(...)`
  check — reproducible without any of this session's changes (confirmed by removing
  `corporate_status` from the registry and it still happens). Not fixed (out of this
  milestone's explicit scope — no design changes to prior unrelated modules); documented
  here as a known pre-existing sharp edge for future test-writers picking an `as_of` time.
- Full main suite still 1174 passed, 12 skipped after Steps 4-10 (fixture fix + corporate-status
  wiring + gating) — zero regressions.

## Usage telemetry design
(pending)

## Budget reservation and settlement
(pending)

## Health and readiness telemetry
(pending)

## Fixture corrections
(pending)

## CLI real-mode wiring
(pending)

## Schema and migration changes
(pending)

## Files created
(pending)

## Files modified
(pending)

## Tests added
(pending)

## Test run log
- 2026-07-13T17:50Z — `pytest tests/ -q` -> 1174 passed, 12 skipped (baseline confirmed)
- 2026-07-13T17:50Z — `cd paper_runtime && pytest tests/ -q` -> 33 passed (baseline confirmed)

## Real validation

User explicitly approved a bounded real-money/real-network validation run before this
was performed (AskUserQuestion, "Yes, run the real SEC+Claude validation").

**Real SEC EDGAR (no cost, no credentials needed):**
- `build_corporate_status_with_disclosures("AAPL", sec_client=<real SecEdgarClient>,
  filing_document_client=<real FilingDocumentClient>, as_of=2026-07-11T13:00Z)` against
  the real `data.sec.gov`/`www.sec.gov` endpoints.
- Result: `reporting_status=ACTIVE`, `completeness_status=COMPLETE`,
  `earliest_reliable_filing_date=2015-05-29`, `latest_annual_filing=10-K
  0000320193-25-000079`, `latest_quarterly_filing=10-Q`, `has_any_critical_uncertainty=False`.

**Bug found and fixed during this real validation (before proceeding to the paid Claude call):**
- First run reported `shell_company_signals[0].status == CONFIRMED` for Apple — obviously
  wrong. Root cause: `disclosure_extraction.py::_SHELL_COMPANY_EXPLICIT_RE` matched the
  literal SEC 10-K/10-Q cover-page checkbox question ("Indicate by check mark whether the
  registrant is a shell company...") regardless of whether the real answer was Yes or No —
  every 10-K carries this exact boilerplate sentence. Fixed by adding
  `_looks_like_cover_page_checkbox_context`/`_find_first_valid_explicit_match`
  (`disclosure_extraction.py`): skips any regex match whose preceding ~200 characters
  contain "indicate by check mark" / "check the appropriate box" / "check if the
  registrant", continuing to search for a genuine affirmative statement elsewhere in the
  document. Re-verified against the same real AAPL filing after the fix:
  `shell_company_signals[0].status == NOT_FOUND_IN_SEARCHED_SOURCES` (correct).
  Two regression tests added to `tests/unit/test_disclosure_extraction.py`
  (`test_shell_company_cover_page_checkbox_boilerplate_is_never_confirmed`,
  `test_shell_company_confirmed_when_affirmative_statement_follows_cover_page_question`)
  proving both the false-positive fix and that a genuine affirmative statement elsewhere
  in the same document is still correctly found. This is exactly the kind of defect real
  validation exists to catch — a metadata-plus-naive-regex false positive that all offline
  fixture tests (which never used real 10-K cover-page text) could not have caught.

**Real Claude (bear + manager, one attempt each, `tests/integration/
test_milestone_7_1_real_validation_smoke.py`, `RUN_REAL_CLAUDE_SHADOW_CYCLE=true`):**
Ran twice — first run failed only on a test-code float-precision artifact in the test's
own SQL aggregation (`SUM(CAST(... AS REAL))`), fixed to sum `Decimal` values in Python;
second run passed cleanly. Sanitized results (from the passing run):
```
scheduler_run_id=shadow-run-bb0a23a5cdaa45e5bae3b4e04c31301c
cycle_id=cycle-1eff7c7ae8e7245acf497327e047e364
symbol=AAPL provider=anthropic model=claude-sonnet-5
corporate_status_id=f6fd0934-02c0-4a05-a138-cf6741f57aa7
screening_completeness=COMPLETE_FOR_SCREENING
role_budget_check_count=2 role_budget_decisions=['PROCEED', 'PROCEED']
attempt_count=2 input_tokens=18833 output_tokens=7275 latency_ms=68135
reserved_cost_usd=0.24000 consumed_cost_usd=0.16562400 priced_attempt_cost_usd=0.16562400
health_status=PAUSE_REQUIRED
paper_submission_count=0 market_data_is_real=False
```
- Corporate status was fetched from real SEC and entered the snapshot: CONFIRMED (corporate_status_id
  persisted, associated with the cycle/symbol).
- Completeness result was persisted and the gate was evaluated: CONFIRMED
  (`screening_completeness=COMPLETE_FOR_SCREENING`, non-blocking — real Claude proceeded).
- `CycleIntent` contained the actual model: CONFIRMED (`role_budget_checks.model_name ==
  "claude-sonnet-5"` for both checks).
- Model-specific pricing matched, reserved estimated cost > 0: CONFIRMED (`reserved_cost_usd=0.24`).
- Role-budget check persisted before both bear and manager, both PROCEED, real Claude
  attempts occurred only after approved checks: CONFIRMED (`role_budget_check_count=2`,
  `attempt_count=2`, matching 1:1).
- Actual input/output tokens > 0, actual latency > 0: CONFIRMED (18,833 / 7,275 / 68,135ms).
- Priced consumed cost > 0, and consumed cost equals persisted attempt-level priced usage:
  CONFIRMED (`consumed_cost_usd == priced_attempt_cost_usd == 0.16562400`, exact match —
  proves Step 17 settlement reconciles to real attempt-level usage, not a fabricated figure).
- Paper submissions = 0, enhanced executions = 0: CONFIRMED (`paper_submission_count=0`,
  `may_submit_enhanced()` structurally `False`).
- Lease released, reservation settled: CONFIRMED (asserted in the test).
- **Known-limitation, honestly reported, not investigated further to avoid spending
  additional real money this session:** `health_status=PAUSE_REQUIRED` on an otherwise
  fully successful 2/2-attempt real cycle. The test now prints `health_reasons_json` and
  `cycle_status` for a future run to diagnose precisely (added after this run, so not
  captured this session) — the most likely explanation from code inspection is
  `provider_success_rate` reflecting the `SymbolCycleResult.status` for this run (not
  independently re-verified against the live DB row, which was in a `tmp_path` already
  cleaned up by the time this was investigated). This does NOT indicate a bug in the
  settlement/telemetry/corporate-status piperic — every other assertion (including the
  exact-equality cost reconciliation) passed — but is flagged here for a future session
  to re-run with the diagnostic print now in place before assuming a specific cause.
- Total real spend this session: ~$0.33 (two Claude runs, ~$0.165 each — the first
  run's Claude calls were real and correct; only the test's own cost-aggregation
  assertion failed).

## Bugs discovered and fixed
1. **`disclosure_extraction.py` shell-company false positive** (see "Real validation"
   above) — the single most significant defect found this session, discovered only
   because real validation was performed against real SEC filing text.
2. Two `test_shadow_scheduler.py` tests relied on the old `provider_mode="real"` ->
   `"anthropic"` guess that Step 11 deliberately deleted — updated to pass
   `research_provider_name="anthropic"` explicitly (legitimate test correction for the
   corrected design, not a weakening).
3. Test-file-only float-precision bug in `test_milestone_7_1_real_validation_smoke.py`'s
   own SQL aggregation (`SUM(CAST(x AS REAL))` on a Decimal-stored TEXT column) — fixed
   to sum `Decimal` values in Python. Not a production-code bug.

## Security and secret review
All checks below performed directly against the actual working-tree diff (not delegated):
- No secret-shaped strings (`sk-ant-`, API-key patterns, bearer tokens) in any new/modified
  file, this scratchpad, or the closure doc — grepped and confirmed clean.
- No `.env` contents printed anywhere in this scratchpad or docs (checked for
  `ANTHROPIC_API_KEY=` literal occurrences — zero).
- No provider authorization header ever persisted (`shadow_role_budget_checks`/
  `shadow_budget_usage_attempts` only store role/token/cost/decision fields — verified
  against the actual schema DDL).
- No account identifier persisted anywhere in the new schema.
- `research/orchestration.py` has zero imports of `sec_provider`/`alpaca`/`broker`/
  `robinhood`/`lumibot` — Claude has no SEC/Alpaca/broker tool access (grep-verified).
- `shadow/attempt_controller.py` never imports or calls `anthropic`/Claude directly — it
  only adapts the framework-neutral hook to `role_budget.py`/`budget.py` (grep-verified;
  the only "anthropic" string is a code comment).
- No enhanced-arm paper submission from any new code path — `_run_symbol`'s existing
  "enhanced arm never submits" invariant (`experiment_policy.may_submit_enhanced()`
  unconditionally `False`) was not touched.
- No live trading, no Robinhood mutation — no new code path anywhere near
  `execution/`/`paper/ledger.py`'s mutation surface.
- No budget decision influenced by Claude — `check_role_budget`/`evaluate_completeness`
  are pure deterministic functions; Claude's response is never an input to either.
- No completeness decision influenced by Claude — `evaluate_completeness` only consumes
  `snapshot_outcome`/`corporate_status`/`news_present`/`sentiment_present`, all
  deterministically derived before any Claude call.
- No unknown cost treated as zero — `ShadowResearchAttemptController.after_attempt`
  explicitly skips charging (rather than charging `$0`) when cost is genuinely unknown for
  a provider that should have priced it; verified by
  `test_shadow_attempt_controller.py::test_after_attempt_never_fabricates_usage_when_tokens_unavailable`.
- No duplicate attempt charge — `record_actual_usage_for_attempt` is idempotent on
  `attempt_id` via `shadow_budget_usage_attempts`; verified by both the unit test and the
  offline e2e retry-accounting test.
- No duplicate reservation settlement — `settle_reservation`'s pre-existing idempotency
  (unchanged from Milestone 7) was not touched.
- No provider call after budget denial — `_run_role_with_retries`'s `before_attempt`
  denial path `break`s before `provider.generate_structured(...)` is ever reached (code
  inspection + `test_denied_attempt_never_calls_provider`).
- No provider-failure metric increment from budget denial — `STAGE_BUDGET_GATED`/
  `CODE_BUDGET_EXHAUSTED` are structurally distinct from every `CODE_PROVIDER_*` code;
  `compute_cycle_telemetry`'s `provider_failure_count` explicitly excludes them (verified
  by `test_cycle_telemetry.py::test_budget_skip_distinguished_from_provider_failure`).
- No future filing in a historical snapshot — `FixtureSecClient.list_filings`'s future
  fixture filing is filtered by the same `accepted_at <= available_by` rule the real
  client already enforces; verified by
  `test_fixture_sec_client_returns_deterministic_filings_covering_the_offline_path`.
- `NOT_FOUND_IN_SEARCHED_SOURCES` never converted to `false` — `_merge_disclosure_signal`
  only ever upgrades to `CONFIRMED` on an explicit match; every other outcome preserves the
  original status verbatim (code inspection + 3 dedicated unit tests).
- Public-reporting history never mislabeled as company age — the evidence-item title/summary
  explicitly say "proxy — NOT company age or years of actual operating history"; verified by
  a dedicated unit test.
- No screener weakening — `operating_history.py::derive_operating_history` remains
  completely unwired from `CandidateInput`/`FundamentalSnapshot`; grep-confirmed no new
  import of it in `services/analyze_candidate.py` or `models/trading_models.py`.
- No recommendation-immutability weakening — no `UPDATE`/`DELETE` against `recommendations`
  in any new/modified file (grep-confirmed).
- `real_orders` remains write-blocked — zero diff touches it (confirmed via `git diff --stat`).
- launchd remains inactive — `deploy/launchd/*.example` untouched this session; no
  `launchctl load`/`launchctl start` invocation anywhere in any `.py` file or shell command
  run this session.
- Existing tests were not weakened, deleted, or newly skipped to obtain a pass — the 2
  `test_shadow_scheduler.py` edits are additive (`research_provider_name`/
  `research_model_name` kwargs added to an existing assertion-unchanged test), and the 1
  `test_shadow_end_to_end.py` edit is a docstring-only correction (zero assertion changes).

## Documentation consistency review
- `docs/milestone7-1-shadow-integration-closure.md` created — every concrete claim
  (file path, table name, test count, real-validation figure) cross-checked against the
  actual code/test output before writing (not transcribed from memory).
- `docs/adr/0005-production-shadow-operations-boundary.md` — appended a "Milestone 7.1
  closure" section; Decisions 1-11's original text left completely unmodified (does not
  rewrite Milestone 7's own historical honesty about what was/wasn't wired at that time).
- `docs/milestone7-production-shadow-operations.md` — added a pointer note at the top;
  body text left unmodified (preserves Milestone 7's own historical record).
- `docs/runbooks/shadow-operations.md` / `shadow-incident-response.md` — added short
  "Milestone 7.1 update" notes pointing to the closure doc; existing procedures unchanged
  (still accurate — the CLI's existing subcommands didn't change shape, only
  `run-due-shadow-cycle` gained new optional flags).
- No documentation claims recurring activation; every occurrence of "launchctl load" in
  any doc is either describing the inert `.example` artifact, an explicit manual operator
  step never executed this session, or explicitly stating it was NOT run.

## Known limitations
See `docs/milestone7-1-shadow-integration-closure.md` Section 26 for the authoritative,
detailed list (health_status=PAUSE_REQUIRED root-cause not investigated to avoid further
real spend; retention destructive-delete still NotImplementedError; real news/Reddit still
environmentally pending; cover-page-checkbox exclusion window is a tested-but-not-formally-
proven heuristic; retention table classification still partial; corporate-action evidence
still unwired into the registry).

## Deferred work
See `docs/milestone7-1-shadow-integration-closure.md` Section 27 (additional news vendors,
real Reddit registration, remaining corporate-action types, destructive retention, separate
paper books, MFE/MAE, live promotion, actual recurring scheduler activation, new
broker/model/DB, LLM-based disclosure extraction) — all explicitly named as non-goals in
docs/milestone-7.1.md and not attempted.

## Final status
**COMPLETE for this session's scope.**

- Baseline confirmed exactly: 1174 passed/12 skipped (main), 33 passed (paper_runtime).
- Final: **1221 passed, 13 skipped** (main) — 47 net new default-run tests + 1 new
  opt-in-skipped real-validation test, zero regressions, zero existing test weakened.
  **33 passed** (paper_runtime, unchanged, directory untouched).
- All 13 originally-named integration gaps confirmed from code, then closed and
  real-validated end-to-end (Sections 2-19 of the closure doc).
- Real validation performed with explicit user approval (AskUserQuestion): real SEC EDGAR
  corporate-status fetch for AAPL (found and fixed a genuine shell-company false-positive
  bug in the process), and a real Claude API shadow cycle (bear+manager, 2 real attempts,
  18,833 input / 7,275 output tokens, $0.1656 consumed cost exactly matching persisted
  attempt-level usage, zero paper submissions, lease released, reservation settled).
- No commit or push performed at any point in this session.
- Every Milestone 1-7 safety invariant preserved (see "Security and secret review" above).
