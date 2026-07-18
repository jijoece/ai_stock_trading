# Milestone 11.3.2 — Operational Integrity Closure

## Starting point and baseline

- Starting commit: `3c86a70244bc000b237ba7a98a3e6303cbdc7225`
- Branch: `agent/milestone-12-codex-provider`
- Initial worktree: the milestone specification was the only untracked file
- Main baseline: 2177 passed, 17 skipped
- Clean-environment baseline: 2177 passed, 17 skipped
- Paper-runtime baseline: 59 passed
- Root and paper-runtime Pyright: unavailable in the existing virtual environment; CI's whole-project jobs remain non-blocking (`continue-on-error: true`)

The starting commit includes PR #15 plus the subsequent Milestone 12 Codex-provider commit. That existing work was preserved.

## Finding summary

| Finding → classification → correction → regression evidence |
|---|
| Hysteresis was observational → CONFIRMED → effective pause now consumes persistent hysteresis, with structural-only bypass → `test_operational_integrity_health.py`, scheduler tests |
| Zero/sub-floor samples were misqualified → CONFIRMED → removed symbol fallback; check status determines qualification → provider-health and sample-floor tests |
| Required coverage was not enforced → CONFIRMED → versioned category/provider policy derived from frozen config, fail-closed missing coverage → operational-integrity health tests |
| Provider ownership used symbol/time windows → CONFIRMED → immutable request correlation and exact cycle query → correlation and migration tests |
| Generic no-response errors were severe → CONFIRMED → typed transport taxonomy at the HTTP boundary; timeouts are transient → transport-category tests |
| Lease verification had TOCTOU gaps → CONFIRMED → `BEGIN IMMEDIATE` plus in-transaction generation verification → real two-connection takeover tests |
| Snapshot persistence raced → CONFIRMED → atomic conflict insert and one header/positions transaction → real concurrent snapshot tests |
| Hysteresis evidence was incomplete → CONFIRMED → append-only per-cycle evaluation history atomically written with rolling state → history/idempotency/rollback tests |

All eight findings are classified `FIXED` by the implemented regression evidence. Real provider and broker behavior remains `NEEDS_RUNTIME_EVIDENCE` because this milestone deliberately made no network call.

## Health-state architecture

The scheduler now follows:

```text
exact cycle telemetry
→ single-cycle checks
→ provider-check qualification
→ atomic hysteresis state + history
→ structural bypass or hysteresis status
→ effective decision
→ pause actor, summary, checks, and alerts
```

Ordinary timeouts, resets, 5xx responses, rate limits, failure-rate breaches, and retry exhaustion go through persistent hysteresis. Authentication, configuration, TLS, and confirmed protocol/schema failures may pause immediately. Duplicate-prevention, configured reconciliation, and configured budget failures remain structural. Recovery changes hysteresis state only; it never calls `resume()`. Manual pause and kill remain blocking.

Each run summary stores the single-cycle, hysteresis, and effective statuses, while `shadow_health_hysteresis_evaluations` stores the complete per-cycle decision evidence.

## Provider-cycle correlation

`evidence_provider_requests` now carries:

- `correlation_mode`
- `research_cycle_id`
- `scheduler_run_id`
- `research_run_id`
- `symbol_attempt_id`
- `provider_request_group_id`

The scheduler establishes the scheduler-run context, and the scheduled-cycle service nests the deterministic cycle/symbol context around every provider call. Context variables isolate overlapping threads/tasks. Scheduled writes require both cycle and scheduler identities. Manual and legacy rows are explicit modes; legacy rows remain nullable and are never attributed to a current cycle. Health queries use only `WHERE research_cycle_id = ?`, with deterministic `created_at, request_id` ordering.

## Required-provider policy

`ProviderCoveragePolicy` is versioned and hashed with the frozen evidence-provider configuration. Production categories are:

- `market_data` → `alpaca-data` required
- `corporate_filings` → `sec-edgar` required
- configured news/social providers → optional

Aliases normalize deterministically. Missing optional providers do not fail health. A required provider that is absent, or a required category with no enabled provider, fails coverage and prevents a healthy result. The resolved policy, observed providers, missing coverage, configuration hash, and per-provider request/success/failure metrics are persisted.

The shipped evidence-provider configuration has market data disabled. Therefore a real recurring profile fails required-category preflight until an operator explicitly configures that provider; this is intentional and keeps unattended scheduling disabled.

## Health qualification and sample floor

No provider rows now means sample size zero and success rate `None`. Fixture mode is explicitly `NOT_APPLICABLE`. Production samples below `minimum_requests_for_failure_rate` remain `INSUFFICIENT_DATA`. Qualification is derived from the provider health check:

- `PASS`, `WARNING`, `FAIL` → qualified
- `INSUFFICIENT_DATA`, `NOT_APPLICABLE` → unqualified

Unqualified cycles move neither failure nor recovery streak. Missing required coverage is an explicit qualified failure, not fabricated success.

## Severe transport taxonomy

The HTTP adapter persists one bounded category:

`NONE`, `TIMEOUT`, `DNS_FAILURE`, `CONNECTION_REFUSED`, `CONNECTION_RESET`, `TLS_FAILURE`, `AUTHENTICATION_FAILURE`, `RATE_LIMITED`, `HTTP_CLIENT_ERROR`, `HTTP_SERVER_ERROR`, `PROTOCOL_ERROR`, `CONFIGURATION_ERROR`, or `UNKNOWN_TRANSPORT_ERROR`.

Typed `httpx` and cause-chain exceptions are mapped once at the adapter boundary. HTTP 401/403, 429, and 5xx responses have distinct categories. Raw exception text is absent from persisted callback records. Unknown no-response failures are no longer assumed to be severe.

## Lease fencing

`OrderLeaseHandle.fenced_write()` performs `BEGIN IMMEDIATE`, reads a fresh clock, verifies `lease_key + owner_id + generation + ACTIVE + unexpired`, executes the protected mutation, and commits. It rolls back on `BaseException` and raises `OrderLeaseLostError` on invalid ownership.

Preview/event persistence, submission reservation/checkpoint, post-runtime event/status writes, fill application, reservation consumption/release, retry-evidence consumption, and cancellation transitions use the fenced transaction. Runtime calls remain outside write transactions. Release remains generation-conditioned, so a stale owner cannot release a newer owner's lease.

The real-connection test proves owner B cannot reclaim while owner A's protected transaction is open, can reclaim generation N+1 after commit and expiry, and causes owner A's next write to fail without a partial row.

## Snapshot concurrency

Snapshot persistence now opens one explicit transaction and executes:

```sql
INSERT ... ON CONFLICT(book_id, snapshot_id) DO NOTHING
```

On conflict it reloads and compares `source_hash`: an identical replay returns `False`; a different hash raises `SnapshotIdentityConflictError`. Position rows are inserted only by the header winner and share the same transaction. A position failure rolls back the header, and retry succeeds.

## Schema and migrations

Schema version 5 adds provider correlation and transport fields while preserving legacy rows. The additive schema also creates hysteresis evaluation history and run-summary evidence columns. Migration behavior is idempotent, forward-version checks remain in force, and the PR #15-shaped provider-request migration test proves legacy data retention and non-attribution.

## Tests added or updated

- `test_operational_integrity_health.py`: exact correlation, migration, coverage, aliases, transport taxonomy, sample qualification, effective hysteresis, complete history, idempotency, and atomic rollback
- `test_operational_integrity_concurrency.py`: identical/conflicting snapshot races, snapshot rollback/retry, write-lock blocking, takeover, and stale-generation rejection
- Existing provider-health, scheduler, lease, snapshot, and end-to-end assertions updated to reject the removed symbol fallback and identify fixture telemetry explicitly

## Verification

- Focused health/telemetry/scheduler/snapshot/lease groups: passing
- Real two-connection concurrency group: passing
- Full suite after implementation: 2195 passed, 17 skipped
- Clean-environment suite (credential-shaped variables removed): 2195 passed, 17 skipped
- Paper-runtime suite: 59 passed
- Pyright: executable unavailable in the existing environment; no passing claim is made

No real provider, model, or broker service was invoked. No credentialed smoke test was run.

## Remaining limitations

- Hysteresis uses the current single global `default` scope; the schema supports more scopes later.
- Existing historical provider rows cannot be exactly correlated and are deliberately excluded from new-cycle health.
- The default real-provider profile is not operationally ready for recurring scheduling because required market data is disabled.
- Real Alpaca paper behavior still requires a separately authorized credentialed smoke test.
- Whole-project Pyright remains non-blocking in CI and was unavailable locally in the existing virtual environment.

## Operational go/no-go

Research-only and local simulation remain available. Unattended real scheduling should remain disabled until required providers are explicitly configured and real health history is accumulated. External Alpaca paper execution remains explicit, operator-initiated, and disabled by default. Live trading remains unavailable.

| Capability | Status |
|---|---|
| Research-only analysis | READY |
| Local simulated paper trading | READY |
| Manual soak campaigns | READY |
| Unattended recurring research scheduling | KEEP_DISABLED |
| Manual external Alpaca paper execution | KEEP_DISABLED |
| Real Alpaca paper smoke | NOT_READY |
| Live trading | NOT_IMPLEMENTED |
