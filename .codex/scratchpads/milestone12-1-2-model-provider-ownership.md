# Milestone 12.1.2

## Baseline
- Commit: `23a438956ead7c00ee19369a0e39667e5ae6e4fd`
- Branch: `agent/milestone-12-codex-provider`
- Main tests: Not run at baseline; one final full run reserved by the milestone instructions. Focused baseline: 68 passed.
- Paper-runtime tests: Not run at baseline; final result below.
- Safety Pyright: Not run at baseline; final result below.

## Findings
| ID | Classification | Root cause | Fix | Tests |
|---|---|---|---|---|
| 1 | FIXED | Scheduler ownership was inferred by joining reusable research identifiers to budget checks. | Persist direct immutable ownership and query it exactly. | Same-run collision, manual/legacy exclusion, retries, gates, migration, ordering. |
| 2 | FIXED | Health evidence and hysteresis shared one global model-provider dimension. | Filter exact provider/model and scope by provider/model/config hash; isolate fixtures. | Provider/model/config partitions, provider switch, empty sample, replay. |
| 3 | FIXED | Immediate pause used a named-code allowlist for two CLI providers. | Use the centralized classifier for all real model providers. | Unknown CLI failures, Anthropic client error, transient failures, next role/symbol blocks. |
| 4 | FIXED | Aggregation required a truthy code before setting structural status. | Count classification independently and use a bounded placeholder. | False/null retryability, unknown codes, bounded persisted evidence. |
| 5 | FIXED | Retry flow copied only the primary failure's retryability. | Require every failure to be explicitly retryable; conservatively rank unknown non-retryable failures. | Mixed/order-independent failures, null retryability, actual one-call control flow. |

## Schema changes

Migration 10 adds nullable `scheduler_run_id`, `research_cycle_id`, and
`attempt_control_check_id`, plus `correlation_mode NOT NULL DEFAULT
'LEGACY_UNKNOWN'`, and a scheduled provider/model lookup index. Existing rows
are preserved without inferred ownership.

## Final results

- Focused suite: 133 passed, 10 consecutive repetitions plus one confirmation.
- Main credential-free suite: 2400 passed, 17 skipped.
- Paper runtime: 59 passed.
- Compileall: passed.
- Safety Pyright: 0 errors.
- Diff check: passed.

## Remaining blockers

None. Legacy attempts remain intentionally excluded from run-specific health.
