You are continuing implementation of my existing AI-driven trading-desk repository.

The previous implementation session completed Milestone 2. Do not repeat Milestone 2, redesign completed components, or start a new LumiBot-based repository.

Your task is to implement the next focused milestone:

# Milestone 3 — LumiBot paper-trading integration

This is a direct implementation task, not a research-only task.

The goal is to integrate LumiBot behind a clean adapter so the existing trading desk can send eligible, frozen recommendations through a deterministic paper-execution workflow.

The existing project remains the domain, policy, audit, persistence, and evaluation authority. LumiBot is only the runtime and simulated-broker component.

Do not stop after producing an architecture plan. Inspect the repository, implement the safe vertical slice, add tests, run the full test suite, and document the result.

---

# Confirmed starting state

The previous session confirmed the following baseline before Milestone 2:

```text
pytest tests/ -q
102 passed
```

Milestone 2 was then completed with:

```text
pytest tests/ -q
169 passed
```

The 169 passing tests are the new required baseline.

The previous implementation added or extended:

* deterministic stock screening;
* deterministic scoring;
* Reddit sentiment aggregation;
* portfolio-level position-sizing guardrails;
* recommendation construction;
* candidate-analysis orchestration;
* persistence for screening runs, candidate scores, factors, and recommendations;
* `screened_out` recommendation support;
* fail-closed incomplete-analysis behavior;
* exact score reconstruction from stored factor contributions;
* configuration files for screening, scoring, and risk;
* developer documentation for the analysis layer.

Important existing modules include:

```text
src/trading_research/analysis/sentiment.py
src/trading_research/analysis/screener.py
src/trading_research/analysis/scorer.py
src/trading_research/risk/position_sizing.py
src/trading_research/recommendations/builder.py
src/trading_research/services/analyze_candidate.py
src/trading_research/storage/trading_repositories.py
src/trading_research/paper/ledger.py
src/trading_research/models/trading_models.py
```

Important existing invariants include:

* unknown or incomplete financial state fails closed;
* missing data is explicitly recorded;
* no fabricated market prices;
* no silent fallback financial values;
* scores are reconstructible from persisted factors;
* recommendations can be frozen;
* frozen recommendations cannot be mutated;
* `real_orders` writes are blocked;
* `ANALYSIS_INCOMPLETE`, `NO_ACTION`, and `screened_out` recommendations cannot contain executable risk plans;
* existing positional constructor compatibility was deliberately preserved;
* real Robinhood and Reddit tools are not invoked in offline tests;
* unknown MCP tools fail closed;
* the full paper ledger already exists and must be reused rather than replaced.

Read the repository and verify these facts before editing. Treat this summary as context, not as a substitute for inspecting the actual code.

---

# Core architectural decision

Use this ownership model:

```text
Existing trading desk
├── Candidate analysis
├── Screening and scoring
├── Recommendation lifecycle
├── Recommendation freezing
├── Risk and position sizing
├── Audit and provenance
├── Internal paper ledger
├── Evaluation
│
└── LumiBot adapter
    ├── Runtime lifecycle
    ├── Market-session scheduling
    ├── Paper order submission
    ├── Simulated broker behavior
    └── Order/fill callbacks
```

The expected flow is:

```text
Existing candidate-analysis service
        ↓
Frozen recommendation
        ↓
Eligibility validation
        ↓
Deterministic PaperOrderIntent
        ↓
LumiBot paper adapter
        ↓
Simulated order and fill events
        ↓
Existing internal paper ledger
        ↓
Reconciliation and evaluation record
```

LumiBot must not become the authoritative source for:

* recommendations;
* risk decisions;
* position-sizing rules;
* audit records;
* permanent paper positions;
* strategy evaluation;
* approval status;
* live-order eligibility.

---

# Scope of this milestone

Implement the smallest safe end-to-end vertical slice that can:

1. Load an existing frozen recommendation.
2. Confirm that it is eligible for paper execution.
3. Convert it deterministically into an internal paper-order intent.
4. Submit the intent through a LumiBot adapter in paper mode.
5. process simulated order and fill callbacks.
6. Record the result through the existing paper ledger.
7. Reconcile LumiBot execution events against the internal ledger.
8. Persist sufficient provenance for deterministic replay and evaluation.
9. Prevent duplicate paper execution.
10. Prove that all live-execution paths remain disabled.

Use one to five fixture-backed symbols for integration tests.

Do not add live market-data dependencies to tests.

---

# Non-goals

Do not implement any of the following in this milestone:

* live Robinhood trading;
* Robinhood order review;
* Robinhood order preview;
* Robinhood order placement;
* Robinhood order cancellation;
* Robinhood order modification;
* options;
* short selling;
* margin;
* autonomous live execution;
* TradingAgents integration;
* FinRL-X integration;
* FinRobot integration;
* NautilusTrader integration;
* LEAN integration;
* direct LLM-to-order execution;
* Claude multi-agent orchestration;
* replacement of the current paper ledger;
* migration of domain models into LumiBot models;
* historical backtesting using current live data.

Do not add another major trading framework.

LumiBot is the only new major framework allowed.

---

# Step 1 — Inspect and establish the baseline

Before changing code:

1. Inspect the repository structure.
2. Read:

   * `docs/milestones/milestone-2.md`;
   * `docs/milestones/milestone2-analysis-layer.md`;
   * the Milestone 1 documentation;
   * recommendation models and schema;
   * recommendation freezing behavior;
   * position-sizing implementation;
   * paper-ledger implementation;
   * storage repositories;
   * migrations;
   * configuration loading;
   * existing CLI or service entry points;
   * test fixtures.
3. Check Git status and current branch.
4. Run:

```bash
pytest tests/ -q
```

The expected baseline is:

```text
169 passed
```

If the baseline differs, investigate and report the cause before introducing unrelated changes. Do not hide or delete failing tests.

If `docs/milestones/milestone-3.md` already exists, inspect it and reconcile it with this prompt. Do not silently ignore conflicting requirements. Prefer the safer and more fail-closed behavior.

Provide a concise current-state assessment before editing.

---

# Step 2 — Integrate LumiBot as an isolated dependency

Inspect the project’s existing dependency-management convention before adding LumiBot.

Requirements:

* verify the current supported LumiBot package and import structure;
* use official LumiBot documentation or source when API behavior is uncertain;
* pin or constrain the dependency consistently with the repository’s existing dependency policy;
* document why the dependency was added;
* avoid importing LumiBot throughout the domain layer;
* keep all LumiBot-specific imports inside the runtime adapter package where practical;
* make tests independent of external services and credentials.

Prefer an optional dependency group when that matches the repository’s existing packaging model, such as a `paper` or `lumibot` extra.

Do not expose LumiBot classes from public domain interfaces.

If LumiBot cannot be imported in the test environment because of a legitimate platform or dependency limitation, implement the boundary and contract tests using dependency injection, but do not falsely claim a working real LumiBot integration. Clearly report the limitation.

---

# Step 3 — Add internal paper-execution contracts

Inspect existing models first and reuse or extend them. Do not create duplicate concepts unnecessarily.

Add framework-neutral models similar to the following where needed.

## PaperOrderIntent

```python
class PaperOrderIntent(BaseModel):
    intent_id: str
    recommendation_id: str
    symbol: str
    side: Literal["BUY"]
    quantity: int
    order_type: Literal["MARKET", "LIMIT"]
    limit_price: Decimal | None
    reference_price: Decimal
    expected_notional: Decimal
    recommendation_created_at: datetime
    recommendation_frozen_at: datetime
    expires_at: datetime
    config_hash: str
    git_sha: str
    policy_version: str
    execution_version: str
```

Validation requirements:

* long-only for this milestone;
* quantity must be a positive whole number;
* no fractional shares unless the existing project explicitly and safely supports them;
* limit orders require a positive limit price;
* market orders must have `limit_price=None`;
* reference price must come from persisted evidence or an injected test fixture;
* no default price is allowed;
* expected notional must reconstruct from quantity and the appropriate reference price;
* expired recommendations cannot produce intents;
* incomplete or missing required fields must fail closed.

## PaperExecutionEvent

```python
class PaperExecutionEvent(BaseModel):
    event_id: str
    intent_id: str
    recommendation_id: str
    symbol: str
    event_type: Literal[
        "SUBMITTED",
        "ACCEPTED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELLED",
        "REJECTED",
        "ERROR",
    ]
    broker_order_id: str | None
    quantity: int
    filled_quantity: int
    fill_price: Decimal | None
    occurred_at: datetime
    raw_status: str | None
    source: Literal["LUMIBOT_PAPER"]
```

## PaperExecutionResult

```python
class PaperExecutionResult(BaseModel):
    intent_id: str
    recommendation_id: str
    final_status: Literal[
        "FILLED",
        "PARTIALLY_FILLED",
        "CANCELLED",
        "REJECTED",
        "ERROR",
    ]
    requested_quantity: int
    filled_quantity: int
    average_fill_price: Decimal | None
    fees: Decimal
    event_ids: list[str]
    completed_at: datetime
```

## ReconciliationResult

```python
class ReconciliationResult(BaseModel):
    intent_id: str
    status: Literal[
        "MATCHED",
        "PENDING",
        "MISMATCH",
        "MISSING_INTERNAL_EVENT",
        "MISSING_BROKER_EVENT",
    ]
    broker_quantity: int
    ledger_quantity: int
    broker_notional: Decimal
    ledger_notional: Decimal
    reasons: list[str]
    reconciled_at: datetime
```

Use deterministic IDs or stable idempotency keys where practical.

Do not generate identity solely from wall-clock time.

---

# Step 4 — Implement recommendation eligibility validation

Create a dedicated validator or service that determines whether a frozen recommendation may enter paper execution.

It must reject:

* `screened_out`;
* `watch`;
* `no_action`;
* `analysis_incomplete`;
* inactive recommendations;
* expired recommendations;
* unfrozen recommendations;
* recommendations without a valid risk plan;
* zero or negative quantity;
* missing entry/reference price;
* stale required price data;
* symbols not accepted by `TickerUniverse.require()`;
* duplicate already-executed recommendations;
* recommendations blocked by the global kill switch;
* recommendations whose configuration or provenance is incomplete;
* recommendations that fail current portfolio guardrails.

Only an eligible `buy_candidate` recommendation with a valid, deterministic position plan may create a paper-order intent.

The validation result should record explicit rejection reasons. Do not reduce it to a bare Boolean.

Example:

```python
class PaperExecutionEligibility(BaseModel):
    recommendation_id: str
    eligible: bool
    reasons: list[str]
    evaluated_at: datetime
    policy_version: str
```

Eligibility evaluation must be deterministic for identical persisted inputs and configuration.

---

# Step 5 — Create the LumiBot adapter boundary

Use a package boundary similar to:

```text
src/trading_research/runtime/
├── __init__.py
└── lumibot/
    ├── __init__.py
    ├── adapter.py
    ├── strategy.py
    ├── event_mapper.py
    ├── configuration.py
    └── errors.py
```

Adjust names to existing repository conventions rather than forcing this exact layout.

Create an internal protocol such as:

```python
class PaperExecutionAdapter(Protocol):
    def submit(self, intent: PaperOrderIntent) -> PaperExecutionResult:
        ...

    def reconcile(self, intent_id: str) -> ReconciliationResult:
        ...
```

Implement:

```python
class LumiBotPaperExecutionAdapter:
    ...
```

The adapter must:

* accept only validated `PaperOrderIntent` objects;
* operate only in paper mode;
* translate internal intents into LumiBot order requests;
* map LumiBot callbacks into internal `PaperExecutionEvent` records;
* avoid leaking LumiBot objects into storage repositories;
* write events idempotently;
* avoid mutating the source recommendation;
* never write to `real_orders`;
* use injected clock and market-data sources where needed for deterministic tests;
* fail closed on unknown LumiBot statuses;
* retain raw external status for debugging without treating it as trusted domain status;
* preserve Decimal values when crossing into internal persistence;
* explicitly document any unavoidable float conversion at the LumiBot boundary.

The internal paper ledger remains authoritative after event translation.

---

# Step 6 — Reuse the existing paper ledger

Inspect `src/trading_research/paper/ledger.py` carefully.

Do not replace or rewrite it merely to resemble LumiBot.

Add only the smallest safe extensions required to ingest normalized paper-execution events.

The integration must preserve existing ledger invariants.

Required behaviors:

* a submitted intent does not immediately become a filled position;
* only fill events affect position quantity and cost basis;
* partial fills are applied incrementally;
* duplicate fill events do not apply twice;
* cancelled or rejected orders do not create positions;
* a zero fill does not change cash or holdings;
* fees are handled consistently with the ledger’s existing accounting model;
* negative cash or holdings must not be introduced accidentally;
* event processing is transactional where the existing storage layer supports transactions;
* replaying the same event stream produces the same ledger state.

If the ledger currently has no explicit event-ingestion API, create a narrow adapter around it rather than rewriting its internal accounting model.

---

# Step 7 — Persistence and idempotency

Inspect the current database schema and migration strategy.

Add only the persistence required for the new boundary. Potential concepts include:

* paper execution intents;
* paper execution events;
* paper execution results;
* reconciliation results;
* idempotency keys;
* adapter/runtime version;
* raw broker status;
* failure reason.

Do not duplicate fields already available in recommendations or the paper ledger.

Hard requirements:

* one active paper intent per recommendation and execution version;
* repeated service invocation must not submit a second paper order;
* repeated callbacks must not duplicate fills;
* event IDs must be unique;
* recommendation-to-intent linkage must be queryable;
* intent-to-ledger-event linkage must be queryable;
* failures must be auditable;
* no migration may weaken frozen-recommendation triggers;
* no migration may enable `real_orders` writes.

Use database constraints where practical, not only application-level checks.

---

# Step 8 — Implement the orchestration service

Create a service similar to:

```text
src/trading_research/services/execute_paper_recommendation.py
```

Suggested responsibility:

```python
def execute_paper_recommendation(
    recommendation_id: str,
    *,
    recommendation_repository: RecommendationRepository,
    execution_repository: PaperExecutionRepository,
    ledger: PaperLedger,
    adapter: PaperExecutionAdapter,
    eligibility_policy: PaperExecutionEligibilityPolicy,
    clock: Clock,
) -> PaperExecutionOutcome:
    ...
```

Expected sequence:

1. Load the recommendation.
2. Confirm it exists.
3. Confirm it is frozen.
4. Evaluate paper-execution eligibility.
5. Return a recorded rejection result when ineligible.
6. Check for an existing idempotent intent.
7. Build the deterministic intent.
8. Persist the intent before external submission.
9. Submit through the paper adapter.
10. Normalize and persist execution events.
11. Apply eligible fill events to the internal ledger.
12. Reconcile adapter state and ledger state.
13. Persist the final outcome.
14. Return a domain result without exposing LumiBot objects.

Failure behavior:

* unknown recommendation → explicit not-found error;
* malformed recommendation → fail closed;
* adapter failure → record `ERROR`, do not invent a fill;
* unknown broker status → record error and require reconciliation;
* persistence failure → avoid submitting another order on blind retry;
* ledger failure after broker fill → retain event and surface reconciliation mismatch;
* interrupted execution → safely resumable from persisted intent and events.

---

# Step 9 — Keep live execution explicitly disabled

Define a framework-neutral future execution interface if it does not already exist:

```python
class LiveExecutionGateway(Protocol):
    def review_order(self, approved_order: ApprovedOrder) -> OrderReview:
        ...

    def place_order(
        self,
        approved_order: ApprovedOrder,
        human_approval: HumanApproval,
    ) -> BrokerOrder:
        ...

    def cancel_order(self, broker_order_id: str) -> BrokerOrder:
        ...

    def reconcile_order(self, broker_order_id: str) -> BrokerOrderState:
        ...
```

Implement only:

```python
class DisabledLiveExecutionGateway:
    ...
```

Every method must raise:

```python
LiveTradingDisabledError
```

The error should clearly state that live execution is disabled by policy and configuration.

Add or preserve configuration similar to:

```yaml
trading_mode: paper
live_trading_enabled: false
human_approval_required: true
```

Requirements:

* default mode is paper;
* an absent mode does not imply live;
* an unknown mode fails closed;
* environment variables cannot silently override repository policy;
* tests prove no code path can reach a live gateway;
* do not connect this gateway to Robinhood MCP in this milestone;
* do not expose Robinhood write tools.

---

# Step 10 — Deterministic offline simulation

Tests and local vertical-slice demonstrations must operate on fixtures.

Provide an injectable deterministic execution backend or test adapter that can simulate:

* full fill;
* partial fill followed by full fill;
* rejection;
* cancellation;
* adapter error;
* duplicate callback;
* out-of-order callback;
* unknown status;
* reconciliation mismatch.

This test backend is not a second production framework. It is a fixture-driven implementation of the internal adapter protocol.

Do not:

* fetch current quotes;
* call Robinhood;
* call Reddit;
* call an LLM;
* depend on network access;
* use random fill values without a fixed seed and persisted inputs;
* substitute a generic price when fixture data is missing.

---

# Step 11 — Optional command-line entry point

If the repository already uses a CLI pattern, add a safe paper command such as:

```bash
python -m trading_research.cli execute-paper \
  --recommendation-id <id>
```

The command must:

* show the selected mode;
* refuse to run unless the mode is explicitly paper;
* print eligibility reasons when rejected;
* print intent ID, result status, and reconciliation status;
* avoid printing secrets;
* avoid exposing raw internal database credentials;
* never contain a `--live` convenience flag.

Do not introduce a new CLI framework when the existing CLI can be extended cleanly.

---

# Step 12 — Tests

Use test-driven development.

Preserve all 169 existing tests.

Add focused unit tests for:

## Eligibility

* frozen valid `buy_candidate` is eligible;
* unfrozen recommendation rejected;
* `screened_out` rejected;
* `watch` rejected;
* `no_action` rejected;
* `analysis_incomplete` rejected;
* missing risk plan rejected;
* expired recommendation rejected;
* stale price rejected;
* unknown symbol rejected;
* global kill switch rejects;
* duplicate execution rejected.

## Intent construction

* deterministic intent creation;
* whole-share quantity;
* no quantity round-up;
* no zero or negative quantity;
* exact expected-notional reconstruction;
* missing price fails closed;
* market/limit validation;
* provenance fields preserved;
* stable idempotency key.

## LumiBot adapter boundary

* correct intent-to-order translation;
* known status mappings;
* unknown status fails closed;
* callbacks map to internal events;
* LumiBot objects do not reach repositories;
* float-to-Decimal boundary behavior is explicit and tested;
* adapter cannot operate in live mode.

## Ledger integration

* full fill updates ledger once;
* partial fills update incrementally;
* duplicate event ignored or rejected safely;
* cancelled order does not update position;
* rejected order does not update position;
* zero fill does not update position;
* replay is deterministic;
* fees are accounted for correctly;
* recommendation remains frozen.

## Persistence and idempotency

* repeated service invocation produces no second intent;
* repeated submission does not create a second order;
* duplicate callback does not create a second fill;
* event IDs are unique;
* intent links to recommendation;
* result links to events;
* reconciliation is persisted;
* transaction failure is handled safely.

## Live-trading protection

* disabled gateway rejects review;
* disabled gateway rejects placement;
* disabled gateway rejects cancellation;
* disabled gateway rejects modification;
* unknown trading mode fails closed;
* missing trading mode does not enable live;
* `real_orders` remains write-blocked;
* no Robinhood mutating tool appears in the allowed tool surface.

## Integration tests

Implement an offline vertical slice:

```text
fixture candidate
→ existing analyze_candidate service
→ frozen eligible recommendation
→ PAPER_ONLY eligibility
→ deterministic order intent
→ simulated LumiBot paper fill
→ internal ledger update
→ reconciliation MATCHED
```

Also test:

```text
analysis incomplete
→ frozen non-executable recommendation
→ no intent
→ no adapter submission
→ no ledger mutation
```

And:

```text
same recommendation executed twice
→ first execution succeeds
→ second invocation returns existing result
→ no duplicate order
→ no duplicate fill
```

Run targeted tests during development and the full suite at completion:

```bash
pytest tests/ -q
```

---

# Step 13 — Documentation

Create a developer guide consistent with the existing documentation style, such as:

```text
docs/milestones/milestone3-lumibot-paper-integration.md
```

Document:

* why the existing trading desk remains the authority;
* why LumiBot is behind an adapter;
* dependency setup;
* paper-mode configuration;
* the recommendation-to-paper-fill lifecycle;
* eligibility rules;
* idempotency behavior;
* event mappings;
* ledger reconciliation;
* failure recovery;
* offline testing;
* how to run the vertical slice;
* why live trading is disabled;
* known LumiBot boundary limitations;
* future Robinhood MCP integration;
* future Claude research-agent integration.

Add an architecture diagram in Mermaid when consistent with existing docs.

Also create or update an ADR describing:

* selection of LumiBot as the sole external trading runtime;
* rejection of a rewrite-from-scratch approach;
* separation between domain authority and runtime adapter;
* one portfolio-state owner;
* one execution owner;
* multiple future research contributors;
* why live execution remains disabled.

---

# Safety invariants

These are hard requirements:

* Do not create a new LumiBot-first repository.
* Do not replace the existing recommendation model.
* Do not replace the existing paper ledger.
* Do not let LumiBot own permanent portfolio state.
* Do not let LumiBot create recommendations.
* Do not let an LLM create orders.
* Do not submit any real order.
* Do not review or preview any real order.
* Do not call Robinhood write tools.
* Do not enable order mutation tools.
* Do not write to `real_orders`.
* Do not fabricate prices.
* Do not default missing financial values.
* Do not execute an incomplete recommendation.
* Do not execute an unfrozen recommendation.
* Do not execute the same recommendation twice.
* Do not apply the same fill twice.
* Do not silently map unknown broker statuses.
* Do not use current live data in historical tests.
* Do not introduce look-ahead bias.
* Do not log credentials, tokens, account numbers, or secrets.
* Do not weaken any Milestone 1 or Milestone 2 tests or safeguards.

---

# Suggested implementation order

Proceed in this order:

1. Inspect repository and run the 169-test baseline.
2. Produce a concise gap analysis.
3. Add framework-neutral paper-execution contracts.
4. Add eligibility policy.
5. Add persistence and constraints.
6. Add the adapter protocol and deterministic fake adapter.
7. Integrate normalized events with the existing paper ledger.
8. Implement the orchestration service.
9. Add the isolated LumiBot adapter.
10. Add reconciliation.
11. Add the disabled live gateway.
12. Add integration tests.
13. Add CLI support only if it fits existing patterns.
14. Add documentation and ADR.
15. Run the complete test suite.
16. Self-review for safety, idempotency, and framework leakage.

Keep each change small and testable.

Do not perform broad unrelated refactoring.

---

# Acceptance criteria

The milestone is complete only when all of the following are true:

1. The original 169 tests still pass.
2. All new tests pass.
3. The existing project remains the recommendation and policy authority.
4. LumiBot is isolated behind an internal adapter.
5. No LumiBot model appears in persisted domain contracts.
6. A valid frozen `buy_candidate` can create one paper intent.
7. Ineligible recommendation sides cannot create paper intents.
8. Paper execution events are normalized before entering the ledger.
9. Full and partial fills update the existing ledger correctly.
10. Duplicate callbacks do not duplicate fills.
11. Repeated service execution does not create duplicate orders.
12. Reconciliation can identify matched and mismatched states.
13. Unknown broker statuses fail closed.
14. Missing or stale prices cannot create orders.
15. The source recommendation remains immutable.
16. `real_orders` remains write-blocked.
17. The live gateway is disabled and tested.
18. Robinhood write tools remain inaccessible.
19. The complete vertical slice runs using fixtures without network access.
20. Documentation explains setup, architecture, operation, and safety behavior.
21. The final report truthfully distinguishes a real LumiBot integration from any fake/test adapter behavior.

---

# Required final response

At completion, provide:

1. Baseline verification.
2. Repository findings.
3. Architecture decisions.
4. LumiBot version and dependency changes.
5. Files created.
6. Files modified.
7. Database or migration changes.
8. Paper-execution lifecycle implemented.
9. Idempotency controls.
10. Ledger and reconciliation behavior.
11. Tests added.
12. Full test results.
13. Commands run.
14. Safety review.
15. Known limitations.
16. Any behavior that uses a deterministic fake rather than real LumiBot.
17. Recommended next milestone.

Also include a concise mapping:

```text
Requirement → implementation file → verifying test
```

Do not claim completion unless the full test suite passes or clearly report the exact remaining failures and their causes.
