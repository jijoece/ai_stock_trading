# Milestone 11.3.1 — Execution, Recovery, Transaction, and Snapshot Safety Closure

## Metadata

- Base branch: agent/milestone-11-3-remaining-integrity-closure
- Working branch: agent/milestone-11-3-1-safety-closure
- Starting commit: 14ca356 (branch tip at checkout time)
- Working-tree status at start: clean except untracked docs/milestones/milestone-11.3.1.md

## Baseline test results

- `pytest tests/ -q --tb=short`: 1894 passed, 15 skipped (18.92s)
- `cd paper_runtime && pytest tests/ -q --tb=short`: 59 passed (0.04s)
- Clean-env `pytest tests/ -q --tb=short` (credential env vars unset): 1894 passed, 15 skipped (17.08s) — identical to above, no credential leakage detected in baseline.
- `pyright` (repo root): 1888 errors, 0 warnings — PRE-EXISTING, mostly in tests/unit/*.py (test fixtures / Optional narrowing). Non-blocking per project convention; recorded honestly, not treated as a new regression baseline requirement.
- `cd paper_runtime && pyright`: 39 errors, 0 warnings — PRE-EXISTING (test fixtures: protocol mismatches, Optional env var indexing).
- `git diff --check`: clean.

## Finding validation tracker

| Item | Finding | Initial status | Evidence | Planned correction | Tests | Final status |
| ---- | ------- | -------------- | -------- | ------------------- | ----- | ------------- |
| 1 | SUBMISSION_REQUESTED stranding / no safe recovery state machine | CONFIRMED | `retry_external_paper_order` required `current["new_state"]==STATE_UNKNOWN`, but a crash right after the reservation+SUBMISSION_REQUESTED checkpoint (before any broker response) left the chain at SUBMISSION_REQUESTED forever — reconciliation recorded lookup evidence but never transitioned the chain, so retry stayed permanently blocked (RETRY_NOT_ALLOWED) | Added `external_broker.py::recover_stranded_submission` (public, lease-fenced, idempotent, never calls submit/preview) delegating to existing `_reconcile_locked`; added `_bridge_stranded_submission_requested` inside `_run_reconciliation` to transition SUBMISSION_REQUESTED -> UNKNOWN_REQUIRES_RECONCILIATION on NOT_FOUND/timeout/malformed-response, with the lookup evidence row correctly tied to the *new* UNKNOWN event (reordered lookup-save to occur after the bridge) | `tests/unit/test_stranded_submission_recovery.py` (11 tests: found/not_found/timeout/malformed/idempotent-reject/no-submit-call/retry-gate/reservation-held/terminal-release/fresh-connection/not-applicable) | FIXED |
| 2 | Transaction helper silently rolls back caller-owned work | CONFIRMED | `storage/database.py::begin_immediate` (pre-fix) called `conn.rollback()` unconditionally whenever `conn.in_transaction` was true | New `storage/transactions.py` module: connections opened with `isolation_level=None` (true autocommit, no implicit transactions); `begin_immediate`/`transaction()` raise `TransactionAlreadyActiveError` instead of rolling back; `schema_version.py` and `storage/trading_repositories.py::save_frozen_recommendation` migrated to the shared `transaction()` CM (the latter had a latent atomicity bug this surfaced: two bare `conn.execute()` calls with a single trailing `conn.commit()` relied on legacy-mode implicit transactions spanning both statements — under `isolation_level=None` each would have autocommitted independently) | `tests/unit/test_transaction_discipline.py` (rewritten, 15 tests) + `tests/unit/test_recommendation_builder.py::test_transaction_rollback_on_persistence_failure` (unchanged, now passes for the right reason) | FIXED |
| 3 | Snapshot identity omits cash/ledger material state | CONFIRMED | `compute_snapshot_id` hashed only `{book_id, as_of, position_price_inputs}` via an unsafe `json.dumps(default=str)`; `source_hash` separately hashed `{book_id, as_of, positions, settlement_policy_version}` — two different payloads, neither including cash_available/reserved/settled, cash-ledger hash, snapshot_methodology_version, or per-position available_quantity/average_cost/realized_pnl/price-provenance fields | One canonical `identity_payload` built in `valuation.py::build_portfolio_snapshot` (book_id, as_of, cash available/reserved/settled, a canonical cash-ledger-entries hash, settlement + snapshot methodology versions, per-position quantity/available_quantity/average_cost/realized_pnl/full price provenance) feeds both `compute_snapshot_id` (now `hash_config`-based, single-arg) and `source_hash`; `save_snapshot` now raises `SnapshotIdentityConflictError` if a stored row's source_hash ever disagrees under the same snapshot_id | `tests/unit/test_snapshot_identity.py` (16 tests: 11 required change scenarios + idempotent replay + conflict-detection + corrected-snapshot-gets-new-row + position/ledger order-independence + unsupported-object-rejected) | FIXED |
| 4 | Lease heartbeat uses caller time, no fencing | CONFIRMED | `OrderLeaseHandle.heartbeat(now)`/`verify(now)` took the caller's stale `now`; `verify()` was defined but never called anywhere; default TTL (30s) equaled the runtime client's own default request timeout (30s) | `OrderLeaseHandle` now stores `clock` and reads fresh time internally on every `heartbeat()`/`verify()`; added `heartbeat_or_raise()`/`verify_or_raise()` (raise `OrderLeaseLostError`) used at every protected-write call site (preview, submit checkpoint, retry, refresh-preview, cancel, reconciliation); TTL default raised 30->45s with a new `PaperBooksConfigError` validation (`TTL > DEFAULT_REQUEST_TIMEOUT_SECONDS + heartbeat_margin`, new shared constant in `runtime/client/process_client.py`) | `tests/unit/test_external_order_lease_handle_fencing.py` (9 tests: fresh-clock heartbeat, stale-now rejected, failed-heartbeat aborts, takeover generation/fencing/no-cross-release, slow-runtime-call survives via heartbeat, TTL config validation both sides, repo default satisfies validation, lease-loss-before-write leaves no partial state) | FIXED |
| 5 | Runtime transport reused after timeout | CONFIRMED | `_request()` only marked the transport unhealthy on a *detected* response mismatch (`ProtocolViolationError`), not on the timeout itself; a bare `queue.Empty` timeout left the transport "healthy" so a later request (including the documented recovery lookup) could read a stale late response | `_mark_unhealthy_after_timeout()` now called on every timeout; `_request` raises `RuntimeUnavailableError` for any op on an unhealthy transport unless the op is in `_RETRYABLE_ON_TIMEOUT`, in which case it transparently calls `start()` (fresh child, re-verified health/capabilities) before proceeding — mutating ops (`submit_order` etc.) are never auto-retried | `tests/unit/test_runtime_client.py` (rewrote 2 stale-contract tests + added 6: unhealthy-on-timeout, no-auto-retry-for-mutating, transparent-restart-for-read-only, stale-response-never-reused-after-restart, kill-escalation, repeated-real-timeout-cycles-no-leak); `tests/unit/test_submit_credentialed_paper_order.py` updated to a two-transport fixture (`sequential_fake_transport_factory`, new in `tests/support/runtime_client_fixtures.py`) | FIXED |
| 6A | Local BUY intent persisted before reservation | CONFIRMED | `execution.py::submit_and_simulate` called `repo.save_order_intent` (auto-commit) then separately `cash_ledger.reserve_for_order` (own commit) — two independent transactions | New intent path: claim+intent+reservation atomic under `transaction()`; existing-intent path: fail-closed consistency check (`remaining_buy_reservation == notional_usd`), no fabricated release | `tests/unit/test_execution_namespace_claims.py` (crash-after-claim, crash-after-intent, replay-fails-closed) | FIXED |
| 6B | No durable execution namespace claim (local vs external) | CONFIRMED | Exclusivity was emergent from two asymmetric checks (`has_external_execution_evidence` scan of 4 tables; external `_intent()`'s terminal-status check) — a claimed-but-not-yet-evidenced or non-terminal race was not caught | New `paper_order_execution_claims` table (book_id+intent_id PK, immutable once claimed — simpler fail-closed policy, no release path); `claim_execution_namespace`/`load_execution_namespace_claim`/`ExecutionNamespaceConflictError` in repo; wired into `execution.py` (local) and `external_broker.py::_intent`/`preview_external_paper_order` (external, claims at first preview); schema-version migration 2 backfills legacy rows | `tests/unit/test_execution_namespace_claims.py` (13 tests: local-wins, external-wins, exactly-one-claim, no-double-reserve, no-double-fill, crash reproductions, replay integrity, cancel-release, restart-survival, legacy-migration-and-idempotency, conflict/idempotent-claim) | FIXED |
| 7 | shadow/config.py permissive boolean coercion | CONFIRMED | All 9 boolean fields (`shadow_operations.*`, `schedule.enabled`, `budgets.require_pricing_for_real_claude`, `safety.pause_on_*`) parsed via plain `bool(...)` — `bool("false")`/`bool("no")` are both `True` | Added local `_strict_bool` (mirrors `paper_books/config.py`/`research/scheduled_research_config.py`'s existing pattern: `type(value) is not bool` fails closed) and applied it to all 9 fields | `tests/unit/test_shadow_config.py` (+70 parametrized cases: real booleans accepted, quoted/int/None/list/mapping rejected, default repo config still loads, config hash still deterministic) | FIXED |
| 8 | Provider health sample floor uses symbols_attempted; no hysteresis | CONFIRMED | `scheduler.py::_build_health_inputs_from_cycle_result` set `provider_request_count=symbols_attempted` and `provider_success_rate=completed/symbols_attempted` — a symbol-count proxy, not real provider-call telemetry; `evaluate_cycle_health` was purely per-cycle with no persisted multi-cycle state; `provider_severe_error` was never set from real data | Part A: new `evidence_providers/health.py::compute_cycle_provider_telemetry`/`classify_severe_error` + `persistence.py::list_provider_requests_in_window` wired into `_build_health_inputs_from_cycle_result` (real per-request count/rate/per-provider breakdown/required-provider-missing when real telemetry exists this cycle's window, falls back to the symbol proxy only when zero real rows exist — e.g. offline/deterministic test cycles). Part B: bounded `SEVERE_ERROR_CATEGORIES` enum classified from persisted `error_code`/`http_status`/`rate_limited`/`retry_count` (never raw exception text). Part C: new `shadow/health_hysteresis.py` + `shadow_health_hysteresis_state` table — versioned `PersistentHealthPolicyConfig`, consecutive-failure/recovery counters, INSUFFICIENT_DATA cycles never counted, idempotent per cycle_id, policy-hash-boundary reset, never overrides PAUSED_MANUAL/KILLED; wired into `scheduler.py`'s per-cycle flow (observational only — no resume() call) | `tests/unit/test_provider_health_telemetry.py` (18 tests: classification + telemetry aggregation + per-provider/required-provider coverage + real-vs-proxy wiring), `tests/unit/test_health_hysteresis.py` (13 tests covering all 14 required scenarios), `test_shadow_scheduler.py::test_completed_cycle_persists_hysteresis_state` (end-to-end wiring) | FIXED |

## Architecture and transaction decisions

See `docs/milestone11-3-1-safety-closure.md`'s "Architecture decisions"
section for the full writeup. Key decisions: `isolation_level=None` +
`TransactionAlreadyActiveError` (reject-nested, not savepoint) for Item 2;
simpler fail-closed (no release path) execution-namespace claims for Item 6;
transparent in-client restart-on-retry (bounded allowlist) for Item 5;
single global `"default"` hysteresis scope for Item 8 Part C.

## Schema changes

- `paper_order_execution_claims` (new, Item 6) — migration 2 backfills
  legacy rows.
- `shadow_health_hysteresis_state` (new, Item 8 Part C) — no backfill
  needed (absent row = fresh HEALTHY state).
- (Unrelated to this milestone: migration 3 `claude_code_usage_provenance`
  columns appeared on this branch from a separate concurrent workstream —
  not authored/reviewed here.)

## Crash and concurrency reproductions

All covered by dedicated regression tests rather than ad hoc manual runs:

- external checkpoint -> crash -> restart -> broker FOUND/NOT_FOUND/timeout/malformed
  -> `test_stranded_submission_recovery.py`
- local namespace claim -> crash before intent/reservation -> `test_execution_namespace_claims.py`
- local intent/reservation transaction interrupted -> same file
- runtime request timeout with late response -> `test_runtime_client.py`
- two concurrent local BUYs / lease takeover while old owner writes -> `test_external_order_lease_handle_fencing.py`,
  `test_transaction_discipline.py` (two-connection BEGIN IMMEDIATE serialization)
- two snapshot writers for the same natural scope -> `test_snapshot_identity.py`
- repeated recovery / lookup / fill application / reservation release / namespace claim / migration / snapshot
  persistence / health evaluation -> idempotency assertions embedded in each item's own test file
- Concurrency-focused test files repeated 10x via shell loop: stable (440/440 passed).

## Files changed

See `docs/milestone11-3-1-safety-closure.md` "Fixes implemented" for the
full list (19 source files + 12 test files).

## Tests added

See `docs/milestone11-3-1-safety-closure.md` "Tests added" table — 190+ new/
updated test cases across 12 test modules.

## Commands run

- git rev-parse HEAD / git branch --show-current / git status --short / git log --oneline -20
- pytest tests/ -q --tb=short (baseline + after every item + final)
- cd paper_runtime && pytest tests/ -q --tb=short
- env -u ANTHROPIC_API_KEY -u ANTHROPIC_MODEL -u ANTHROPIC_BATCH_POLL_INTERVAL_SECONDS -u ALPACA_API_KEY
  -u ALPACA_API_SECRET -u ALPACA_IS_PAPER -u ALPACA_BASE_URL -u REDDIT_MCP_MODE -u REDDIT_MCP_COMMAND
  -u REDDIT_AUTH_MODE pytest tests/ -q --tb=short
- pyright ; cd paper_runtime && pyright
- git diff --check
- per-new-test-module `pytest <file> -q` runs
- 10x repeated concurrency-test-file runs via shell loop

## Open issues

None blocking. See "Remaining limitations" below for documented,
non-blocking follow-ups.

## Remaining limitations

- Item 8: `compute_cycle_provider_telemetry`'s `required_providers` param
  is not yet populated from a concrete per-category config at the
  `scheduler.py` call site (no single canonical "required providers"
  constant exists in the codebase yet) — the missing-required-provider
  detection is implemented and tested at the function level; wiring a
  concrete list through the scheduler is a follow-up.
- Item 8: TLS failures are not distinguishable from generic connection
  failures at the currently-persisted field granularity (both fold into
  `DNS_OR_CONNECTION_FAILURE`, documented in `classify_severe_error`'s
  docstring).
- Item 8 Part C: hysteresis is a single global `"default"` scope (correct
  for this system's current one-pipeline shape).
- pyright is non-blocking per project convention; post-milestone error
  count (1951 root, up from 1888 baseline) follows the codebase's existing
  Optional-narrowing test-fixture pattern in ~15 new test modules — no new
  category of error.

## Resume instructions

Not applicable — milestone complete on this branch
(`agent/milestone-11-3-1-safety-closure`). If further work is needed, start
from the "Remaining limitations" list above.

## Final status

COMPLETE — all 8 items CONFIRMED and FIXED with passing regression tests.
Full suite: 2079 passed, 16 skipped (clean and credential-free-env
identical). `docs/milestone11-3-1-safety-closure.md` has the full
implementation report.
