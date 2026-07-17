# Milestone 11.1 — External Paper Execution Safety and Integration Closure

Corrective milestone resolving the confirmed review findings from Milestone 11
("Implement isolated Alpaca paper boundary"). No new trading capabilities were
added. Local simulation remains the default; external paper execution remains
disabled by default; external submission remains operator-initiated; recurring
scheduling never mutates the external broker; only the exact Alpaca paper
endpoint is accepted; live trading remains structurally unavailable.

> Note: the corrective-milestone brief referenced commit
> `ec1516af7d9e995688a62a40e2945ac16700c196`, which does not exist in this
> repository. Milestone 11 is present on this branch via `b4d9705`
> ("Implement isolated Alpaca paper boundary") and `e60e550` ("Record
> Milestone 11 publication"); this closure work was carried out against that
> history.

## 1. Campaign-attempt activation compatibility

**Problem.** `recurring_scheduler.validate_activation_review` required the
*campaign definition* row's `status` to equal `COMPLETED_READY_FOR_REVIEW`.
Milestone 9.3.1 made campaign-definition rows immutable (`paper_soak_campaigns
.status` stays `"DEFINED"` forever); only the *attempt* row's status
transitions to a terminal value. The check could never pass for a real
campaign — recurring activation was silently unusable.

**Fix.** `validate_activation_review` now resolves the review's
`campaign_attempt_id`, and requires: the attempt exists; the attempt's
`campaign_id` matches the review's; the attempt's status is exactly
`COMPLETED_READY_FOR_REVIEW`; the attempt's `manifest_hash`/`config_hash`
match the review's; and the review is the latest, non-superseded review for
its campaign (via `list_soak_activation_reviews` ordering). The campaign
definition row is never consulted for this check again.

**Tests.** `tests/unit/test_recurring_paper_scheduler.py`: definition stays
`DEFINED` while a terminal attempt activates; missing attempt rejected;
running attempt rejected; superseded review rejected; cross-campaign attempt
rejected.

## 2. BUY cash-reservation lifecycle

**Problem.** `_release_terminal_buy_reservation` released the *entire*
original reservation the instant the broker-reported state reached any
terminal value — including `FILLED` with an empty or delayed fills list, and
regardless of how much had actually been applied on a partial fill before
cancellation.

**Fix.** New `cash_ledger` helpers: `remaining_buy_reservation` (reserved
minus every release recorded so far), `release_settled_buy_reservation`
(per-fill, idempotent by `fill_id`, clamped to what remains),
`release_remaining_buy_reservation` (idempotent by a named event, e.g.
`"terminal-closed"`/`"fully-filled"`). `apply_external_fills` releases the
settled portion inside the same atomic per-fill transaction as the fill
application; the terminal-state handler now only fires for
`CANCELLED`/`REJECTED`/`EXPIRED` and releases the remainder — never `FILLED`,
which is handled exclusively by the per-fill/fully-filled sweep once the
approved quantity is durably applied. `cancel_external_paper_order` now
sweeps any last-moment fills (`apply_external_fills`) before releasing,
which it previously skipped entirely.

**Tests.** Broker `FILLED` with no fills retains the reservation and produces
a critical reconciliation; delayed fills later settle and release in full;
partial fill preserves the remainder; partial fill then cancel releases only
the remainder; repeated reconciliation does not over-release.

## 3. SELL share reservations

**Problem.** External closing SELL orders never reserved shares. A second
SELL against the same shares, or a local lifecycle exit racing an external
SELL, could both proceed.

**Fix.** New append-only table `paper_external_position_reservation_events`
(event types `RESERVED`, `CONSUMED_BY_FILL`, `RELEASED_CANCELLED`,
`RELEASED_REJECTED`, `RELEASED_EXPIRED`, `MANUAL_CORRECTION`) backing
`paper_book_positions.reserved_quantity`/`available_quantity`. New
`positions` functions mirror the cash-reservation design:
`reserve_shares_for_sell` (fails closed via `InsufficientPositionError`,
idempotent per intent), `consume_share_reservation_for_fill` (per fill,
clamped), `release_remaining_share_reservation` (idempotent per event),
`manual_correct_share_reservation` (requires operator + reason — no silent
correction). `_submit_once` reserves shares for SELL before
`SUBMISSION_REQUESTED`, symmetric with the BUY cash reservation.

**Tests.** SELL reserves shares and a second SELL over the remainder fails
closed; partial fill preserves the remainder; cancel releases only the
remainder; an ambiguous SELL submission keeps shares reserved; a full
end-to-end scenario (long 10 → SELL 10 → blocks a second exit → partial fill
4 → remaining 6 → cancel → released → position 6 → exit re-openable) is
covered as an integration test.

> This integration test caught a genuine bug: `positions.apply_sell_fill`'s
> oversell guard checked `available_quantity` (confirmed minus reserved),
> which double-counted an external order's *own* reservation when applying
> that order's fill, spuriously rejecting it. Fixed with a new
> `already_reserved: bool` parameter — the external fill-application path
> checks against confirmed `quantity` instead (still an absolute ceiling);
> the local-simulated path is unchanged.

## 4. Unresolved external SELL detection

**Problem.** `lifecycle._has_unresolved_pending_sell` only checked the local
intent's `status == PENDING_SUBMISSION`. An externally `SUBMITTED`/
`PARTIALLY_FILLED` SELL (whose local status tracks the external state
machine, not `PENDING_SUBMISSION`) was invisible to this check, so a second
exit intent could be created for the same symbol.

**Fix.** The check now also loads the latest external order event for each
SELL intent (`repo.load_latest_external_order_event_for_intent` — one lookup
suffices since `client_order_id` is deterministic per intent) and treats
`SUBMISSION_REQUESTED`/`SUBMITTED`/`PARTIALLY_FILLED`/`CANCEL_REQUESTED`/
`UNKNOWN_REQUIRES_RECONCILIATION` as blocking. As a safety net, `PREVIEWED`
and terminal states are also treated as blocking if a share reservation is
still outstanding for that intent (covers a release lagging one
reconciliation cycle).

**Tests.** Unresolved external SELL blocks a new exit; terminal + released
reservation does not block; terminal + unreleased reservation still blocks.

## 5. Ambiguous retry evidence

**Problem.** An authoritative broker `NOT_FOUND` lookup was meant to
authorize exactly one retry, but the code only checked "is there *some*
authoritative `NOT_FOUND` lookup" — an old lookup from a prior ambiguous
attempt could authorize a later retry attempt too.

**Fix.** `paper_external_order_lookups` gained `attempt_number`,
`ambiguous_event_id`, `payload_hash`, `lookup_started_at`,
`lookup_completed_at`, `consumed_by_retry_event_id` (additive columns; the
immutability trigger was relaxed to allow exactly one `NULL → set`
transition of `consumed_by_retry_event_id`). `retry_external_paper_order`
now requires the lookup to be unconsumed *and* to reference the exact
current ambiguous event/attempt/payload hash/account fingerprint, and
consumes it immediately after resubmission (regardless of the outcome).

**Tests.** Full replay of attempt 0 UNKNOWN → lookup 0 → retry 1 (consumes
lookup 0) → retry 1 UNKNOWN → retry 2 rejected until a fresh lookup 1 →
retry 2 succeeds; nonauthoritative `NOT_FOUND` rejected; a lookup tied to a
stale/different ambiguous event rejected; a lookup with a mismatched
payload hash rejected.

## 6. Order-scope submission lease

**Problem.** Nothing serialized concurrent preview/submit/retry/cancel/
reconcile calls against the same external order — two racing callers could
fork the event chain or both mutate the broker.

**Fix.** New `paper_external_order_leases` table + `acquire_external_order_lease`/
`load_external_order_lease`/`release_external_order_lease` (atomic
conditional upsert: only claims if not `ACTIVE` or already expired; 30s TTL;
wrong owner cannot release). A new `_order_lease` context manager wraps the
mutating bodies of preview/submit/retry/cancel; acquisition happens before
reading/transitioning current state, release is in `finally`. Reconciliation
is split into a public `reconcile_external_paper_order` (resolves the target
order, acquires the lease) and an internal `_reconcile_locked` — submit/retry
call `_reconcile_locked` directly for their own post-submission reconciliation
to avoid deadlocking on a lease they already hold.

**Tests.** A concurrent duplicate submit blocked by a held lease performs
zero runtime calls; a stale (expired) lease recovers for a new caller; the
wrong owner cannot release a lease.

## 7. Concurrency-safe state transitions

**Problem.** Event append was read-current-state → validate → insert, with
no database-level guarantee against two children claiming the same previous
state, and the insert's success/failure was never checked (a failed
`INSERT OR IGNORE` was silently treated as a successful transition).

**Fix.** Added `scope_sequence` (monotonic per `client_order_id`) with a
`UNIQUE(book_id, client_order_id, scope_sequence)` index — a
defense-in-depth backstop behind the Part 6 lease. `_append_event` now
checks the insert's return value and raises `EVENT_CHAIN_CONFLICT` if it
did not occur.

**Tests.** A manually-constructed second event at the same
`(book_id, client_order_id, scope_sequence)` is rejected at the database
level.

## 8. Reconciliation fail-safe persistence

**Problem.** `apply_external_fills` was called unguarded inside
reconciliation; any raised exception (malformed fill shape/quantity/price,
an unexpected bug) escaped reconciliation entirely with zero persisted
evidence. Several other failure paths collapsed to a generic `UNKNOWN`
even when a more precise status was available.

**Fix.** Reconciliation is split into `_reconcile_locked` (outer fail-safe:
any unhandled exception persists a `RECONCILIATION_INTERNAL_ERROR` critical
record before propagating; a failure to even persist raises
`RECONCILIATION_PERSIST_FAILED`) wrapping `_run_reconciliation`. New
precise statuses: `FILL_APPLICATION_FAILED`, `MALFORMED_BROKER_ORDER`,
`MALFORMED_BROKER_FILL`, `BROKER_STATE_UNKNOWN`,
`RECONCILIATION_INTERNAL_ERROR`, `RESERVATION_MISMATCH`,
`SHARE_RESERVATION_MISMATCH`, `FROZEN_INTENT_MISMATCH`. Fill-application
failures skip reservation release (cash/shares stay safely reserved).

**Tests.** Malformed fill, malformed order, an unexpected exception inside
fill application, and a non-numeric broker cash value each persist the
correct critical status.

## 9. Duplicate broker-order detection

**Problem.** `BROKER_ORDER_DUPLICATE` was a declared status with no
detection code behind it.

**Fix.** New bounded, read-only `LIST_RECENT_ORDERS` v2 runtime operation
(paper_runtime `dispatcher.py`/`protocol.py`) that *reuses* the existing
Milestone 4 `list_recent_orders` gateway capability rather than adding a
second broker call path. `RuntimeClient.list_recent_external_orders`
(distinct name from the pre-existing, differently-signatured
`list_recent_orders(limit)`, to avoid a positional-argument collision).
`external_broker._detect_duplicate_broker_order` compares recent orders
against the frozen intent: the same `client_order_id` mapped to more than
one `broker_order_id`, or a materially identical order (book/symbol/side/
quantity/limit_price) under a different `client_order_id` within a 300s
window. Only bounded, non-secret identifiers are persisted.

**Tests.** One matching order → `MATCHED`; two orders under the same
`client_order_id` → duplicate; a materially identical payload under a
different `client_order_id` → duplicate; an unrelated order (different
symbol/quantity) → not flagged; later submissions in the book are blocked
once a duplicate is recorded.

## 10. Timestamp validation

**Problem.** `_intent()`'s staleness check (`now - as_of > threshold`) never
rejected a *future* `as_of` (a negative delta is never greater than the
threshold). Several other timestamps (broker order `submitted_at`/
`updated_at`, fill `filled_at`) were parsed and checked for timezone
awareness but not for being in the future or internally consistent.

**Fix.** Added explicit future-rejection for intent `as_of`/`created_at`,
broker order `submitted_at`/`updated_at` (now also requires
`submitted_at <= updated_at`), and fill `filled_at` (future-rejected, and
must not precede the order's own `submitted_at`) via a shared
`_CLOCK_SKEW = 5s` allowance.

**Tests.** Future intent rejected; broker `submitted_at` after `updated_at`
rejected; naive broker timestamp rejected; equivalent-offset timestamps
accepted (everything is parsed to aware `datetime` before comparison, never
compared as strings); future fill timestamp rejected; fill before the
order's own submission rejected.

## 11. Runtime credential isolation

**Problem.** `paper_runtime/configuration.py::_load_dotenv_if_present`
called `find_dotenv(usecwd=True)`, which searches *upward* from the process
working directory. Since the runtime is spawned with `cwd=REPO_ROOT`, this
silently discovered and loaded the *main repository's own* `.env`
(Anthropic/Reddit/Robinhood/database secrets) into the isolated runtime's
environment — defeating the isolation despite the subprocess environment
dict itself being minimal.

**Fix.** Removed the upward search. `_load_dotenv_if_present` now loads a
dotenv file only if explicitly named via `PAPER_RUNTIME_ENV_FILE`.
`cli.py::_paper_runtime_command_env` allowlist-copies exactly `PATH`,
`PYTHONPATH`, `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `ALPACA_IS_PAPER`,
`ALPACA_BASE_URL`, `PAPER_BROKER_PROVIDER`, `PAPER_RUNTIME_ENV_FILE` from
the main process's environment (verbatim pass-through — the main process
never parses or acts on these values); every other secret is excluded by
construction, since this is an allowlist rather than a filtered copy.

**Tests.** `paper_runtime/tests/test_configuration.py`: the runtime ignores
a `.env` in its cwd or a parent directory, loads only the explicitly-named
file, real environment variables take precedence, health output exposes
presence booleans only. `tests/unit/test_cli_runtime_env_isolation.py`:
unrelated secrets never forwarded, allowlisted keys pass through verbatim,
the constructed environment contains only allowlisted keys.

## 12. Strict configuration booleans

**Problem.** `paper_books.enabled`, `books.baseline/enhanced.enabled`,
`execution.allow_external_paper_broker`/`allow_live_broker`,
`scheduled_integration.enabled`, `lifecycle.enabled`,
`lifecycle.exits.enabled`/`exit_on_recommendation_reversal` used plain
`bool(...)`, which silently coerces `"false"` → `True` and `0`/`1` → real
booleans.

**Fix.** All replaced with the existing `_strict_bool` helper (already used
elsewhere in the file), which requires `type(value) is bool` exactly.

**Tests.** Parameterized rejection of `"false"`/`0`/`1`/`"true"` for every
field above; real YAML booleans still load correctly.

## 13. Fractional / non-finite broker values

**Problem.** Four places truncated through `int(Decimal(...))` or
`int(float(...))`: `external_broker._payload`'s quantity,
`paper_runtime.dispatcher._validate_confirmed_long`'s position quantity,
and `lumibot_gateway._order_to_snapshot`'s `filled_qty`/`qty`. `Decimal("NaN")`
and `Decimal("Infinity")` parse successfully, so these values could slip
through several checks untouched.

**Fix.** New strict finite+integral helpers (`external_broker._exact_int`,
`trading_paper_runtime.models._parse_exact_int`) replace every truncating
conversion; `models._parse_decimal` now also rejects non-finite values
globally. `_validate_order_response`'s quantity/limit_price/filled_quantity
and `apply_external_fills`'s fill quantity/price gained explicit
`.is_finite()` + integral checks.

**Tests.** Fractional/NaN/Infinity broker fill and order quantities persist
critical `MALFORMED_BROKER_FILL`/`MALFORMED_BROKER_ORDER`; the paper_runtime
confirmed-long check rejects a fractional broker position quantity.

## 14. Frozen notional recomputation

**Problem.** `_intent()` validated the strictest notional cap against the
*stored* `notional_usd` field directly, never recomputing
`quantity * limit_price` — a tampered/corrupted row with a low stored
notional next to a high quantity/price would pass.

**Fix.** `_validate_frozen_notional(intent, cfg, risk)` recomputes and
requires exact equality with the stored `notional_usd`; for BUY orders
approved via a risk decision, also cross-checks the risk decision's
`approved_notional_usd`; applies the strictest configured cap to the
*recomputed* value; fails closed with `FROZEN_INTENT_MISMATCH`. Called from
`_intent()` (covers preview/submit/retry) and independently inside
reconciliation (persists a critical record rather than raising).

**Tests.** A hand-crafted corrupted row (quantity × limit_price ≠ stored
notional) fails closed with a "recomputed notional" error on preview.

## 15. Reservation / fill transaction ordering

**Audit result: already correct, no code change required.** Every
reservation/event-append call in the submit/retry/cancel paths uses the
repository layer's default `commit=True` (each auto-commits its own single
statement immediately), so no transaction is ever held open across a
`runtime.*` call. The only explicit `BEGIN IMMEDIATE`/`commit=False` usage
is `apply_external_fills`'s per-fill block, which runs entirely *after* the
runtime response is already in hand.

**Tests.** New regression tests instrument a fake runtime to assert
`conn.in_transaction is False` at every runtime call across a full
preview → submit → fill → reconcile flow and a submit → cancel flow — both
pass, confirming the invariant end-to-end.

## 16. Queue-state lifecycle

**Problem.** `paper_external_submission_queue` rows were inserted once with
a hardcoded `AWAITING_OPERATOR_EXTERNAL_SUBMISSION` status and never
updated — no code anywhere derived or displayed a live status, so the queue
silently stayed "awaiting submission" forever regardless of what actually
happened at the broker.

**Fix.** `derive_external_queue_status`/`list_external_submission_queue_view`
compute the status fresh from the latest external order event for that
intent every time (never a separately-maintained column). A non-terminal
order under an active critical reconciliation is surfaced as
`BLOCKED_BY_RECONCILIATION`; terminal states are shown as-is. New read-only
CLI command `external-paper-queue-show` (no runtime client or credentials
needed).

**Tests.** Queue status moves off `AWAITING_OPERATOR_EXTERNAL_SUBMISSION`
once submitted; a non-terminal order under a critical reconciliation shows
`BLOCKED_BY_RECONCILIATION`; a fully-filled order shows the immutable
terminal `FILLED` status.

## 17. CLI error safety

**Problem.** `_external_paper_cli`'s generic exception handler returned
`str(exc)` verbatim as the error message — could leak filesystem paths or
subprocess detail for an unexpected failure. `external_paper_order_show_cli`
had no generic-exception catch-all at all.

**Fix.** `_sanitized_cli_error`/`_bounded_message`: `ExternalPaperError` and
`RuntimeOperationError` (both already curated, bounded domain errors) pass
through their own code/message; any other `RuntimeClientError` maps to a
fixed generic message; anything else collapses to `EXTERNAL_RUNTIME_ERROR` /
"an unexpected internal error occurred" — never the exception's own text.
CLI `main()` already returns a nonzero exit whenever any command's outcome
contains an `"error"` key; `RuntimeClient.diagnostics()` already exposed
only a bounded suppressed-line count.

**Tests.** Unexpected exception text never appears in the sanitized output;
domain errors still pass through their real code/message; oversized
messages truncate.

## Recovery procedures

- **Ambiguous submission (`UNKNOWN_REQUIRES_RECONCILIATION`)**: run
  `external-paper-reconcile`. If it resolves to `ORDER_MISSING_AT_BROKER`
  with authoritative evidence, an operator may run
  `external-paper-retry-submit` exactly once per fresh lookup.
- **Critical reconciliation active**: further submission in the book is
  blocked. An operator must investigate the persisted critical record
  (`external-paper-order-show`) and, if the broker-side truth is confirmed
  safe, resolve it through a subsequent successful reconciliation (there is
  no automatic critical-evidence clearing — by design, per the "no
  automatic reconciliation repair" scope boundary).
- **Stale order-scope lease**: recovers automatically for the next caller
  once its 30s TTL expires; no manual intervention needed.
- **Queue stuck-looking**: `external-paper-queue-show` always reflects the
  live derived status; if it shows `BLOCKED_BY_RECONCILIATION`, follow the
  critical-reconciliation recovery path above.

## Known limitations

- `manual_correct_share_reservation` exists but has no CLI wiring yet — an
  operator needing to correct share-reservation drift must currently do so
  by calling it directly (Python), not through a command.
- The duplicate-order detection's "materially identical order under another
  `client_order_id`" match uses a fixed 300-second window; this is a
  reasonable default but is not independently configurable per book.
- Real Alpaca paper smoke (`RUN_EXTERNAL_PAPER_BROKER_TESTS=true`) remains
  blocked until this closure's offline tests and review are accepted — it
  was not run as part of this work, per the milestone's own instructions.
- The activation-review "market-day age" staleness check
  (`recurring.activation_review_max_age_market_days`) was not otherwise
  revisited in this milestone.

## Safety confirmation

- Local simulation remains the default execution path.
- External paper submission remains disabled by default
  (`external_broker.enabled: false`, `allow_order_submission: false`,
  `enabled_book_ids: []`).
- Only the exact Alpaca paper endpoint (`https://paper-api.alpaca.markets`)
  is accepted; no live endpoint or live flag exists anywhere in this
  repository.
- The recurring scheduler never submits or cancels externally — enforced
  both structurally (`recurring_scheduler.py` never imports
  `external_broker`) and by the pre-existing scheduled-integration test
  that an externally-enabled book's pending order is queued, not
  submitted.
- The main process never reads `ALPACA_API_KEY`/`ALPACA_API_SECRET` values;
  at most it passes them through verbatim into the runtime subprocess's
  environment.
- No commit or push was made as part of this work.
