# Milestone 5 — Evidence-backed Claude research — progress tracker

Started: 2026-07-12

## Baseline (confirmed before any edits)
- Main suite: `pytest tests/ -q` → **422 passed, 1 skipped** ✅ matches expected
- Paper runtime: `cd paper_runtime && pytest tests/ -q` → **33 passed** ✅ matches expected
- Git branch: `main`, only untracked file was `docs/milestone-5.md`

## Key repo-convention findings (inform design)
- `anthropic` SDK is already a **base** dependency (pyproject.toml) — used today only by
  `scripts/submit_batch.py` (one-time meta-research pipeline, unrelated to trading pipeline).
  `Config.anthropic_api_key` / `Config.anthropic_model` already exist in `config.py`.
  Decision: keep anthropic as base dep (already established convention), do NOT add an
  optional `research` extra — would contradict "follow existing reasonable conventions".
  Anthropic import stays isolated inside `research/anthropic_provider.py` regardless.
- Persistence convention: one `*_schema.py` per concern with idempotent
  `CREATE TABLE IF NOT EXISTS` + `_COLUMN_UPGRADES` dict + trigger-based immutability,
  applied from `storage/database.py::connect()`. One `*_repositories.py` per schema file.
  Pattern to copy: `storage/execution_schema.py` + `storage/trading_repositories.py`.
- Dataclass convention: `@dataclass(frozen=True)`, UTC-aware datetimes enforced in
  `__post_init__`, `Decimal` for money, config hashing via `hashing.hash_config()`.
- CLI convention: `argparse` subparsers in `cli.py::main()`, handler functions return a
  `dict`, printed via `json.dumps(..., indent=2, default=str)`, non-zero exit on error.
- Recommendation builder (`recommendations/builder.py`) is schema-validated
  (`schemas/recommendation.schema.json`) and immutable once frozen — DB trigger blocks
  UPDATE/DELETE. Never touch this schema's `risk_plan`/side/status semantics.
- Robinhood MCP read-only tool policy **already exists**: `config/tool_policy.yaml` +
  `mcp/tool_classifier.py` (allowlist > denylist-pattern > unknown-fail-closed). Reuse,
  do not reimplement, for Step 5's "Robinhood MCP policy" requirement.
- Existing prompt-injection annotator: `collection/prompt_injection_filter.py`
  (regex patterns → InjectionRisk NONE/LOW/MEDIUM/HIGH). Reuse the same pattern style
  for evidence-item annotation rather than inventing a second detector from scratch.
- Test layout: `tests/unit/`, `tests/integration/`, `tests/support/`, `tests/fixtures/`.

## Design decisions
- New package: `src/trading_research/research/` (models, provider_protocol,
  deterministic_provider, anthropic_provider, prompts.py, prompt_registry, evidence,
  evidence_validation, output_validation, claim_validation, orchestration, overlay,
  replay, usage, errors, configuration).
- Anthropic structured output mechanism: forced tool-use (single tool whose
  input_schema == our JSON schema, `tool_choice={"type":"tool","name":...}`) — the
  stable, documented Anthropic pattern for strict JSON, avoids beta-feature risk.
- New DB schema file `storage/research_schema.py` + `storage/research_repositories.py`,
  wired into `storage/database.py`. Tables: research_evidence_snapshots, research_runs,
  research_attempts, research_role_reports, research_decisions,
  research_overlay_decisions, research_experiment_assignments, research_failures.
- New config `config/research.yaml` (research disabled by default) +
  `config/research_pricing.yaml` (optional, effective-dated).
- Prompts under `prompts/research/<role>/v1.txt`.
- CLI: build-evidence, run-research, replay-research, compare-research-arms,
  research-performance, research-usage — added to existing `cli.py`.

## Step checklist (docs/milestone-5.md Suggested Implementation Order) — ALL DONE
- [x] 1. Inspect repository and run both baselines
- [x] 2. Gap analysis (this file)
- [x] 3. Draft research-boundary ADR (docs/adr/0003-claude-research-boundary.md)
- [x] 4. Evidence and research contracts (research/models.py)
- [x] 5. Deterministic snapshot hashing and persistence
- [x] 6. Fixture evidence providers (research/evidence.py, research/fixtures.py)
- [x] 7. Provider protocol + scripted provider
- [x] 8. Prompt registry and versioning
- [x] 9. Role-report schemas
- [x] 10. Structured-output validation
- [x] 11. Claim-to-evidence validation
- [x] 12. Prompt-injection defenses
- [x] 13. Orchestration + bounded retries
- [x] 14. Replay and idempotency
- [x] 15. Deterministic overlay
- [x] 16. Integrate overlay with recommendation builder (research/recommendation_overlay.py)
- [x] 17. Experiment assignment + baseline comparison
- [x] 18. Usage, latency, cost tracking
- [x] 19. Anthropic provider (kept as base dep — see ADR 0003 Decision 1)
- [x] 20. CLI commands (build-evidence, run-research, replay-research, compare-research-arms, research-performance, research-usage)
- [x] 21. Offline integration tests (tests/integration/test_research_end_to_end.py, 4 scenarios)
- [x] 22. Opt-in real Claude smoke test — ATTEMPTED, blocked by Anthropic account billing (insufficient credit), not code
- [x] 23. Extend evaluation reporting (evaluation/research_comparison.py)
- [x] 24. Documentation (docs/milestone5-evidence-backed-claude-research.md + ADR 0003)
- [x] 25. Run full main suite — 571 passed, 2 skipped (baseline was 422/1; net +149 new tests, 0 regressions)
- [x] 26. Run isolated paper-runtime suite — 33 passed, unchanged, zero files touched under paper_runtime/
- [x] 27. Ran Claude smoke test with real credentials present — failed on Anthropic billing (HTTP 400, insufficient credit), NOT a code defect; fixed a real bug found in the process (400 was being misclassified as retryable)
- [x] 28. Self-review — no anthropic imports outside anthropic_provider.py, no execution/paper/runtime imports in research/, no real_orders references, no robinhood mutating-tool calls

## MILESTONE 5 COMPLETE — 2026-07-12

## Notes / decisions log
- Renamed the new `research_runs` table to `research_committee_runs` after discovering a
  pre-existing, unrelated `research_runs` table in `storage/migrations.py` (leftover from the
  earlier batch-API meta-research pipeline) — a real collision caught by an end-to-end smoke
  test before writing the formal test suite.
- Found and fixed a real bug in `anthropic_provider.py` via the live smoke-test attempt: HTTP 400
  (billing error) was being classified as `ProviderTransientError` (retryable) instead of
  `ProviderUnavailableError`. Fixed to treat only explicit 5xx/529 as transient.
- IMPORTANT: while inspecting `.env` for the smoke test, a `cat`-equivalent grep command
  accidentally printed `ALPACA_API_SECRET` in plaintext into the conversation transcript (my
  exclusion filter only matched `API_KEY`/`TOKEN`, not `API_SECRET`). Flagged to the user;
  recommend rotating that Alpaca secret out of caution.
