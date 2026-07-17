# Milestone 11 — Isolated Alpaca Paper Broker

Milestone 11 adds an optional manual Alpaca paper-account path to the
isolated BASELINE/ENHANCED paper books. Local simulation remains the default.
There is no live endpoint, live flag, automatic external scheduler submission,
market order, fractional order, short, option, margin, or replacement path.

## Architecture

The main process owns frozen paper intents, book risk, explicit preview and
operator authorization, the append-only state machine, broker-fill ledger
application, and reconciliation evidence. It does not import LumiBot or an
Alpaca SDK and does not read `ALPACA_API_KEY` or `ALPACA_API_SECRET`.

The isolated `paper_runtime` process owns those credentials, constructs the
existing LumiBot Alpaca paper gateway, verifies
`https://paper-api.alpaca.markets`, performs broker operations, hashes the raw
account ID into a bounded fingerprint, and returns normalized
`paper-runtime.v2` JSON Lines messages. Credentials, authorization headers,
raw account IDs, SDK objects, and raw broker bodies never cross the boundary.

## Controls

`config/paper_books.yaml` ships with `external_broker.enabled: false`,
`allow_order_submission: false`, and `enabled_book_ids: []`. Credentials do
not alter those values. A single runtime credential set can enable at most one
book. The strictest of the paper-book and external notional caps applies.

Only positive whole-share US-equity `LIMIT` orders with `DAY` time-in-force
are accepted. BUY opens/increases a long. SELL must be no larger than both the
book's and broker's confirmed long quantity. Extended hours is false.

## Workflow and state

The operator first runs account check and preview. Preview verifies the active
book, approved frozen intent, exact payload, notional, staleness, pause/kill,
critical alerts, paper endpoint, and stable account fingerprint. It is
read-only and expires after the configured interval.

Submit reloads and revalidates the same frozen payload, commits
`SUBMISSION_REQUESTED`, then calls the runtime without holding a SQLite write
transaction. Results append one of `SUBMITTED`, `PARTIALLY_FILLED`, `FILLED`,
`REJECTED`, `EXPIRED`, or `CANCELLED`. Events and previews are immutable and
carry policy/config hashes, book identity, operator, reason, and bounded
runtime request identity.

Any timeout, process exit, malformed response, broken transport, or unknown
post-request outcome appends `UNKNOWN_REQUIRES_RECONCILIATION`. No code retries
submission automatically. Reconciliation looks up the deterministic,
book-prefixed client order ID. A found order repairs state. Authoritative
`NOT_FOUND` evidence permits one configured, explicit operator retry of the
same frozen payload; otherwise retry is rejected.

## Fills and reconciliation

Normalized broker fills are persisted before application and atomically
applied to the matching book's existing fill, FIFO lot, position, and cash
ledger tables. Fill IDs are deterministic and replay is idempotent. An
external-enabled scheduled intent is queued instead of locally simulated, so
the same intent cannot receive both execution providers.

Read-only reconciliation compares the intent, namespace, order IDs, account
fingerprint, symbol, side, quantity, price, fill quantity, cash, and positions.
Immutable evidence records `MATCHED` or bounded mismatch statuses. The latest
critical mismatch blocks a later preview/submit until a subsequent successful
reconciliation replaces it as current evidence.

The offline main and runtime test suites make no broker or network calls. Real
paper smoke remains opt-in via `RUN_EXTERNAL_PAPER_BROKER_TESTS=true` plus all
configuration, endpoint, credential, one-book, and explicit-command gates.

## Milestone 11.1 corrections

A follow-up review found and fixed several gaps in the mechanisms described
above; the full account is in
[`milestone11-1-external-paper-safety-closure.md`](milestone11-1-external-paper-safety-closure.md).
In summary:

- **BUY cash reservations** now release only as fills are durably applied
  locally (per-fill, plus a final sweep once fully filled) — a broker
  `FILLED` response with a temporarily empty fills list no longer releases
  cash on trust.
- **External closing SELLs now reserve shares** before submission
  (`paper_external_position_reservation_events` + `paper_book_positions
  .reserved_quantity`), symmetric with the BUY cash reservation; a second
  SELL cannot reserve or submit the same shares, and lifecycle exit
  evaluation now recognizes an unresolved external SELL (not just a local
  `PENDING_SUBMISSION` row).
- **Every order mutation (preview/submit/retry/cancel/reconcile) is now
  serialized by an order-scope lease** (`paper_external_order_leases`,
  keyed by `book_id + client_order_id`), and the event chain carries a
  monotonic `scope_sequence` with a uniqueness constraint as a
  database-level backstop against a forked chain.
- **Ambiguous-retry `NOT_FOUND` evidence is now single-use and attempt-scoped**:
  a lookup only authorizes a retry of the exact ambiguous event it was
  taken against, and is consumed on use.
- **Reconciliation never exits without persisting evidence**: fill
  application, order/response validation, and the broker positions/account
  comparison are all wrapped so any failure — malformed data, an
  unexpected exception, a numeric conversion error — persists a critical,
  precisely-coded record (`MALFORMED_BROKER_ORDER`,
  `MALFORMED_BROKER_FILL`, `FILL_APPLICATION_FAILED`,
  `RECONCILIATION_INTERNAL_ERROR`, `RESERVATION_MISMATCH`/
  `SHARE_RESERVATION_MISMATCH`, `FROZEN_INTENT_MISMATCH`) rather than a
  bare `UNKNOWN` or an uncaught exception.
- **`BROKER_ORDER_DUPLICATE` is now a real check**, comparing recent
  broker orders (a new bounded, paper-only `LIST_RECENT_ORDERS` runtime
  operation reusing the existing gateway capability) against the frozen
  intent.
- **The isolated runtime no longer discovers the main repository's `.env`**
  via an upward filesystem search; it loads credentials only from an
  explicitly-named `PAPER_RUNTIME_ENV_FILE` (or a verbatim, allowlisted
  subprocess-environment pass-through), never scanning parent directories.
- **The external submission queue status is now derived live** from the
  order-event chain (`AWAITING_OPERATOR_EXTERNAL_SUBMISSION` through
  terminal states, plus `BLOCKED_BY_RECONCILIATION`) instead of a
  write-once column that silently never updated after submission.

Recurring activation, notional recomputation, timestamp validation, strict
configuration booleans, and non-finite/fractional broker-value rejection
were also corrected — see the closure doc for the complete list.
