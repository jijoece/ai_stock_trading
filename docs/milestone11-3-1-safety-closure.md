# Milestone 11.3.1 — Execution, Recovery, Transaction, and Snapshot Safety Closure

## Status: COMPLETE — all 8 items closed with passing regression tests

## Executive summary

All eight items in `docs/milestones/milestone-11.3.1.md` were investigated
against the actual codebase (not assumed from the spec text), confirmed as
real gaps, and fixed with the narrowest correction that closes the gap,
backed by new regression tests. No hard boundary was weakened: local
simulation remains the default, external paper submission remains disabled
by default, live trading remains structurally unavailable, and recurring
scheduling still never submits or cancels a broker order.

The headline structural change is Item 2 (transaction ownership): every
connection from `storage/database.py::connect()` is now opened with
`isolation_level=None` (true SQLite autocommit), and the shared
`begin_immediate`/`transaction()` helpers (`storage/transactions.py`) now
raise `TransactionAlreadyActiveError` instead of silently rolling back a
caller-owned transaction. This surfaced and fixed one latent bug on its own
(`storage/trading_repositories.py::save_frozen_recommendation` relied on the
old implicit-transaction behavior for its own atomicity) and became the
foundation Items 1, 4, and 6 build on.

## Starting commit and branch

- Base branch: `agent/milestone-11-3-remaining-integrity-closure` (tip `14ca356`)
- Working branch: `agent/milestone-11-3-1-safety-closure`
- Working tree at start: clean except the untracked milestone spec document

## Baseline

```
pytest tests/ -q --tb=short                    -> 1894 passed, 15 skipped
cd paper_runtime && pytest tests/ -q            -> 59 passed
clean-env pytest tests/ -q                      -> 1894 passed, 15 skipped (identical)
pyright (repo root)                             -> 1888 errors, 0 warnings (pre-existing, non-blocking)
cd paper_runtime && pyright                     -> 39 errors, 0 warnings (pre-existing, non-blocking)
git diff --check                                -> clean
```

Pyright is treated as guidance per this repository's convention — the
pre-existing errors are almost entirely `Optional`-narrowing patterns in test
fixtures (dict-shaped rows typed `dict | None`), not defects this milestone
introduced or fixed. This report records pyright's actual result honestly
rather than describing it as a passing type check.

## Finding validation

| Item | Finding | Status | Evidence | Correction | Tests |
| --- | --- | --- | --- | --- | --- |
| 1 | `SUBMISSION_REQUESTED` had no safe recovery path | CONFIRMED → FIXED | `retry_external_paper_order` required `current["new_state"] == STATE_UNKNOWN`; a crash right after the reservation+checkpoint commit (before any broker response) left the chain at `SUBMISSION_REQUESTED` forever — reconciliation recorded lookup evidence but never transitioned the chain | New `external_broker.py::recover_stranded_submission` (lease-fenced, idempotent, never calls submit/preview) + `_bridge_stranded_submission_requested` inside `_run_reconciliation` | `test_stranded_submission_recovery.py` (11) |
| 2 | Transaction helper silently rolled back caller-owned work | CONFIRMED → FIXED | `begin_immediate` called `conn.rollback()` unconditionally whenever `conn.in_transaction` was true | New `storage/transactions.py`; `isolation_level=None`; `TransactionAlreadyActiveError` instead of silent rollback; migrated `schema_version.py` and `trading_repositories.py::save_frozen_recommendation` | `test_transaction_discipline.py` (14) + `test_recommendation_builder.py` (unchanged, now correct) |
| 3 | Snapshot identity omitted cash/ledger material state | CONFIRMED → FIXED | `compute_snapshot_id`/`source_hash` hashed two different, narrow payloads via unsafe `json.dumps(default=str)` | One canonical `identity_payload` (cash available/reserved/settled, cash-ledger hash, settlement + snapshot methodology versions, full per-position price provenance) feeds both identity hashes via `hash_config`; `save_snapshot` raises `SnapshotIdentityConflictError` on a same-id/different-hash collision | `test_snapshot_identity.py` (16) |
| 4 | Lease heartbeat used caller time; `verify()` never called | CONFIRMED → FIXED | `OrderLeaseHandle.heartbeat/verify` took the caller's stale `now`; `verify()` was dead code; default TTL (30s) equaled the runtime client's own default request timeout | `OrderLeaseHandle` reads a fresh clock internally; `heartbeat_or_raise()`/`verify_or_raise()` used at every protected write; TTL default raised to 45s with a new config validation | `test_external_order_lease_handle_fencing.py` (9) |
| 5 | Timed-out runtime transport could be reused | CONFIRMED → FIXED | Only a *detected* response mismatch marked the transport unhealthy — a bare timeout left it "healthy" | Every timeout marks unhealthy + tears down the child; retryable read-only ops transparently restart onto a clean process; mutating ops are never auto-retried | `test_runtime_client.py` (rewrote 2, added 6) + `test_submit_credentialed_paper_order.py` fixture fix |
| 6 | No atomic local-BUY intent/reservation; no durable execution-namespace claim | CONFIRMED → FIXED | `execution.py` persisted the intent then separately reserved cash; local/external exclusivity was two asymmetric evidence checks, not a claim | New `paper_order_execution_claims` table + `claim_execution_namespace`; atomic claim+intent+reservation for new local BUYs; claim checked at external `_intent()`/claimed at first preview; schema-migration backfill for legacy rows | `test_execution_namespace_claims.py` (13) |
| 7 | `shadow/config.py` used permissive `bool(...)` | CONFIRMED → FIXED | All 9 boolean fields coerced via `bool()` | Reused the repo's `_strict_bool` pattern (`type(value) is not bool`) across all 9 fields | `test_shadow_config.py` (+70 parametrized cases) |
| 8 | Provider health used `symbols_attempted`; no hysteresis | CONFIRMED → FIXED | `provider_request_count=symbols_attempted`; `evaluate_cycle_health` was purely per-cycle | Real per-request telemetry (`compute_cycle_provider_telemetry`), bounded severe-error enum, persistent versioned hysteresis (`health_hysteresis.py`) wired into the scheduler | `test_provider_health_telemetry.py` (18) + `test_health_hysteresis.py` (13) + scheduler integration test |

## Architecture decisions

- **Transaction ownership (Item 2).** Chose `isolation_level=None` (explicit
  transaction control) over keeping legacy mode and trying to detect
  "abandoned" transactions heuristically — the latter is exactly the
  unsound pattern being removed. Nested `begin_immediate`/`transaction()`
  calls fail closed with `TransactionAlreadyActiveError`; the repository's
  existing `commit=False` parameter convention remains the supported way
  for an inner call to participate in an outer transaction.
- **Execution-namespace exclusivity (Item 6).** Chose the simpler
  fail-closed policy the milestone explicitly recommended: a claim is
  immutable once made, with no release/abandonment workflow. A preview
  alone permanently claims `EXTERNAL_PAPER`.
- **Runtime transport restart (Item 5).** Chose transparent, in-client
  restart-on-retry for the bounded read-only allowlist (`_RETRYABLE_ON_
  TIMEOUT`) rather than pushing restart responsibility onto every caller —
  this preserves the existing "submit fails, then look the order up"
  caller pattern (`services/submit_credentialed_paper_order.py`,
  `external_broker.py`'s `SUBMISSION_REQUESTED` recovery) without a call-site
  rewrite, while still guaranteeing a mutating operation is never retried
  automatically and a stale response can never resurface.
- **Health hysteresis scope (Item 8 Part C).** Implemented as a `scope`-keyed
  singleton (`"default"`) rather than per-book/per-provider-set state — the
  schema supports a future multi-scope deployment without migration, but
  this milestone's shadow-operations system only has one active research
  pipeline today.

## Schema changes

| Table | Change | Migration |
| --- | --- | --- |
| `paper_order_execution_claims` | New table (Item 6): `(book_id, paper_order_intent_id)` PK, `execution_namespace`, `claim_generation`, `claimed_at`, `claimed_by` | `schema_version.py` migration 2 backfills one claim per pre-existing `paper_book_orders` row, inferring namespace from existing external-evidence tables |
| `shadow_health_hysteresis_state` | New table (Item 8 Part C): one row per `scope`, persisted consecutive-failure/recovery counters, decision, policy hash | Additive `CREATE TABLE IF NOT EXISTS`; no backfill needed (absent row = fresh `HEALTHY` state on first evaluation) |

`schema_version.py` also picked up an unrelated migration 3
(`claude_code_usage_provenance` columns) committed by another concurrent
workstream on this branch's history — not part of this milestone's scope,
left untouched.

## Transaction ownership model

See `storage/transactions.py`'s module docstring for the full contract.
Summary: every connection is explicit-transaction-only; `begin_immediate`
starts a transaction the caller owns end-to-end; `transaction()` is the
context-manager form (commit on success, rollback on any `BaseException`);
nested use fails closed rather than discarding the outer caller's work.

## Recovery state machines

`SUBMISSION_REQUESTED` recovery (Item 1): a lease-fenced,
idempotent, read-only-only operation. On authoritative broker FOUND,
normalizes state/fills exactly like ordinary reconciliation. On NOT_FOUND,
timeout, or a malformed response, bridges the chain to
`UNKNOWN_REQUIRES_RECONCILIATION` so the existing, fully-gated
`retry_external_paper_order` path becomes reachable — recovery itself never
calls `submit_limit_order`/`preview_limit_order`.

Runtime transport lifecycle (Item 5): `healthy` → (timeout) → `unhealthy` →
(explicit or transparent `start()`) → `healthy`. A mutating operation on an
unhealthy transport raises immediately; a retryable read-only operation
transparently restarts first.

## Lease and fencing design

`OrderLeaseHandle` now owns a `clock` reference and reads fresh time on
every `heartbeat()`/`verify()` call. `heartbeat_or_raise()`/
`verify_or_raise()` are the versions every call site actually uses — a
failed heartbeat or fencing check stops the operation immediately.
Fencing is applied immediately before every protected write this
milestone names: preview persistence, the reservation+checkpoint
transaction, retry-evidence consumption, cancel-state transitions, and
the reconciliation write path. TTL must now exceed the isolated runtime's
maximum single-request timeout plus the heartbeat margin (enforced by
`PaperBooksConfigError`), removing the pre-11.3.1 30s/30s coincidence.

## Snapshot identity design

One canonical `identity_payload` dict (book_id, as_of, cash
available/reserved/settled, a canonical cash-ledger-entries hash keyed by
`ledger_entry_id`, settlement + snapshot methodology versions, and — per
position — quantity, available_quantity, average cost, realized P&L, and
full price provenance) feeds both `compute_snapshot_id` and `source_hash`
via `hash_config` (sorted keys, rejects unsupported types). Because both
identifiers derive from the same payload, a same-id/different-hash
collision is structurally unreachable outside a SHA-256 break; `save_snapshot`
still fails closed (`SnapshotIdentityConflictError`) rather than assuming
that can never happen.

## Health hysteresis design

`shadow/health_hysteresis.py::evaluate_and_persist_hysteresis` reads/writes
`shadow_health_hysteresis_state` fresh on every call (no in-memory global).
Policy (`PersistentHealthPolicyConfig`, versioned + hashed):
`warning_after_n_failures <= pause_recommended_after_n_failures <=
pause_required_after_m_failures`, plus `recovery_streak`. A severe provider
error bypasses ordinary counting and immediately sets `PAUSE_REQUIRED`.
`INSUFFICIENT_DATA` cycles (no real provider-request data) never move
either streak. A configuration change (detected via `policy_hash` mismatch)
resets the streak as a policy boundary. The persisted `decision` is forced
to `PAUSE_REQUIRED` whenever the actual system pause state is
`PAUSED_MANUAL` or `KILLED` — this module never calls `resume()` itself.

## Fixes implemented

See the Finding validation table above for the concrete change per item.
Full file list: `storage/transactions.py` (new), `storage/database.py`,
`storage/schema_version.py`, `storage/trading_repositories.py`,
`storage/paper_books_repositories.py`, `storage/paper_books_schema.py`,
`storage/shadow_alerts_repositories.py`, `storage/shadow_alerts_schema.py`,
`paper_books/execution.py`, `paper_books/external_broker.py`,
`paper_books/models.py`, `paper_books/valuation.py`,
`paper_books/config.py`, `runtime/client/process_client.py`,
`shadow/config.py`, `shadow/scheduler.py`, `shadow/health_hysteresis.py`
(new), `evidence_providers/health.py`, `evidence_providers/persistence.py`.

## Tests added

| Module | Count |
| --- | --- |
| `test_transaction_discipline.py` (rewritten) | 14 |
| `test_stranded_submission_recovery.py` (new) | 11 |
| `test_execution_namespace_claims.py` (new) | 13 |
| `test_external_order_lease_handle_fencing.py` (new) | 9 |
| `test_snapshot_identity.py` (new) | 16 |
| `test_shadow_config.py` (extended) | +70 (strict-bool matrix) |
| `test_health_hysteresis.py` (new) | 13 |
| `test_provider_health_telemetry.py` (new) | 18 |
| `test_runtime_client.py` (2 rewritten, 6 added) | net +8, 25 total |
| `test_submit_credentialed_paper_order.py` (fixture fix) | 7 (unchanged count, now correct) |
| `test_paper_books_valuation.py` (updated) | 10 |
| `test_shadow_scheduler.py` (+1 integration test) | 41 |

## Final test results

```
pytest tests/ -q --tb=short                    -> 2079 passed, 16 skipped
clean-env pytest tests/ -q --tb=short           -> 2079 passed, 16 skipped
cd paper_runtime && pytest tests/ -q            -> 59 passed
pyright (repo root)                             -> 1951 errors (baseline 1888; +63 from ~15 new
                                                    test modules following the same pre-existing
                                                    Optional-narrowing pattern; no new category)
cd paper_runtime && pyright                     -> 39 errors (unchanged from baseline)
git diff --check                                -> clean
Concurrency-focused test files x10 repeats      -> stable (440/440 passed across 10 runs)
```

Each new/modified test module was also run in isolation (see the scratchpad
for per-file `pytest -q` output); all pass individually.

## Remaining limitations

- **Item 8 required-provider set is caller-supplied, not auto-derived.**
  `compute_cycle_provider_telemetry(required_providers=...)` accepts an
  explicit tuple; `shadow/scheduler.py`'s current call site does not yet
  pass one (no single canonical "required providers per category" constant
  exists in the codebase today — deriving it correctly from
  `evidence_providers/config.py`'s per-category enabled-provider config
  was judged out of scope for this milestone's narrowest-safe-correction
  mandate). The missing-required-provider detection is implemented and
  tested at the `compute_cycle_provider_telemetry` level; wiring a concrete
  required-provider list through the scheduler is a follow-up.
- **TLS failures are not distinguishable from generic connection failures**
  at the currently-persisted field granularity (`evidence_providers/
  health.py::classify_severe_error` — documented in its docstring, both
  fold into `DNS_OR_CONNECTION_FAILURE`).
- **Health hysteresis is a single global scope today** (`DEFAULT_SCOPE =
  "default"`) — correct for this system's current one-pipeline shape;
  the schema supports per-scope rows without migration if that changes.
- **Pyright is non-blocking per project convention**; the post-milestone
  error count (1951) is honestly reported above rather than described as a
  clean type check. All new errors follow the codebase's existing
  `Optional`-narrowing test-fixture pattern.
- Schema migration 3 (`claude_code_usage_provenance`) present in
  `schema_version.py` on this branch belongs to a separate, concurrent
  workstream (visible in git history as `agent/claude-code-production-provider`
  branching from this one) — not authored or reviewed as part of this
  milestone.

## Operational go/no-go table

| Capability | Status | Evidence |
| --- | --- | --- |
| Research-only analysis | READY | Unaffected by this milestone; full suite green |
| Local simulated paper trading | READY | Item 6 crash-atomicity + namespace-claim tests pass; existing execution suite unaffected |
| Manual soak campaigns | READY | Unaffected; existing soak-campaign suite green |
| Unattended recurring local scheduling | READY | Item 7 (strict booleans) + Item 8 (real health telemetry/hysteresis) close the flagged gaps; recurring scheduling still structurally never submits/cancels an external broker order |
| Manual external Alpaca paper execution | READY | Items 1, 2, 4, 5, 6 close every flagged crash/timeout/lease/namespace gap with passing regression tests; still gated behind `external_broker.enabled`/`allow_order_submission`, both default false |
| Real Alpaca paper smoke | NOT_READY | No credentialed smoke test was run (explicitly out of scope — no real credentials, no network calls) |
| Live trading | NOT_IMPLEMENTED | `allow_live_broker`/`allow_live_promotion` remain structurally forbidden (`__post_init__` raises if ever true) — unchanged by this milestone |

## Finding → correction → regression evidence summary

See the Finding validation table above — every row already carries this
mapping in one place: finding, status, evidence for the finding, the
correction applied, and the specific test module(s) proving it.
