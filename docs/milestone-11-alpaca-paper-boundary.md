# Milestone 11 — External paper-broker boundary and Alpaca paper integration

Implement an optional external paper-broker integration for the existing isolated paper books.

All current local-simulated behavior must remain the default and must remain fully functional.

This milestone is paper-account only.

## Objective

Add a broker-neutral external paper-execution boundary and one Alpaca paper implementation without creating any path to live trading.

Primary flow:

```text
approved isolated paper intent
→ external paper execution adapter
→ Alpaca paper endpoint only
→ broker order status
→ fills
→ book-specific settlement
→ reconciliation against external paper broker
```

Do not implement live Alpaca trading or Robinhood trading.

---

## Token-efficiency rules

* Use LSP before file reads.
* Read only execution, paper-book, runtime, config, and reconciliation symbols.
* Do not inspect unrelated research components.
* Keep adapter and protocol minimal.
* Run targeted tests with fakes.
* Do not make real network calls during implementation.
* Full suite only once at completion.
* Do not commit or push.

---

## Hard boundaries

Do not:

* use Alpaca live endpoints;
* accept live account credentials;
* add a live-mode flag;
* reuse paper credentials for live trading;
* place a real external paper order during tests;
* add Robinhood mutation;
* add margin, shorting, or options;
* let Claude invoke the broker;
* let Claude choose account or book;
* share broker orders between books;
* automatically enable external paper execution;
* remove local simulation.

---

## 1. Broker-neutral protocol

Create a narrow interface such as:

```python
class ExternalPaperBroker(Protocol):
    def submit_limit_order(...)
    def get_order(...)
    def cancel_order(...)
    def list_fills(...)
    def get_account_snapshot(...)
    def get_positions(...)
```

Every method must be explicitly paper-account scoped.

Return typed normalized models independent of Alpaca SDK objects.

---

## 2. Configuration

Add disabled-by-default configuration conceptually:

```yaml
paper_books:
  external_broker:
    enabled: false
    provider: alpaca_paper
    base_url: https://paper-api.alpaca.markets
    allow_order_submission: false
```

Requirements:

* exact paper hostname allowlist;
* any live hostname rejected;
* credentials do not enable execution;
* both `enabled` and `allow_order_submission` required;
* credentials loaded only at runtime;
* secrets never persisted or logged;
* no account identifiers in normal logs.

---

## 3. Book-to-broker isolation

Decide and document how books map externally.

Preferred safe options:

1. one separate Alpaca paper account per book; or
2. one paper account with strict client-order-ID namespacing and an explicit limitation that broker cash/positions cannot represent truly isolated books.

Fail closed when true isolation cannot be guaranteed.

Do not silently claim one external account gives independent cash and positions.

If only one paper account is configured, external execution may initially support one book only.

---

## 4. Alpaca paper adapter

Use official Alpaca API documentation and the repository’s dependency conventions.

Requirements:

* paper endpoint only;
* limit orders only;
* long-only;
* no notional market orders;
* deterministic client order ID containing book identity;
* timeout;
* bounded retries for safe reads only;
* no blind order-submission retry after ambiguous timeout;
* normalized errors;
* sanitized logging.

---

## 5. Submission state machine

Persist:

```text
NOT_SUBMITTED
SUBMISSION_REQUESTED
SUBMITTED
PARTIALLY_FILLED
FILLED
CANCEL_REQUESTED
CANCELLED
REJECTED
UNKNOWN_REQUIRES_RECONCILIATION
```

An ambiguous submission result must become:

```text
UNKNOWN_REQUIRES_RECONCILIATION
```

Never resubmit automatically until broker lookup proves no order exists.

---

## 6. Reconciliation

Reconcile:

```text
local paper intent
broker client order ID
broker order ID
broker fills
book cash ledger
book positions
```

Statuses should distinguish:

```text
MATCHED
ORDER_MISSING_LOCALLY
ORDER_MISSING_AT_BROKER
FILL_MISMATCH
QUANTITY_MISMATCH
PRICE_MISMATCH
BOOK_NAMESPACE_MISMATCH
UNKNOWN
```

Do not mutate history to force a match.

Use compensating events where required.

---

## 7. Runtime separation

Prefer one isolated process or client instance per externally enabled book.

Do not share mutable broker session state when it could mix book identity.

The main research process must send only bounded approved intent data.

No raw Claude content crosses the broker boundary.

---

## 8. CLI

Add read-only and explicitly mutating commands:

```bash
python -m trading_research.cli external-paper-account-check \
  --book-id BASELINE

python -m trading_research.cli external-paper-preview \
  --book-id BASELINE \
  --intent-id <id>

python -m trading_research.cli external-paper-submit \
  --book-id BASELINE \
  --intent-id <id>

python -m trading_research.cli external-paper-order-show \
  --book-id BASELINE \
  --client-order-id <id>

python -m trading_research.cli external-paper-reconcile \
  --book-id BASELINE
```

Submission must require explicit invocation.

Do not add automatic external submission to the recurring scheduler in this milestone.

---

## 9. Tests

Use fake HTTP or fake adapter responses only.

Test:

* live hostname rejected;
* missing paper credentials;
* credentials do not enable submission;
* disabled config;
* order preview;
* deterministic client order IDs;
* book namespace isolation;
* successful submission;
* rejection;
* ambiguous timeout;
* no automatic resubmission;
* partial fill normalization;
* cancellation;
* reconciliation;
* sanitized errors;
* no network in unit tests;
* no live mode.

---

## 10. Optional real smoke

Do not perform automatically.

Document a future bounded smoke requiring explicit operator approval:

```text
one enabled book
one small paper-only limit order
paper endpoint verified
no live credentials
explicit submit command
reconcile
cancel when appropriate
```

---

## 11. Documentation

Create:

```text
docs/milestone11-external-paper-broker.md
docs/adr/0007-external-paper-broker-isolation.md
docs/runbooks/alpaca-paper-operations.md
```

Document:

* paper-only endpoint enforcement;
* book/account mapping;
* ambiguous submission handling;
* reconciliation;
* credentials;
* operator commands;
* rollback;
* why live trading remains structurally unavailable.

---

## Acceptance criteria

Complete when:

1. Local simulation remains default.
2. External broker integration ships disabled.
3. Live endpoints are rejected.
4. Credentials cannot enable submission.
5. Submission requires an explicit operator command.
6. Limit orders and long-only constraints remain.
7. Book identity is present in broker identifiers.
8. Ambiguous submission never causes blind retry.
9. External fills reconcile to the correct book.
10. No raw model content crosses the broker boundary.
11. Unit tests make no real network calls.
12. No automatic recurring external submission exists.
13. No live-trading path exists.
14. Existing tests remain passing.
15. No commit or push occurs.

## Output

Return:

1. Unified patch.
2. Concise implementation notes.
3. Exact missing context paths when required.

Do not claim tests were run.
