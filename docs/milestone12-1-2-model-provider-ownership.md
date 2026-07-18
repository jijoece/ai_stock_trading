# Milestone 12.1.2 — Model-Provider Ownership and Fail-Closed Retry Closure

Starting point: commit `23a438956ead7c00ee19369a0e39667e5ae6e4fd` on branch
`agent/milestone-12-codex-provider`.

## Findings

| ID | Classification | Resolution |
|---|---|---|
| 1. Exact scheduler ownership | FIXED | Scheduled attempts persist immutable scheduler-run, research-cycle, attempt-control-check, and correlation identities. Operational lookup uses only direct `SCHEDULED` ownership. |
| 2. Provider/model health partitioning | FIXED | Health filters to the expected provider and configured model, and hysteresis scopes also include the provider-configuration hash. Fixture providers use a separate non-production scope and are not applicable production evidence. |
| 3. Immediate provider pause | FIXED | The attempt controller uses the centralized structural/transient classifier for Codex, Claude Code, and Anthropic. |
| 4. Code-less structural failures | FIXED | Structural status is independent of code presence; bounded evidence uses `UNCLASSIFIED_STRUCTURAL_FAILURE`. |
| 5. Retry authorization | FIXED | Attempt retryability is true only when every failure is explicitly retryable; primary failure selection remains deterministic and ranks unknown non-retryable failures conservatively. |

## Schema and behavior changes

Migration 10 additively adds `scheduler_run_id`, `research_cycle_id`,
`attempt_control_check_id`, and `correlation_mode` to `research_attempts`, plus
an index on correlation mode, scheduler run, provider, model, creation time,
and attempt ID. Existing rows remain unowned with `LEGACY_UNKNOWN`; no
historical scheduler ownership is inferred. Scheduled attempt IDs include a
deterministic scheduler-run digest so revisiting a deterministic research run
does not collide with an earlier scheduler invocation.

`list_research_attempts_for_scheduler_run` now queries direct scheduled
ownership and deterministically orders by creation time and attempt ID.
Budget-gated attempts remain auditable but are excluded from provider-call
health. The configured `research_attempts.model_name` is the health partition
identifier; resolved-model fields remain provenance only.

Model-provider hysteresis uses
`MODEL_PROVIDER_FAILURE:<provider>:<model>:<configuration-hash>` after safe
normalization. Configuration changes, model changes, and provider changes form
separate policy boundaries. Persisted hysteresis evidence contains bounded
counts and taxonomy codes only—never messages, prompts, raw output, or secrets.

## Validation

- Focused safety suite: `133 passed`, repeated 10 consecutive times (plus one confirmation run).
- Full credential-free suite: `2400 passed, 17 skipped`.
- Isolated paper runtime: `59 passed`.
- `python -m compileall -q src paper_runtime`: passed.
- `pyright --project pyright-safety.json`: `0 errors`.
- `git diff --check`: passed.

All tests were offline. No real model provider, evidence provider, or broker was
called. Research remains local/deterministic by default; scheduled research,
external paper execution, and live trading remain disabled or unavailable.

## Remaining limitations

Legacy attempts intentionally have no scheduler ownership and cannot
participate in run-specific operational model health. Manual attempts remain
outside scheduled health by design. No migration guesses historical ownership.

| Capability | Status |
|---|---|
| Deterministic research | READY |
| Manual Codex research | SUPERVISED_ONLY |
| Manual Claude Code research | SUPERVISED_ONLY |
| Manual Anthropic research | SUPERVISED_ONLY |
| Unattended scheduled research | KEEP_DISABLED |
| External Alpaca paper execution | KEEP_DISABLED |
| Live trading | NOT_IMPLEMENTED |
