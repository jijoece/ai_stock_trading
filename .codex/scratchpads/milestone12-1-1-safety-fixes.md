# Milestone 12.1.1 Safety Fixes

## Baseline
- Commit: eb66a47fed00929d6be623023f9f8a0ae1210b46
- Branch: agent/milestone-12-codex-provider
- Main tests: 2376 passed, 17 skipped (also clean with credentials stripped)
- Paper-runtime tests: 59 passed
- Safety Pyright: 0 errors, 0 warnings (pyright-safety.json expanded for all directly-changed production modules)

## Findings
| ID | Classification | Root cause | Fix | Tests |
|---|---|---|---|---|
| 1 | FIXED | `_RETRYABLE_ERRORS` except-block in orchestration.py always `continue`d regardless of `retryable`; `before_attempt` never checked pause/kill state | orchestration.py: break when rt_retryable is False; attempt_controller.py: pause/kill check at top of before_attempt (new decision code SKIPPED_PAUSED_OR_KILLED in role_budget.py) | test_attempt_control_hooks.py (4 new), test_shadow_attempt_controller.py (3 new) |
| 2 | FIXED | No `select_primary_failure` existed; `_structured_failure_fields` used `failures[0]` | Added `select_primary_failure` + `_failure_tier` in failure_taxonomy.py (7-tier priority policy, deterministic tie-break by stage/code/field_path/claim_id/failure_id); orchestration.py `_structured_failure_fields` now calls it | test_select_primary_failure.py (7 new) |
| 3 | FIXED | `dimension_is_qualified` used `!= INSUFFICIENT_DATA` denylist, treating NOT_APPLICABLE as qualified | Changed to allowlist `status in (PASS, WARNING, FAIL)` | test_health_dimension_independence.py (6 new) |
| 4 | FIXED | scheduler.py hysteresis calls used `cycle_id=cycle_id or scheduler_run_id` (research_cycle_id as identity) | cycle_id param now always scheduler_run_id; new additive nullable `research_cycle_id` column/param for provenance only; migration in shadow_alerts_schema.py `_SHADOW_ALERTS_COLUMN_UPGRADES` | test_health_hysteresis.py (6 new incl. PR#19 schema migration test) |
| 5 | FIXED | `_UNHEALTHY_REQUIRED_CATEGORY_STATUSES` excluded INSUFFICIENT_DATA; sample floors only Python defaults | Added `insufficient_required_categories` telemetry field + INSUFFICIENT_DATA branch in evaluate_cycle_health (ranked below FAIL/MISSING, above rate check); added strict `provider_health` YAML section (shadow/config.py) + `apply_provider_health_policy_overrides` fail-closed overlay; wired into cli.py | test_provider_health_telemetry.py (7 new), test_shadow_config.py (11 new) |
| 6 | FIXED | `preflight()` validated only SUPPORTED_CODEX_CLI_RANGES, never compared installed version to `self._config.minimum_version` | Added installed<configured_minimum check after adapter classification in codex_provider.py preflight(); added `configured_minimum_version` provenance field to CodexPreflight; extended ALLOWED_METADATA_KEYS with configured_minimum_version/adapter_version | test_codex_provider.py (5 new) |
| 7 | FIXED | No independent model-provider health dimension existed; attempt_controller.py duplicated an ad hoc pause-code allowlist | New research/model_provider_health_policy.py (centralized STRUCTURAL/TRANSIENT classification, attempt_controller.py now imports from it); new shadow/model_provider_health.py (evidence from research_attempts via new join query); new DIMENSION_MODEL_PROVIDER_FAILURE hysteresis dimension + CHECK_NAME_MODEL_PROVIDER_FAILURE_RATE wired into health.py/scheduler.py; optional model_provider health_hysteresis config dimension + safety.pause_on_model_provider_failure_rate | test_model_provider_health.py (10 new), test_model_provider_health_hysteresis_integration.py (6 new), test_shadow_config.py (+2) |

## Files changed
src/trading_research/research/orchestration.py, failure_taxonomy.py, deterministic_provider.py,
codex_provider.py, model_provider_health_policy.py (new)
src/trading_research/shadow/attempt_controller.py, role_budget.py, health.py, health_hysteresis.py,
scheduler.py, config.py, model_provider_health.py (new)
src/trading_research/evidence_providers/health.py
src/trading_research/storage/shadow_alerts_schema.py, shadow_alerts_repositories.py,
shadow_operations_repositories.py
src/trading_research/cli.py
pyright-safety.json
+ 8 new/extended test files

## Migrations
- shadow_health_hysteresis_evaluations.research_cycle_id (additive nullable TEXT + index),
  via _SHADOW_ALERTS_COLUMN_UPGRADES (ALTER TABLE for pre-existing DBs, present in fresh DDL)

## Final results
All 7 findings CONFIRMED and FIXED. Full suite + credential-stripped suite + paper_runtime suite
all pass. Safety pyright clean. 10x repeated focused retry/health/config runs stable (0 flakes).

## Remaining blockers
None identified for the scoped findings. cli.py has pre-existing unrelated pyright errors
(reportArgumentType etc.) not touched by this patch and not added to pyright-safety.json.
