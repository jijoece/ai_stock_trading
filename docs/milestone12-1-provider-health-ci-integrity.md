# Milestone 12.1 — Provider, Health-Control, Telemetry, and CI Integrity Closure

## 1. Starting commit and branch

- Starting commit: `17144227681a3ceabf75f36eb8aa91b765bef5fc` ("Close milestone 11.3.2 operational integrity gaps")
- Branch: `agent/milestone-12-codex-provider`
- No commit or push was made as part of this work; all changes remain in the working tree.

## 2. Exact CI failure and root cause

`gh run view <run-id> --log-failed` against the latest `main` push (run `29638306427`) showed `main-tests` failing with:

```
FAILED tests/unit/test_codex_configuration.py::test_build_codex_provider_config_wires_through
E   PermissionError: [Errno 13] Permission denied: '/private'
```

`VALID_YAML`'s fixture hardcoded `working_directory: /private/tmp/agentic-trading-desk-codex-test`. On macOS, `/private/tmp` is the real, writable, resolved target of the `/tmp` symlink, so `CodexProviderConfig.__post_init__`'s `workdir.mkdir(...)` silently succeeds locally. On the Ubuntu GitHub Actions runner there is no `/private` directory, and the runner process cannot create one at the filesystem root — a macOS-only path assumption. Fixed by deriving `working_directory` from pytest's `tmp_path` fixture, matching the pattern already used for `binary_path` in the same test. Verified against a fresh Python 3.11 venv with credential-shaped environment variables unset: 2185→2308 passed as the milestone progressed, 0 failures, matching CI's environment as closely as practical without Docker.

PRs #17/#18 merged despite the red `main-tests` run because GitHub branch protection did not require it to pass — a repository-settings gap, addressed in Item 10 / `docs/ci-branch-protection.md`.

## 3. Finding classifications

| ID | Finding | Classification |
|---|---|---|
| CI-1 | macOS-only hardcoded path in Codex config test | CONFIRMED — FIXED |
| Item 1 | Attempt-controller pause decision scanned free-text `failure_reason` (including the bare word "retry") | CONFIRMED — FIXED |
| Item 2 | Codex version check was open-ended (`>= minimum`), accepted any future/prerelease version | CONFIRMED — FIXED |
| Item 3 | `turn.failed` always collapsed to generic `CODEX_PROCESS_EXITED` regardless of its message | CONFIRMED — FIXED |
| Item 4 | Reasoning-token semantics undocumented; value parsed but never persisted or validated | CONFIRMED — FIXED |
| Item 5 | Hysteresis fed by one global `qualified` boolean from only the evidence-provider check | CONFIRMED — FIXED |
| Item 6 | Required-provider health used aggregate rate + total-absence detection only | CONFIRMED — FIXED |
| Item 7 | Operational health queried provider telemetry by `research_cycle_id` only | CONFIRMED — FIXED |
| Item 8 | `timeout_rate` counted every generic `ProviderRequestError`, not just timeouts | CONFIRMED — FIXED |
| Item 9 | Hysteresis thresholds were hard-coded Python defaults, not config-driven | CONFIRMED — FIXED |
| Item 10 | `main-tests` red but merged; no blocking safety-critical type check | CONFIRMED — FIXED (code); branch protection DOCUMENTED-NOT-APPLIED |

Full evidence/correction/test mapping is in `.codex/scratchpads/milestone12-1-provider-health-ci-integrity.md`'s finding tracker table.

## 4. Provider failure propagation design (Item 1)

`ResearchAttemptRecord` gained `failure_code`/`failure_stage`/`failure_retryable`/`failure_metadata` fields, populated at every raise site in `research/orchestration.py` by copying (never re-deriving) the already-validated, already-sanitized fields from the first `ResearchValidationFailure` attributed to that attempt (`_structured_failure_fields` helper). `shadow/attempt_controller.py` gained an explicit `IMMEDIATE_PROVIDER_PAUSE_CODES` allowlist (auth/unexpected-auth-method/version-unsupported/credit-or-quota-exhausted codes for both Claude Code and Codex) and now checks `attempt.failure_code in IMMEDIATE_PROVIDER_PAUSE_CODES` exclusively — the free-text substring scan (including the bare-word-"retry" bug) is gone. New columns persist via schema migration 6.

## 5. Codex version policy (Item 2)

New `research/codex_version_policy.py`: `SUPPORTED_CODEX_CLI_RANGES = ((0,144,5), (0,145,0), "codex-jsonl/v1")` — a closed `[minimum, maximum_exclusive)` range table, not an open-ended floor. `classify_codex_version` rejects any version outside every declared range and any prerelease/build-tagged version unconditionally. `CodexPreflight` gained `adapter_version`, persisted into `UsageRecord.provider_adapter_version` (schema migration 7) and exposed via `codex-provider-preflight` CLI readiness output. No inference subprocess runs after a failed version preflight (verified by a fake binary that `sys.exit(99)` on `exec`).

## 6. Terminal failure classification (Item 3)

New `research/codex_failure_classifier.py::classify_codex_diagnostic` is the single classifier both a nonzero process exit and a zero-exit `turn.failed` event route through. Categories: authentication, quota, rate-limit, network (new, split from generic transient), transient, unsupported-model (new), invalid-configuration (new), schema-rejection, and a shared unknown fallback (`CODEX_PROCESS_EXITED`, deliberately identical for both surfaces). Raw diagnostic text is bounded (4096 chars) and never echoed into the fixed, safe per-category message.

## 7. Token-accounting policy (Item 4)

Documented and implemented Policy A (`REASONING_INCLUDED_IN_OUTPUT`): `gpt-5.1-codex`'s `reasoning_output_tokens` is a subset of `output_tokens` (OpenAI Responses-API convention), never additional. `UsageRecord` gained `reasoning_output_tokens`/`token_accounting_policy` with an enforced invariant `0 <= reasoning_output_tokens <= output_tokens`, violated cases fail closed (`CODEX_REASONING_TOKENS_INVALID`). No ceiling/budget arithmetic changed — `output_tokens` was already the effective total under this policy. Persisted via schema migration 8.

## 8. Dimension-specific health architecture (Item 5)

`shadow/health.py` gained `check_by_name`/`dimension_is_qualified`/`dimension_cycle_status`/`worst_health_status`. The scheduler now calls `health_hysteresis_mod.evaluate_and_persist_hysteresis` three times — once per rate-based dimension (`EVIDENCE_PROVIDER_FAILURE` at the pre-existing `DEFAULT_SCOPE`, `RETRY_EXHAUSTION` and `UNSUPPORTED_CLAIMS` at new independently-tracked scopes) — reusing the pre-existing per-`scope` hysteresis engine (no schema change needed for this item; `scope` was already a real column). Each call's `qualified`/`cycle_status` derive from that dimension's own `HealthCheckResult`, never from another dimension's sample size. The overall hysteresis status fed into `combine_effective_health_decision` is `worst_health_status` of all three. Structural dimensions (reconciliation, duplicate-prevention, budget breach) remain immediate, unaffected.

## 9. Required-provider health policy (Item 6)

New `evidence_providers/health.py::evaluate_required_category_health` computes one independent verdict (`PASS`/`WARNING`/`FAIL`/`INSUFFICIENT_DATA`/`MISSING`/`NOT_APPLICABLE`) per required category, using only that category's own acceptable-provider rows — never the cross-provider aggregate. `ProviderCoveragePolicy` gained per-category `category_minimum_requests`/`category_minimum_success_rate` (defaulting to 1 request / 100% success). `CycleHealthInputs.provider_unhealthy_required_categories` now also forces the `provider_failure_rate` health check to `FAIL`, closing the dilution gap (SEC 9/9 + Alpaca 0/1 now correctly fails).

## 10. Scheduler-run telemetry ownership (Item 7)

New `evidence_providers/persistence.py::list_provider_requests_for_scheduled_run(research_cycle_id, scheduler_run_id)`, filtered to `correlation_mode='SCHEDULED'`. `shadow/scheduler.py::_build_health_inputs_from_cycle_result` uses it for the current run's health decision whenever `scheduler_run_id` is supplied; `list_provider_requests_for_cycle` remains for historical/aggregate reporting. Resumption identity policy (documented in `tests/unit/test_scheduler_run_telemetry_scoping.py` and inline): this repository generates a brand-new `scheduler_run_id` on every `run_scheduled_cycle()` invocation — there is no "resume the same run" path, so a crash-and-restart always produces a new operational attempt even against the same deterministic cycle ID. Migration 9 adds a covering index.

## 11. Transport-metric corrections (Item 8)

`evidence_providers/health.py::compute_provider_health` now computes all rates via `_TRANSPORT_CATEGORY_TO_RATE_FIELD`, an exact lookup over the existing `transport_failure_category` enum (`http_client.py`). `timeout_rate` now counts only `TRANSPORT_TIMEOUT` (previously any generic `ProviderRequestError`). Ten new typed rate fields added to `ProviderHealthSummary` (dns/connection_refused/connection_reset/tls/authentication/rate_limit/http_client/http_server/protocol/configuration/unknown-error). A legacy/`NONE`-category failure contributes to no typed rate (never fabricated into `unknown_transport_error_rate`).

## 12. Hysteresis configuration (Item 9)

New strict `health_hysteresis` section in `shadow/config.py` / `config/shadow_operations.yaml`: one independent threshold set (`warning_after_failures`/`pause_recommended_after_failures`/`pause_required_after_failures`/`recovery_streak`) per dimension (`evidence_provider`, `retry_exhaustion`, `unsupported_claims`). Strict typing rejects quoted numbers, floats, booleans, null, zero, negative values, unknown dimensions, and unknown fields; enforces `warning <= recommended <= required` and `recovery_streak >= 1`. The optional section defaults to the pre-existing hard-coded values (`persistent-health/v1`-equivalent thresholds) if absent, but the shipped `config/shadow_operations.yaml` now declares it explicitly under `policy_version: persistent-health/v2` — a real threshold or version change produces a new `policy_hash`, which the existing `health_hysteresis.py` policy-boundary mechanism already resets streaks on (verified: `test_completed_cycle_persists_hysteresis_state` now observes `persistent-health/v2`).

## 13. Schema and migration changes

Migrations 6–9 added to `storage/schema_version.py` (additive, idempotent, `CURRENT_SCHEMA_VERSION` now 9):

| # | Adds |
|---|---|
| 6 | `research_attempts.failure_code`, `.failure_stage`, `.failure_retryable`, `.failure_metadata_json` |
| 7 | `research_attempts.provider_adapter_version` |
| 8 | `research_attempts.reasoning_output_tokens`, `.token_accounting_policy` |
| 9 | `idx_evidence_provider_requests_scheduled_run` on `evidence_provider_requests(correlation_mode, research_cycle_id, scheduler_run_id, created_at, request_id)` |

Fresh databases get every column via the `CREATE TABLE IF NOT EXISTS` DDL directly; existing databases get them via `ALTER TABLE ADD COLUMN` (nullable / safe defaults). Verified: a hand-built pre-Item-1 database (`_pre_migration_6_db` fixture) upgrades cleanly, existing row count and `failure_reason` text preserved verbatim, all new columns NULL/`NOT_APPLICABLE` (never backfilled by guessing).

## 14. Tests added

~130 new tests across 8 new files plus targeted additions to 6 existing files: `test_codex_version_policy.py`, `test_codex_failure_classifier.py`, `test_health_dimension_independence.py`, `test_scheduler_run_telemetry_scoping.py`, `test_research_attempt_structured_failure_migration.py`, `test_usage_record_reasoning_tokens.py`, plus expansions to `test_codex_provider.py`, `test_codex_configuration.py`, `test_shadow_attempt_controller.py`, `test_provider_health_telemetry.py`, `test_shadow_config.py`, `test_shadow_scheduler.py`. All offline; every Codex/Claude Code interaction uses a fake Python executable, never the real installed binary.

## 15. Final verification

- `pytest tests/ -q` (local, Python 3.14 venv): 2308 passed, 17 skipped
- `pytest tests/ -q` (fresh Python 3.11 venv, `/tmp/ci-repro-venv`, credential-shaped env vars unset — matches GitHub Actions): 2308 passed, 17 skipped
- `paper_runtime`: `pytest tests/ -q`: 59 passed
- `pyright --project pyright-safety.json` (11 safety-critical production modules): **0 errors** (fixed 2 real pre-existing type errors along the way: an unnarrowed `RoleResearchReport | ResearchDecision` union in `orchestration.py`, and an `AlertSink.name` Protocol-variance mismatch in `alerts.py`)
- `pyright` (whole project, non-blocking baseline): 2072 errors — up from the 2027 pre-milestone baseline, entirely from new test files' loose kwargs-dict typing pattern (the same pattern used throughout hundreds of pre-existing tests in this repository); zero new errors in any production module
- `git diff --check`: clean
- 10 repeated runs of the Codex/health/attempt-controller/scheduler-telemetry test modules: 115/115 passed every time, fully deterministic (no sleeps; fake clocks and barriers only)

## 16. Remaining limitations

- **Branch protection is documented, not applied.** `docs/ci-branch-protection.md` specifies the exact GitHub settings required (`main-tests`, `paper-runtime-tests`, `migration-smoke`, `type-check-safety` as required checks; no admin bypass); applying them requires GitHub admin access outside this codebase and was not done or independently verified against the live repository.
- **`MODEL_PROVIDER_FAILURE` is not a separately hysteresis-tracked dimension.** There is currently no distinct, thresholded "model-provider health" check independent of the evidence-provider check in `evaluate_cycle_health` — only the three genuinely thresholded rate dimensions (evidence-provider, retry-exhaustion, unsupported-claims) got independent hysteresis scopes. Adding a fourth dimension would require first introducing a new thresholded health check, which was out of this milestone's narrowest-safe-correction scope.
- **Hysteresis policy hash/version is not yet threaded into readiness reports or run-summary output**, only into the persisted hysteresis evaluation records themselves (which already satisfy "history can explain which dimension caused the pause").
- **Whole-project Pyright baseline (2072 errors) was not reduced** — out of scope per the milestone's explicit instruction not to perform broad type-error cleanup unless requested.

## 17. Operational go/no-go table

| Capability                      | Status                              |
| -------------------------------- | ------------------------------------ |
| Deterministic research          | READY                               |
| Manual Codex research           | SUPERVISED_ONLY (real binary/network never exercised in CI; typed failure/version/reasoning-token paths now covered by tests) |
| Manual Claude Code research     | SUPERVISED_ONLY (unchanged by this milestone) |
| Local simulated paper trading   | READY                               |
| Manual soak campaigns           | READY                               |
| Unattended scheduled research   | KEEP_DISABLED (repository default `shadow_operations.enabled=false`/`schedule.enabled=false` unchanged; dimension-specific health + frozen hysteresis config now in place, but branch protection is not yet applied to the live repository) |
| External Alpaca paper execution | KEEP_DISABLED (unchanged)           |
| Real Alpaca paper smoke         | NOT_READY (unchanged; no real credentials used) |
| Live trading                    | NOT_IMPLEMENTED                     |

No real Codex, Claude Code, Alpaca, Anthropic, SEC, or Reddit network service was invoked at any point. No commit or push occurred.
