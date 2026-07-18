# Claude Code Production Provider

## Metadata

- Starting commit: `06e16b4be68930fcc12d0b8cc412fe596a545431`
- Branch: started on `agent/milestone-11-3-1-safety-closure`; publishing from
  `agent/claude-code-production-provider`
- Started: `2026-07-18T04:06:20Z`
- Last updated: `2026-07-18T05:54:43Z`
- Working-tree status: implementation and supplied milestone document staged on
  the dedicated publishing branch; unrelated milestone 11.3.1 work remains in
  the parent commit.

## Baseline

- Main tests: 2,028 passed, 16 skipped, 1 pre-existing failure in ambiguous external-paper recovery.
- Paper-runtime tests: 59 passed.
- Type checking: repository 1,932 existing errors; paper runtime 39 existing errors.
- Existing scheduled-research configuration: disabled; paper submission false; promotion false.
- Existing shadow configuration: disabled schedule and operations; paper submission false; generic token/latency/cost caps.
- Existing provider construction: explicit deterministic/Anthropic branches in CLI; no shared fallback.
- Existing budget behavior: cycle reservation plus per-attempt reconciliation; Anthropic alone previously required pricing.
- Existing launchd behavior: inert plist; stale wrapper activated a venv and omitted real provider mode/token retrieval.

## Implementation tracker

- [x] Provider protocol integration
- [x] Provider configuration
- [x] Locked-down subprocess invocation
- [x] Minimal environment
- [x] OAuth authentication preflight
- [x] Claude Code version preflight
- [x] Bounded stdout and stderr
- [x] Process-group timeout termination
- [x] Structured-output parsing
- [x] Local schema revalidation
- [x] Failure-taxonomy integration
- [x] Usage and provenance parsing
- [x] Estimated-cost classification
- [x] Per-call/cycle/day/month limits
- [x] Health and automatic-pause integration
- [x] CLI provider construction
- [x] Readiness reporting
- [x] Launchd wrapper
- [x] Keychain documentation/tooling
- [x] Production configuration profile
- [x] Offline fake-process tests
- [x] Scheduler integration tests
- [x] Documentation
- [x] Final verification

## Finding and decision tracker

| ID | Area | Existing behavior | Decision | Implementation | Tests |
|---|---|---|---|---|---|
| F01 | Provider | Deterministic and direct Anthropic only | Add an explicit no-fallback Claude Code provider | Protocol implementation, CLI construction, production profiles | Unit and integration fake-process tests |
| F02 | Credentials | Direct API key path existed | Permit only `CLAUDE_CODE_OAUTH_TOKEN` in a rebuilt child environment | OAuth preflight, API-key rejection, Keychain wrapper | Missing-token, API-key-auth, and environment-leak tests |
| F03 | Subprocess | No Claude Code lifecycle runner | Bound all I/O and terminate the owned process group | Concurrent pumps, monotonic timeout, TERM/KILL/reap | Overflow, timeout, and 20-run lifecycle checks |
| F04 | Output | Providers returned project-shaped JSON | Strictly unwrap once, then re-run unchanged local validation | Outer-envelope parser and canonical structured output | Trailing JSON, missing/ambiguous usage, schema tests |
| F05 | Cost | Direct API estimates | Label subscription costs as API-equivalent estimates | Usage provenance columns and dated alias pricing | Persistence and CLI aggregation tests |
| F06 | Scheduling | Generic cycle/day controls | Add Claude call, token, latency, and estimate caps with automatic pause | Scheduler and attempt-controller integration | Budget, call-cap, and missing-usage pause tests |
| F07 | Activation | Base configuration disabled | Keep base safe and require explicit dormant production profiles | Three `config/production` profiles; submission/promotion false | Deployment/configuration tests |

## Files changed

| File | Purpose |
|---|---|
| `.codex/scratchpads/claude-code-production-provider.md` | Required implementation record |
| `src/trading_research/research/claude_code_provider.py` | Locked-down subprocess provider and preflight |
| `config/production/*` | Explicit dormant production profile |
| `docs/claude-code-production-provider.md` | Architecture, operations, rollout, and limitations |
| `tests/unit/test_claude_code_provider.py` | Offline fake-process security/lifecycle suite |
| `tests/integration/test_claude_code_research_pipeline.py` | Existing validation/persistence pipeline integration |
| `src/trading_research/{cli.py,research/*,shadow/*,storage/*}` | Provider wiring, taxonomy, provenance, persistence, budgets, health, and readiness |
| `config/{research.yaml,research_pricing.yaml,shadow_operations.yaml}` | Safe defaults and API-equivalent pricing/caps |
| `deploy/launchd/*` | Hardened Keychain-backed scheduler wrapper and operator instructions |
| `README.md`, `.env.example`, `docs/INDEX.md`, `docs/runbooks/shadow-operations.md` | Discovery and operational documentation |
| `tests/unit/test_claude_code_deployment.py` | Production profiles, wrapper, and migration checks |
| `tests/unit/test_shadow_attempt_controller.py` | Claude call-cap and automatic-pause coverage |

## Commands run

- Recorded starting commit, branch, worktree status, and recent history.
- Read the complete milestone and repository instructions.
- Inspected the pre-existing `shadow/config.py` diff before overlap.
- Ran baseline main, paper-runtime, and Pyright checks.
- Ran 258 focused shadow/config/budget/scheduler tests: 258 passed, 1 skipped.
- Ran the final provider/deployment/pipeline/attempt-controller group: 31 passed.
- Ran the full suite and credential-stripped full suite: each produced 2,051
  passed, 16 skipped, and the same one pre-existing external-paper recovery
  failure recorded at baseline.
- Ran paper-runtime tests: 59 passed.
- Ran provider lifecycle tests 20 consecutive times: all passed.
- Ran final focused provider/attempt-controller tests: 26 passed.
- Ran final Pyright: repository 1,910 errors (baseline 1,932); paper runtime 39
  errors (unchanged). Type checking is not clean and is not reported as passing.
- Ran `git diff --check`, shell syntax checks, and launchd plist validation: passed.

## Open issues

- One unrelated baseline failure remains in ambiguous external-paper recovery.
- Real binary/auth preflight and all rollout stages deliberately remain for the
  operator; no real inference was authorized.

## Resume instructions

- Last completed task: final offline verification and implementation report update.
- Exact next task: operator review, then non-inference preflight with the real
  Keychain token if the operator chooses to begin rollout.
- Tests already run: baseline, full, credential-stripped full, paper runtime,
  focused suites, and repeated lifecycle checks recorded above.
- Remaining blockers: operational token/CLI/subscription validation and the
  pre-existing unrelated full-suite failure.

## Final status

Implementation complete. Offline safety and integration coverage are ready;
operational Claude Code rollout is intentionally not activated. Local and
external paper submission remain disabled, and live trading remains unavailable.
