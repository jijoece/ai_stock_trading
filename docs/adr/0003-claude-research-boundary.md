# ADR 0003: Claude is an evidence-bound research provider behind a deterministic overlay, never an execution agent

**Status:** Accepted
**Date:** 2026-07-12 (Milestone 5)

## Context

Milestones 1-4 delivered a fully deterministic pipeline — screener, scorer, risk engine,
recommendation builder, paper execution, ledger, evaluation — with no LLM anywhere in the
decision path. `docs/milestone-5.md` asks for a "safe, reproducible Claude-powered research
layer that enhances the existing deterministic screening and scoring pipeline while preserving
deterministic control over" every decision that touches money: recommendation construction,
risk decisions, position sizing, paper execution, portfolio state, ledger accounting, broker
communication, and performance evaluation.

Three architectural questions had to be answered before writing code:

1. Where does Claude's output enter the pipeline, and where is it forbidden?
2. Does the existing recommendation builder, scorer, or execution layer get replaced, wrapped,
   or left alone?
3. How does untrusted external text (news, filings, Reddit) reach a model prompt without ever
   being interpretable as an instruction?

Repository inspection also surfaced two pre-existing facts that shaped the design:

* `anthropic` is already a **base** (non-optional) dependency in `pyproject.toml`, used today by
  `scripts/submit_batch.py` for a one-time meta-research pipeline (the batch-API investigation
  that produced `docs/AI-Driven-Stock-Trading-Architecture.md`). `Config.anthropic_api_key` /
  `Config.anthropic_model` already exist in `config.py`.
* `config/tool_policy.yaml` + `mcp/tool_classifier.py` already implement a deterministic,
  fail-closed Robinhood/Reddit MCP read/write classifier (allowlist > denylist-pattern > unknown
  fails closed). Milestone 5's "Robinhood MCP policy" requirement is already satisfied by this
  existing code and is reused, not reimplemented.

## Decision 1: Claude is a replaceable research provider behind a `Protocol`, never imported by domain code

`src/trading_research/research/provider_protocol.py` defines `ResearchModelProvider`, a single
`generate_structured(request) -> response` method. `orchestration.py`, `overlay.py`,
`claim_validation.py`, and the CLI depend only on this Protocol. The Anthropic SDK import is
confined to `research/anthropic_provider.py` — no other file in `research/` imports `anthropic`.

Two offline implementations exist and are what the default test suite and CLI use:

* `DeterministicResearchProvider` — no LLM call at all; derives a structured response purely
  from the evidence snapshot's own `deterministic_factors` using a fixed, documented rule. This
  is the CLI's `--provider deterministic` default.
* `ScriptedResearchProvider` — a test double that lets tests script an exact sequence of
  responses/errors per `(role, attempt_number)`, used to exercise retries, timeouts, and
  malformed output deterministically.

Because `anthropic` was already a base dependency for an unrelated, pre-existing pipeline, this
ADR does **not** add a new optional `research` extra as `docs/milestone-5.md` Step 3 suggests in
the abstract — that would contradict the instruction to follow existing, reasonable repository
conventions. The default test suite still needs zero credentials and zero network access; it
simply also happens to have the SDK importable, exactly as it already did before this milestone.
This is recorded as a known, deliberate deviation from the milestone document's suggestion — see
"Known limitations" in `docs/milestone5-evidence-backed-claude-research.md`.

## Decision 2: A point-in-time `EvidenceSnapshot` is the only thing a role prompt ever sees

`research/evidence.py` builds an immutable `EvidenceSnapshot` from small provider Protocols
(`FundamentalsEvidenceProvider`, `NewsEvidenceProvider`, `FilingEvidenceProvider`, ...) — never
one unrestricted research tool. `research/fixtures.py` supplies the initial vertical slice's
fixture-backed evidence for `AAPL`, `MSFT`, `SHEL`, and a deliberately thin `XXXX` symbol used to
exercise the missing-data path.

The snapshot's `snapshot_id` is a SHA-256 hash of its own canonicalized content (excluding
`created_at`, which is wall-clock metadata, not content) — the same evidence always produces the
same `snapshot_id`, which is what lets the orchestrator recognize "rerun the same inputs" and
reuse an existing completed research run instead of calling the provider again.

Every `SourceRecord` carries four independent timestamps — `published_at`, `effective_at`,
`available_at`, `retrieved_at` — specifically to prevent look-ahead bias: `build_evidence_snapshot`
computes `point_in_time_safe` from whether every source's `available_at` was at or before the
snapshot's `as_of`.

## Decision 3: Evidence text is quoted, delimited, untrusted data — never concatenated into an instruction

`research/evidence_validation.py` wraps every evidence item's rendered text between explicit
`<<<UNTRUSTED_EVIDENCE_DATA ... UNTRUSTED_EVIDENCE_DATA>>>` delimiters, neutralizes obvious
control tokens and fake closing delimiters, strips Unicode `Cf`/`Cc` control characters (e.g.
right-to-left override), and reuses the existing `collection/prompt_injection_filter.py` pattern
list to annotate an injection-risk level per item. Every role's system prompt
(`research/prompt_registry.py::SAFETY_PREAMBLE`) explicitly states that content between those
delimiters may contain malicious instructions and must never be followed.

This is intentionally not the only defense. Even if a model complied with an injected instruction
anyway, `research/output_validation.py` enforces `additionalProperties: false` JSON Schema
validation with an explicit denylist of executable-order field names (`shares`, `quantity`,
`order_type`, `limit_price`, `side`, `submit_order`, ...) scanned recursively through the parsed
JSON before schema validation runs — so a field named `shares` cannot reach a
`RoleResearchReport`/`ResearchDecision` object even if a future schema edit accidentally allowed
extra properties.

## Decision 4: Structured output uses forced Anthropic tool use, not a beta response-format flag

`research/anthropic_provider.py` sends a single tool whose `input_schema` is exactly the same
JSON Schema `output_validation.py` validates against, with `tool_choice` forced to that tool's
name. This is Anthropic's documented, stable mechanism for reliable structured JSON extraction
(as opposed to hoping the model emits clean, unprefixed JSON in a plain-text response, or
depending on a beta-only structured-output flag whose availability/stability was not something
this milestone could verify against current documentation with confidence). Every provider
returns an already-JSON-decoded `parsed_json` — a provider that cannot produce one raises
`MalformedOutputError` instead of returning a partial response.

## Decision 5: Claim-to-evidence validation is a separate, independent pass — never model self-assessment

`research/claim_validation.py` re-derives support for every claim from the exact
`EvidenceSnapshot` used in the run: unknown evidence IDs, cross-snapshot/cross-symbol citations,
stale evidence, point-in-time-unsafe sources, and numeric claims outside a documented 2% rounding
tolerance of the cited evidence's `normalized_values` are all rejected, never "trusted because the
model said so." A `high`-importance claim that fails validation forces the entire role
report/decision invalid; validation failure feeds back into the next bounded retry attempt's
prompt as plain-text feedback, never as new evidence.

## Decision 6: A deterministic overlay is the only thing allowed to touch a recommendation's `side`

`research/overlay.py::decide_overlay_action` maps a `ResearchDecision.rating` to one of four
actions (`ALLOW_BASELINE`, `DOWNGRADE_TO_WATCH`, `FORCE_NO_ACTION`, `ANALYSIS_INCOMPLETE`) via
ordinary, versioned Python — never a model-produced field. `resolve_side_after_overlay` is the
single function that turns that action into a `side` transition, and it is structurally
one-directional: it can turn `buy_candidate` into `watch`/`no_action`/`analysis_incomplete`, and
it always leaves a `screened_out` or already-`analysis_incomplete` baseline untouched — there is
no code path anywhere in this function that assigns `SIDE_BUY_CANDIDATE`. Model `confidence` is
persisted as reported metadata and never multiplies into a score or share count.

`research/recommendation_overlay.py::apply_overlay_to_recommendation` applies this to an
already-frozen, already schema-validated baseline `FrozenRecommendation` payload, producing a new,
separately schema-validated, immutable "enhanced" recommendation with a rec_id
deterministically derived from `(baseline rec_id, overlay_id)`. It never imports or duplicates
`recommendations/builder.py`'s scoring/eligibility logic, and it can only ever null out
`risk_plan` — there is no code path that constructs one — so it cannot raise position size and
cannot turn a screened-out or incomplete baseline into anything executable.

## Decision 7: Both experiment arms are always recorded, whatever either one's outcome

`research/experiment.py::build_experiment_assignments` always constructs a `BASELINE` and an
`ENHANCED` `ExperimentAssignment` together, deterministically keyed by `(candidate_run_id,
policy_version)`. A screened-out, watch, no-action, or `ANALYSIS_INCOMPLETE` recommendation gets
an assignment exactly like an executable one —
`evaluation/research_comparison.py` never filters by execution outcome, which is what prevents
survivorship bias in the eventual baseline-vs-enhanced comparison.

## Decision 8: Persistence is additive-only new tables, wired the same way every prior milestone's schema was

`storage/research_schema.py` follows the exact convention of `trading_schema.py` /
`execution_schema.py`: idempotent `CREATE TABLE IF NOT EXISTS` DDL plus SQLite triggers that
reject `UPDATE`/`DELETE` on immutable tables (`research_evidence_snapshots`,
`research_decisions`, `research_role_reports`), applied from `storage/database.py::connect`.
`research_attempts` is strictly append-only — retries add new rows, never rewrite a prior
attempt. The one table name collision discovered during implementation
(`migrations.py`'s pre-existing, unrelated `research_runs` table from the earlier batch-API
meta-research pipeline) was resolved by naming Milestone 5's run table
`research_committee_runs` instead of overloading the existing name.

## Consequences

* Replay (`research/replay.py::replay_research_run`) has no `provider` parameter at all — it is
  structurally impossible for it to call a provider or an execution API, not just conventionally
  disallowed.
* A research failure (retry exhaustion, missing evidence, provider unavailable) can only ever
  produce `ANALYSIS_INCOMPLETE`; it cannot delete or overwrite the deterministic baseline
  recommendation, which remains a fully separate, already-persisted `FrozenRecommendation`.
* Cost is never fabricated: `research/usage.py::build_usage_record` returns one of
  `CALCULATED | PRICING_NOT_CONFIGURED | USAGE_NOT_RETURNED | NOT_APPLICABLE`, and pricing is
  optional, effective-dated configuration (`config/research_pricing.yaml`, empty by default) —
  never a hardcoded "timeless" price.
* The known limitation this ADR accepts: `anthropic` remains a base dependency (Decision 1)
  rather than becoming a milestone-scoped optional extra, because it already was one before this
  milestone for an unrelated reason. A future milestone that wants a truly SDK-free install
  would need to split `scripts/submit_batch.py`'s dependency out first.
