# Claude Code production research provider

## 1. Architecture

`ClaudeCodeResearchProvider` implements the existing `ResearchModelProvider`
protocol. Scheduled production flow is launchd → Keychain wrapper →
`run-due-shadow-cycle --provider-mode real` → existing scheduler/budget and
attempt controller → provider → existing schema, claim, evidence, numeric,
point-in-time, overlay, persistence, health, and pause pipeline. There is no
fallback provider and no execution path in the provider.

## 2. Security model

The process uses an absolute executable, `shell=False`, a new process group,
bounded stdin/stdout/stderr, a private working directory, a fixed environment,
one turn, and no tools, MCP, browser, slash commands, plugins, project
customizations, permission prompts, or persisted session. Timeouts and output
overflow terminate the process group with SIGTERM followed by bounded SIGKILL.

Dynamic prompts, evidence, and validation feedback are sent only on stdin. The
schema is the sole dynamic command argument and is size-bounded. Raw prompts,
stdout, stderr, the outer result envelope, and credentials are never logged or
persisted.

## 3. Authentication model

Only `CLAUDE_CODE_OAUTH_TOKEN` is copied to the child. The provider never reads
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, interactive credentials, or a
direct API fallback. `--bare` is deliberately not used because subscription
OAuth tokens are not read in bare mode. The launch wrapper gets the token from
macOS Keychain service `agentic-trading-desk-claude-oauth`; it does not belong
in `.env`, YAML, the plist, or command arguments.

Generate and store a token:

```bash
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
/opt/homebrew/bin/claude setup-token
bash deploy/launchd/store_claude_oauth_token.sh
```

The helper reads hidden input. Rotate by replacing the Keychain item. Remove:

```bash
/usr/bin/security delete-generic-password \
  -a "$USER" -s agentic-trading-desk-claude-oauth
```

## 4. Subprocess command

Production inference uses these flags:

```text
/opt/homebrew/bin/claude -p
  --safe-mode
  --tools ""
  --disallowedTools "mcp__*"
  --strict-mcp-config
  --disable-slash-commands
  --permission-mode dontAsk
  --no-session-persistence
  --no-chrome
  --max-turns 1
  --model sonnet
  --system-prompt <short static instruction>
  --output-format json
  --json-schema <canonical bounded schema>
  --max-budget-usd 0.50
```

No shell, `--bare`, resume/continue/session flags, allowed tools, additional
directories, MCP config, plugins, Chrome, or skipped permissions are allowed.

## 5. Environment allowlist

The provider reconstructs the environment immediately before every subprocess
after any project `.env` loading. Allowed keys are `HOME`, `USER`, `LOGNAME`,
`TMPDIR`, `LANG`, `LC_ALL`, fixed `PATH`, and `CLAUDE_CODE_OAUTH_TOKEN`.
Anthropic API configuration, AWS/Google/Azure credentials, MCP/plugin values,
Python virtualenv values, evidence-provider credentials, broker credentials,
paper-runtime settings, and database URLs are excluded.

## 6. Preflight behavior

`claude --version` must parse as semantic version `2.1.205` or newer.
`claude auth status` must return one JSON object, exit zero, report authenticated,
and report subscription/OAuth authentication when a method is exposed. API-key
authentication is rejected. A successful result is cached in memory for at most
five minutes. `claude-code-provider-preflight` prints sanitized fields and makes
no inference request. The real shadow CLI runs preflight before opening its
scheduler lease or reserving budget.

## 7. Output parsing

Stdout must be exactly one UTF-8 JSON object with no trailing content. Error
results are rejected. `structured_output` must be an object; `usage` must have
non-negative input/output/cache counts; `modelUsage` must name exactly one
resolved model; and `num_turns` must equal one. The extracted object is locally
revalidated against the unmodified original Draft-07 schema, including the
forbidden executable-field scan. Only canonical JSON for `structured_output`
becomes `raw_text`.

## 8. Failure mapping

Subprocess details map to stable `CLAUDE_CODE_*` taxonomy codes. Missing or old
binaries and bad authentication are non-retryable unavailable errors; timeout,
rate limit, and allowlisted transient service patterns use their existing typed
errors; malformed envelopes and overflows use malformed-output errors; local
schema failures remain `SchemaValidationError`. Only exit code, safe category,
latency, byte counts, version, and usage-presence metadata may be persisted.
Raw stderr is never persisted.

## 9. Usage and budget behavior

Usage rows persist reported input, output, cache-read, and cache-creation tokens,
latency, opaque session ID, configured alias, resolved model ID, and CLI version.
Missing or ambiguous usage fails closed. Estimated cost is explicitly labeled
`SUBSCRIPTION_API_EQUIVALENT_ESTIMATE`; it is not a subscription charge.
Effective-dated `claude_code`/`sonnet` pricing is used for conservative
reservation while the resolved model is always recorded.

Claude Code caps cover calls per cycle/day/month, tokens per cycle, latency per
role/cycle, and API-equivalent cost per call/cycle/day/month. Failed persisted
attempts count toward day/month call limits, and allowed attempt checks count
toward the current cycle. The CLI `--max-budget-usd` remains an additional
per-process guard, not the primary budget mechanism.

## 10. Launchd integration

The wrapper uses `set -euo pipefail`, `umask 077`, absolute paths, the repository
venv interpreter directly, an explicit symbol array, Keychain retrieval, API-key
unsets, and production config paths. It invokes only the existing scheduler
entry point. The plist contains no token and no `KeepAlive`.

## 11. Production configuration

Safe base configs remain disabled. Dormant profiles under `config/production/`
select `claude_code`, enable scheduled research and shadow scheduling, and keep
paper submission and promotion false. They activate only when explicitly passed
with `--research-config`, `--scheduled-research-config`, and `--shadow-config`.
Review the private runtime path and all caps before use.

## 12. Tests

Offline fake executables cover command hardening, stdin prompt delivery,
environment sanitation, version/auth failures, API-key rejection, structured
output extraction, local schema rejection, missing/ambiguous usage, stdout
overflow, timeout cleanup, usage provenance, and configuration permissions.
Default tests never invoke the installed Claude binary.

Final verification on 2026-07-18 produced:

- full suite: 2,051 passed, 16 skipped, with the same single pre-existing
  ambiguous external-paper recovery failure recorded at baseline;
- credential-stripped full suite: the identical 2,051 passed, 16 skipped, and
  one pre-existing failure;
- paper runtime: 59 passed;
- focused Claude Code/deployment/pipeline/attempt-control tests: 31 passed;
- 20 consecutive provider lifecycle runs: all passed;
- Pyright: 1,910 repository errors (1,932 at baseline) and 39 unchanged paper
  runtime errors; type checking is therefore not claimed as clean;
- `git diff --check`, shell syntax checks, and launchd plist validation passed.

The one full-suite failure is outside this change and was reproduced before the
implementation: `test_ambiguous_submission_recovers_via_lookup_not_blind_retry`
expected `SUBMITTED` but observed `SUBMISSION_UNKNOWN`.

## 13. Rollout procedure

1. Run the full offline, clean-environment, paper-runtime, type, and diff checks.
2. Run `claude-code-provider-preflight --research-config config/production/research.yaml`;
   this spends no inference budget.
3. Manually run one AAPL role and inspect validation, usage, model provenance,
   latency, and estimate.
4. Run one complete manual AAPL shadow cycle and verify no paper order exists.
5. Activate the one-symbol launchd job and observe several intended schedules.
6. Expand symbols only after stable auth, usage, latency, retry, and monthly-call
   evidence. Paper submission stays disabled pending separate review.

Direct API access remains more predictable for strict uptime or high throughput.

## 14. Remaining limitations

- Subscription capacity and Agent SDK credits are external operational
  dependencies and are not authoritative billing APIs.
- Alias pricing is an API-equivalent estimate; operators must maintain dated
  pricing and review resolved-model changes.
- A successful offline suite does not validate a local Keychain item, installed
  CLI build, subscription entitlement, or real service availability.
- Scheduled multi-symbol rollout requires successful one-symbol operating
  history and readiness evidence.

## 15. Operational go/no-go

| Check | Go condition | Failure action |
|---|---|---|
| Binary | Executable and version ≥ 2.1.205 | Block provider |
| Auth | OAuth token present and auth status is subscription OAuth | Block and provider-health pause |
| Output | One envelope, one model, usage present, local schema valid | Fail role/cycle closed |
| Budget | Every call/token/latency/cost cap has room | Block before inference |
| Scheduling | Explicit production profiles and symbols supplied | No-op/block |
| Paper/live execution | All submission flags false; live unavailable | Do not roll out if changed |

The code and offline safety checks are implementation-ready. Operational Claude
Code stages remain no-go until an operator supplies a Keychain token, runs the
real non-inference preflight, and completes the staged one-symbol checks. No real
Claude inference or broker request was made during implementation.

This is research software, not financial advice.
