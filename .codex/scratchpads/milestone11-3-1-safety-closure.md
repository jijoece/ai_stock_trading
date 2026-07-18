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
| 3 | Snapshot identity omits cash/ledger material state | TBD | TBD | TBD | TBD | TBD |
| 4 | Lease heartbeat uses caller time, no fencing | CONFIRMED | `OrderLeaseHandle.heartbeat(now)`/`verify(now)` took the caller's stale `now`; `verify()` was defined but never called anywhere; default TTL (30s) equaled the runtime client's own default request timeout (30s) | `OrderLeaseHandle` now stores `clock` and reads fresh time internally on every `heartbeat()`/`verify()`; added `heartbeat_or_raise()`/`verify_or_raise()` (raise `OrderLeaseLostError`) used at every protected-write call site (preview, submit checkpoint, retry, refresh-preview, cancel, reconciliation); TTL default raised 30->45s with a new `PaperBooksConfigError` validation (`TTL > DEFAULT_REQUEST_TIMEOUT_SECONDS + heartbeat_margin`, new shared constant in `runtime/client/process_client.py`) | `tests/unit/test_external_order_lease_handle_fencing.py` (9 tests: fresh-clock heartbeat, stale-now rejected, failed-heartbeat aborts, takeover generation/fencing/no-cross-release, slow-runtime-call survives via heartbeat, TTL config validation both sides, repo default satisfies validation, lease-loss-before-write leaves no partial state) | FIXED |
| 5 | Runtime transport reused after timeout | CONFIRMED | `_request()` only marked the transport unhealthy on a *detected* response mismatch (`ProtocolViolationError`), not on the timeout itself; a bare `queue.Empty` timeout left the transport "healthy" so a later request (including the documented recovery lookup) could read a stale late response | `_mark_unhealthy_after_timeout()` now called on every timeout; `_request` raises `RuntimeUnavailableError` for any op on an unhealthy transport unless the op is in `_RETRYABLE_ON_TIMEOUT`, in which case it transparently calls `start()` (fresh child, re-verified health/capabilities) before proceeding — mutating ops (`submit_order` etc.) are never auto-retried | `tests/unit/test_runtime_client.py` (rewrote 2 stale-contract tests + added 6: unhealthy-on-timeout, no-auto-retry-for-mutating, transparent-restart-for-read-only, stale-response-never-reused-after-restart, kill-escalation, repeated-real-timeout-cycles-no-leak); `tests/unit/test_submit_credentialed_paper_order.py` updated to a two-transport fixture (`sequential_fake_transport_factory`, new in `tests/support/runtime_client_fixtures.py`) | FIXED |
| 6A | Local BUY intent persisted before reservation | CONFIRMED | `execution.py::submit_and_simulate` called `repo.save_order_intent` (auto-commit) then separately `cash_ledger.reserve_for_order` (own commit) — two independent transactions | New intent path: claim+intent+reservation atomic under `transaction()`; existing-intent path: fail-closed consistency check (`remaining_buy_reservation == notional_usd`), no fabricated release | `tests/unit/test_execution_namespace_claims.py` (crash-after-claim, crash-after-intent, replay-fails-closed) | FIXED |
| 6B | No durable execution namespace claim (local vs external) | CONFIRMED | Exclusivity was emergent from two asymmetric checks (`has_external_execution_evidence` scan of 4 tables; external `_intent()`'s terminal-status check) — a claimed-but-not-yet-evidenced or non-terminal race was not caught | New `paper_order_execution_claims` table (book_id+intent_id PK, immutable once claimed — simpler fail-closed policy, no release path); `claim_execution_namespace`/`load_execution_namespace_claim`/`ExecutionNamespaceConflictError` in repo; wired into `execution.py` (local) and `external_broker.py::_intent`/`preview_external_paper_order` (external, claims at first preview); schema-version migration 2 backfills legacy rows | `tests/unit/test_execution_namespace_claims.py` (13 tests: local-wins, external-wins, exactly-one-claim, no-double-reserve, no-double-fill, crash reproductions, replay integrity, cancel-release, restart-survival, legacy-migration-and-idempotency, conflict/idempotent-claim) | FIXED |
| 7 | shadow/config.py permissive boolean coercion | TBD | TBD | TBD | TBD | TBD |
| 8 | Provider health sample floor uses symbols_attempted; no hysteresis | TBD | TBD | TBD | TBD | TBD |

## Architecture and transaction decisions

(TBD — populated during implementation)

## Schema changes

(TBD)

## Crash and concurrency reproductions

(TBD)

## Files changed

(TBD)

## Tests added

(TBD)

## Commands run

- git rev-parse HEAD / git branch --show-current / git status --short / git log --oneline -20
- pytest tests/ -q --tb=short
- cd paper_runtime && pytest tests/ -q --tb=short
- env -u ... pytest tests/ -q --tb=short
- pyright ; cd paper_runtime && pyright
- git diff --check

## Open issues

(TBD)

## Remaining limitations

(TBD)

## Resume instructions

If resuming: check this file's finding tracker for TBD rows, re-run baseline commands above to confirm no drift, then continue from the last item without a FIXED final status.

## Final status

IN PROGRESS — baseline recorded, findings not yet validated.
