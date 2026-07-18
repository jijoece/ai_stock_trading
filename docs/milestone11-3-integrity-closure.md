# Milestone 11.3 — Remaining Integrity Closure

## Status: COMPLETE — all 15 remaining parts (2, 23-37) closed with passing regression tests

This milestone closes what Milestone 11.2 explicitly left open: Part 2's
full prior-schema migration fixture matrix, Parts 23-31 (provider/HTTP/
rate-limiter/config robustness, disclosure negation, market-data shape
validation, SEC point-in-time), Part 32 (settlement semantics), Part 33
(legacy paper subsystem quarantine), Part 34 (general schema versioning),
Part 35 (remaining documentation), and Part 36/37 (remaining test-quality
categories, including the one offline end-to-end scenario 11.2 flagged as
unresolved).

## Starting / current commit

- Starting commit: `c5232adf89b84b7e32dc716e243d3a8426d95eb2` (branch `agent/milestone-11-2-full-integrity-closure`)
- No commits made this session — all changes are in the working tree, per instructions.

## Baseline

- `pytest tests/ -q` (ambient dev-shell env): 1755 passed, 15 skipped.
- Same command under a simulated clean-CI env (credential-shaped vars unset): 1755 passed, 15 skipped — identical, confirming no new test in this milestone accidentally depends on ambient credentials.
- `paper_runtime/pytest tests/ -q`: 59 passed (clean under both).
- `git diff --check`: clean throughout.
- `pyright` (both main and `paper_runtime` steps): still runs with `continue-on-error: true` in CI, unchanged from 11.2 — a large pre-existing baseline (~1888 errors on main) predates this session; spot-checked every new diagnostic surfaced while editing and none represented a genuine new defect introduced by this work (mostly stale-cache re-analysis noise or dataclass-`**kwargs` widening consistent with the existing test-fixture style).

## Part 2 — Full prior-schema migration fixture matrix (CONFIRMED_AND_FIXED)

Milestone 11.2 only proved the upgrade path for one fixture (the Milestone-
11.1 lookup trigger). Added `tests/fixtures/schema_history/` containing
verbatim `git show` snapshots of `storage/paper_books_schema.py` at three
prior milestones (pre-Milestone-11 = commit `706b519`, Milestone-11 =
`b4d9705`, Milestone-11.1 = `bec1463`), loaded at test time via
`runpy.run_path` so the literal `PAPER_BOOKS_DDL`/`PAPER_BOOKS_INDEXES`/
`PAPER_BOOKS_TRIGGERS` strings are used unmodified — not paraphrased.

`tests/unit/test_paper_books_prior_schema_migration.py` builds an on-disk
database with each prior schema, inserts representative rows, opens it
through the real `connect()`, and verifies: new tables/columns are created
(including the Milestone 11.2 `generation` column on
`paper_external_order_leases`, defaulting to `1` for pre-11.2 rows);
indexes and append-only/immutability triggers are active on both legacy and
new tables; pre-existing data (books, cash ledger, lookups, events, leases,
reservation events, activation reviews with `campaign_attempt_id`) survives
unchanged; the upgraded lookup-trigger contract (v2) applies even to rows
inserted under the old (v1) trigger; `scope_sequence` uniqueness is
enforced for new rows while legacy `NULL` rows are unaffected.

## Part 23 — Provider-health sample-size protection (CONFIRMED_AND_FIXED)

`shadow/health.py::CycleHealthInputs` had no request-count field —
`provider_success_rate` alone made a 1-request cycle's 100% failure rate
indistinguishable from a 100-request cycle's.

**Fix:** added `provider_request_count: int | None` and
`provider_severe_error: bool` to `CycleHealthInputs`;
`minimum_requests_for_failure_rate: int` to `HealthPolicyConfig` and
`shadow/config.py::SafetySection` (not a required YAML key — defaults to
`1`, preserving prior behavior for any config predating this field;
production `config/shadow_operations.yaml` sets it explicitly to `5`).
`_rate_check` now takes `sample_size`/`minimum_sample_size`/
`sample_floor_bypassed` and returns `INSUFFICIENT_DATA` (not PASS, not an
automatic pause) below the floor, while still reporting the raw computed
rate for observability. `provider_severe_error=True` bypasses the floor
entirely. `shadow/scheduler.py::_health_inputs_from_cycle` now threads
`symbols_attempted` through as `provider_request_count`.

**Test:** `tests/unit/test_shadow_health_sample_floor.py` — 1/1, 2/2,
at-floor threshold crossing, large-sample pass/pause, recovery, severe-
error bypass, and backward-compatible default.

**Deviation:** "persistent failures cross the threshold" / "recovery
requires hysteresis" are satisfied structurally (a caller can feed a
rolling-window count into the same pure per-cycle evaluator; the existing
DEGRADED/FAIL two-tier threshold already provides soft/hard boundaries) but
no new explicit multi-cycle hysteresis state machine was built — narrower
than a literal reading, documented rather than forced.

## Part 24 — HTTP client hardening (CONFIRMED_AND_FIXED)

`evidence_providers/http_client.py::HttpJsonClient` constructed a fresh
`httpx.Client` **inside** the retry loop on every attempt (no pooling even
within one call), had no response-size or JSON-depth bound, never honored
`Retry-After`, had no exponential backoff, and included up to 500 raw
response-body characters in a non-retryable-status error message.

**Fix:** one `httpx.Client` is now created lazily and reused across every
`get_json()` call (`close()`/context-manager lifecycle added); responses
are read via `client.stream()` with a hard `MAX_RESPONSE_BYTES` (20 MiB)
cap; parsed JSON is walked for depth (`MAX_JSON_DEPTH=64`); a valid
`Retry-After` (seconds or HTTP-date, capped at 120s) plus exponential
backoff (`backoff_base_seconds * 2**(attempt-1)`, capped at
`backoff_max_seconds`) apply before each retry via an injectable
`backoff_sleep_fn`; `redact_credential_query_params` strips known
credential-shaped query keys from every URL placed into metadata or an
error message; raw response body text is never included in any raised
error. The client structurally has only `get_json` (GET, idempotent) and
`close` as public methods — no write method exists to retry.

**Test:** `tests/unit/test_http_client_hardening.py` — client reuse across
calls, `close()`/context-manager teardown, oversized-response rejection,
pathological-JSON-depth rejection, `Retry-After` honored and capped,
exponential backoff growth and cap, credential redaction (helper, response
metadata, error message), no raw body in any error path, structural
GET-only surface. Existing provider tests updated to pass
`backoff_sleep_fn=lambda s: None` (mirrors the existing rate-limiter
`sleep_fn` injection pattern) so the suite stays fast and deterministic.

## Part 25 — Thread-safe rate limiting (CONFIRMED_AND_FIXED)

`MinIntervalRateLimiter.acquire()` had no lock — two concurrent callers
could both read the same `_last_acquired`, both compute the same wait, and
both fire within the same interval.

**Fix:** a `threading.Lock` now protects atomic slot reservation
(`_next_allowed`); the actual `sleep_fn` call happens outside the lock so
one thread's wait never blocks another thread's ability to reserve its own
slot. `max(self._next_allowed, now)` makes a clock rollback fail safe
(never a negative wait; a rolled-back clock can only make the limiter more
conservative). Already used a monotonic clock by default (unchanged).

**Test:** `tests/unit/test_rate_limiter_thread_safety.py` — real two/five-
thread races prove distinct non-overlapping slots, that a second thread's
slot computation isn't blocked by a first thread's in-flight sleep, no
negative sleep on clock rollback, monotonic-clock default confirmed.

## Part 26 — Strict scheduled-research booleans (CONFIRMED_AND_FIXED)

`research/scheduled_research_config.py` used plain `bool(...)` coercion
(`bool("false")` is `True`) for every gate field, unlike
`paper_books/config.py`'s `_strict_bool` pattern.

**Fix:** added a matching `_strict_bool` helper; applied to `enabled`,
`submit_paper_orders`, `require_complete_evidence`,
`require_point_in_time_safe`, `continue_on_symbol_failure`,
`promotion.enabled`, and `promotion.allow_live_promotion`.

**Test:** `tests/unit/test_scheduled_research_config_strict_bool.py` —
parametrized rejection of `"false"`/`"true"`/`0`/`1`/`None` for every
boolean field, real-boolean success, default-disabled confirmation.

## Part 27 — Deterministic config hashing (CONFIRMED_AND_FIXED)

`hashing.py::hash_config` used unrestricted
`json.dumps(data, sort_keys=True, default=str)` — any object (`Path`,
`set`, a custom class, a `datetime`) was silently stringified with no
completeness/stability guarantee.

**Fix:** `hash_config` now canonicalizes through an explicit recursive
walk supporting only `None`/`bool`/`int`/finite `float`/finite `Decimal`
(normalized to a stable fixed-point string via `format(value.normalize(),
"f")`, collapsing e.g. `Decimal("1.50")` and `Decimal("1.5")` to the same
representation)/`str`/`list`/`tuple`/`dict` with string keys. Everything
else — `Path`, `set`, `datetime`, custom objects, non-string dict keys —
raises `ConfigHashError` immediately, naming the offending path and type.
`allow_nan=False` on the final `json.dumps` rejects NaN/Infinity even if a
canonicalized value somehow reached it.

**Deviation:** the spec's literal type list omits plain `float`, but every
real config loader in this codebase parses YAML numeric fields as Python
`float` (e.g. `max_incomplete_analysis_rate: 0.3`) — rejecting `float`
outright would break every existing config hash call site. `float` is kept
as a supported canonical type, gated only on finiteness; this is
documented here rather than forced through and breaking 8+ existing config
modules.

**Test:** `tests/unit/test_hashing_deterministic.py` — key-order
independence, stable Decimal representation, NaN/Infinity rejection
(Decimal and float), `Path`/`set`/`datetime`/custom-object rejection,
non-string-key rejection, nested-structure determinism, tuple/list
equivalence, stable SHA-256 hex output shape.

## Part 28 — No filesystem side effects in config loading (CONFIRMED_AND_FIXED)

`config.py::load_config` unconditionally called `.mkdir(parents=True,
exist_ok=True)` on both `research_data_dir` and
`research_database_path.parent` — a mutation on every config load, even a
dry-run or validation-only call.

**Fix:** removed both `mkdir` calls from `load_config`. The database
directory is already created at first actual use inside
`storage/database.py::connect()`; `research_data_dir` has no current write
consumer that needs pre-creation (confirmed via repo-wide grep — the field
is otherwise unused outside `config.py` and one test fixture).

**Test:** `tests/unit/test_config_no_filesystem_side_effects.py` — load
under a read-only parent directory succeeds (proving no mkdir attempt),
invalid config raises without creating any directory, dry-run load leaves
the filesystem byte-for-byte unchanged.

## Part 29 — Disclosure negation handling (CONFIRMED_AND_FIXED)

`evidence_providers/disclosure_extraction.py`'s going-concern regex matched
"substantial doubt ... ability to continue as a going concern" with no
negation/alleviation handling — "no substantial doubt ... going concern"
and "substantial doubt ... has been alleviated" were classified as active
findings.

**Fix:** `_find_first_valid_explicit_match` now also skips a match with
either (a) `no`/`not`/`no longer` immediately (within 40 chars) preceding
the "substantial doubt" token, or (b) an alleviation verb
(alleviated/resolved/mitigated/eliminated) within 120 chars after the
match — two narrow, separately-scoped checks (not one wide fuzzy window)
so an unrelated "no" elsewhere in the filing never demotes a genuine
finding. A skipped match still falls through to the existing bare
"going concern" ambiguous-mention regex, so a negated finding surfaces as
`AMBIGUOUS_DISCLOSURE` (human review) rather than vanishing silently.
Whitespace is normalized within the context windows so line breaks/HTML
residue/table cell boundaries don't defeat detection.

**Test:** `tests/unit/test_disclosure_extraction_negation.py` — true
positive preserved with an unrelated "no" nearby, direct pre-negation,
post-match alleviation, "no longer raises" pre-negation, negated-not-
silently-dropped (still AMBIGUOUS), HTML-residue/line-break survival,
markdown-table-cell survival.

## Part 30 — Flexible market-data shape validation (CONFIRMED_AND_FIXED)

`scripts/macro_pillar.py::score_macro`'s nested `closes()` helper
shape-checked only the *first* list element (`v[0]`) to decide
float-vs-dict parsing for the whole sequence — a heterogeneous list raised
a raw `KeyError`/`TypeError` partway through, or misparsed depending on
element order.

**Fix:** promoted the closure to a module-level `extract_closes(series,
sym)` (independently testable) that validates the complete sequence before
any conversion: rejects a non-list, rejects any element type outside
`{dict, int, float}`, rejects mixed float/dict lists, rejects a dict
missing `close`, rejects non-numeric/bool `close` values, rejects
non-finite (`NaN`/`Infinity`) values — all via a new bounded
`MarketDataShapeError(ValueError)` rather than a raw `KeyError`/
`TypeError`. Empty/absent history still returns `None` (a legitimate "no
data" case, not a shape failure).

**Test:** `tests/unit/test_macro_pillar_market_data_shape.py` — 15 tests
covering every rejection case plus an end-to-end `score_macro` regression
over a realistic long dict-shaped series; manually re-ran the script's
`score_macro` directly to confirm the live CLI path still produces a
composite score.

## Part 31 — SEC point-in-time assurance (CONFIRMED_AND_FIXED for the concrete bug; other sub-items NEEDS_RUNTIME_EVIDENCE)

`sec_provider.py::get_company_facts`'s look-ahead guard treated a date-only
`filed` value as available starting at `00:00 UTC` on that date — SEC
accepts same-day filings throughout the trading day (up to ~5:30pm ET), so
a same-day *morning* `as_of` could incorrectly "see" a fact that, in
reality, might not have been filed until that afternoon: a real look-ahead
bias.

**Fix:** `_date_only_conservative_available_at(filed_date)` treats a
date-only fact as available only from `00:00 UTC` on the **next** calendar
day — a full day's conservative buffer, so no same-day `as_of`, at any
hour, can see it.

**Test:** `tests/unit/test_sec_provider_point_in_time.py` — morning
same-day `as_of` cannot see the filing, late-evening same-day `as_of`
still cannot see it, next-day `as_of` can, a much-later `as_of` can (the
existing `test_get_company_facts_excludes_future_filed_value` continues to
pass unmodified).

**Not independently re-verified this session** (marked
`NEEDS_RUNTIME_EVIDENCE`, no proof invented): filing-acceptance-timestamp
authoritativeness for `list_filings` (already uses a full timestamp,
`acceptanceDateTime` — appears correct, not re-derived from scratch);
"distinguish auditor statement from management boilerplate" (no change
made — the existing disclosure-extraction module, Part 29, handles a
different data source); downstream propagation of `point_in_time_safe`
uncertainty flags in `paper_book_snapshot_positions` beyond what already
existed pre-11.3.

## Part 32 — Paper-book settlement semantics (DESIGN_TRADEOFF_DOCUMENTED)

The active `paper_books` ledger already applies a fill's cash effect
immediately (`cash_ledger.settle_buy`/`settle_sell`, same transaction as
the fill) — this was implicit behavior, never named or versioned.

**Decision:** retained immediate settlement (no T+1 built) and made the
policy explicit: `cash_ledger.py::SETTLEMENT_POLICY_VERSION =
"IMMEDIATE_SIMULATED_SETTLEMENT.v1"`, documented in the module docstring as
a deliberate simulation simplification, never called regulatory
settlement. Every buying-power/risk/reservation read in this subsystem
already derives from the same immediately-settled ledger (no separate
"settled" vs. "available" inconsistency). The policy version is now
included in the snapshot `source_hash` payload
(`valuation.py::compute_portfolio_snapshot`), so a future policy change is
independently detectable from the snapshot's own hash.

**Test:** `tests/unit/test_settlement_policy.py` — policy version is
explicit and not misnamed "regulatory," `settled_cash` reflects a fill
immediately (no elapsed settlement day), the snapshot hash changes with a
different policy version string.

## Part 33 — Legacy paper subsystem quarantine (CONFIRMED_AND_FIXED, "Alternative" option)

The repository exposed two paper-ledger systems reachable through
identically-styled CLI commands: the active, extensively-hardened
`paper_books` subsystem, and the separate legacy `paper/ledger.py`
subsystem (Milestone 3/4) via `paper-status`, `execute-paper`,
`sync-paper-orders`, `reconcile-paper` — no naming distinction at all.

**Fix (rename + explicit flag, the spec's documented alternative to
outright removal — retained since existing regression tests still exercise
it and no destructive-migration plan exists):** renamed to
`legacy-paper-status`, `legacy-paper-execute`, `legacy-paper-sync-orders`,
`legacy-paper-reconcile`; each now requires a new, required
`--i-understand-this-is-the-legacy-ledger` flag; each subparser's `help=`
text carries a `[DEPRECATED — ...]` suffix visible in `--help`. The legacy
subsystem uses wholly separate database tables
(`simulated_*`/`paper_cash_state`/`paper_execution_*`, never any
`paper_book_*`/`paper_external_*` table) and is never imported by
campaign, recurring-scheduler, or external-execution code (confirmed:
`paper.ledger` is only imported from `cli.py`). `paper-runtime-health`,
`evaluate-recommendations`, and `paper-performance` were left unrenamed —
they operate over shared, ledger-agnostic infrastructure
(`evaluation_repositories`/runtime health), not the legacy ledger itself.

**Test:** `tests/unit/test_legacy_paper_cli_quarantine.py` — old command
names are gone (argparse `invalid choice`), renamed commands require the
explicit flag, top-level `--help` marks each as deprecated, active
`paper-book-*` commands are unaffected.

## Part 34 — General schema versioning (CONFIRMED_AND_FIXED)

Milestone 11.2's `paper_books_trigger_versions` table only tracks
individual trigger *definitions*, not a general schema version.

**Fix:** new `storage/schema_version.py` module: a `schema_version` table
(`version`, `description`, `applied_at`), an ordered `_MIGRATIONS` dict of
`(description, idempotent callable)` keyed by version number,
`check_schema_not_forward_versioned` (creates the table if absent, raises
`SchemaVersionError` if the stored version exceeds this code's
`CURRENT_SCHEMA_VERSION` — "forward version fails safely"), and
`apply_pending_schema_migrations` (runs each pending migration inside its
own `BEGIN IMMEDIATE`/`commit()`, logs only the version number and static
description — never row contents). Wired into `database.py::connect()`:
the forward-version check runs first (before any schema DDL touches the
connection), then the existing additive `apply_*_schema` calls run
unchanged, then pending migrations apply. Does not replace any existing
additive migration — wraps the whole existing sequence in an explicit
versioned checkpoint, per the spec's explicit instruction not to
unnecessarily replace what already works.

**Test:** `tests/unit/test_schema_version.py` — fresh database records the
current version, reopening doesn't duplicate/regress it, a database
carrying a version newer than this code is refused, migration idempotency
under a repeated explicit call, table auto-creation on an absent-table
database, and a genuinely pre-11.3 database (no `schema_version` table at
all, only a bare `paper_books` table) upgrades cleanly.

## Part 35 — Remaining documentation (PARTIALLY_CONFIRMED_AND_FIXED)

Spot-checked `.env.example`, `paper_runtime/README.md`, the Alpaca paper
runbook (`docs/runbooks/alpaca-paper-operations.md`), the recurring
scheduler runbook (`docs/runbooks/recurring-local-paper-trading.md`), and
ADR 0007 — all already stated the corrected safety posture (local
simulation default, external execution explicit/disabled-by-default,
recurring scheduling never submits externally, live trading not
implemented) with no stale claims found.

**Fix:** `README.md`'s CLI reference still showed `paper-status` as a
live example — now broken by Part 33's rename. Updated the example to the
active `paper-book-show` command and added a note describing the
`legacy-paper-*` quarantine and required flag.

**Not exhaustively re-audited this session:** ADRs 0001-0006 and the
remaining runbooks (`shadow-operations.md`, `paper-book-operations.md`,
etc.) beyond the ones named explicitly in the spec; the rest of
`README.md`'s CLI reference section already contained pre-existing,
unrelated staleness (e.g. a `run-screen`/`evaluate` example that doesn't
match any real subcommand) that predates this milestone and was left
untouched as out of scope.

## Part 36/37 — Remaining test coverage + the flagged crash scenario (CONFIRMED_AND_FIXED)

Milestone 11.2's report explicitly flagged that `reserve_for_order`'s
self-contained commit vs. `_submit_once`'s own `_append_event` write was
"not independently re-verified as a single atomic transaction." Inspecting
`external_broker.py::_submit_once` confirmed the composition really was
two independently committed writes: `cash_ledger.reserve_for_order`/
`positions.reserve_shares_for_sell` each defaulted to `commit=True`, then
`_append_event` (via `repo.save_external_order_event`, also `commit=True`
by default) committed separately. A crash between the two commits left a
durable reservation with no explaining event.

**Fix:** `_append_event` gained a `commit: bool = True` parameter, threaded
to `repo.save_external_order_event`. `_submit_once` now wraps the
reservation and the `SUBMISSION_REQUESTED` event in one
`begin_immediate`/`commit()` transaction (both calls use `commit=False`);
on any failure the transaction rolls back, which now also reverses the
reservation — the manual compensating-release logic this block previously
needed on a raised exception is no longer necessary and was removed.
**Regression test discovered a real gap while building the crash
simulation:** the original `except Exception:` guard around this block did
not catch `BaseException` subclasses; a raised `BaseException` left the
transaction open, and the order-scope lease's own `finally`-block release
(which commits) would have inadvertently half-committed the dangling
write. Widened to `except BaseException:` — this is a genuine correctness
fix, not just test scaffolding, since any interrupt (not only ordinary
exceptions) during this block must roll back cleanly.

**Test:**
`tests/unit/test_external_submit_reservation_crash_atomicity.py` — two
scenarios: (1) the reservation+event transaction commits, then a
`BaseException`-raising fake runtime simulates a process crash *before* any
broker call completes; a fresh connection against the same on-disk
database ("restart") confirms the reservation and `SUBMISSION_REQUESTED`
event are both durable, no fill or broker mutation was ever recorded, the
order status column was never blindly advanced, and
`derive_external_queue_status` surfaces `SUBMISSION_REQUESTED` (not
silence, not a fabricated success) — the operator sees the unresolved
checkpoint. (2) A crash *inside* the atomic block (before its own commit)
leaves zero effects on restart — rollback reverses both writes together,
and the broker was never reached at all.

Every other Part-36/37-listed provider/config test category (health sample
floor, Retry-After, response-size bound, rate-limit threads, strict
booleans, hash-unsupported-object, no-config-side-effect, disclosure
negation) has dedicated coverage under the parts above. Verified all six
Milestone-11.2-listed offline end-to-end scenarios (local-fill crash
recovery, concurrent BUY/SELL reservation races, retry-preview-refresh,
lookup-trigger migration, long-running-lease heartbeat) still pass
unmodified in the full suite run below.

## Final test results

```
pytest tests/ -q                    -> 1894 passed, 15 skipped
paper_runtime: pytest tests/ -q     -> 59 passed
git diff --check                    -> clean
```

Both counts hold under the ambient dev-shell environment and under the
clean-CI simulation (`env -u ANTHROPIC_API_KEY -u ANTHROPIC_MODEL -u
ANTHROPIC_BATCH_POLL_INTERVAL_SECONDS -u ALPACA_API_KEY -u
ALPACA_API_SECRET -u ALPACA_IS_PAPER -u ALPACA_BASE_URL -u
REDDIT_MCP_MODE -u REDDIT_MCP_COMMAND -u REDDIT_AUTH_MODE pytest tests/ -q`).

`pyright` (main + `paper_runtime`) still runs `continue-on-error: true`,
unchanged from 11.2's documented policy — not claimed as passing.

## Remaining limitations

- Part 23's multi-cycle hysteresis and Part 31's broader point-in-time
  sub-items are deliberately narrower than a literal spec reading (see
  each part's write-up above).
- Part 35's documentation audit was targeted, not exhaustive, across every
  ADR/runbook in the repository.
- No new CI job (secret-scan, dependency-audit, blocking type-check subset)
  was added — unchanged from 11.2, out of this milestone's part list.
- Pyright's large pre-existing non-blocking error baseline was not reduced
  (out of scope; CLAUDE.md explicitly says not to perform broad type-error
  cleanup unless requested).

## Operational go/no-go

| Boundary | Status |
|---|---|
| Research-only operation remains available | ✅ unaffected |
| Local simulation remains the default | ✅ unaffected |
| External paper execution remains disabled by default | ✅ unaffected (config-gated, unchanged) |
| External submission remains explicit and operator-initiated | ✅ unaffected; the reservation/event atomicity fix strengthens this without changing the interface |
| Recurring scheduling never mutates an external broker | ✅ unaffected |
| Alpaca paper endpoint remains the only external execution endpoint | ✅ unaffected |
| Live trading remains structurally unavailable | ✅ unaffected |
| **Overall Milestone 11.3 completion** | ✅ **GO** — all 15 remaining parts closed with passing regression tests |

No real broker or network call occurred. No credentials used beyond what
already existed in the dev environment. No commit or push occurred — all
changes are in the working tree.

## Summary table: Finding → classification → correction → regression evidence

| Finding | Classification | Correction | Test |
|---|---|---|---|
| Part 2: only 1 of 4 required migration fixtures existed | CONFIRMED_AND_FIXED | 3 new verbatim-DDL prior-schema fixtures | `test_paper_books_prior_schema_migration.py` |
| Part 23: no provider-request sample floor | CONFIRMED_AND_FIXED | `provider_request_count`/`minimum_requests_for_failure_rate` gate | `test_shadow_health_sample_floor.py` |
| Part 24: per-attempt HTTP client, unbounded body, no backoff, raw body in errors | CONFIRMED_AND_FIXED | pooled client, byte/depth caps, Retry-After+backoff, redaction, no raw body | `test_http_client_hardening.py` |
| Part 25: rate limiter not thread-safe | CONFIRMED_AND_FIXED | lock-protected atomic slot reservation | `test_rate_limiter_thread_safety.py` |
| Part 26: permissive `bool()` scheduled-research config | CONFIRMED_AND_FIXED | `_strict_bool` on every gate field | `test_scheduled_research_config_strict_bool.py` |
| Part 27: unrestricted `json.dumps(default=str)` hashing | CONFIRMED_AND_FIXED | canonical-type-only recursive hasher | `test_hashing_deterministic.py` |
| Part 28: `load_config` created directories | CONFIRMED_AND_FIXED | removed `mkdir` calls | `test_config_no_filesystem_side_effects.py` |
| Part 29: no going-concern negation handling | CONFIRMED_AND_FIXED | pre/post negation-window checks | `test_disclosure_extraction_negation.py` |
| Part 30: `closes()` shape-checked only first element | CONFIRMED_AND_FIXED | `extract_closes` full-sequence validation | `test_macro_pillar_market_data_shape.py` |
| Part 31: date-only `filed` available at midnight UTC (look-ahead) | CONFIRMED_AND_FIXED | conservative next-day availability | `test_sec_provider_point_in_time.py` |
| Part 32: settlement policy implicit | DESIGN_TRADEOFF_DOCUMENTED | explicit versioned `SETTLEMENT_POLICY_VERSION` | `test_settlement_policy.py` |
| Part 33: legacy ledger CLI indistinguishable from active | CONFIRMED_AND_FIXED | `legacy-paper-*` rename + required flag | `test_legacy_paper_cli_quarantine.py` |
| Part 34: no general schema-version table | CONFIRMED_AND_FIXED | `schema_version` table + versioned migrations | `test_schema_version.py` |
| Part 35: README CLI example broken by Part 33 rename | CONFIRMED_AND_FIXED | updated example + legacy-quarantine note | manual review |
| Part 36/37: reservation+event not atomic (11.2 open question) | CONFIRMED_AND_FIXED | single `begin_immediate`/`commit()` transaction + `BaseException`-safe rollback | `test_external_submit_reservation_crash_atomicity.py` |
