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
