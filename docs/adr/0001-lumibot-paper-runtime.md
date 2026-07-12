# ADR 0001: LumiBot as the sole external paper-trading runtime, behind an internal adapter

**Status:** Accepted
**Date:** 2026-07-11 (Milestone 3)

## Context

Milestone 2 delivered a complete offline research/decision pipeline
(screening, scoring, sentiment, risk sizing, recommendation freezing,
persistence) and an already-existing internal paper ledger
(`paper/ledger.py`, present since Milestone 1). Nothing in the codebase
could yet turn a frozen `buy_candidate` recommendation into a simulated
order/fill. `docs/milestone-3.md` requires closing that gap using LumiBot as
"the runtime and simulated-broker component," while the existing trading
desk remains "the domain, policy, audit, persistence, and evaluation
authority."

Three architectural questions had to be answered before writing code:

1. Where does LumiBot's object model (`Order`, `Asset`, its own status
   enums) live, and where is it forbidden?
2. Does the existing paper ledger get replaced, rewritten, or extended?
3. Do the new paper-execution contracts follow this repository's existing
   modeling convention, or the `pydantic.BaseModel` shown illustratively in
   `docs/milestone-3.md`?

## Decision 1: LumiBot is the sole external trading runtime, isolated to `runtime/lumibot/`

LumiBot is the only new major framework introduced (per
`docs/milestone-3.md`'s explicit constraint — no NautilusTrader, LEAN,
FinRL-X, FinRobot, or TradingAgents). It is imported in exactly one
package, `src/trading_research/runtime/lumibot/`, behind the
framework-neutral `execution.adapter_protocol.PaperExecutionAdapter`
Protocol. `tests/unit/test_lumibot_adapter.py::
test_no_lumibot_import_outside_runtime_package` enforces this by walking
the AST of every other file under `src/trading_research/`.

**Rejected alternative: rewrite the trading desk around LumiBot's
`Strategy`/`Trader` model.** LumiBot's natural usage pattern is to *be* the
application (a `Strategy` subclass with lifecycle callbacks, run by a
`Trader` event loop). Adopting that shape would have meant migrating
recommendation/risk/ledger logic into LumiBot-owned lifecycle hooks —
exactly what `docs/milestone-3.md` forbids ("Do not create a new
LumiBot-first repository," "Do not replace the existing recommendation
model"). Instead, LumiBot is consumed as a library for two narrow,
genuinely LumiBot-shaped pieces of work: constructing a well-formed order
object (`Order`/`Asset`/`OrderType`/`OrderSide`) and interpreting its
`OrderStatus` values — both isolated in `runtime/lumibot/adapter.py` and
`event_mapper.py`.

## Decision 2: one portfolio-state owner, one execution owner, multiple future research contributors

`paper/ledger.py::PaperLedger` remains the single owner of simulated
cash/position state. It was extended (one new method, `apply_external_fill`,
sharing a refactored-out `_apply_fill` helper with the existing
`submit_and_fill`) rather than replaced or rewritten — its existing 11
tests pass unchanged, and its invariants (T+1 settlement, idempotency-key
dedup, no negative cash) apply identically to both fill paths.

`services/execute_paper_recommendation.py` is the single owner of the
paper-execution *sequence* (eligibility → intent → submit → ledger →
reconcile) — analogous to how `services/analyze_candidate.py` is the single
owner of the analysis sequence. Both are thin orchestrators over
independently-testable collaborators, not god-objects.

Multiple future producers can feed recommendations into this same pipeline
(a future Claude research agent, a future batch screener run, a manually
constructed fixture) without touching the execution layer — recommendation
production and paper execution were already separate concerns before this
milestone, and remain separate after it.

## Decision 3: contracts as `@dataclass(frozen=True)`, not `pydantic.BaseModel`

`docs/milestone-3.md` Step 3 illustrates `PaperOrderIntent` etc. as
`pydantic.BaseModel` subclasses. This repository has never used pydantic —
every existing typed contract (`models/trading_models.py`,
`analysis/screener.py::ScreeningConfig`, `analysis/scorer.py::
ScoringConfig`, `recommendations/builder.py::FrozenRecommendation`) is a
`@dataclass(frozen=True)` with `__post_init__` validation, and `pydantic` is
not in `pyproject.toml`'s dependencies today.

**Rejected alternative: add pydantic as a new base dependency.** This would
introduce a second validation paradigm alongside the existing one for a
single milestone's contracts, with no behavioral benefit — dataclasses with
`__post_init__` already provide the fail-closed validation
`docs/milestone-3.md` Step 3 requires (long-only, positive quantity,
limit/market price rules, exact notional reconstruction, expiry ordering).
`docs/milestone-3.md`'s own instruction ("Add framework-neutral models
*similar to* the following") permits this substitution; matching the
existing convention was judged more valuable than matching the illustrative
snippet verbatim.

## Decision 4: why live execution remains disabled

`execution/live_gateway.py::LiveExecutionGateway` is defined as a `Protocol`
with exactly one implementation shipped, `DisabledLiveExecutionGateway`,
whose every method unconditionally raises `LiveTradingDisabledError`. This
is enforced at three independent layers so no single mistake can enable
live trading:

1. **No alternate implementation exists in the codebase** to construct or
   inject — there is nothing to feature-flag on.
2. **`config/execution.yaml` hard-codes** `trading_mode: paper` and
   `live_trading_enabled: false`; `execution/config.py::
   load_execution_config` raises `ExecutionConfigError` if either is ever
   changed to something else, and never reads an environment variable for
   either field.
3. **`real_orders` remains trigger-protected** at the SQLite level
   (unchanged from Milestone 1/2) — even a hypothetical bug elsewhere could
   not write a live order row.

This mirrors `docs/milestone-3.md`'s explicit non-goals (live Robinhood
trading, order review/preview/placement/cancellation/modification,
autonomous live execution, direct LLM-to-order execution) and Milestone
1/2's existing fail-closed posture.

## Consequences

* LumiBot's ~140-package transitive dependency footprint and its
  jsonschema/python-dotenv version conflicts with this repository's pinned
  floor are isolated to an optional `paper` extra — the default
  development/test environment never installs it.
* The full paper-execution vertical slice (eligibility → intent → fill →
  ledger → reconciliation) is provable offline via a deterministic adapter,
  independent of whether LumiBot is installed at all.
* A future milestone implementing a real broker connection only needs to
  implement `runtime.lumibot.adapter.PaperBrokerGateway` against a
  credentialed LumiBot `Trader`/`Strategy` — no other module in this
  codebase needs to change.
