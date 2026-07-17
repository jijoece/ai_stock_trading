# Milestone 11 — Isolated Alpaca paper-broker integration and final execution closure

Work directly in the existing `ai_stock_trading` repository.

Milestones through 10 should already provide:

* isolated BASELINE and ENHANCED local paper books;
* deterministic paper risk decisions;
* local simulated order, fill, cash, position, lot, valuation, and reconciliation state;
* resumable controlled-soak campaigns;
* point-in-time-safe activation reviews;
* controlled recurring local-paper scheduling;
* explicit activation and cycle queues;
* singleton leases and safety pauses;
* an isolated `paper_runtime` process containing the existing LumiBot/Alpaca paper boundary.

Milestone 11 must add an optional **external Alpaca paper-account execution path** while preserving local simulation as the default.

This milestone is paper trading only.

Do not implement live trading.

---

# Objective

Implement:

```text
approved paper-book order intent
→ explicit operator preview
→ explicit operator submit
→ isolated paper_runtime process
→ Alpaca PAPER endpoint only
→ ambiguity-safe order state
→ broker fills
→ book-scoped ledger application
→ broker/local reconciliation
→ immutable execution evidence
```

The external broker path must:

* ship disabled;
* require explicit configuration;
* require an explicit operator command;
* support only one safely isolated paper book unless distinct paper accounts are configured;
* never be activated solely by credentials;
* never be called automatically by the recurring scheduler in this milestone.

---

# Working mode

You are a coding agent with direct repository access.

Use repository tools to:

* inspect symbols and references;
* edit source and configuration;
* add additive SQLite schema;
* extend the isolated paper runtime;
* run focused tests;
* run final test suites.

Implement directly in the repository.

Do not return only a hypothetical patch.



---

# Token-efficiency requirements

1. Verify Milestones 9.3.1 and 10 are present.
2. Use symbol search and references before opening full files.
3. Reuse existing paper runtime, execution protocols, subprocess transport, paper-book models, and reconciliation code.
4. Do not create a second independent Alpaca client in the main process.
5. Read only directly relevant files.
6. Keep the scratchpad concise.
7. Run targeted tests during implementation.
8. Run the full main suite only:

   * once for baseline;
   * once at completion.
9. Run the paper-runtime suite only:

   * once for baseline;
   * once at completion.
10. Use:

```bash
pytest -q --tb=short
```

11. Do not print full successful-test lists.
12. Do not make real broker or network calls.
13. Avoid broad refactoring.
14. Stop once acceptance criteria are met.

---

# Prerequisite verification

Confirm the repository contains equivalents of:

```text
src/trading_research/paper_books/recurring_scheduler.py
src/trading_research/paper_books/soak_campaign.py
src/trading_research/paper_books/controlled_soak_readiness.py
src/trading_research/paper_books/cross_book_verification.py

paper_runtime/src/trading_paper_runtime/
```

Confirm Milestone 10 provides:

* recurring execution disabled by default;
* explicit activation events;
* an explicit cycle queue;
* scheduler-run evidence;
* no external broker call from the scheduler.

Confirm Milestone 9.3.1 provides:

* campaign attempts;
* point-in-time-safe activation reviews;
* canonical UTC timestamps;
* qualifying real-provider history;
* crash-safe campaign recovery.

When names differ, trace and use actual repository symbols.

When prerequisites are materially missing, stop and report exact missing symbols. Do not recreate Milestones 9.3.1 or 10 inside this task.

---

# Initial files to inspect

Inspect relevant symbols only in:

```text
src/trading_research/execution/adapter_protocol.py
src/trading_research/execution/models.py
src/trading_research/execution/config.py
src/trading_research/execution/reconciliation.py
src/trading_research/execution/ledger_events.py

src/trading_research/services/execute_paper_recommendation.py

src/trading_research/paper_books/models.py
src/trading_research/paper_books/config.py
src/trading_research/paper_books/risk.py
src/trading_research/paper_books/execution.py
src/trading_research/paper_books/reconciliation.py
src/trading_research/paper_books/recurring_scheduler.py
src/trading_research/paper_books/cli_support.py

src/trading_research/runtime/client/
src/trading_research/runtime/lumibot/

src/trading_research/storage/execution_schema.py
src/trading_research/storage/execution_repositories.py
src/trading_research/storage/paper_books_schema.py
src/trading_research/storage/paper_books_repositories.py
src/trading_research/storage/database.py

src/trading_research/cli.py

paper_runtime/src/trading_paper_runtime/configuration.py
paper_runtime/src/trading_paper_runtime/models.py
paper_runtime/src/trading_paper_runtime/
paper_runtime/tests/

config/execution.yaml
config/paper_books.yaml
config/paper_runtime.yaml
```

Read relevant documentation only:

```text
docs/milestone4-isolated-paper-broker.md
docs/milestone10-controlled-recurring-local-paper.md
docs/runbooks/recurring-local-paper-trading.md
```

Use existing implementation as the source of truth.

---

# Scratchpad

Create:

```text
.codex/scratchpads/milestone11-progress.md
```

Use only:

```markdown
# Milestone 11 Progress

## Baseline
## Prerequisite verification
## Existing runtime boundary
## Account isolation
## Submission state machine
## Runtime protocol
## Reconciliation
## CLI and operator controls
## Tests
## Documentation
## Safety review
## Known limitations
## Final status
```

Do not store credentials, account numbers, raw broker payloads, private reasoning, or large source excerpts.

---

# Baseline

Run:

```bash
pytest tests/ -q --tb=short
```

Then:

```bash
cd paper_runtime
pytest tests/ -q --tb=short
cd ..
```

Record summarized results.

Check Git status and preserve unrelated work.

---

# Hard boundaries

Do not:

* support Alpaca live endpoints;
* support Robinhood mutation;
* add a live trading mode;
* add a `--live` flag;
* add an environment variable that enables live execution;
* accept `https://api.alpaca.markets`;
* place real or paper orders during tests;
* make network calls in the default test suites;
* use market orders;
* use notional orders;
* use fractional quantities unless already safely supported and explicitly required;
* use options;
* short;
* use margin;
* permit an opening SELL order;
* permit selling more than the book’s confirmed long position;
* permit the main process to read Alpaca broker credentials;
* permit Claude or an LLM to invoke broker submission;
* infer operator approval from natural-language model output;
* enable external submission merely because credentials exist;
* automatically connect the recurring scheduler to external submission;
* blindly retry an ambiguous order submission;
* silently combine BASELINE and ENHANCED activity in one broker account;
* modify or remove the disabled live gateway;
* weaken current paper-book risk limits;
* auto-install launchd or cron;


---

# Part 1 — Architecture decision

Reuse the existing isolated `paper_runtime` process.

Required architecture:

```text
main process
    owns:
        campaign/scheduler state
        paper-book risk decision
        approved paper intent
        book identity
        local audit and reconciliation

isolated paper_runtime
    owns:
        Alpaca paper credentials
        paper endpoint validation
        broker request/response handling
        broker order lookup
        normalized broker events
```

The main process must not:

* import LumiBot;
* import an Alpaca trading SDK;
* read `ALPACA_API_KEY`;
* read `ALPACA_API_SECRET`;
* construct broker authorization headers.

The runtime must return bounded normalized messages, not SDK objects or arbitrary raw broker payloads.

---

# Part 2 — Disabled-by-default configuration

Extend paper-book configuration conceptually:

```yaml
paper_books:
  external_broker:
    enabled: false
    provider: alpaca_paper
    allow_order_submission: false

    enabled_book_ids: []

    require_explicit_preview: true
    require_recent_preview_seconds: 300
    maximum_order_notional_usd: "50.00"

    permitted_order_types:
      - limit

    permitted_time_in_force:
      - day
```

Use the repository’s current risk limit when it is stricter than the external limit.

Runtime configuration should enforce:

```yaml
paper_runtime:
  broker:
    provider: alpaca
    environment: paper
    base_url: https://paper-api.alpaca.markets
```

Requirements:

* external broker disabled by default;
* order submission independently disabled by default;
* credentials cannot enable either flag;
* `enabled_book_ids` empty by default;
* unknown keys fail closed;
* strict booleans;
* positive bounded values;
* exact base-host allowlist;
* any live or unknown hostname rejected;
* no configuration fallback from paper to live;
* configuration hash persisted with every external execution attempt.

---

# Part 3 — Paper-account and book isolation

Do not claim two books are isolated when using one external paper account.

Preferred safe implementation:

```text
one external paper account
→ one externally enabled book
```

Requirements:

* when only one credential set is configured, permit at most one book in `enabled_book_ids`;
* reject simultaneous external execution for BASELINE and ENHANCED through one account;
* local simulation may continue for the other book;
* book identity must be included in every client order ID and persisted external-order record;
* broker account identity must be verified before submission;
* do not persist the raw broker account number;
* persist a stable non-secret account fingerprint, such as a salted or one-way hash of the broker account identifier;
* reconciliation must fail if the account fingerprint changes unexpectedly.

Only support multiple externally enabled books when the existing runtime already has a secure, explicit separate-account configuration per book. Do not invent implicit credential mapping.

---

# Part 4 — Broker-neutral normalized protocol

Create or extend a narrow paper-broker protocol with normalized models.

Conceptual operations:

```python
class ExternalPaperBroker(Protocol):
    def account_check(...)
    def preview_limit_order(...)
    def submit_limit_order(...)
    def get_order_by_client_order_id(...)
    def get_order_by_broker_order_id(...)
    def cancel_order(...)
    def list_fills(...)
    def get_positions(...)
    def get_account_snapshot(...)
```

Every operation must be explicitly paper scoped.

Normalized models should include only required bounded fields:

```text
provider
environment
account_fingerprint
book_id
client_order_id
broker_order_id
symbol
side
quantity
limit_price
time_in_force
status
submitted_at
updated_at
filled_quantity
average_fill_price
rejection_code
```

Do not expose:

* credentials;
* authorization headers;
* full raw responses;
* unnecessary account data;
* buying power beyond what is needed for a bounded account check.

---

# Part 5 — Runtime process protocol

Extend the existing JSON Lines protocol rather than introducing another transport.

Suggested protocol version:

```text
paper-runtime.v2
```

Supported request types:

```text
ACCOUNT_CHECK
PREVIEW_LIMIT_ORDER
SUBMIT_LIMIT_ORDER
GET_ORDER_BY_CLIENT_ID
GET_ORDER
CANCEL_ORDER
LIST_ORDER_FILLS
GET_POSITIONS
GET_ACCOUNT_SNAPSHOT
```

Requirements:

* strict request schema;
* strict response schema;
* bounded payload size;
* request ID required;
* book ID required where relevant;
* protocol version required;
* unknown operation rejected;
* unknown fields rejected where practical;
* no arbitrary method names;
* no arbitrary URLs;
* no shell execution;
* no credentials returned;
* sanitized error types;
* runtime exit or malformed response fails closed.

Use the existing process client, timeout, and subprocess-isolation patterns.

---

# Part 6 — Allowed order constraints

External order submission supports only:

```text
asset class: US equities
direction: long-only
entry side: BUY
exit side: SELL only to close confirmed long quantity
order type: LIMIT
time in force: DAY
quantity: positive whole shares
```

Reject:

* market orders;
* stop orders;
* stop-limit orders;
* trailing stops;
* bracket orders;
* notional orders;
* fractional orders;
* short sales;
* options;
* crypto;
* extended-hours orders;
* order replacement in this milestone;
* quantities exceeding the approved paper intent;
* prices different from the approved and previewed limit price;
* stale intents;
* inactive books;
* paused or killed state;
* unresolved critical alerts;
* book/account mismatch.

---

# Part 7 — Deterministic client-order identity

Generate a deterministic client order ID from immutable approved inputs:

```text
book ID
paper intent ID
symbol
side
quantity
limit price
execution policy version
```

Requirements:

* valid for Alpaca’s client-order-ID constraints;
* deterministic for identical approved payload;
* collision resistant;
* includes a readable book prefix where possible;
* payload hash persisted separately;
* any material input drift produces a different identity;
* repeated submission of the identical intent resolves through lookup, not blind resubmission.

Do not use random UUIDs as the only order identity.

---

# Part 8 — External submission state machine

Implement an append-only or strongly audited state machine.

Required states:

```text
NOT_SUBMITTED
PREVIEWED
SUBMISSION_REQUESTED
SUBMITTED
PARTIALLY_FILLED
FILLED
CANCEL_REQUESTED
CANCELLED
REJECTED
EXPIRED
UNKNOWN_REQUIRES_RECONCILIATION
```

Suggested table:

```text
paper_external_order_events
```

Suggested fields:

```text
external_order_event_id
external_order_scope_id
book_id
paper_order_intent_id
client_order_id
broker_order_id
account_fingerprint
previous_state
new_state
payload_hash
quantity
limit_price
operator
reason
runtime_request_id
error_code
created_at
policy_version
config_hash
```

Requirements:

* persist `SUBMISSION_REQUESTED` before the runtime submission call;
* state events immutable;
* current state derived from latest valid event;
* preview cannot mutate broker state;
* submission requires a recent matching preview when configured;
* rejection remains auditable;
* cancellation is a separate explicit state transition;
* fills may transition through `PARTIALLY_FILLED`;
* no state may jump directly from `NOT_SUBMITTED` to `FILLED`;
* impossible transitions rejected.

---

# Part 9 — Ambiguous submission handling

This is mandatory.

A timeout, runtime crash, broken pipe, malformed response, HTTP uncertainty, or unknown broker response after `SUBMISSION_REQUESTED` must produce:

```text
UNKNOWN_REQUIRES_RECONCILIATION
```

Never automatically call `submit` again.

Recovery flow:

```text
UNKNOWN_REQUIRES_RECONCILIATION
→ query broker by deterministic client_order_id
```

Possible results:

```text
broker order found
→ persist SUBMITTED/PARTIALLY_FILLED/FILLED/REJECTED/CANCELLED

authoritative broker NOT_FOUND
→ persist NOT_FOUND_CONFIRMED evidence
→ require another explicit operator retry command

broker result still uncertain
→ remain UNKNOWN_REQUIRES_RECONCILIATION
```

Requirements:

* no blind retry;
* no retry loop;
* no retry merely because the process restarted;
* lookup before any repeated submit;
* operator retry requires operator, reason, and the same frozen payload;
* retry count bounded;
* prior attempts remain immutable;
* duplicate broker orders prevented through client-order-ID lookup and broker idempotency behavior.

---

# Part 10 — Preview workflow

Preview is a deterministic local and runtime preflight, not a broker order.

A preview should validate:

```text
book active
external broker enabled for book
paper intent exists
paper intent approved
intent not already externally completed
symbol and side valid
quantity within approved amount
limit price exact
order notional within all configured limits
account fingerprint matches
paper endpoint verified
market-session policy known
no blocking pause/kill/alert/readiness state
```

Persist a bounded preview event:

```text
preview_id
paper_order_intent_id
payload_hash
book_id
account_fingerprint
previewed_at
expires_at
operator
result
reasons
```

Submission must reject:

* missing preview;
* failed preview;
* expired preview;
* payload drift after preview;
* changed account fingerprint;
* changed risk state that invalidates submission.

Preview must never call a broker mutation endpoint.

---

# Part 11 — Main-process execution coordinator

Create a focused service, conceptually:

```text
src/trading_research/paper_books/external_broker.py
```

Possible entry points:

```python
preview_external_paper_order(...)
submit_external_paper_order(...)
reconcile_external_paper_order(...)
cancel_external_paper_order(...)
```

Processing order for submission:

```text
load frozen paper intent
→ validate config and enabled book
→ load recent successful preview
→ revalidate current safety state
→ verify payload hash
→ persist SUBMISSION_REQUESTED
→ invoke isolated runtime
→ normalize response
→ persist resulting state
→ retrieve broker fills
→ apply new broker events idempotently
→ reconcile
```

Do not invoke CLI functions from this service.

---

# Part 12 — Ledger application

External paper mode must not also generate a local simulated fill for the same order.

For an externally executed intent:

* broker fills are authoritative execution events;
* apply only newly observed fills;
* use deterministic fill IDs derived from broker fill identity;
* persist each normalized fill before applying it;
* apply fills to the correct book only;
* maintain cash reservations and settlement consistently;
* reject negative or impossible quantities;
* reject fill symbols that differ from the intent;
* reject fills that exceed the approved order quantity;
* do not overwrite historical fills;
* use compensating events for corrections.

Local simulation remains the default provider for all books not explicitly external-enabled.

---

# Part 13 — Reconciliation

Reconcile:

```text
paper intent
external-order state
client order ID
broker order ID
broker fills
book cash ledger
book position lots
book aggregate position
broker position
account fingerprint
```

Required statuses:

```text
MATCHED
ORDER_MISSING_LOCALLY
ORDER_MISSING_AT_BROKER
AMBIGUOUS_SUBMISSION
BROKER_ORDER_DUPLICATE
BOOK_NAMESPACE_MISMATCH
ACCOUNT_FINGERPRINT_MISMATCH
SYMBOL_MISMATCH
SIDE_MISMATCH
QUANTITY_MISMATCH
FILL_QUANTITY_MISMATCH
PRICE_MISMATCH
CASH_MISMATCH
POSITION_MISMATCH
UNKNOWN
```

Requirements:

* reconciliation is read-only except for persisting reconciliation evidence;
* no history mutation to force a match;
* a mismatch raises or persists an operational alert according to existing patterns;
* a critical mismatch blocks further external submission;
* reconciliation history immutable;
* results bounded and sanitized.

---

# Part 14 — Recurring scheduler boundary

Do not automatically submit queued recurring paper intents to Alpaca.

Milestone 10 recurring behavior remains:

```text
local simulated paper execution only
```

When a book is configured for external execution, the scheduler may at most:

* generate an externally eligible paper intent;
* place it into a manual external-submission queue;
* persist `AWAITING_OPERATOR_EXTERNAL_SUBMISSION`.

It must not call:

```text
SUBMIT_LIMIT_ORDER
CANCEL_ORDER
```

Add an explicit test proving recurring scheduler invocation never calls the external runtime mutation operations.

Automatic external paper submission is deferred.

---

# Part 15 — Operator CLI

Add:

```bash
python -m trading_research.cli external-paper-account-check \
  --book-id BASELINE

python -m trading_research.cli external-paper-preview \
  --book-id BASELINE \
  --intent-id <id> \
  --operator <name>

python -m trading_research.cli external-paper-submit \
  --book-id BASELINE \
  --intent-id <id> \
  --preview-id <id> \
  --operator <name> \
  --reason "<reason>"

python -m trading_research.cli external-paper-order-show \
  --book-id BASELINE \
  --client-order-id <id>

python -m trading_research.cli external-paper-reconcile \
  --book-id BASELINE \
  [--client-order-id <id>]

python -m trading_research.cli external-paper-cancel \
  --book-id BASELINE \
  --client-order-id <id> \
  --operator <name> \
  --reason "<reason>"

python -m trading_research.cli external-paper-retry-submit \
  --book-id BASELINE \
  --intent-id <id> \
  --operator <name> \
  --reason "<reason>"
```

Retry command requirements:

* allowed only after authoritative broker `NOT_FOUND`;
* must reuse the frozen payload;
* must not bypass preview validity unless a new preview is explicitly required;
* must be bounded;
* must be audited.

CLI output:

* structured bounded JSON;
* no raw broker response;
* no credentials;
* no account number;
* no authorization headers;
* errors mapped to stable codes;
* nonzero exit for failed mutating commands.

---

# Part 16 — Credential and endpoint safety

Only `paper_runtime` may read:

```text
ALPACA_API_KEY
ALPACA_API_SECRET
ALPACA_IS_PAPER
```

Runtime must require:

```text
ALPACA_IS_PAPER=true
```

and exact paper endpoint verification.

Reject:

```text
api.alpaca.markets
broker-api.alpaca.markets
localhost endpoint overrides
HTTP endpoints
unknown proxy URLs
runtime-supplied arbitrary base URLs
```

Do not print or persist:

* API key;
* API secret;
* authorization headers;
* raw account ID;
* full HTTP request/response bodies;
* credential-bearing URLs.

Register secrets with existing redaction utilities where applicable.

Pass a minimal explicit environment to the runtime subprocess rather than the entire parent environment when possible.

---

# Part 17 — Database and transaction behavior

Use the repository’s WAL and bounded busy-timeout setup from Milestone 9.3.1.

Submission transaction boundaries must ensure:

```text
SUBMISSION_REQUESTED persisted
before runtime mutation call
```

Do not hold a SQLite write transaction open during a network call.

Recommended sequence:

```text
transaction 1:
    validate frozen state
    persist SUBMISSION_REQUESTED
    commit

runtime call:
    no DB transaction held

transaction 2:
    persist normalized broker response
    persist external state event
    persist fills
    apply idempotent ledger events
    persist reconciliation
    commit
```

Crash recovery must recognize any state left between these stages.

---

# Part 18 — Tests

Use fakes and mocked process transports only.

## Configuration

* external broker absent means disabled;
* external broker explicitly disabled;
* credentials do not enable it;
* submission flag false blocks;
* unknown provider rejected;
* live endpoint rejected;
* HTTP endpoint rejected;
* multiple books with one account rejected;
* strict booleans enforced.

## Account isolation

* enabled book maps to expected account fingerprint;
* account fingerprint mismatch blocks;
* second book rejected for single account;
* book ID included in client order ID;
* no raw account ID persisted.

## Preview

* valid preview;
* missing intent;
* inactive book;
* stale intent;
* risk-cap breach;
* payload mismatch;
* expired preview;
* preview performs no mutation.

## Submission

* successful explicit limit submission;
* no preview blocks;
* expired preview blocks;
* changed payload blocks;
* market order rejected;
* fractional quantity rejected;
* short order rejected;
* oversell rejected;
* disabled config blocks;
* deterministic client order ID;
* repeated successful invocation performs lookup and no duplicate submit.

## Ambiguity

* timeout after request produces `UNKNOWN_REQUIRES_RECONCILIATION`;
* runtime crash produces unknown state;
* malformed response produces unknown state;
* process restart does not resubmit;
* broker lookup finds order;
* broker lookup confirms not found;
* explicit retry requires operator and reason;
* retry without confirmed not-found rejected;
* retry count bounded.

## Fills and ledger

* partial fill;
* multiple fills;
* duplicate fill replay;
* fill over quantity rejected;
* symbol mismatch rejected;
* correct book updated;
* other book unchanged;
* local simulator not invoked for external intent;
* settlement and positions reconcile.

## Cancellation

* explicit cancellation;
* cancellation of unknown order rejected;
* cancellation after fill handled correctly;
* cancellation ambiguity reconciled;
* no automatic cancellation.

## Reconciliation

* matched;
* missing local order;
* missing broker order;
* duplicate broker order;
* quantity mismatch;
* price mismatch;
* cash mismatch;
* position mismatch;
* account mismatch;
* namespace mismatch;
* critical mismatch blocks later submission.

## Runtime protocol

* strict operation allowlist;
* unknown operation rejected;
* oversized payload rejected;
* malformed JSON rejected;
* credentials never returned;
* paper environment required;
* live base URL rejected;
* timeout bounded.

## Scheduler boundary

* recurring scheduler never submits externally;
* scheduler never cancels externally;
* external intent remains awaiting explicit operator action;
* no external mutation occurs during recurring lifecycle-only run.

---

# Part 19 — Offline integration tests

Add one successful operator workflow:

```text
persistent database
→ external broker enabled for BASELINE only
→ fake paper account verified
→ approved frozen BASELINE limit intent
→ explicit preview
→ explicit submit
→ SUBMISSION_REQUESTED persisted
→ fake runtime returns SUBMITTED
→ fake partial fill
→ fake final fill
→ fills applied once to BASELINE
→ ENHANCED unchanged
→ reconciliation MATCHED
→ replay
→ no second runtime submit
→ no duplicate fills or ledger events
```

Add one ambiguous-submission workflow:

```text
explicit preview
→ explicit submit
→ runtime timeout after request
→ UNKNOWN_REQUIRES_RECONCILIATION
→ retry attempt blocked
→ lookup by deterministic client order ID
→ broker order found
→ state repaired to SUBMITTED
→ no duplicate order
```

Add one authoritative-not-found workflow:

```text
UNKNOWN_REQUIRES_RECONCILIATION
→ broker lookup returns authoritative NOT_FOUND
→ not-found evidence persisted
→ explicit operator retry
→ one new bounded submission attempt
→ audit history preserved
```

Add one scheduler isolation workflow:

```text
Milestone 10 recurring scheduler due
→ external-enabled book has eligible intent
→ scheduler persists awaiting-operator state
→ no runtime mutation request
→ operator later previews/submits manually
```

---

# Part 20 — Optional real paper smoke

Do not execute automatically.

Document an opt-in smoke test requiring all of:

```text
RUN_EXTERNAL_PAPER_BROKER_TESTS=true
ALPACA_IS_PAPER=true
external broker enabled
order submission enabled
exact paper endpoint verified
one externally enabled book
explicit operator command
small bounded whole-share limit order
```

The smoke test must:

* be skipped by default;
* never run merely because credentials exist;
* avoid a marketable limit when practical;
* reconcile;
* cancel if still open;
* print no credentials or raw account IDs.

---

# Part 21 — Documentation

Create:

```text
docs/milestone11-isolated-alpaca-paper-broker.md
docs/adr/0007-external-paper-account-isolation.md
docs/runbooks/alpaca-paper-operations.md
```

Update:

```text
README.md
.env.example
docs/milestone10-controlled-recurring-local-paper.md
```

Document:

* local simulation remains default;
* external execution is disabled;
* paper endpoint enforcement;
* one-account/one-book limitation;
* credential process isolation;
* preview and explicit submission;
* submission state machine;
* ambiguous submission recovery;
* reconciliation;
* cancellation;
* scheduler non-integration;
* operator rollback and recovery;
* opt-in paper smoke;
* live trading remains structurally unavailable.

Remove or correct README commands that were previously described as implemented before they actually existed.

---

# Deferred items

Do not implement:

```text
live Alpaca trading
Robinhood trading
automatic recurring external submission
automatic order cancellation
multiple books in one paper account
multi-account credential orchestration unless already supported
options
shorting
margin
fractional orders
market orders
bracket orders
trailing stops
order replacement
web dashboard
automatic promotion
automatic activation
automatic incident resolution
```

---

# Required final tests

At completion run:

```bash
pytest tests/ -q --tb=short
```

Then:

```bash
cd paper_runtime
pytest tests/ -q --tb=short
```

Do not run real broker or network tests.

---

# Acceptance criteria

Milestone 11 is complete when:

1. Existing main tests pass.
2. Existing paper-runtime tests pass.
3. Local simulation remains the default.
4. External broker integration ships disabled.
5. Credentials cannot enable submission.
6. Only the isolated runtime reads broker credentials.
7. Live endpoints are rejected structurally.
8. Main process does not import Alpaca/LumiBot trading clients.
9. One account cannot silently serve two isolated books.
10. Account fingerprint is verified.
11. Limit orders are the only supported order type.
12. Long-only and whole-share constraints are enforced.
13. Submission requires a recent matching preview.
14. Submission requires an explicit operator command.
15. `SUBMISSION_REQUESTED` persists before broker mutation.
16. Ambiguous outcomes never trigger blind retry.
17. Broker lookup by client order ID precedes retry.
18. Retry requires authoritative not-found evidence.
19. Client order IDs are deterministic and book scoped.
20. Broker fills apply idempotently to the correct book.
21. External and local simulated fills cannot both execute the same intent.
22. Reconciliation detects order, fill, cash, position, account, and namespace drift.
23. Critical reconciliation failures block later external submission.
24. Cancellation is explicit and audited.
25. Recurring scheduler performs no external broker mutation.
26. Runtime payloads are bounded and schema validated.
27. Credentials and raw account IDs are not logged or persisted.
28. Default tests make no network calls.
29. Optional real-paper smoke is skipped by default.
30. Live trading remains unavailable.
31. Documentation matches implementation.


---

# Final response

Keep the final response concise.

Report only:

1. Baseline and final tests.
2. Files created and modified.
3. Runtime architecture.
4. Account/book isolation.
5. Configuration and endpoint enforcement.
6. Preview and submission workflow.
7. Submission state machine.
8. Ambiguous-outcome recovery.
9. Fill and ledger behavior.
10. Reconciliation behavior.
11. Scheduler boundary.
12. CLI commands.
13. Optional paper-smoke status.
14. Known limitations.
15. Safety confirmation.

Include a compact table:

```text
Requirement → implementation → test
```

Use labels:

```text
EXTERNAL-PAPER-ONLY
DISABLED-BY-DEFAULT
ISOLATED-RUNTIME
ONE-ACCOUNT-ONE-BOOK
EXPLICIT-PREVIEW
EXPLICIT-SUBMISSION
AMBIGUITY-SAFE
NO-BLIND-RETRY
IDEMPOTENT-FILLS
BOOK-SCOPED
RECONCILED
RECURRING-NOT-CONNECTED
LIVE-ENDPOINT-REJECTED
LIVE-TRADING-NOT-IMPLEMENTED
```

commit, push and create a PR.
