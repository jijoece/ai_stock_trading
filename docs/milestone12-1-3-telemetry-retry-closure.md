# Milestone 12.1.3 Telemetry and Retry Closure

## Starting commit

`a2cd8600a0547f85f579ccbbc72efa17e8728807`

## Root causes

1. Scheduled health used cycle-wide telemetry owned only by reusable
   `research_run_id` values, so separate scheduler invocations could mix
   attempts, failures, usage, and rates.
2. The role loop appended `RETRY_EXHAUSTED` after every unsuccessful exit,
   including non-retryable failures and control gates that made no provider
   call.

## Files changed

- `src/trading_research/research/orchestration.py`
- `src/trading_research/storage/research_repositories.py`
- `src/trading_research/shadow/scheduler.py`
- `tests/unit/test_attempt_control_hooks.py`
- `tests/unit/test_scheduler_run_telemetry_scoping.py`
- `docs/INDEX.md`
- `docs/milestone12-1-3-telemetry-retry-closure.md`

## Tests

- Focused: 118 passed.
- Critical regressions: 76 passed in each of five consecutive runs.
- Credential-free full suite: 2,418 passed, 17 skipped.
- Paper runtime: 59 passed.
- Compileall: passed.
- Safety Pyright: 0 errors, 0 warnings, 0 information messages.

All validation was offline and credential-free. Scheduling and external paper
or live execution remain disabled.

## Remaining limitations

Historical failure rows that do not reference an owned attempt cannot be
safely attributed to a scheduler run and are intentionally excluded from
scheduler-scoped telemetry. Manual and historical cycle-wide reporting remains
unchanged.
