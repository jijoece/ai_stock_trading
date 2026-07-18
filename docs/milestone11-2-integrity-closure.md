# Milestone 11.2 — Full Execution, Transaction, Recovery, and Codebase Integrity Closure

## Status: PARTIAL — Parts 1, 3-22 closed with regression tests; Parts 2 (full fixture matrix), 23-37 not attempted

This milestone specifies 37 parts / 44 acceptance criteria. This pass closed
Parts 1 and 3 through 22 (CI, migration/trigger versioning, transaction
discipline, atomic local fills, BUY/SELL reservation atomicity, local/
external execution exclusivity, renewable/fenced order leases, sequence-
based event ordering, post-submit/post-cancel fail-safe fill handling,
runtime open-SELL accounting, duplicate-broker-order detection, qualifying-
provider activation gating, an audited retry-preview-refresh feature,
lookup immutability, runtime timeout/process cleanup, and dedicated
runtime env-file allowlisting, and recovery-lookup failure persistence),
each with a passing regression test added or updated. **Part 2's full
prior-schema fixture matrix (beyond the one lookup-trigger fixture) and
Parts 23-37 (provider/HTTP/rate-limiter robustness, strict config booleans,
deterministic config hashing, no-filesystem-side-effects config loading,
disclosure-extraction negation handling, flexible market-data validation,
SEC point-in-time assurance, settlement semantics, legacy subsystem
quarantine, schema versioning, remaining documentation, and the full Part
37 offline end-to-end scenario suite) were not attempted.** Nothing below
claims completion of an item that wasn't verified with a passing test.

## Starting / current commit

- Starting commit: `05067b5b00e8e19585b0f21c1e2b26bc4ba5af81` (branch `agent/milestone-11-1-external-paper-safety`)
- No commits made this session — all changes are in the working tree, per instructions.

## Baseline

- `pytest tests/ -q` (ambient dev-shell env, carries a real `ANTHROPIC_API_KEY`/`ALPACA_API_KEY`/`ALPACA_API_SECRET`): 1714 passed, 15 skipped.
- Same command under a simulated clean-CI env (all credential-shaped vars unset): **2 failures**, reproducing the actual GitHub Actions `main-tests` failure (Part 1).
- `paper_runtime/pytest tests/ -q`: 47 passed (clean under both ambient and stripped env).
- GitHub Actions: `main-tests` failing on the last two PR runs (#7, #8); `type-check`, `paper-runtime-tests`, `migration-smoke` green. `type-check` already runs both pyright steps with `continue-on-error: true` (documented, not silently claimed green).
- `git diff --check`: clean throughout.

## Part 1 — CI root cause and fix (CONFIRMED_AND_FIXED)

**Root cause:** two tests never stub `cli_mod.load_config`, so the CLI's
credentials preflight (`cfg.anthropic_api_key`) reads whatever is in the
*ambient* process environment. This dev shell carries a real
`ANTHROPIC_API_KEY` (an artifact of running inside a Claude Code session),
so locally the credentials check passes through to the pricing check and
the test correctly observes `PRICING_NOT_CONFIGURED`. In clean CI (no such
key), the same path returns `MISSING_CREDENTIALS` first, and the
hard-coded assertion fails.

**Fix:** `tests/unit/test_shadow_cli_provider_mode.py::test_real_mode_anthropic_missing_pricing_fails_closed` and `tests/integration/test_milestone_7_1_shadow_integration.py::test_pricing_failure_cli_preflight_blocks_before_db_session` now stub `cli_mod.load_config` with a present fake credential, isolating the pricing-only failure path from ambient credentials — mirroring the sibling `test_real_mode_anthropic_missing_credentials_fails_closed` test's existing pattern.

**Verification:** the full suite passes with zero failures under `env -u ANTHROPIC_API_KEY -u ANTHROPIC_MODEL -u ANTHROPIC_BATCH_POLL_INTERVAL_SECONDS -u ALPACA_API_KEY -u ALPACA_API_SECRET -u ALPACA_IS_PAPER -u ALPACA_BASE_URL -u REDDIT_MCP_MODE -u REDDIT_MCP_COMMAND -u REDDIT_AUTH_MODE pytest tests/ -q` — the standard reproduction command for "clean CI" going forward.

**Not done:** `secret-scan` and `dependency-audit` CI jobs (required per spec) do not exist yet; the blocking safety-critical-subset type-check policy (vs. today's fully non-blocking `continue-on-error`) was not implemented.

## Parts 3 + 18 — Lookup-trigger migration (CONFIRMED_AND_FIXED)

The Milestone-11.1 `trg_paper_external_lookups_no_update` trigger only
checked `OLD.consumed_by_retry_event_id IS NOT NULL` — it blocked
re-consumption but permitted arbitrary edits to every other field on an
unconsumed row (reproduced: `UPDATE ... SET result='FOUND'` on an
unconsumed row succeeded). `CREATE TRIGGER IF NOT EXISTS` also cannot
change an existing trigger's behavior on a pre-existing database.

**Fix:** rewrote the trigger to the full field-immutability contract
(NULL-safe `IS` comparisons on every column, one allowed
`consumed_by_retry_event_id: NULL -> non-NULL` transition), and added a
`paper_books_trigger_versions` table + `_upgrade_triggers()` that
explicitly `DROP TRIGGER`s a stale/unversioned trigger before the
`CREATE TRIGGER IF NOT EXISTS` script runs — the first real "changed
trigger" migration mechanism in this codebase, reusable for future
trigger-behavior changes.

**Test:** `tests/unit/test_paper_external_lookup_trigger_migration.py` — builds a database with the exact Milestone-11.1 trigger SQL, confirms the legacy behavior reproduces, then verifies the upgraded contract (single consumption succeeds; second consumption, field-clearing, co-mingled-field-update, and deletion all rejected) and idempotent re-open.

## Part 4 — Explicit SQLite transaction mode (DESIGN_TRADEOFF_DOCUMENTED)

`storage/database.py::connect` still uses Python's default (legacy)
`isolation_level=""` rather than `isolation_level=None` with fully
explicit `BEGIN`/`COMMIT`/`ROLLBACK` everywhere. **Deliberate scope
decision:** the repository layer pervasively relies on the
`commit: bool = True/False` "participate in caller's transaction"
convention across 20+ modules, which implicitly depends on Python's legacy
auto-BEGIN-before-DML behavior. A global switch to `isolation_level=None`
would require auditing and rewriting the transaction boundary of every
write path in the codebase — disproportionate to "the narrowest safe
correction" for one milestone, and high-risk without exhaustive regression
coverage across every subsystem in one pass. The concrete defect this
created (Part 5) was fixed directly instead.

## Part 5 — Manual `BEGIN IMMEDIATE` hardening (CONFIRMED_AND_FIXED)

All 8 manual `BEGIN IMMEDIATE` call sites (`shadow/lease.py` ×4,
`paper_books/recurring_scheduler.py` ×3, `paper_books/external_broker.py`
×1) issued `conn.execute("BEGIN IMMEDIATE")` **before** entering their own
`try` block — if a prior unguarded write had left a pending implicit
transaction open (Python's legacy auto-BEGIN), `BEGIN IMMEDIATE` itself
would raise outside the `try`, bypassing rollback and leaving the
connection's transaction state ambiguous for the caller.

**Fix:** `storage/database.py::begin_immediate(conn)` rolls back any
pending implicit transaction first, then issues `BEGIN IMMEDIATE`; all 8
call sites now use it, moved inside their `try` blocks.

**Tests:** `tests/unit/test_transaction_discipline.py` — pending-DML-before-`begin_immediate` doesn't raise; the cleared write is genuinely rolled back; manual-transaction success/rollback; rollback leaves the connection usable; a failed op on one connection never blocks a second real connection against the same file; `busy_timeout` confirmed at 5000ms.

## Part 6 — Atomic local simulated fill (CONFIRMED_AND_FIXED, partial)

`execution.py::submit_and_simulate`'s fill-application sequence (save →
position/lot → cash settlement → reservation release → order status) used
five independent, default-`commit=True` writes — a crash between any two
left a fill persisted with no corresponding position/cash effect, and the
existing `fill_exists()` idempotency check would then falsely treat that
partial state as complete.

**Fix:** wrapped the whole sequence in one `begin_immediate`/`commit`
transaction with `commit=False` on every sub-call; added a `commit`
parameter to `paper_books_repositories.py::update_order_status` (it
previously always committed unconditionally).

**Tests:** `tests/unit/test_paper_books_local_fill_atomicity.py` — crash injection at each of 4 stages leaves zero effects and a clean retry succeeds exactly once; full-success invariant; replay-does-not-duplicate.

**Not done:** a scanner for *pre-existing* legacy partial-fill rows (from before this fix) was not built — new writes are now atomic, but nothing detects historical corruption from before this session.

## Parts 7 + 8 — Reservation atomicity (CONFIRMED_AND_FIXED)

Both `cash_ledger.py::reserve_for_order` (BUY cash) and
`positions.py::reserve_shares_for_sell` (SELL shares — despite its
docstring literally claiming "Atomically reserve") performed a
check-then-act sequence with no lock between the read and the write. Two
concurrent reservation attempts against the same book (or book+symbol)
could both observe sufficient availability and both succeed, together
exceeding what was actually available.

**Fix:** both now wrap their check+insert in a `begin_immediate`
book-scoped (or book+symbol-scoped) transaction.

**Tests:** `tests/unit/test_paper_books_reservation_concurrency.py` — real two-thread, two-`sqlite3.Connection` races confirm exactly one of two concurrent 80-of-100 BUY reservations succeeds, exactly one of two concurrent 7-of-10 SELL reservations succeeds with `quantity = available + reserved` preserved, and different client/intent IDs cannot bypass the lock.

**Not done:** whether the reservation + `SUBMISSION_REQUESTED` event still need to be a single transaction for external submission (vs. the existing two-sequential-commits-with-compensating-release composition) was not independently re-verified against `external_broker.py::_submit_once`.

## Part 9 — Local/external execution exclusivity, reverse direction (CONFIRMED_AND_FIXED)

The local simulator already refused to fill an intent once external
evidence existed. The reverse direction did not exist:
`external_broker.py::_intent()` never checked local order status at all.

**Fix:** `_intent()` now rejects (`INTENT_NOT_ELIGIBLE_FOR_EXTERNAL`) when
local status is in the existing `TERMINAL_STATES` constant
(FILLED/CANCELLED/REJECTED/EXPIRED) — chosen over a narrower
"must-be-PENDING_SUBMISSION" check because external submission itself
writes its own in-flight states into that same shared column, and a
narrower check would have broken legitimate external retries.

**Tests:** `tests/unit/test_external_paper_broker.py::test_local_fill_blocks_subsequent_external_preview`, `::test_local_cancel_blocks_subsequent_external_submit`, `::test_pending_intent_still_previews_normally`.

## Part 10 — Renewable, fenced order leases (CONFIRMED_AND_FIXED)

The order-scope lease used a fixed 30-second TTL with no heartbeat/renewal
and no fencing generation — the default runtime request timeout is also
30s, so a single runtime call could exhaust the entire lease with zero
margin, and nothing prevented a stale owner (reclaimed by another caller)
from writing after a takeover.

**Fix:** added a `generation` column + versioned schema upgrade; repo
functions `acquire_external_order_lease` (now returns the acquired
generation), `heartbeat_external_order_lease`, `verify_external_order_lease`,
and a generation-fenced `release_external_order_lease`; config fields
`external_broker.order_lease_ttl_seconds`/`order_lease_heartbeat_seconds`
(validated TTL ≥ 2× heartbeat); `_order_lease` now yields an
`OrderLeaseHandle` with `.heartbeat()`/`.verify()`, wired into
heartbeat calls around the runtime calls in preview/submit.

**Tests:** `tests/unit/test_external_order_lease_fencing.py` — fresh-acquire generation, second-acquire-blocked, heartbeat survives past the original TTL, stale owner rejected after reclaim, verify fails after expiry.

**Not done:** per-write `.verify()` gating was only wired into preview/submit's heartbeat points, not independently added to cancel/reconcile/retry's own writes — the acquire/heartbeat/release fencing mechanism itself is fully tested, but full "every event-chain mutation verifies owner+generation" coverage across all 5 operations was not completed.

## Part 11 — Sequence-based event ordering (CONFIRMED_AND_FIXED)

External event-chain "current" queries used `ORDER BY created_at DESC,
rowid DESC` instead of the already-populated `scope_sequence` column — a
backward clock jump between two writes could select the earlier event as
current.

**Fix:** `load_latest_external_order_event`, `load_latest_external_order_event_for_intent`, and the general listing query now order by `scope_sequence` first (SQLite sorts NULL lowest, so legacy pre-upgrade rows never shadow sequenced ones).

**Tests:** `tests/unit/test_external_event_sequence_ordering.py` — backward-clock, mixed-offset timestamps, legacy-null-sequence-never-shadows.

## Parts 12 + 13 — Post-submit / post-cancel fail-safe fill handling (CONFIRMED_AND_FIXED)

`_submit_once`'s post-submit fill sweep and `cancel_external_paper_order`'s
post-cancel fill sweep were both completely unprotected — a raised
exception propagated with zero persisted evidence, and (for cancel) with
reservation-release/status-update accidentally-but-silently skipped.

**Fix:** both wrapped in try/except persisting a critical
`_persist_reconciliation` record (`MALFORMED_BROKER_FILL` or
`FILL_APPLICATION_FAILED`, tagged with a `stage` field) before re-raising;
for cancel, reservation release and terminal status update remain
downstream of the now-protected call so a failure there still visibly
withholds them.

**Tests:** `tests/unit/test_external_paper_broker.py::test_post_submit_fill_sweep_failure_persists_critical_reconciliation_before_raising`, `::test_post_cancel_fill_sweep_failure_persists_critical_and_withholds_reservation_release`.

## Part 14 — Runtime-side open-SELL accounting (CONFIRMED_AND_FIXED)

`paper_runtime/dispatcher.py::_validate_confirmed_long` checked only the
raw confirmed position quantity, never subtracting shares already
committed to other active open SELL orders for the same symbol.

**Fix:** now sums remaining (quantity − filled_quantity) across
active-state open SELL orders for the same symbol, excluding the current
client_order_id (idempotent retry), terminal orders, BUY orders, and other
symbols; uses `_parse_exact_int` so a fractional broker-reported quantity
fails closed. Wired into all 3 call sites.

**Tests:** `paper_runtime/tests/test_dispatcher_open_sell_accounting.py` — 9 tests covering every scenario named in the spec (no-open-SELL/6-open blocks 5/6-open allows 4/same-client-id-retry/BUY-excluded/other-symbol-excluded/terminal-excluded/partial-fill-remainder/fractional-quantity-fails-closed).

## Part 15 — Duplicate broker order detection (CONFIRMED_AND_FIXED)

`_detect_duplicate_broker_order` explicitly skipped any candidate order
whose `client_order_id` lacked the project's `epb-{book_id}-` prefix — a
manually-created Alpaca order, or one from another application, was never
detectable. It also treated a malformed/oversized recent-orders response
as "no duplicate" (fail-open).

**Fix:** removed the prefix-skip — same-prefix and non-prefixed
duplicates are now both flagged (with a distinguishing reason string);
malformed/non-dict/oversized entries now raise `MALFORMED_RUNTIME_RESPONSE`,
caught by `_reconcile_locked`'s existing outer wrapper and persisted as a
critical `RECONCILIATION_INTERNAL_ERROR`.

**Tests:** `tests/unit/test_external_paper_broker.py::test_manually_created_order_without_project_prefix_is_detected_as_duplicate`, `::test_malformed_recent_orders_response_fails_closed`.

## Part 16 — Qualifying-provider activation gating (CONFIRMED_AND_FIXED)

`recurring_scheduler.py::validate_activation_review` read
`provider_success_counts.real_provider_success_cycles` — a naive metric
that counts a cycle merely because *any* real provider succeeded, even
alongside another real provider's failure in the same cycle —
instead of the already-computed, already-persisted
`qualifying_real_provider_cycles` (requires *every* real-provider row in
the cycle to have succeeded; `provider_provenance.py` computes both
metrics separately and `soak_campaign.py` already persists both).

**Fix:** now reads `qualifying_real_provider_cycles`; a review missing
that key entirely (a legacy pre-field review) fails closed requiring
regeneration, rather than defaulting to 0 or silently falling back to the
naive metric.

**Tests:** `tests/unit/test_recurring_paper_scheduler.py::test_activation_uses_qualifying_not_naive_success_count`, `::test_activation_rejects_legacy_review_missing_qualifying_field`. (Fixing this also required updating the test file's own `ready_review` fixture, which itself constructed a legacy-shaped review missing the field — that's why the initial fix broke 9 pre-existing tests before the fixture was corrected.)

## Part 17 — Audited retry-preview refresh (CONFIRMED_AND_FIXED — net-new feature)

No mechanism existed to recover from a confirmed `NOT_FOUND` order whose
original preview had expired before a retry could be issued — the order
would become permanently stuck.

**Added:** `external_broker.refresh_retry_preview()` — read-only, makes no
broker/runtime call whatsoever; requires the order to be in
`UNKNOWN_REQUIRES_RECONCILIATION` with a fresh, unconsumed, authoritative
`NOT_FOUND` lookup matching the current event's ambiguous_event_id/
attempt_number/payload_hash/account_fingerprint, and the retry limit not
yet exceeded; creates a new preview row (new ID and expiry) without
consuming the lookup. Wired into the CLI as
`external-paper-refresh-retry-preview` (bypasses `_external_paper_cli`'s
runtime-subprocess spawn since it needs no runtime).

**Tests:** `tests/unit/test_external_paper_broker.py::test_refresh_retry_preview_unblocks_retry_after_original_preview_expires` (full UNKNOWN → NOT_FOUND → expire → refresh → retry → lookup-consumed-exactly-once scenario), `::test_refresh_retry_preview_rejected_once_broker_order_is_found`, `::test_refresh_retry_preview_makes_no_broker_call`.

## Part 19 — Runtime timeout response poisoning (PARTIALLY_CONFIRMED_AND_FIXED — deliberately narrower than literal spec)

**Finding confirmed and reproduced:** nothing prevented a late response
from a timed-out request from being consumed by a later, unrelated
request.

**Design decision:** the spec's literal "restart runtime after every
request timeout" was **not** implemented as a blanket policy, because
this codebase already has a deliberate, tested recovery pattern —
`_RETRYABLE_ON_TIMEOUT` — where a read-only follow-up lookup (e.g.
`get_order` after a timed-out `submit_order`) reuses the *same* connection
specifically to resolve the ambiguity. A blanket "mark unhealthy on every
timeout" fix broke `test_ambiguous_submission_recovers_via_lookup_not_blind_retry`
and 8 related pre-existing tests, because it disabled exactly that
documented recovery path. Instead: the transport is marked unhealthy and
torn down the moment `parse_response_line` actually detects a
request_id/operation mismatch — the concrete proof that desync happened —
rather than pre-emptively on every timeout regardless of whether the
follow-up read would have succeeded cleanly.

**Tests:** `tests/unit/test_runtime_client.py::test_timeout_alone_does_not_block_the_documented_recovery_lookup` (the documented pattern still works), `::test_late_stale_response_after_timeout_poisons_the_next_call_and_client_is_marked_unhealthy` (a genuine desync is detected and the transport is disabled for any further request).

## Part 20 — Runtime thread/process cleanup (CONFIRMED_AND_FIXED)

`SubprocessTransport.terminate()` never joined the stdout/stderr pump
threads or drained the stdout queue.

**Fix:** `terminate()` now always joins both pump threads (bounded
timeout) and drains the stdout queue after the process exits;
kill-then-wait added as a final fallback; streams explicitly closed.

**Test:** `tests/unit/test_runtime_client.py::test_repeated_start_shutdown_cycles_join_pump_threads_without_leaking` — uses a real `SubprocessTransport` and a real trivial child process across 3 start/shutdown cycles, asserts no new threads are left running.

## Part 21 — Dedicated runtime env-file validation (CONFIRMED_AND_FIXED)

`paper_runtime/configuration.py::_load_dotenv_if_present` called
`load_dotenv(explicit_path, override=False)` directly with no allowlist
enforcement — any key in the named file was loaded, no absolute-path
requirement, no permission check. A pre-existing test literally named
"loads only an explicitly named env file" proved this: its own fixture
mixed `ANTHROPIC_API_KEY` into the file and asserted credentials still
loaded successfully.

**Fix:** switched to `dotenv_values` (reads without mutating `os.environ`)
plus an explicit allowlist check — any unknown key anywhere in the file
rejects the *entire* file (not just that key), an absolute-path
requirement, symlink resolution before every check, and a POSIX
group/other-writable rejection. Never raises (health/capabilities must
stay answerable); failed validation just means nothing from the file
loads, leaving credentials absent and the environment correctly disabled.

**Tests:** `paper_runtime/tests/test_configuration.py` — updated the pre-existing test to assert the corrected behavior, plus 3 new tests (whole-file-rejected-on-unknown-key, relative-path-rejected, group-writable-rejected).

## Part 22 — Recovery lookup failure persistence (CONFIRMED_AND_FIXED)

`submit_credentialed_paper_order.py`'s recovery-lookup-after-ambiguous-submit
used a bare `except Exception: recovered = None` — the recovery lookup's
own failure reason was completely discarded; only the *original* submit
failure got a persisted record.

**Fix:** captures the recovery lookup's exception and persists it via
`exec_repo.record_failure(..., stage="credentialed_recovery_lookup", ...)`,
distinct from the original submit failure's own record.

**Test:** `tests/unit/test_submit_credentialed_paper_order.py::test_submission_unknown_when_recovery_lookup_also_fails` extended to assert both failure stages are persisted.

## Parts not attempted this session

- **Part 2** (full prior-schema fixture matrix for pre-Milestone-11 /
  Milestone-11 / Milestone-11.1 schemas): only the one lookup-trigger
  fixture (Part 3) exists.
- **Part 23** (provider-health sample-size floor): confirmed the gap —
  `shadow/health.py::CycleHealthInputs` has no request/symbol count field
  at all, so a 1-request cycle's 100% failure rate is indistinguishable
  from a 100-request cycle's — but adding `minimum_requests_for_failure_rate`/
  `minimum_symbols_for_failure_rate` requires threading a new count field
  through the dataclass, its construction call site(s), and new config
  fields; not done.
- **Parts 24-25** (HTTP client pooling/Retry-After/rate-limiter
  thread-safety): not inspected.
- **Parts 26-28** (strict scheduled-research booleans, deterministic config
  hashing, no-filesystem-side-effects config loading): not inspected.
- **Parts 29-31** (disclosure-extraction negation handling, flexible
  market-data validation, SEC point-in-time assurance): not inspected.
- **Part 32** (settlement semantics documentation/policy): not inspected.
- **Part 33** (legacy paper subsystem quarantine): not inspected — Part 16's
  triage noted the *active* `paper_books` qualifying-provider machinery is
  solid, but the separate legacy `paper/` ledger subsystem's operator-CLI
  reachability was not audited.
- **Part 34** (real schema-version table beyond the one trigger-version
  table added for Part 3): not done — Part 3's `paper_books_trigger_versions`
  table is scoped only to trigger definitions, not a general schema version.
- **Part 35** (remaining documentation): only the README top/bottom safety
  banners were corrected (done, see below); `.env.example`,
  `paper_runtime/README.md`, the Alpaca paper runbook, the recurring
  scheduler runbook, and ADRs were not audited for consistency.
- **Part 36/37** (remaining test-quality categories and offline end-to-end
  scenarios beyond what's listed above under each part).

## Part 35 (partial) — README safety banner (CONFIRMED_AND_FIXED for README.md only)

The top and bottom banners claimed "No real orders are placed, prepared,
previewed, or staged anywhere in this codebase" — false as of Milestone 11
(an explicit, operator-initiated Alpaca **paper**-account preview/submit
path exists, disabled by default). Both corrected to state accurately:
local simulation is the default, external execution is limited to explicit
Alpaca paper-account operations and disabled by default, live trading is
not implemented, recurring scheduling does not submit externally.
`.env.example` and `paper_runtime/README.md` were spot-checked and do not
contain the same stale claim.

## Final test results

```
pytest tests/ -q                    -> 1755 passed, 15 skipped
paper_runtime: pytest tests/ -q     -> 59 passed
git diff --check                    -> clean
```

Both counts hold under the ambient dev-shell environment and under the
clean-CI simulation (`env -u ANTHROPIC_API_KEY -u ANTHROPIC_MODEL -u
ANTHROPIC_BATCH_POLL_INTERVAL_SECONDS -u ALPACA_API_KEY -u
ALPACA_API_SECRET -u ALPACA_IS_PAPER -u ALPACA_BASE_URL -u
REDDIT_MCP_MODE -u REDDIT_MCP_COMMAND -u REDDIT_AUTH_MODE pytest tests/ -q`).

## Files changed

| File | Purpose |
|---|---|
| `tests/unit/test_shadow_cli_provider_mode.py`, `tests/integration/test_milestone_7_1_shadow_integration.py` | Part 1: CI hermeticity fix |
| `src/trading_research/storage/paper_books_schema.py` | Part 3/10/18: strict lookup trigger + versioned trigger upgrade; lease `generation` column |
| `src/trading_research/storage/database.py` | Part 5: `begin_immediate()` helper |
| `src/trading_research/shadow/lease.py`, `src/trading_research/paper_books/recurring_scheduler.py` | Part 5: hardened BEGIN IMMEDIATE call sites; Part 16 fix |
| `src/trading_research/paper_books/execution.py` | Part 6: atomic fill application |
| `src/trading_research/storage/paper_books_repositories.py` | Part 6/10/11: `commit` param, lease repo functions, sequence-ordered queries |
| `src/trading_research/paper_books/cash_ledger.py`, `src/trading_research/paper_books/positions.py` | Part 7/8: atomic reservations |
| `src/trading_research/paper_books/external_broker.py` | Part 5/9/10/12/13/15/17: exclusivity check, lease handle/heartbeat, fail-safe fill sweeps, duplicate detection, refresh-retry-preview |
| `src/trading_research/paper_books/config.py` | Part 10: lease TTL/heartbeat config fields |
| `src/trading_research/cli.py` | Part 17: refresh-retry-preview CLI wiring |
| `src/trading_research/runtime/client/process_client.py` | Part 19/20: mismatch-triggered unhealthy marking, thread/queue cleanup |
| `paper_runtime/src/trading_paper_runtime/dispatcher.py` | Part 14: open-SELL accounting |
| `paper_runtime/src/trading_paper_runtime/configuration.py` | Part 21: env-file allowlist validation |
| `src/trading_research/services/submit_credentialed_paper_order.py` | Part 22: recovery-lookup failure persistence |
| `README.md` | Part 35 (partial): safety banner correction |
| 9 new test files + 5 updated test files | regression coverage for every part above |
| `.codex/scratchpads/milestone11-2-integrity-closure.md` | mandatory scratchpad |

## Migration strategy

Two additive schema changes: `paper_books_trigger_versions` (new table,
Part 3) and `paper_external_order_leases.generation` (new column with
`DEFAULT 1`, Part 10). Both applied via the existing additive-migration
pattern (`_ensure_columns`/`executescript` in `apply_paper_books_schema`).
No destructive schema change was made. Part 2's full fixture matrix and
Part 34's general schema-versioning table were not built.

## Operational go/no-go

| Boundary | Status |
|---|---|
| Research-only operation remains available | ✅ unaffected |
| Local simulation remains the default | ✅ unaffected |
| External paper execution remains disabled by default | ✅ unaffected (config-gated, unchanged) |
| External submission remains explicit and operator-initiated | ✅ unaffected; the new refresh-retry-preview action is also explicit/operator-initiated and makes no broker call |
| Recurring scheduling never mutates an external broker | ✅ unaffected — not independently re-verified this session |
| Alpaca paper endpoint remains the only external execution endpoint | ✅ unaffected |
| Live trading remains structurally unavailable | ✅ unaffected |
| **Overall milestone 11.2 completion** | ❌ **NO-GO as "complete"** — Part 2's full fixture matrix and Parts 23-37 remain outstanding |

No real broker or network call occurred. No credentials used beyond what
already existed in the dev environment. No commit or push occurred — all
changes are in the working tree.
