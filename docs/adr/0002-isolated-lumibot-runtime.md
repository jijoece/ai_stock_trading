# ADR 0002: LumiBot moves behind a process boundary; a versioned JSON protocol replaces in-process adapter injection for credentialed submission

**Status:** Accepted
**Date:** 2026-07-12 (Milestone 4)

## Context

Milestone 3 (ADR 0001) isolated LumiBot to `src/trading_research/runtime/lumibot/` within the
*same process and virtualenv* as the rest of the trading desk, behind the framework-neutral
`execution.adapter_protocol.PaperExecutionAdapter` Protocol. That was sufficient to prove the
recommendation → intent → fill → ledger → reconciliation vertical slice deterministically, but
ADR 0001's own "Consequences" section flagged the unresolved cost: installing `lumibot==4.5.74`
(`pip install -e ".[paper]"`) pulls roughly 140 transitive packages unrelated to this project
(`langchain`, `google-adk`, a Kubernetes client, `openai`, several other LLM/agent SDKs, ...) and
downgrades this repository's own pinned dependency floor (`jsonschema>=4.26.0` → `4.23.0`,
`python-dotenv>=1.2.2` → `1.0.1`). At Milestone 4 implementation time, `lumibot` was in fact
already installed in this development environment for verification purposes — Python 3.14.5rc1,
`lumibot` 4.5.74 confirmed importable, and the Milestone 1-3 baseline (308 tests) re-verified
passing under it. That does not change the underlying conflict: nothing prevents a future
dependency bump in either project from reintroducing a real, blocking conflict, and the milestone
explicitly requires proving a **credentialed** paper-broker round trip is possible without ever
requiring the main trading-desk process to accept LumiBot's dependency tree as its own.

`docs/milestones/milestone-4.md` requires closing that gap: "Because LumiBot 4.5.74 introduces a large
dependency tree and conflicts with dependency floors in the main project, Milestone 4 must move the
real LumiBot runtime behind a process boundary."

## Decision 1: LumiBot moves to a separate installable package, run as a child process

A new top-level package, `paper_runtime/` (installable as `trading-paper-runtime`), owns the
`lumibot==4.5.74` dependency as a *base* dependency (not optional — this package has no reason to
exist without it). It is never installed into the main project's environment and the main project's
`pyproject.toml` is unchanged by this milestone. The main process spawns it as a child process
(`python3 -m trading_paper_runtime`, configurable in `config/paper_runtime.yaml`) and speaks to it
exclusively over a versioned JSON Lines protocol (`paper-runtime.v1`) on its stdin/stdout.

**Rejected alternative: keep LumiBot in-process behind Milestone 3's adapter, and just document
the risk.** This was Milestone 3's approach and remains structurally sound for the deterministic
adapter path (which requires no LumiBot object at all in production). But it cannot ever be made to
prove a **credentialed** round trip without installing LumiBot into the main environment, which is
exactly the dependency-conflict outcome this milestone must eliminate. A process boundary is the
only way to keep "LumiBot's dependency tree" and "the main trading desk's dependency tree" truly
independent while still allowing a real credentialed connection to exist somewhere.

**Rejected alternative: a local-only HTTP service instead of stdio JSON Lines.** `docs/milestones/milestone-
4.md` Step 2 permits either but recommends stdio unless the repository already has an established
HTTP-service pattern — it does not (no web framework dependency exists anywhere in this repository
today). stdio avoids introducing one, is trivial to spawn/tear down as a child process, and every
test in this milestone can inject a fake in-memory transport (`tests/support/
runtime_client_fixtures.py::FakeTransport`) without a real socket or port.

## Decision 2: the protocol is a small, explicit, versioned JSON contract — no shared Python types

`paper-runtime.v1` request/response envelopes (`protocol_version`, `request_id`, `operation`,
timestamp, `payload`, and on the response side `runtime_version`/`success`/`retryable`/`error`) are
implemented **twice**, independently: once in `paper_runtime/src/trading_paper_runtime/protocol.py`
(the isolated runtime's side) and once in `src/trading_research/runtime/client/protocol.py` (the
main process's side). Both implementations were written to a shared understanding of the wire
format, but neither is installed as a shared dependency of the other, and neither ever passes a
constructed Python object (no `pickle`, no `pydantic` model shared across the boundary — matching
this repository's Decision 3 from ADR 0001, dataclasses stay `@dataclass(frozen=True)` everywhere).

**Rejected alternative: a shared `trading-runtime-protocol` package.** `docs/milestones/milestone-4.md` Step 4
explicitly permits this ("If sharing protocol models is necessary, create a very small
framework-neutral package... with no LumiBot dependencies") but also warns against "circular
installation requirements." Given the JSON contract is genuinely small (9 operations, ~5 envelope
fields), duplicating ~150 lines of dataclass/validation code twice was judged simpler and safer
than introducing a third installable package that both other packages would need to depend on —
and it forces both sides to independently validate the wire format rather than trusting a shared
type system, which is the actual safety property this boundary needs (docs/milestones/milestone-4.md: "reject
unknown protocol versions... malformed payloads... responses with mismatched request IDs").

## Decision 3: submission is asynchronous — a new, additive orchestration path, not a rewrite of Milestone 3's synchronous adapter

Milestone 3's `execution.adapter_protocol.PaperExecutionAdapter.submit()` returns `(events, result)`
synchronously — every existing adapter (deterministic, in-process LumiBot) assumes the fill outcome
is known by the time `submit()` returns. A real credentialed broker submission is fundamentally
asynchronous: the broker acknowledges receipt now, and fills/cancellations/rejections arrive later.
Rather than force this shape through the existing synchronous Protocol (which would require either
blocking the whole service on a broker fill that may never come, or fabricating a placeholder
result), Milestone 4 adds two new, additive services that reuse every Milestone 3 primitive except
the top-level orchestration function itself:

* `services/submit_credentialed_paper_order.py` — acknowledgement only. Reuses
  `execution.eligibility.PaperExecutionEligibilityPolicy`, `execution.intent_builder.
  build_paper_order_intent`, and `storage.execution_repositories.save_intent` verbatim; adds a new
  `paper_broker_submissions` table and a `PENDING_SUBMISSION` → `ACCEPTED`/`SUBMISSION_UNKNOWN`/...
  state machine (docs/milestones/milestone-4.md Step 8) that did not exist in Milestone 3's contracts.
* `services/sync_paper_orders.py` — polls the runtime for state changes and reuses `execution.
  ledger_events.apply_all_new_events` (the exact Milestone 3 ledger-application code path)
  unchanged, converting cumulative broker-reported fill quantities into the incremental
  `PaperExecutionEvent.filled_quantity` that function already expects.

**Rejected alternative: make `PaperExecutionAdapter.submit()` itself asynchronous** (e.g. return a
future/pending marker). This would have required changing the Protocol every existing adapter and
every Milestone 3 test implements, violating "preserve all Milestone 1-3 tests" and "avoid broad
unrelated refactoring." The chosen design leaves `execute_paper_recommendation` (the deterministic/
in-process-LumiBot path) byte-for-byte unchanged and adds a parallel, equally-tested path for the
one adapter that is genuinely asynchronous.

## Decision 4: one lookup-before-submit call handles fresh submission, restart recovery, and ambiguous-timeout recovery identically

`submit_credentialed_paper_order` always calls `client.get_order(client_order_id)` before ever
calling `client.submit_order(...)` — for every invocation, not only "retry" invocations. This single
rule (documented in the module's own docstring) means: a never-submitted intent gets `UNKNOWN_ORDER`
back and proceeds to submit; an intent whose submission was interrupted by a crash gets the broker's
already-existing order back and never resubmits; an ambiguous `submit_order` timeout is followed by
exactly one more such lookup rather than a blind retry. No separate "is this a restart" flag exists
anywhere in the code, because it was not needed once this ordering was chosen.

## Decision 5: no live historical-price data source ships with this milestone

`evaluation.price_provider.PriceProvider` is a `Protocol` with one implementation,
`DeterministicPriceProvider` (an in-memory fixture, test/offline use only). This repository has
never had a historical-bars/market-data fetcher of any kind (confirmed absent from `collection/`,
`processing/`, `mcp/` at implementation time), and Step 11's explicit "no look-ahead bias" / "do not
use a current quote as a historical close" requirements rule out any quick substitute (e.g. calling
a live quote endpoint and treating it as a historical close). Rather than fabricate a data source
under time pressure, the evaluation service, market calendar, and metrics layer are implemented and
fully tested against the deterministic fixture provider; a future milestone implements `PriceProvider`
against a real, verified point-in-time data source without changing `evaluation_service.py`,
`metrics.py`, or their persistence layer at all.

## Consequences

* The main trading-desk process's `pyproject.toml` gains zero new dependencies from this milestone.
  `lumibot`'s ~140-package transitive footprint is fully isolated to `paper_runtime/`'s own
  environment, which is never installed alongside the main project.
* A genuine, real end-to-end smoke test was performed during implementation (not just fake-transport
  unit tests): `python3 -m trading_research.cli paper-runtime-health`, run from the main project with
  no arguments, spawned the real `python3 -m trading_paper_runtime` subprocess, which imported real
  LumiBot 4.5.74 and reported it back over the protocol (`lumibot_version: "4.5.74"`), correctly
  detected the absence of Alpaca credentials in this environment, and was correctly refused by the
  main process's `RuntimeClient` (`RuntimeCapabilityError`, since `paper_endpoint_verified` was
  `False`). See `docs/milestones/milestone4-isolated-paper-broker.md` "Known limitations" for why no
  credentialed acknowledgement or fill was exercised.
* That same smoke test caught a real bug this ADR's design did not anticipate: LumiBot prints an
  unguarded startup banner directly to its process's stdout at import time, which would have
  corrupted the `paper-runtime.v1` protocol stream. Fixed in `paper_runtime/src/
  trading_paper_runtime/__main__.py` by capturing the real stdout handle before any other import and
  redirecting `sys.stdout` to `sys.stderr` for the remainder of the process's life — see that file's
  comment for the exact ordering constraint (dispatcher.py imports `lumibot` at module scope to
  report its version in `health`, so the redirection must happen before that import, not merely
  before `main.run()`'s body).
* A future milestone implementing real Robinhood-assisted live execution would follow the same
  process-isolation shape if a similarly heavy dependency were required, but nothing in this
  milestone brings that path any closer — `execution/live_gateway.py::DisabledLiveExecutionGateway`
  is untouched and still the only `LiveExecutionGateway` implementation in the codebase.
