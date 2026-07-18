# Milestone 12.1.1 — Remaining Provider and Health Safety Fixes: Closure Report

Starting commit: `eb66a47fed00929d6be623023f9f8a0ae1210b46` on branch
`agent/milestone-12-codex-provider`.

All seven findings in `docs/milestones/milestone12.1.1-remaining-provider-health.md`
were CONFIRMED against the current implementation and FIXED. None were
already fixed or not reproducible.

## Finding classifications and fixes

### 1. Honor provider error retryability — CONFIRMED, FIXED

`research/orchestration.py`'s `_RETRYABLE_ERRORS` except-block always
`continue`d to the next attempt regardless of the caught failure's
`retryable` flag — a `MalformedOutputError` constructed with
`retryable=False` (e.g. `CODEX_USAGE_METADATA_MISSING`,
`CODEX_REASONING_TOKENS_INVALID`) still retried. Separately,
`ShadowResearchAttemptController.before_attempt` never checked the
persistent pause/kill system state — only role-budget limits — so an
automatic pause requested by `after_attempt` on attempt 1 did not block
attempt 2 of the same role loop.

Fix: the except-block now `break`s when the primary failure's `retryable`
is `False`. `before_attempt` checks `pause.current_state` first, before any
budget arithmetic, for every provider (not only managed CLI providers), and
records the gate via a new `role_budget.DECISION_SKIPPED_PAUSED_OR_KILLED`
decision code distinct from budget exhaustion.

### 2. Select structured failures deterministically — CONFIRMED, FIXED

No `select_primary_failure` existed anywhere; `_structured_failure_fields`
took `failures[0]`, making the persisted `failure_code`/`failure_stage`/
`failure_retryable` summary depend on append order.

Fix: `research/failure_taxonomy.py::select_primary_failure` ranks failures
into 7 explicit tiers (structural provider failure → non-retryable provider
contract failure → budget/kill gate → retryable provider failure → schema
validation → claim validation → diagnostic) derived only from the already
-validated `stage`/`retryable` fields, with a deterministic tie-break key
`(tier, stage, code, field_path, claim_id, failure_id)` — stable under
reordering, never influenced by free-text `message`.

### 3. Exclude `NOT_APPLICABLE` from hysteresis recovery — CONFIRMED, FIXED

`shadow/health.py::dimension_is_qualified` was a denylist
(`status != INSUFFICIENT_DATA`), so `NOT_APPLICABLE` (fixture-only cycles)
counted as qualified and could clear a real failure streak.

Fix: changed to an allowlist (`status in (PASS, WARNING, FAIL)`), so
`INSUFFICIENT_DATA`, `NOT_APPLICABLE`, and any future non-conclusive status
all fail closed to unqualified.

### 4. Use scheduler-run identity for health hysteresis — CONFIRMED, FIXED

The scheduler passed `cycle_id=cycle_id or scheduler_run_id` into
`evaluate_and_persist_hysteresis`, where `cycle_id` is the deterministic
`research_cycle_id` — two scheduler runs for the same deterministic
schedule slot collided into one idempotent replay.

Fix: the evaluation-identity `cycle_id` parameter is now always
`scheduler_run_id`. A new, additive, nullable `research_cycle_id` column on
`shadow_health_hysteresis_evaluations` stores the deterministic cycle id
purely for reporting/provenance, never as part of the idempotency key.

### 5. Required-category insufficient data must not pass — CONFIRMED, FIXED

A required category's own `INSUFFICIENT_DATA` verdict was excluded from
`unhealthy_required_categories`, so the overall provider dimension fell
through to the aggregate `provider_failure_rate` check — which could read
100% healthy even while a required category never met its own sample floor.
Sample floors/success thresholds also had no configuration surface, only
Python defaults.

Fix: added `insufficient_required_categories` telemetry and a new
`INSUFFICIENT_DATA` branch in `evaluate_cycle_health`, ranked between the
FAIL/MISSING branch and the ordinary rate check. Added a strict, optional
`provider_health` YAML section (`shadow/config.py`) with per-category
`providers`/`minimum_requests`/`minimum_success_rate`, validated with
`apply_provider_health_policy_overrides` (fails closed on an unknown
category or a mismatched provider list), wired into `cli.py`'s real (non
-fixture) coverage-policy resolution.

### 6. Enforce configured Codex minimum version — CONFIRMED, FIXED

`CodexResearchProvider.preflight()` validated only the closed
`SUPPORTED_CODEX_CLI_RANGES` adapter-contract table and never compared the
installed version against `self._config.minimum_version` — a configured
minimum above the adapter's own floor had no effect.

Fix: after adapter classification, preflight additionally requires
`installed_version >= configured_minimum`, failing with
`CODEX_VERSION_UNSUPPORTED` before any inference subprocess. `CodexPreflight`
now carries `configured_minimum_version` provenance alongside
`binary_version`/`adapter_version`.

### 7. Add independent model-provider health — CONFIRMED, FIXED

No `MODEL_PROVIDER_FAILURE` hysteresis dimension existed; evidence-provider
health said nothing about Codex/Claude Code/Anthropic's own health, and the
one existing structural-failure allowlist
(`attempt_controller.py::IMMEDIATE_PROVIDER_PAUSE_CODES`) had no shared
source of truth.

Fix: new `research/model_provider_health_policy.py` centralizes
STRUCTURAL/TRANSIENT failure-code classification (driven by the already-typed
`retryable` boolean, falling closed to STRUCTURAL for any unknown
non-retryable code); `attempt_controller.py` now imports its allowlist from
there instead of duplicating it. New `shadow/model_provider_health.py`
computes attempt/success/failure/retryable/timeout/rate-limit/auth/quota
/configuration/protocol/missing-usage counts from `research_attempts` rows
scoped to one scheduler run via a new join query,
`list_research_attempts_for_scheduler_run` (joins through
`shadow_role_budget_checks`, the only table already carrying the
`scheduler_run_id ↔ research_run_id` linkage — no schema change to the core
`research_attempts` table). A new `DIMENSION_MODEL_PROVIDER_FAILURE`
hysteresis dimension and `CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE` check are
wired into `evaluate_cycle_health`/`scheduler.py`, folded into the existing
worst-of-every-dimension hysteresis computation, with an optional
`model_provider` `health_hysteresis` config dimension and a new
`safety.pause_on_model_provider_failure_rate` threshold (both default to
safe repository values, preserving behavior for a config predating this
dimension).

## Files changed

```
src/trading_research/research/orchestration.py
src/trading_research/research/failure_taxonomy.py
src/trading_research/research/deterministic_provider.py
src/trading_research/research/codex_provider.py
src/trading_research/research/model_provider_health_policy.py   (new)
src/trading_research/shadow/attempt_controller.py
src/trading_research/shadow/role_budget.py
src/trading_research/shadow/health.py
src/trading_research/shadow/health_hysteresis.py
src/trading_research/shadow/scheduler.py
src/trading_research/shadow/config.py
src/trading_research/shadow/model_provider_health.py            (new)
src/trading_research/evidence_providers/health.py
src/trading_research/storage/shadow_alerts_schema.py
src/trading_research/storage/shadow_alerts_repositories.py
src/trading_research/storage/shadow_operations_repositories.py
src/trading_research/cli.py
pyright-safety.json
```

Plus 8 new/extended test files (`test_select_primary_failure.py`,
`test_model_provider_health.py`,
`test_model_provider_health_hysteresis_integration.py`, and extensions to
`test_attempt_control_hooks.py`, `test_shadow_attempt_controller.py`,
`test_health_dimension_independence.py`, `test_health_hysteresis.py`,
`test_provider_health_telemetry.py`, `test_shadow_config.py`,
`test_codex_provider.py`).

## Migrations

One additive migration: `shadow_health_hysteresis_evaluations.research_cycle_id`
(nullable `TEXT` + supporting index), added via the existing
`_SHADOW_ALERTS_COLUMN_UPGRADES` `ALTER TABLE` mechanism for pre-existing
databases and present directly in the `CREATE TABLE` DDL for fresh ones.
A dedicated test (`test_migration_preserves_pr19_schema_history`) builds a
database under the literal pre-Milestone-12.1.1 (PR #19) schema, inserts a
historical evaluation row, reconnects through `storage/database.py::connect`,
and confirms the historical row is unchanged and new evaluations succeed.

No changes were made to the `research_attempts` schema — model-provider
health evidence is read via a join against the pre-existing
`shadow_role_budget_checks` table instead.

## Retry and pause-barrier changes

- `_RETRYABLE_ERRORS` handling now respects `select_primary_failure(...).retryable`.
- `ShadowResearchAttemptController.before_attempt` checks
  `pause.current_state` before any budget check, for every provider.

## Hysteresis identity and qualification changes

- `dimension_is_qualified` is an allowlist (`PASS`/`WARNING`/`FAIL` only).
- `evaluate_and_persist_hysteresis(cycle_id=...)` is always
  `scheduler_run_id`; `research_cycle_id` is a separate, optional,
  reporting-only parameter/column.

## Required-provider policy changes

- New `insufficient_required_categories` telemetry field and INSUFFICIENT_DATA
  branch in `evaluate_cycle_health`.
- New optional, strict `provider_health` YAML section with per-category
  sample floors/success thresholds, enforced via
  `apply_provider_health_policy_overrides`.

## Codex version changes

- `preflight()` enforces `installed_version >= configured_minimum` in
  addition to the closed adapter-contract range.
- `CodexPreflight.configured_minimum_version` provenance field added.

## Model-provider health changes

- New `research/model_provider_health_policy.py` (centralized classification).
- New `shadow/model_provider_health.py` (evidence computation).
- New `DIMENSION_MODEL_PROVIDER_FAILURE` / `CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE`.
- New optional `health_hysteresis.model_provider` config dimension and
  `safety.pause_on_model_provider_failure_rate` threshold.

## Tests and results

- `pytest tests/ -q`: 2376 passed, 17 skipped (both with and without
  provider/broker credentials in the environment).
- `paper_runtime` suite: 59 passed.
- `pyright --project pyright-safety.json` (expanded to cover every
  production module directly changed by this patch): 0 errors, 0 warnings.
- `python -m compileall -q src paper_runtime`: clean.
- `git diff --check`: clean.
- Focused retry/attempt-controller/health/scheduler/codex-version/config test
  files run 10 times in a row with zero flakes (483 tests × 10).

## Remaining limitations

- `cli.py` carries pre-existing, unrelated `reportArgumentType`-class pyright
  errors (predating this patch) and was intentionally NOT added to
  `pyright-safety.json` — the two-line wiring change made there for Finding 5
  does not introduce new type errors, but bringing the whole file under the
  strict safety gate would require an out-of-scope cleanup.
- `research/model_provider_health_policy.py`'s named structural/transient
  code sets are documentation of the currently-known Codex/Claude Code
  taxonomy; the actual decision boundary is the `retryable` boolean, so a
  future unnamed non-retryable code still fails closed automatically.

## Safety confirmations

- No real Codex, Claude Code, Anthropic, Alpaca, SEC, or Reddit network call
  occurred at any point — every test uses fake providers, fake executables,
  fixtures, and temporary SQLite databases.
- Execution and scheduling defaults remain disabled (`shadow_operations.enabled`,
  `schedule.enabled`, `allow_baseline_paper_submission`,
  `allow_enhanced_submission` are unchanged; external Alpaca paper execution
  was never touched).
- No commit or push occurred as part of this work.

## Operational readiness

| Capability                      | Status                              |
| -------------------------------- | ----------------------------------- |
| Deterministic research          | READY                               |
| Manual Codex research           | SUPERVISED_ONLY                     |
| Manual Claude Code research     | SUPERVISED_ONLY                     |
| Local simulated paper trading   | READY                               |
| Unattended scheduled research   | KEEP_DISABLED                       |
| External Alpaca paper execution | KEEP_DISABLED                       |
| Live trading                    | NOT_IMPLEMENTED                     |
