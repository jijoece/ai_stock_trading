You are continuing implementation of my existing AI-driven trading-desk repository.

Milestone 3 completed the framework-neutral paper-execution pipeline and isolated LumiBot behind an adapter. Your task is to implement:

# Milestone 4 — Isolated credentialed paper broker and evaluation loop

This is a direct implementation task, not a research-only task.

Do not start a new trading-desk repository. Do not replace the existing domain model, paper ledger, recommendation lifecycle, execution contracts, or persistence layer.

The primary goal is to prove a genuine paper-broker round trip through LumiBot while keeping:

* the main trading-desk environment isolated from LumiBot’s dependency tree;
* the existing trading desk authoritative for recommendations, policy, ledger state, and evaluation;
* all real-money trading disabled;
* Robinhood write tools disabled;
* tests deterministic and runnable without network access or broker credentials.

Do not stop after creating a plan. Inspect the repository, implement the milestone, run tests, smoke-test the isolated runtime where credentials are available, and document the actual result honestly.

---

# Confirmed starting state

Milestone 3 completed with:

```text
With LumiBot installed:
pytest tests/ -q
308 passed
```

And with LumiBot absent:

```text
pytest tests/ -q
298 passed, 1 module skipped
```

Milestone 3 added:

* framework-neutral paper-execution contracts;
* paper-execution eligibility validation;
* deterministic intent construction;
* execution persistence and idempotency;
* deterministic fake paper adapter;
* LumiBot order and status translation;
* normalized execution events;
* paper-ledger event ingestion;
* reconciliation;
* disabled live-execution gateway;
* `execute-paper` CLI support;
* optional `paper` dependency group;
* architecture and implementation documentation.

Important paths include:

```text
src/trading_research/execution/
src/trading_research/runtime/
src/trading_research/runtime/lumibot/
src/trading_research/services/execute_paper_recommendation.py
src/trading_research/storage/execution_schema.py
src/trading_research/storage/execution_repositories.py
src/trading_research/paper/ledger.py
config/execution.yaml
docs/milestone3-lumibot-paper-integration.md
docs/adr/0001-lumibot-paper-runtime.md
```

Important existing invariants:

* the existing trading desk owns recommendations and policy;
* only frozen eligible recommendations can create paper intents;
* one paper intent per recommendation and execution version;
* duplicate callbacks cannot apply fills twice;
* normalized events enter the existing paper ledger;
* LumiBot objects cannot leak outside the isolated runtime package;
* unknown broker statuses fail closed;
* missing or stale prices cannot create orders;
* `real_orders` remains write-blocked;
* all live gateway methods raise `LiveTradingDisabledError`;
* Robinhood mutating tools remain inaccessible;
* deterministic fake adapters remain available for offline tests;
* the real LumiBot submission boundary has not yet been exercised against a credentialed paper broker.

Read and verify the actual repository before modifying anything. Treat this summary only as context.

---

# Core architectural decision

Because LumiBot 4.5.74 introduces a large dependency tree and conflicts with dependency floors in the main project, Milestone 4 must move the real LumiBot runtime behind a process boundary.

Use this ownership model:

```text
Main trading-desk process
├── Domain models
├── Recommendations
├── Screening and scoring
├── Deterministic risk
├── PaperOrderIntent
├── Internal paper ledger
├── Reconciliation
├── Evaluation
└── Runtime client
        │
        │ versioned JSON protocol
        ▼
Isolated LumiBot paper-runtime process
├── LumiBot dependency
├── Credentialed paper broker
├── Broker order submission
├── Broker status retrieval
├── Broker position/account reads
└── Raw broker events
```

The isolated runtime may use Alpaca paper trading or another broker natively supported by LumiBot, but the first implementation should prefer **Alpaca paper** unless repository configuration or verified LumiBot compatibility gives a strong reason to choose otherwise.

Robinhood is not the paper broker for this milestone.

---

# Milestone objectives

Implement a safe vertical slice that can:

1. Receive a previously persisted `PaperOrderIntent`.
2. Send it to an isolated LumiBot runtime.
3. Submit it to a credentialed paper broker.
4. Persist the broker acknowledgement before considering the order submitted.
5. Retrieve or receive order-state changes.
6. Normalize broker statuses into existing `PaperExecutionEvent` models.
7. Apply valid fills to the existing paper ledger.
8. Reconcile internal orders, fills, cash, and positions against broker state.
9. Resume safely after process restart or interrupted execution.
10. Compute forward-performance and benchmark evaluation records.
11. Keep all offline tests deterministic and credential-free.
12. Keep live-money execution structurally impossible.

The milestone must support real paper-broker round trips when credentials are supplied, but the default test suite must not require credentials or network access.

---

# Non-goals

Do not implement:

* Robinhood live execution;
* Robinhood order review or placement;
* real-money Alpaca trading;
* live brokerage mode;
* options;
* margin;
* short selling;
* fractional shares unless already explicitly supported and tested;
* autonomous LLM order submission;
* Claude research committee integration;
* TradingAgents;
* FinRL-X;
* LEAN;
* NautilusTrader;
* high-frequency trading;
* multi-broker smart routing;
* production deployment to a public cloud;
* Kubernetes;
* distributed queues unless clearly necessary;
* replacement of the deterministic fake adapter;
* replacement of the existing paper ledger;
* direct database access from the isolated LumiBot process.

The isolated runtime must not become an independent trading desk.

---

# Step 1 — Inspect the repository and establish the baseline

Before changing code:

1. Check Git status and current branch.
2. Read:

   * `docs/milestone3-lumibot-paper-integration.md`;
   * `docs/adr/0001-lumibot-paper-runtime.md`;
   * execution contracts;
   * runtime adapter protocol;
   * LumiBot adapter;
   * deterministic adapter;
   * execution service;
   * reconciliation code;
   * paper ledger;
   * execution persistence;
   * configuration loading;
   * CLI commands;
   * dependency declarations.
3. Run the main suite without requiring LumiBot:

```bash
pytest tests/ -q
```

4. Run the LumiBot-specific suite in the optional runtime environment when available.
5. Confirm the expected baseline:

   * approximately 308 tests with LumiBot installed;
   * approximately 298 tests plus one skip without LumiBot.

Report the actual baseline before editing. Do not hide unexpected failures.

---

# Step 2 — Decide the isolated-runtime protocol

Create a small, explicit, versioned protocol between the main process and the LumiBot runtime.

Prefer the simplest reliable local boundary:

1. JSON over stdin/stdout, or
2. a local-only HTTP service.

Default recommendation: use **JSON Lines over stdin/stdout** for the first implementation because it:

* avoids introducing a web framework;
* minimizes dependencies;
* is easy to test;
* can run as a child process;
* keeps the runtime local;
* provides a clear protocol boundary.

Use local HTTP only if the repository already has an established HTTP-service pattern.

Do not use Python object serialization or pickle.

Do not share database connections across the process boundary.

Define a protocol version, for example:

```text
paper-runtime.v1
```

Every request and response must include:

* protocol version;
* request ID;
* operation;
* timestamp;
* payload;
* runtime version;
* success/failure status;
* structured error code;
* retryability indicator.

Example request envelope:

```json
{
  "protocol_version": "paper-runtime.v1",
  "request_id": "req_...",
  "operation": "submit_order",
  "sent_at": "2026-07-12T18:00:00Z",
  "payload": {}
}
```

Example response envelope:

```json
{
  "protocol_version": "paper-runtime.v1",
  "request_id": "req_...",
  "operation": "submit_order",
  "runtime_version": "lumibot-runtime-1",
  "success": true,
  "retryable": false,
  "error": null,
  "payload": {}
}
```

Reject:

* unknown protocol versions;
* unknown operations;
* malformed payloads;
* extra fields that create ambiguity where strict validation is practical;
* responses with mismatched request IDs;
* responses that do not match the requested operation.

---

# Step 3 — Add runtime operations

Implement only the operations needed for reliable paper execution.

Suggested operations:

```text
health
capabilities
submit_order
get_order
list_open_orders
list_recent_orders
get_account
list_positions
cancel_paper_order
```

`cancel_paper_order` is allowed only for the credentialed paper broker, not for real-money trading.

Do not add generic arbitrary-tool execution.

Do not allow the main process to pass raw natural-language broker instructions.

## `health`

Returns:

* runtime availability;
* protocol version;
* runtime version;
* LumiBot version;
* configured broker type;
* broker mode;
* credential-presence indicators without exposing credentials;
* whether network submission is enabled;
* whether real-money mode is disabled.

## `capabilities`

Returns a fixed allowlist such as:

```json
{
  "supported_operations": [
    "health",
    "capabilities",
    "submit_order",
    "get_order",
    "list_open_orders",
    "list_recent_orders",
    "get_account",
    "list_positions",
    "cancel_paper_order"
  ],
  "supported_asset_types": ["equity"],
  "supported_sides": ["BUY"],
  "supported_order_types": ["MARKET", "LIMIT"],
  "fractional_shares": false,
  "short_selling": false,
  "options": false,
  "margin": false,
  "real_money": false
}
```

## `submit_order`

Accepts only the existing framework-neutral order-intent fields required by the runtime.

Validate again inside the isolated process:

* long-only;
* equity only;
* positive whole quantity;
* supported order type;
* valid limit price;
* unexpired intent;
* broker is configured in paper mode;
* real-money mode is false;
* intent ID and recommendation ID are present;
* idempotency key is present.

The runtime must not trust validation from the main process alone.

## `get_order`

Returns a normalized raw broker-order snapshot sufficient for the main process to map into internal execution events.

## `get_account`

Returns paper-account values required for reconciliation:

* cash;
* equity;
* buying power where applicable;
* currency;
* broker timestamp.

Do not let buying power enable margin assumptions in the domain layer.

## `list_positions`

Returns:

* symbol;
* quantity;
* average entry price;
* market value where available;
* broker timestamp.

## `cancel_paper_order`

Must:

* work only in paper mode;
* operate only on an existing broker order associated with a known internal intent;
* reject unknown orders;
* persist or return the resulting broker state;
* never expose a generic cancel-by-natural-language operation.

---

# Step 4 — Create a dedicated isolated runtime package or app

Use a layout similar to:

```text
paper_runtime/
├── pyproject.toml
├── README.md
├── src/
│   └── trading_paper_runtime/
│       ├── __init__.py
│       ├── main.py
│       ├── protocol.py
│       ├── dispatcher.py
│       ├── configuration.py
│       ├── broker_gateway.py
│       ├── lumibot_gateway.py
│       ├── models.py
│       ├── errors.py
│       └── logging_config.py
└── tests/
```

Or use an equivalent repository-consistent structure.

The isolated runtime should have its own dependency definition.

It may depend on:

* LumiBot;
* the minimum required validation/configuration libraries;
* broker SDK dependencies required by LumiBot.

It should not import the main trading-desk application package as a broad dependency.

If sharing protocol models is necessary, create a very small framework-neutral package or JSON schema with no LumiBot dependencies.

Avoid creating circular installation requirements.

---

# Step 5 — Environment and credential safety

The runtime must obtain paper-broker credentials only from environment variables or an existing approved secret mechanism.

For Alpaca paper trading, use environment-variable names appropriate to the verified broker integration. Do not guess names when implementing; inspect official LumiBot and broker documentation or source.

Hard requirements:

* paper endpoint only;
* no production endpoint fallback;
* missing endpoint configuration fails closed;
* missing credentials fail closed;
* malformed credentials fail closed;
* credentials never appear in logs;
* credentials never appear in exceptions returned to the main process;
* credentials never enter the database;
* credentials never appear in test fixtures;
* `.env` files remain ignored;
* provide a `.env.example` with placeholders only;
* health responses report credential presence as Boolean, never values.

Add startup validation proving the configured endpoint is the paper endpoint.

If the broker SDK exposes an explicit paper flag, require it.

If the broker configuration cannot conclusively prove paper mode, refuse to start submission operations.

---

# Step 6 — Implement the runtime client in the main project

Add a framework-neutral client such as:

```text
src/trading_research/runtime/client/
├── __init__.py
├── protocol.py
├── process_client.py
├── models.py
└── errors.py
```

Or equivalent.

The client must:

* start or connect to the isolated runtime;
* perform a health check;
* verify protocol compatibility;
* verify broker mode is paper;
* verify real-money capability is false;
* enforce request timeouts;
* validate response IDs and operation names;
* distinguish retryable and non-retryable failures;
* avoid blind retries for order submission;
* support safe process shutdown;
* capture stderr separately from protocol stdout;
* reject non-JSON protocol output;
* avoid logging secrets;
* avoid treating runtime process exit as a fill or rejection.

Do not automatically retry `submit_order` unless idempotency has been confirmed by checking existing persisted broker linkage.

---

# Step 7 — Credentialed broker gateway

Implement the isolated runtime’s real paper-broker gateway using LumiBot.

The gateway must:

* construct the verified LumiBot broker in paper mode;
* translate protocol requests into LumiBot/broker orders;
* submit a real paper order;
* return the broker-generated order ID;
* retrieve order status;
* list open/recent paper orders;
* retrieve account state;
* retrieve positions;
* cancel paper orders;
* preserve raw broker status;
* map broker errors into structured runtime errors;
* remain isolated from main-project storage.

Do not fabricate successful submission when the broker call fails.

Do not treat local LumiBot `Order` construction as broker acknowledgement.

A submission is successful only when the broker returns or exposes a broker-side identifier or verifiable accepted state.

---

# Step 8 — Submission idempotency and crash recovery

Milestone 3 already provides internal idempotency. Extend it across the process and broker boundary.

Use the internal intent ID as the primary client idempotency identity when the broker supports a client order ID.

Requirements:

* derive a stable broker client-order ID from `intent_id`;
* conform to broker length and character requirements;
* persist client-order ID before submission;
* persist broker-order ID immediately after acknowledgement;
* on ambiguous timeout, query the broker by client-order ID before retrying;
* never resubmit blindly after an unknown submission outcome;
* repeated `submit_order` requests for the same intent must return the existing broker order when found;
* detect conflicting reuse of an idempotency key with different order contents;
* record submission attempts and outcomes;
* support process restart without duplicating orders.

Define explicit states such as:

```text
PENDING_SUBMISSION
SUBMISSION_UNKNOWN
SUBMITTED
ACCEPTED
PARTIALLY_FILLED
FILLED
CANCELLED
REJECTED
ERROR
```

Do not collapse `SUBMISSION_UNKNOWN` into `ERROR` if the broker may have received the order.

---

# Step 9 — Broker event polling and recovery

Implement a safe polling mechanism in the main process or orchestration service.

It should:

1. Load unresolved paper intents.
2. Query the runtime for current broker order state.
3. Normalize new state into `PaperExecutionEvent`.
4. Persist events idempotently.
5. Apply newly observed fills to the internal paper ledger.
6. Reconcile state.
7. Mark terminal orders complete.

Support:

* accepted order;
* partially filled order;
* multiple partial fills;
* full fill;
* cancellation;
* rejection;
* expired order;
* broker-side unknown status;
* temporary runtime failure;
* temporary broker failure.

Use bounded polling and configurable intervals.

Do not create a busy loop.

For CLI execution, provide a bounded `--wait` or `--poll-until-terminal` behavior only if consistent with repository conventions.

Do not imply that the assistant or Claude Code will continue running after the command exits.

---

# Step 10 — Reconcile broker state and internal ledger

Extend reconciliation from event-level checks to account and position checks.

Add framework-neutral broker snapshot models if needed:

```python
@dataclass(frozen=True)
class BrokerAccountSnapshot:
    cash: Decimal
    equity: Decimal
    currency: str
    as_of: datetime


@dataclass(frozen=True)
class BrokerPositionSnapshot:
    symbol: str
    quantity: Decimal
    average_entry_price: Decimal
    market_value: Decimal | None
    as_of: datetime
```

Reconciliation should compare:

* broker order quantity versus internal intent;
* broker filled quantity versus persisted fill events;
* broker average fill price versus internal fills;
* broker position quantity versus paper-ledger position;
* broker cash versus internal paper-ledger cash;
* terminal broker status versus internal result;
* open broker orders versus unresolved internal intents.

Use configurable Decimal tolerances only where broker rounding makes exact equality impractical.

Do not silently repair mismatches.

Return and persist statuses such as:

```text
MATCHED
PENDING
ORDER_MISMATCH
POSITION_MISMATCH
CASH_MISMATCH
MISSING_INTERNAL_ORDER
MISSING_BROKER_ORDER
UNKNOWN
```

Persist:

* compared values;
* differences;
* tolerances;
* reasons;
* timestamps;
* broker snapshot ID;
* internal ledger version.

---

# Step 11 — Forward-performance evaluation

Implement an evaluation loop for frozen recommendations and completed paper executions.

The evaluation layer should remain independent of LumiBot.

Add or extend models and persistence for:

* evaluation horizon;
* recommendation price;
* execution price;
* benchmark price;
* ending symbol price;
* gross return;
* net return;
* benchmark return;
* excess return;
* slippage;
* fees;
* maximum favorable excursion where data supports it;
* maximum adverse excursion where data supports it;
* evaluation status;
* missing-data reasons;
* data-source timestamps;
* model version;
* prompt version where present;
* strategy/config hash;
* market regime where present.

Support horizons:

```text
1 trading day
5 trading days
10 trading days
20 trading days
60 trading days
```

Use SPY as the default benchmark unless existing configuration specifies otherwise.

All historical evaluation data must be point-in-time appropriate.

Do not use a current quote as a historical close.

Do not fill missing dates by inventing prices.

Use the next valid market session when a horizon lands on a holiday or weekend, with this rule documented and tested.

The evaluation service must support:

* pending evaluation;
* completed evaluation;
* incomplete due to missing market data;
* benchmark missing;
* delisted or unavailable symbol;
* recommendation never executed;
* partially filled recommendation.

---

# Step 12 — Portfolio and strategy metrics

Add deterministic aggregate metrics over completed evaluations.

Support, where sufficient data exists:

* hit rate;
* average return;
* median return;
* average gain;
* average loss;
* gain/loss ratio;
* cumulative return;
* benchmark-relative cumulative return;
* Sharpe ratio;
* Sortino ratio;
* maximum drawdown;
* Calmar ratio;
* turnover;
* average slippage;
* total fees;
* recommendation-to-fill rate;
* rejection rate;
* cancellation rate;
* average time to fill;
* confidence calibration;
* performance by recommendation side;
* performance by score bucket;
* performance by model;
* performance by prompt version;
* performance by configuration hash;
* performance by market regime.

Fail or return insufficient-data status when sample size is inadequate.

Do not return misleading zero values for undefined metrics.

Document annualization assumptions.

Use deterministic Decimal or carefully bounded numerical calculations consistent with existing project conventions.

---

# Step 13 — Market calendar and stale-order handling

Use a verified market-calendar source already available through LumiBot or a lightweight existing dependency.

Implement:

* market-open validation;
* next market-session calculation;
* trading-day horizon calculation;
* stale unfilled-order detection;
* configurable time-in-force;
* expiration handling;
* pre-market/after-hours policy;
* weekend and holiday behavior.

Default policy:

* regular-hours U.S. equities only;
* no extended-hours orders;
* long-only;
* limit orders preferred where existing configuration requires them;
* unfilled day orders expire at broker close;
* no automatic resubmission on the next day;
* expired recommendations require a new recommendation, not reuse of the old intent.

Do not submit an order when the market-session policy cannot be determined.

---

# Step 14 — Configuration

Add or extend configuration with explicit safe defaults.

Example:

```yaml
paper_runtime:
  protocol_version: paper-runtime.v1
  transport: stdio
  command:
    - python
    - -m
    - trading_paper_runtime
  startup_timeout_seconds: 15
  request_timeout_seconds: 30

paper_broker:
  provider: alpaca
  mode: paper
  real_money_enabled: false
  asset_types:
    - equity
  allowed_sides:
    - BUY
  allowed_order_types:
    - LIMIT
    - MARKET
  allow_fractional: false
  allow_shorting: false
  allow_margin: false
  allow_extended_hours: false

order_monitoring:
  poll_interval_seconds: 10
  max_poll_attempts: 30
  stale_order_minutes: 390

evaluation:
  benchmark: SPY
  horizons_trading_days:
    - 1
    - 5
    - 10
    - 20
    - 60
```

Requirements:

* absent broker mode does not imply paper;
* unknown mode fails closed;
* `real_money_enabled` must remain false;
* runtime and main-process configuration must agree;
* configuration mismatch prevents submission;
* environment variables may supply secrets but may not silently enable new capabilities;
* paper endpoint verification must be explicit.

---

# Step 15 — CLI commands

Extend the CLI only using existing conventions.

Suggested commands:

```bash
python -m trading_research.cli paper-runtime health

python -m trading_research.cli execute-paper \
  --recommendation-id <id> \
  --adapter credentialed

python -m trading_research.cli sync-paper-orders

python -m trading_research.cli reconcile-paper

python -m trading_research.cli evaluate-recommendations

python -m trading_research.cli paper-performance
```

Requirements:

* default adapter remains deterministic/offline unless explicitly configured;
* credentialed adapter requires successful runtime health and paper-mode verification;
* print the selected mode and broker;
* never print secrets;
* show request ID, intent ID, broker order ID, status, and reconciliation result;
* support dry inspection where appropriate;
* do not add a `--live` flag;
* do not allow Robinhood selection for order submission;
* use non-zero exit codes for failures.

---

# Step 16 — Testing strategy

Preserve all Milestone 1–3 tests.

The default main-project suite must run without:

* LumiBot;
* broker credentials;
* network access;
* the isolated runtime process;
* Robinhood;
* Reddit;
* an LLM.

Use multiple test layers.

## A. Protocol unit tests

Test:

* valid request/response envelopes;
* protocol-version mismatch;
* request-ID mismatch;
* operation mismatch;
* malformed JSON;
* unknown operation;
* missing required fields;
* structured retryable errors;
* secret redaction;
* strict capability validation.

## B. Runtime client tests

Use a fake subprocess or fake transport.

Test:

* startup health check;
* startup timeout;
* request timeout;
* runtime crash;
* stderr separation;
* non-JSON stdout;
* mismatched request ID;
* incompatible capabilities;
* non-paper broker mode rejection;
* real-money capability rejection;
* safe shutdown;
* no blind submit retry.

## C. Isolated runtime tests

Mock the broker boundary.

Test:

* paper-mode startup;
* production endpoint rejection;
* missing credentials;
* submit validation;
* long-only enforcement;
* no fractional quantity;
* no shorting;
* no options;
* no margin;
* client-order-ID idempotency;
* ambiguous timeout recovery;
* duplicate submit returns existing order;
* cancel paper order;
* account snapshot;
* position snapshot;
* unknown broker status;
* credential redaction.

## D. LumiBot translation tests

Guard with `pytest.importorskip` only inside the isolated-runtime or LumiBot-specific test set.

Test:

* framework-neutral order to real LumiBot order translation;
* LumiBot order/status to protocol response translation;
* broker identifier extraction;
* supported order types;
* invalid asset type rejection;
* float/Decimal boundary behavior;
* raw status retention.

## E. Main orchestration tests

Using deterministic transports, test:

* successful broker acknowledgement;
* accepted then filled;
* multiple partial fills;
* rejection;
* cancellation;
* order expiration;
* runtime temporarily unavailable;
* ambiguous submission;
* restart recovery;
* duplicate service execution;
* duplicate callback;
* unknown broker status;
* ledger failure after broker fill;
* reconciliation mismatch.

## F. Reconciliation tests

Test:

* exact match;
* quantity mismatch;
* fill-price mismatch;
* cash mismatch;
* position mismatch;
* missing broker order;
* missing internal order;
* pending order;
* configured rounding tolerance;
* mismatch persistence.

## G. Evaluation tests

Test:

* 1/5/10/20/60 trading-day horizons;
* weekend adjustment;
* market holiday adjustment;
* missing price;
* missing benchmark;
* no execution;
* partial execution;
* gross versus net return;
* slippage;
* benchmark-relative return;
* no look-ahead;
* idempotent recomputation;
* historical data revision handling.

## H. Metrics tests

Test:

* hit rate;
* gain/loss ratio;
* Sharpe;
* Sortino;
* maximum drawdown;
* Calmar;
* turnover;
* confidence calibration;
* insufficient-data behavior;
* undefined metric behavior;
* grouping by model/configuration/regime.

## I. Real paper-broker smoke tests

Mark these tests separately, for example:

```text
@pytest.mark.paper_broker
```

They must:

* be excluded from the default suite;
* require explicit environment enablement;
* require paper credentials;
* verify paper endpoint;
* use a very small configured notional;
* preferably submit a non-marketable limit order and cancel it, or use the safest verified paper-order flow;
* never run against a production endpoint;
* clean up open paper orders;
* record broker order IDs;
* avoid claiming a fill when only acknowledgement was tested.

Do not run a real paper-broker smoke test automatically when credentials happen to exist. Require an explicit opt-in flag such as:

```text
RUN_PAPER_BROKER_TESTS=true
```

---

# Step 17 — Genuine paper-broker validation

Where valid paper credentials are available and explicit opt-in is enabled, perform a controlled smoke test.

Preferred sequence:

1. Health check.
2. Verify paper endpoint.
3. Verify real-money capability is false.
4. Retrieve paper account snapshot.
5. Submit one small, non-marketable limit order for a highly liquid allowed equity.
6. Confirm broker acknowledgement and broker order ID.
7. Retrieve order state.
8. Cancel the paper order.
9. Confirm cancellation.
10. Reconcile no fill and no position change.
11. Persist the test outcome without secrets.

Do not choose the symbol or price dynamically through an LLM.

Use explicit test configuration.

If the market is closed, the smoke test may still validate acknowledgement and cancellation where the broker supports queued paper orders. Document the observed behavior.

If no credentials are available, complete all implementation and offline tests, then report that the credentialed round trip was not executed. Do not fabricate success.

---

# Step 18 — Documentation and ADR

Create:

```text
docs/milestone4-isolated-paper-broker.md
docs/adr/0002-isolated-lumibot-runtime.md
```

Document:

* why the process boundary exists;
* dependency-conflict findings from Milestone 3;
* protocol design;
* runtime operations;
* credential handling;
* paper endpoint enforcement;
* idempotency;
* ambiguous-submission recovery;
* event polling;
* account and position reconciliation;
* market-calendar policy;
* evaluation horizons;
* aggregate metrics;
* CLI usage;
* offline-test mode;
* opt-in credentialed smoke tests;
* known limitations;
* recovery procedures;
* future Robinhood assisted-live path.

Include Mermaid diagrams for:

1. process architecture;
2. submit-order sequence;
3. ambiguous-timeout recovery;
4. reconciliation flow;
5. evaluation lifecycle.

Update the main README or developer setup documentation only where necessary.

---

# Safety requirements

These are hard requirements:

* The main trading-desk process must not directly import LumiBot.
* The isolated runtime must not access the main database.
* No Python object serialization across the process boundary.
* No credentials in code, logs, responses, database records, fixtures, or docs.
* No production broker endpoint.
* No real-money trading mode.
* No fallback from paper endpoint to production.
* No Robinhood write operations.
* No options.
* No short selling.
* No margin.
* No fractional shares unless explicitly implemented later.
* No extended-hours execution by default.
* No natural-language execution instructions.
* No arbitrary remote operation invocation.
* No blind retry after ambiguous submission.
* No fabricated broker acknowledgement.
* No fabricated prices or fills.
* No duplicate broker submission.
* No duplicate ledger fill.
* No silent reconciliation repair.
* No current-price substitution for historical evaluation.
* No look-ahead bias.
* No weakening of frozen-recommendation protections.
* No writes to `real_orders`.
* No deletion or weakening of existing tests.

---

# Suggested implementation order

Proceed in this order:

1. Inspect the repository and run the baseline.
2. Produce a concise gap analysis.
3. Write the process-isolation ADR.
4. Define protocol envelopes and operations.
5. Build a fake protocol runtime and client tests.
6. Create the isolated runtime package and dependency environment.
7. Add runtime startup and credential validation.
8. Implement the LumiBot paper-broker gateway.
9. Add stable client-order-ID idempotency.
10. Implement main-process runtime client.
11. Integrate credentialed submission into the existing execution service.
12. Add order polling and restart recovery.
13. Extend reconciliation to orders, positions, and cash.
14. Add market-calendar and stale-order policies.
15. Implement forward evaluation.
16. Implement aggregate metrics.
17. Add CLI commands.
18. Add opt-in paper-broker smoke tests.
19. Run all offline suites.
20. Run the credentialed smoke test only when explicitly enabled.
21. Complete documentation.
22. Self-review for safety, dependency leakage, idempotency, and secret exposure.

Avoid broad unrelated refactoring.

---

# Acceptance criteria

Milestone 4 is complete only when:

1. All Milestone 1–3 tests still pass.
2. All new offline tests pass without LumiBot, credentials, or network access in the main environment.
3. LumiBot runs in a separate environment or process.
4. The main process communicates through a versioned JSON protocol.
5. The protocol rejects unknown versions and operations.
6. The runtime proves paper mode before allowing submission.
7. Production endpoints are rejected.
8. Credentials are never exposed.
9. A persisted paper intent can be submitted to the isolated runtime.
10. Broker acknowledgement is distinguished from local order construction.
11. Broker order IDs and client-order IDs are persisted.
12. Duplicate requests do not create duplicate paper orders.
13. Ambiguous submission outcomes trigger lookup, not blind retry.
14. Order-state polling supports partial and terminal states.
15. Broker fills normalize into existing internal execution events.
16. The existing paper ledger remains authoritative.
17. Account, order, cash, and position reconciliation is implemented.
18. Reconciliation mismatches are persisted and surfaced.
19. Market-session and stale-order policies are deterministic.
20. Forward evaluation supports 1, 5, 10, 20, and 60 trading-day horizons.
21. Benchmark-relative metrics are implemented.
22. Aggregate performance metrics fail safely with insufficient data.
23. Credentialed smoke tests are explicit opt-in only.
24. Real-money trading remains structurally disabled.
25. Robinhood write tools remain inaccessible.
26. `real_orders` remains write-blocked.
27. Documentation clearly distinguishes:

    * deterministic fake behavior;
    * real LumiBot object translation;
    * real paper-broker acknowledgement;
    * real paper fill, if one was actually observed.
28. The final report does not claim a credentialed round trip unless it was actually run successfully.

---

# Required final response

At completion, provide:

1. Baseline verification.
2. Repository and gap-analysis findings.
3. Process-isolation architecture.
4. Protocol operations and version.
5. Dependency and environment changes.
6. Broker selected and why.
7. Files created.
8. Files modified.
9. Schema and migration changes.
10. Credential and endpoint protections.
11. Idempotency and ambiguous-submission behavior.
12. Order polling and restart recovery.
13. Ledger integration.
14. Reconciliation behavior.
15. Market-calendar behavior.
16. Evaluation and metrics implemented.
17. CLI commands added.
18. Tests added.
19. Default-suite results.
20. Isolated-runtime test results.
21. Credentialed smoke-test result:

    * completed successfully;
    * skipped because no credentials;
    * or failed with the exact reason.
22. Commands run.
23. Safety review.
24. Known limitations.
25. Recommended Milestone 5.

Include a concise mapping:

```text
Requirement → implementation file → verifying test
```

Also clearly label each implementation area as one of:

```text
OFFLINE-DETERMINISTIC
REAL-LUMIBOT-TRANSLATION
REAL-PAPER-BROKER-ACKNOWLEDGEMENT
REAL-PAPER-BROKER-FILL
```

Do not claim Milestone 4 is fully complete if the acceptance criteria requiring a genuine credentialed paper-broker acknowledgement were not executed. In that case, report the code milestone as implemented and the environmental validation as pending.
