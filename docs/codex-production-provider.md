# Codex production research provider

`provider: codex` is a locked-down research provider that invokes the
locally installed **Codex CLI** using the operating-system user's cached
**ChatGPT** login. It never uses `OPENAI_API_KEY`, an undocumented endpoint,
browser scraping, or direct ChatGPT web automation. It follows the same
architecture and safety posture as `provider: claude_code`
(`docs/claude-code-production-provider.md`) and shares its provider-neutral
hardened subprocess runner (`src/trading_research/research/bounded_subprocess.py`).

Research and paper trading only. Live trading remains unavailable. This
provider cannot import or call broker/execution modules, cannot construct an
order, and no provider failure can activate execution — see
`docs/adr/0003-claude-research-boundary.md`.

## Architecture

```
config/research.yaml (research.provider: codex, research.model, codex: {...})
  -> ResearchConfiguration.build_codex_provider_config()
    -> CodexProviderConfig  (validated: absolute paths, private 0700 working
                              directory outside the repo, positive limits,
                              explicit model, authentication flags forced true)
      -> CodexResearchProvider.generate_structured(ResearchModelRequest)
        -> preflight()                    # version + `codex login status`, cached 5 min
        -> write private 0600 schema file under working_directory/.codex-schemas
        -> bounded_subprocess.BoundedProcessRunner.run(argv, env=sanitized, stdin=prompt)
        -> codex_jsonl_adapter.parse_codex_jsonl(stdout)
        -> decode + validate the final agent_message against the original schema
        -> build_usage_record(...)  -> ResearchModelResponse
```

Files:

* `src/trading_research/research/codex_provider.py` — `CodexProviderConfig`,
  `CodexPreflight`, `CodexResearchProvider`.
* `src/trading_research/research/codex_jsonl_adapter.py` — version-specific
  JSONL event parsing, isolated so a future CLI version bump only requires
  updating this module.
* `src/trading_research/research/bounded_subprocess.py` — the
  provider-neutral hardened subprocess runner extracted from
  `claude_code_provider.py` (absolute-executable, `shell=False`, new process
  group, bounded stdin/stdout/stderr, timeout, SIGTERM→SIGKILL). Both Codex
  and Claude Code build on this; nothing Claude-specific lives here.

## Why no API key is used

Codex CLI's `--ignore-user-config` flag (required — see "Command
restrictions" below) means `config.toml`'s `forced_login_method = "chatgpt"`
is never read. Authentication is instead enforced three ways:

1. **Sanitized environment construction** — the child process only ever
   receives an explicit allowlist (`HOME`, `USER`, `LOGNAME`, `TMPDIR`,
   `LANG`, `LC_ALL`, a fixed minimal `PATH`, and optionally `CODEX_HOME` when
   configured). `OPENAI_API_KEY` and every other credential variable is
   never copied — because the allowlist is additive (nothing is copied
   except these names), there is no explicit-removal step to bypass.
2. **Explicit removal of API credential variables** — the allowlist itself
   proves absence; `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`,
   `OPENAI_ACCESS_TOKEN`, `OPENAI_BASE_URL`, `OPENAI_API_BASE`,
   `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`, and every unrelated
   Anthropic/broker/MCP/database credential can never reach the child.
3. **Strict `codex login status` parsing** — a zero exit code alone is never
   sufficient. The output is decoded and checked for an explicit `chatgpt`
   marker; any mention of an API key or access token in the output is
   rejected even on a zero exit code (`CODEX_UNEXPECTED_AUTH_METHOD`).

## CLI installation and login

Install the standalone CLI (not the binary embedded in a versioned VS Code
extension):

```bash
npm install -g @openai/codex
codex login          # interactive; caches ChatGPT session under $CODEX_HOME (default ~/.codex)
codex login status    # should report "Logged in using ChatGPT"
codex --version
```

Never install or upgrade Codex automatically from application runtime code.

### Keychain credential storage recommendation

Codex's cached ChatGPT session lives under `$CODEX_HOME` (default
`~/.codex`), not in a portable OAuth token like Claude Code's
`CLAUDE_CODE_OAUTH_TOKEN`. There is nothing to store in Keychain for this
provider — the scheduler must run as the same trusted macOS user who
completed `codex login`, and `HOME` (or an explicitly configured
`CODEX_HOME`) must resolve to that same login's cache. Never copy
`~/.codex` credential files into the repository, runtime directory, CI
artifacts, logs, or launchd configuration.

## Command restrictions

The effective invocation is:

```bash
codex exec \
  --ephemeral \
  --ignore-user-config \
  --ignore-rules \
  --sandbox read-only \
  --skip-git-repo-check \
  --color never \
  --output-schema <private-temp-schema-path> \
  --json \
  -c 'approval_policy="never"' \
  -c 'web_search="disabled"' \
  -c 'features.shell_tool=false' \
  -c 'features.apps=false' \
  -c 'features.remote_plugin=false' \
  -c 'features.network_proxy.enabled=false' \
  --model <configured-model> \
  -
```

Dynamic content (system instructions, evidence, user prompt, retry feedback)
is sent on stdin only — never in argv, filenames, environment variables,
logs, or temp files. Never used: `--yolo`,
`--dangerously-bypass-approvals-and-sandbox`, `workspace-write` or
`danger-full-access` sandboxing, session resume, MCP servers, plugins,
skills, browser/web search, the repository working directory, project
instructions (`AGENTS.md`), or command-execution tools. `--add-dir` is never
passed — Codex cannot inspect the application repository during inference.

## Dedicated working directory

`codex.working_directory` (default `/private/tmp/agentic-trading-desk-codex`)
must be absolute, not the repository root or inside it, not a symlink,
created with mode `0700`, and rejected if group/other-accessible. It
contains no `AGENTS.md`, `.codex`, Git metadata, source code, or trading
data — Codex is invoked with this as its process `cwd` and never told to
look at the repository (`--skip-git-repo-check`, no `--add-dir`).

## Environment allowlist

Only `HOME`, `USER`, `LOGNAME`, `TMPDIR`, `LANG`, `LC_ALL`, and a fixed
minimal `PATH` are copied from the parent process; `CODEX_HOME` is set only
when explicitly configured. `HOME` must resolve to the scheduler's trusted
macOS user's cached `codex login` session for authentication to succeed.

## Preflight

`CodexResearchProvider.preflight()` runs `codex --version` then
`codex login status`, both through the same hardened subprocess runner and
sanitized environment used for inference. A successful preflight
establishes: the executable exists and is a regular executable file, the
version is parseable and meets `codex.minimum_version`, `login status`
exits successfully, and the reported authentication mode is explicitly
ChatGPT (API-key/access-token mentions are rejected even on exit code 0).
Successful results are cached in memory for up to 5 minutes; failed results
are never cached as ready. Preflight runs before every scheduled cycle's
budget reservation and before every ad hoc `run-research --provider codex`
call — a preflight failure blocks the cycle before any inference call.

```bash
python -m trading_research.cli codex-provider-preflight \
  --research-config config/production/research-codex.yaml
```

prints only: `ready`, CLI version, `authenticated`,
`authentication_method`, `checked_at`, and a sanitized `failure_code` — never
raw CLI output. It makes no inference call.

## JSONL parsing

`codex exec --json` emits newline-delimited JSON events, parsed by
`codex_jsonl_adapter.py` (captured against codex-cli 0.144.5, 2026-07-18;
sanitized fixtures in `tests/fixtures/codex_jsonl_fixtures.py`):

```
{"type": "thread.started", "thread_id": "<uuid>"}
{"type": "turn.started"}
{"type": "item.completed", "item": {"id": ..., "type": "agent_message", "text": "<json>"}}
{"type": "item.completed", "item": {"id": ..., "type": "error", "message": "..."}}
{"type": "error", "message": "..."}
{"type": "turn.completed", "usage": {"input_tokens": N, "cached_input_tokens": N,
                                      "output_tokens": N, "reasoning_output_tokens": N}}
{"type": "turn.failed", "error": {"message": "..."}}
```

`turn.completed`/`turn.failed` are the only terminal events; the parser
rejects a second terminal event, any event after the terminal event, blank
or malformed lines, invalid UTF-8, an unrecognized event type, and an
oversize line or event count. Never logs a raw event payload and never
scans arbitrary nested JSON for a plausible answer or token count.

## Schema handling

### Canonical schema versus transport schema

`output_validation.py`'s canonical role/decision JSON Schemas are valid
Draft-07, but Codex's structured-output validator accepts a stricter subset
than Draft-07 — in particular it rejects numeric/string/array bound
keywords (`minLength`, `maxLength`, `minItems`, `maxItems`, `minimum`,
`maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `pattern`)
and any object node whose `required` list does not name every key in
`properties`. `codex_schema_transport.py::build_codex_transport_schema`
rewrites the canonical schema into a Codex-compatible **transport schema**
that Codex is asked to emit against. This mirrors the pattern
`anthropic_provider.py::_strict_compatible_schema` already established for
Claude's strict tool-use schema compiler.

The transport schema is only ever used to *shape* what Codex is asked to
produce — it is never a second source of truth. The canonical schema is
unchanged, is still the only schema `generate_structured` locally validates
before any subprocess runs, and is still the only schema the parsed
response is checked against after Codex returns.

### Supported normalization behavior

- Deep-copies the input; never mutates the canonical schema.
- Strips the unsupported bound keywords listed above (canonical
  post-response validation still enforces every one of them).
- Drops pure-metadata keywords (`$schema`, `title`, `description`,
  `default`, `examples`) that do not affect validation.
- Sets `required` to every key in `properties` on each object node (so an
  optional-but-nullable canonical field, e.g. `numeric_value`, becomes
  transport-required-but-nullable) and forces `additionalProperties: false`
  wherever `properties` is declared.
- Inlines local `$defs`/`definitions` references (`#/$defs/...`,
  `#/definitions/...`) that are acyclic.
- Preserves enums, array `items` schemas, and object shape wherever the
  Codex-accepted subset allows.
- Output is deterministic: repeated calls on the same canonical schema
  produce byte-identical transport schemas once serialized with
  `sort_keys=True`.

### Fail-closed unsupported constructs

`build_codex_transport_schema` raises `ProviderUnavailableError` with code
`CODEX_TRANSPORT_SCHEMA_UNSUPPORTED` — never a silent, potentially-unsafe
transformation — for: recursive or remote `$ref`s, `oneOf`/`anyOf`/`allOf`/
`not`/`if`/`then`/`else`, `patternProperties`, a schema-valued
`additionalProperties`, `unevaluatedProperties`, `dependentSchemas`/
`dependentRequired`, `contains`/`minContains`/`maxContains`,
`propertyNames`, `format`, non-string enum members, excessive nesting
depth, and an invalid (non-mapping) input schema. None of these constructs
appear in the current canonical role/decision schemas — the checks exist so
a future canonical-schema change that introduces one fails loudly at
transport-build time instead of being silently mistranslated.

### Two-stage validation

```text
canonical schema
  -> validate canonical schema locally (Draft7Validator.check_schema)
  -> build Codex transport schema (build_codex_transport_schema)
  -> validate transport schema locally (Draft7Validator.check_schema)
  -> enforce the configured byte limit on the *normalized* transport schema
  -> pass transport schema to Codex (--output-schema)
  -> parse terminal Codex JSONL response
  -> decode response JSON
  -> validate response against the canonical schema (validate_against_schema)
  -> accept and persist only if canonical validation succeeds
```

The response is never validated only against the transport schema. A
response that satisfies the broader transport schema but violates a
canonical-only bound (e.g. a `summary` string canonical caps at 4000
characters, which the transport schema no longer bounds) is rejected by
canonical validation and never persisted as a successful role report.

### Diagnostic failure codes

| Code | Meaning |
| --- | --- |
| `CODEX_TRANSPORT_SCHEMA_UNSUPPORTED` | The canonical schema could not be safely rewritten into a Codex-accepted transport schema, or the normalized transport schema is invalid Draft-07 or exceeds the configured byte limit. |
| `CODEX_SCHEMA_REJECTED` | The canonical schema itself is unserializable/invalid, or Codex's own CLI rejected the transport schema at runtime (see `codex_failure_classifier.py`'s `_SCHEMA_MARKERS`), or the schema file could not be written. |
| `CODEX_FINAL_OUTPUT_MALFORMED` / `CODEX_FINAL_OUTPUT_MISSING` | Codex's terminal response was not valid JSON, or no final `agent_message` was present. |
| (generic `CODE_SCHEMA_*` codes, stage `STRUCTURED_SCHEMA`) | The decoded response failed canonical post-response validation (`output_validation.classify_schema_error`) — this is provider-agnostic and identical to how Claude Code/Anthropic canonical failures are classified. |

These codes are never collapsed into one generic bucket — `CODEX_TRANSPORT_SCHEMA_UNSUPPORTED`
is raised before any subprocess starts, `CODEX_SCHEMA_REJECTED` covers both
pre-send local invalidity and a live CLI-side rejection, and a canonical
post-response failure always surfaces its specific `CODE_SCHEMA_*` code.

### Known limitations

- The normalizer currently only inlines `$ref`s local to the document
  (`#/$defs/...`, `#/definitions/...`); remote and recursive references are
  rejected outright rather than partially resolved.
- Because required-but-nullable transport fields may cause Codex to emit an
  explicit `null` for a canonical-optional field, the canonical schema must
  keep accepting `null` for any field this normalizer makes
  transport-required — already true for every current role/decision field
  (`numeric_value`, `unit`) but a constraint on any future optional field.
- The transport schema is deliberately broader, never narrower, than the
  canonical schema — it cannot be used as a stand-in for canonical
  validation, and this repository's tests specifically guard against that
  substitution (`test_codex_schema_transport.py`,
  `test_codex_provider.py::test_response_passing_transport_schema_but_failing_canonical_is_rejected`).

### Temporary schema file handling

The Codex **transport** schema (not the canonical schema) is what gets
written to a private temporary file under `working_directory/.codex-schemas/`:
directory mode `0700`, file mode `0600`, exclusive creation with an
unpredictable (`secrets.token_hex`) name, never overwritten, and removed in
a `finally` block after success, failure, timeout, or cancellation. A
bounded-age sweep (`cleanup_abandoned_schema_files`) removes any
`codex-schema-*` file older than the configured threshold left behind by an
interrupted process.

### Single-role smoke-test procedure

Before any live Codex call: confirm focused tests and fixture tests pass,
run one supervised role only (`fundamental`, symbol `AAPL`), print — but
never log the full schema or raw response — the provider, model, role,
symbol, canonical/transport schema hashes, transport schema byte size,
timeout, and maximum attempts. Scheduler execution, paper books, and
external Alpaca paper must remain disabled for this and every other Codex
research invocation.

## Usage and cost semantics

Only values Codex actually reports are captured: input/output tokens,
cached-input tokens, latency, the opaque `thread_id`, the configured model,
and the Codex CLI version. Codex's JSONL stream never reports an
independently resolved model or cache-write/creation tokens — those fields
stay unset rather than being invented; `UsageRecord.resolved_model_name`
is `None` for every Codex row (Codex either runs exactly the requested
model or fails the turn outright — confirmed against a live smoke test — so
there is no silent substitution to detect, but there is also nothing to
honestly report as "resolved"). `cost_estimate_basis` is
`SUBSCRIPTION_API_EQUIVALENT_ESTIMATE`, labeled as an estimate and never
presented as the user's actual ChatGPT subscription charge — mirrors the
existing `claude_code` semantics (`models.py::UsageRecord`).

`research/usage.py::build_usage_record` and `research_attempts.provider_cli_version`
generalize the previously Claude-Code-only provenance fields:
`configured_model_alias`, `provider_cli_version` (new, additive column —
`claude_code_version` is preserved unchanged for existing and new Claude
Code rows, never overloaded with a Codex value; see
"Database migration" below).

## Scheduler health and pause behavior

Codex participates in the same shadow scheduler, budget, and health
infrastructure as every other provider — no second scheduler:

* `shadow/budget.py::REAL_CLAUDE_PROVIDERS` includes `codex` (pricing is
  required before any scheduled Codex cycle can start).
* `shadow/config.py::BudgetsSection` has a parallel `max_codex_*` budget
  family (calls per cycle/day/month, input/output tokens per cycle, latency
  per role/cycle, API-equivalent cost per call/cycle/day/month) mirroring
  the existing `max_claude_code_*` fields.
* `shadow/attempt_controller.py::ShadowResearchAttemptController` enforces
  Codex's call caps and, on authentication/quota/usage-metadata failures,
  requests the existing automatic provider-health pause
  (`shadow/pause.py::STATE_PAUSED_PROVIDER_HEALTH`) — the same mechanism
  Claude Code uses, not a new one.
* A later successful preflight never auto-resumes scheduling — resume
  remains operator-controlled.

## Launchd behavior

No Codex-specific launchd wrapper is required: unlike Claude Code's
portable OAuth token, Codex's cached login is tied to the macOS user's
`$HOME`/`$CODEX_HOME`, so the existing `deploy/launchd/run_shadow_cycle.sh.example`
pattern (run as the trusted user, `set -euo pipefail`, `umask 077`, absolute
paths, explicit credential-variable unsetting, all submission flags
disabled) applies unchanged — just point `--research-config` at
`config/production/research-codex.yaml` and select `--provider-mode real`.

## Rollout process

1. Run the entire offline suite: `pytest tests/ -q`.
2. Run `pytest tests/unit/test_codex_provider.py tests/unit/test_codex_configuration.py tests/unit/test_codex_deployment.py -q`.
3. Run `codex-provider-preflight` without inference against a real login.
4. Run one manually approved role for one symbol (`run-research --provider codex`).
5. Inspect schema validation, token usage, model provenance, latency, and
   persisted failure metadata for that run.
6. Run one complete research cycle for one symbol.
7. Confirm no paper order or external intent was submitted.
8. Run a limited shadow schedule (`config/production/research-codex.yaml`).
9. Expand symbols only after several successful cycles.
10. Keep all submission and promotion flags disabled throughout.

## Limitations of personal ChatGPT authentication

* Codex's JSONL stream does not report an independently resolved model —
  pricing and provenance rely on the configured model alone.
* The cached login is tied to one macOS user's `$HOME`/`$CODEX_HOME`; there
  is no portable token to rotate into a different scheduler host without
  re-running `codex login` there.
* ChatGPT plan usage limits are not queryable — this provider never scrapes
  ChatGPT usage pages or infers remaining subscription quota; it only reacts
  to an explicit `CODEX_QUOTA_EXHAUSTED` failure from a real call.

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `CODEX_BINARY_MISSING` | `codex.binary_path` wrong or CLI not installed | `command -v codex`, fix `binary_path` |
| `CODEX_VERSION_UNSUPPORTED` | Installed CLI older than `codex.minimum_version` | `codex --version`, upgrade via npm |
| `CODEX_NOT_AUTHENTICATED` | No cached ChatGPT session for this user | `codex login`, re-run preflight |
| `CODEX_UNEXPECTED_AUTH_METHOD` | Logged in with an API key, not ChatGPT | `codex login` interactively (ChatGPT) |
| `CODEX_QUOTA_EXHAUSTED` | ChatGPT plan usage limit hit | Wait for reset; scheduling pauses automatically |
| `CODEX_SCHEMA_REJECTED` | Research JSON Schema oversize/invalid, or CLI rejected it | Check `codex.maximum_schema_bytes`; validate schema locally |
| `CODEX_INVALID_JSONL` / `CODEX_TERMINAL_EVENT_MISSING` | CLI JSONL shape changed (version bump) | Update `codex_jsonl_adapter.py`; re-capture fixtures |

## Operational go/no-go checklist

* [ ] `codex --version` meets `codex.minimum_version`.
* [ ] `codex login status` reports ChatGPT authentication for the scheduler's macOS user.
* [ ] `codex-provider-preflight` returns `"ready": true`.
* [ ] `config/production/research-codex.yaml` has an explicit `research.model`.
* [ ] Codex pricing entry exists in `config/research_pricing.yaml` for that model, or the operator has accepted `PRICING_NOT_CONFIGURED` cost semantics.
* [ ] All paper-submission and promotion flags remain `false` in every config touched by this rollout.
* [ ] `codex.working_directory` exists, mode `0700`, outside the repository.
* [ ] Offline test suite (including `test_codex_*`) passes with no real Codex/OpenAI/ChatGPT request.
