# Milestone 11.3.2 Operational Integrity

## Metadata

- Starting commit: `3c86a70244bc000b237ba7a98a3e6303cbdc7225`
- Branch: `agent/milestone-12-codex-provider`
- Working-tree status: pre-existing untracked milestone specification only
- Started: 2026-07-18T08:12:02Z
- Last updated: 2026-07-18T08:35:00Z

## Baseline

- Main tests: `.venv/bin/pytest tests/ -q --tb=short` -> 2177 passed, 17 skipped
- Paper-runtime tests: `../.venv/bin/pytest tests/ -q --tb=short` -> 59 passed
- Clean-environment tests: credential-shaped variables removed -> 2177 passed, 17 skipped
- Pyright root: unavailable in the existing virtual environment (`.venv/bin/pyright` absent)
- Pyright paper_runtime: unavailable in the existing virtual environment (`.venv/bin/pyright` absent)
- CI configuration: both whole-project Pyright jobs use `continue-on-error: true`
- Scheduling defaults: scheduled integration remains opt-in/disabled by default; shadow scheduling config remains research-only
- External execution defaults: external broker and submission are disabled by default; live broker is rejected structurally

## Finding tracker

| ID | Finding | Current status | Evidence | Implementation | Tests | Final status |
|---|---|---|---|---|---|---|
| 1 | Hysteresis does not govern pause action | CONFIRMED | scheduler applies single-cycle result first and discards hysteresis decision | pending | pending | CONFIRMED |
| 2 | Zero/sub-floor samples are misqualified | CONFIRMED | scheduler falls back to symbol counts and qualifies with `bool(count)` | pending | pending | CONFIRMED |
| 3 | Required provider/category coverage is not enforced | CONFIRMED | scheduler calls telemetry without required providers | pending | pending | CONFIRMED |
| 4 | Provider requests use timestamp/symbol correlation | CONFIRMED | production query is `symbol + created_at` window | pending | pending | CONFIRMED |
| 5 | Timeout is conflated with severe connection failure | CONFIRMED | no-response `ProviderRequestError` maps to severe DNS/connection bucket | pending | pending | CONFIRMED |
| 6 | Lease fencing has TOCTOU gaps | CONFIRMED | multiple protected writes call `verify_or_raise()` before an unconditioned committed write | pending | pending | CONFIRMED |
| 7 | Snapshot persistence races | CONFIRMED | `SELECT`-then-`INSERT`, header/positions followed by one final commit | pending | pending | CONFIRMED |
| 8 | Hysteresis evidence is incomplete | CONFIRMED | evaluator carries old provider metrics and writes no per-cycle history | pending | pending | CONFIRMED |

## Architecture decisions

### Provider-cycle correlation

Pending implementation. Exact cycle ownership will replace timestamp-window attribution; legacy/manual rows remain explicitly uncorrelated.

### Required-provider policy

Implemented. Production policy fails closed for missing required categories/providers and ignores absent optional providers.

### Health qualification

Provider health qualifies only when its check is PASS/WARNING/FAIL; INSUFFICIENT_DATA and NOT_APPLICABLE move neither streak.

### Hysteresis and pause action

Implemented. Effective decisions combine structural immediate failures with durable provider-health hysteresis, with no automatic resume.

### Severe-error taxonomy

Implemented. Transport categories are classified at the HTTP boundary and persisted without raw exception text.

### Lease fencing

Implemented. Verification and protected writes share one `BEGIN IMMEDIATE` transaction.

### Snapshot persistence

Implemented. Header claim uses atomic conflict handling and positions share its explicit transaction.

## Schema changes

- Added provider-request correlation mode, research-cycle/scheduler-run/research-run/symbol-attempt/request-group identifiers, and bounded transport category.
- Added cycle and scheduler-run indexes; legacy rows remain `LEGACY_MANUAL` and uncorrelated.
- Added append-only `shadow_health_hysteresis_evaluations` with unique `(scope, cycle_id, policy_hash)` identity.
- Added run-summary single-cycle/hysteresis/effective state, sample qualification, policy hash, coverage, and severe-category columns.
- Added schema migration 5; all changes are additive and preserve prior rows.

## Concurrency reproductions

- Before concurrency tests: code inspection confirms snapshot SELECT/INSERT and lease verify/write gaps; deterministic real-connection reproductions pending.
- Before snapshot concurrency regression: implementation now uses `BEGIN IMMEDIATE` plus `INSERT ... ON CONFLICT DO NOTHING`; two-connection barrier test pending.
- Before lease concurrency regression: implementation now verifies inside the held SQLite write transaction; real takeover/blocking test pending.
- After snapshot concurrency regression: two real connections produced exactly one insert and one idempotent replay; same-ID/different-hash failed closed; injected position failure rolled back the header and retry succeeded.
- After lease concurrency regression: owner B remained blocked while owner A held the fenced transaction, acquired generation N+1 after commit, and owner A's post-runtime stale write was rejected with no partial row.

## Files changed

| File | Purpose |
|---|---|
| `.codex/scratchpads/milestone11-3-2-operational-integrity.md` | Required work log and resume point |
| `src/trading_research/storage/evidence_provider_schema.py` | Correlation and transport schema |
| `src/trading_research/storage/schema_version.py` | Versioned migration 5 |
| `src/trading_research/storage/shadow_alerts_schema.py` | Hysteresis history and summary evidence schema |
| `src/trading_research/evidence_providers/persistence.py` | Exact correlation context and cycle query |
| `src/trading_research/evidence_providers/http_client.py` | Typed transport classification |
| `src/trading_research/evidence_providers/health.py` | Coverage policy, aliases, and structural taxonomy |
| `src/trading_research/shadow/health.py` | Qualification and effective decision model |
| `src/trading_research/shadow/health_hysteresis.py` | Atomic state/history persistence |
| `src/trading_research/shadow/scheduler.py` | Correlated telemetry and hysteresis-governed pause flow |
| `src/trading_research/storage/paper_books_repositories.py` | Atomic snapshot insertion and retry-evidence transaction support |
| `src/trading_research/paper_books/external_broker.py` | Atomic lease-fenced protected writes |

## Commands run

- Baseline git metadata and 20-commit log
- Main, clean-environment, and paper-runtime pytest baselines
- Root and paper-runtime Pyright availability checks
- Focused `rg`/`sed` inspection of health, telemetry, storage, lease, snapshot, schema, and scheduler paths

## Open issues

- Existing virtual environment does not contain Pyright.
- Current branch includes the post-PR-15 Milestone 12 commit; it was preserved.

## Resume instructions

- Last completed item: final lease-fencing audit, scheduler correlation correction, and verification
- Tests: main 2195 passed/17 skipped; clean environment 2195 passed/17 skipped; paper runtime 59 passed
- Remaining blockers: Pyright executable unavailable in the existing virtual environments

## Final status

Implementation complete; no real provider, model, or broker service was invoked.
