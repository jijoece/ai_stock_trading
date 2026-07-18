# Full Codebase Audit

Audit of commit `bec1463` ("Implement Milestone 11.1 external paper execution safety corrections"), branch `agent/milestone-11-1-external-paper-safety`, performed offline (no brokers, no credentials, no network, no repository modification other than this report and the audit scratchpad).

---

## Executive summary

The safety-critical external Alpaca **paper** path (Milestone 11/11.1) is in materially good shape: paper-endpoint pinning, account-fingerprint binding, order-scope leases, an append-only event chain with enforced transitions, evidence-gated retry after ambiguous submission, per-fill clamped reservation releases, and fail-closed numeric normalization were all verified in source and are covered by tests. **No P0 finding was identified in the external submission path itself.**

The highest-risk defects found are elsewhere:

1. **N1 (HIGH)** — the *local simulated* fill path (`paper_books/execution.py::submit_and_simulate`) applies a fill across several auto-committing repository calls. A crash between `save_fill` and the position/cash updates permanently loses the fill's ledger effects, and the `fill_exists` idempotency guard then *blocks* replay. This is the main blocker for unattended recurring local operation.
2. **A1 (MEDIUM, confirmed by reproduction)** — connections use Python's legacy implicit-transaction mode. `BEGIN IMMEDIATE` raises `OperationalError` if any uncommitted DML is pending on the connection, and in `shadow/lease.py` the `BEGIN IMMEDIATE` sits *outside* the `try`, so the failure leaves the connection wedged in-transaction (blocking other connections until busy-timeout). Latent today because most repository writes auto-commit, but fragile against any future `commit=False` caller.
3. **A3 (MEDIUM)** — cash/share reservation is check-then-insert without a surrounding write transaction; the order-scope lease serializes operations on *one* order but not two different orders on the same book. Narrow window, but the invariant "available cash never negative" is not transactionally enforced.
4. **D5 (MEDIUM)** — README's headline safety claim ("No real orders are placed, prepared, previewed, or staged anywhere in this codebase") is stale: Milestone 11 previews and submits real orders to the external Alpaca *paper* endpoint.

Of the 35 supplied candidates: 13 CONFIRMED, 12 PARTIALLY_CONFIRMED, 6 ALREADY_FIXED, 3 DESIGN_TRADEOFF, 4 FALSE_POSITIVE, 1 NOT_REPRODUCIBLE (no defect found). Several confirmed findings are scoped to the **legacy** `paper/ledger.py` subsystem, which is still reachable from the CLI alongside the active `paper_books` subsystem.

---

## Repository and test baseline

```text
HEAD:      bec1463 Implement Milestone 11.1 external paper execution safety corrections
Dirty:     M docs/pitfalls_and_improvements.md
Untracked: docs/codebase-analysis-pitfalls.md
           src/trading_research/research/openai_compatible_provider.py  (unwired; not part of HEAD)
```

| Suite | Result |
|---|---|
| `pytest tests/ -q` | **1714 passed, 15 skipped** (~15 s) |
| `cd paper_runtime && pytest tests/ -q` | **47 passed** |
| `pyright` (1.1.411) | **1824 errors, 0 warnings** — pre-existing baseline; CI runs pyright **non-blocking** by documented decision in `ci.yml` |

All 15 skips are opt-in credentialed/real-provider smoke tests gated on `RUN_*_TESTS` env vars (correct fail-closed default; none run merely because credentials exist). CI exists (`.github/workflows/ci.yml`): main tests, paper_runtime tests, non-blocking type check; offline-only, no secrets configured. No CI evidence is attached to this un-pushed commit itself.

---

## Architecture and trust-boundary map

```text
.env / YAML configs
  → evidence providers (HttpJsonClient + SEC/Alpaca/Reddit adapters; auth via headers)
  → scheduled research cycle (shadow_run_leases + derive_cycle_id idempotency)
  → recommendation persistence (research_schema; frozen payloads + config hashes)
  → shadow operations (health / pause / alerts / budget — fail-closed gates)
  → paper_books: append-only cash ledger + FIFO lots + positions aggregate
  → risk / exit decisions (approved decisions gate every order intent)
  → local simulated execution (execution.py — LOCAL-SIMULATED-PAPER only)
  → soak campaign + activation review (9.3/9.3.1 integrity gating)
  → recurring scheduler (singleton lease; BEGIN IMMEDIATE queue claims)
  → external paper coordinator (external_broker.py: order-scope lease,
      append-only event chain, preview→submit→retry→cancel→reconcile)
  → RuntimeClient (stdio JSON-lines, 64 KB bounds, strict correlation)
  → paper_runtime subprocess (ONLY process reading ALPACA_API_KEY/SECRET;
      base_url pinned to https://paper-api.alpaca.markets)
  → reconciliation (critical statuses block further submission)
  → metrics / reporting (Decimal, windowed, no fabricated zeros)
```

Key boundaries verified:

- **Credential boundary** — `cli.py::_paper_runtime_command_env` passes an explicit 8-key allowlist (`PATH`, `PYTHONPATH`, `ALPACA_*`, `PAPER_BROKER_PROVIDER`, `PAPER_RUNTIME_ENV_FILE`); Anthropic/Reddit/Robinhood/database secrets are excluded by construction. The runtime loads dotenv only from an explicitly named `PAPER_RUNTIME_ENV_FILE` (a prior `find_dotenv(usecwd=True)` repo-`.env` leak was already fixed and is documented in `paper_runtime/.../configuration.py`).
- **Live-trading barrier** — `execution/live_gateway.py` ships exactly one implementation, `DisabledLiveExecutionGateway`, every method of which raises unconditionally. Structurally unavailable, not merely configured off.
- **Duplicated/competing subsystems** — legacy `paper/ledger.py` (`paper_cash_state` JSON row, `simulated_*` tables, `MAX(equity)` drawdown) is still wired to the CLI commands `paper-status`, `execute-paper-recommendation`, `sync-paper-orders`, `reconcile-paper`, in parallel with the active `paper_books` ledger. Campaign state vs attempt state, and local order state vs external event state, are reconciled via derived (never separately maintained) queue status — verified in `derive_external_queue_status`.

---

## Validation of supplied candidate findings

### Candidate-validation table

| Candidate | Status | Severity | Evidence (file:line) | Recommended action |
|---|---|---|---|---|
| A1 implicit txn vs `BEGIN IMMEDIATE` | **CONFIRMED** | MEDIUM | `storage/database.py:25` (no `isolation_level=None`); `shadow/lease.py:75` raise outside `try`; repro below | Open connections with `isolation_level=None` (or assert `not conn.in_transaction` before manual BEGIN); move BEGIN inside try |
| A2 legacy cash RMW race | **CONFIRMED** (legacy scope) | MEDIUM-LOW | `paper/ledger.py:81-103` load/save with no locking | Retire or freeze legacy subsystem; not on paper_books path |
| A3 reservation non-atomic | **PARTIALLY_CONFIRMED** | MEDIUM | `cash_ledger.py:86-101` check-then-insert; `positions.py:188-216` same; order lease keys only one order | Wrap reserve check+insert in `BEGIN IMMEDIATE` |
| A4 no migration framework | **DESIGN_TRADEOFF** | LOW | `storage/migrations.py:3-5` documented; additive `_ensure_columns`; `SCHEMA_VERSION=2` never persisted/compared | Persist schema version; add migration smoke test |
| B1 T+1 calendar days | **PARTIALLY_CONFIRMED** | MEDIUM-LOW | Legacy: `paper/ledger.py:215` `timedelta(days=1)` (Fri→Sat). Active book ledger: **no settlement model at all** (`cash_ledger.settle_sell` credits immediately) | Document active-path settlement semantics; fix or retire legacy |
| B2 drawdown future peak | **CONFIRMED** (legacy only) | LOW | `paper/ledger.py:294-298` `MAX(equity)` no as_of. Active `paper_books/metrics.py:119-127` uses in-window running peak — no look-ahead | Retire legacy snapshot; no active-path change needed |
| B3 missing mark aborts snapshot | **ALREADY_FIXED** (active path) | — | `valuation.py:134-227` partial statuses, unvalued counts, never fabricates | None (legacy fail-closed is by design) |
| B4 BUY reservation lifecycle | **ALREADY_FIXED / verified** | — | `_submit_once` reserves pre-event; `release_settled_buy_reservation` per-fill clamped; FILLED-with-no-fill-details keeps reservation (`_release_terminal_reservation` docstring); terminal releases idempotent | None |
| B5 SELL reservation / oversell | **ALREADY_FIXED / verified** | — | `reserve_shares_for_sell` fail-closed; `_intent` OVERSELL check (`external_broker.py:287-291`); `already_reserved` ceiling = confirmed quantity | None |
| B6 consistency invariants | **PARTIALLY_CONFIRMED** | HIGH (local path) | External fills atomic (`external_broker.py:833-867`, BEGIN IMMEDIATE + commit=False). **Local sim path is not** → new finding N1 | Fix N1 (below) |
| C1 client per retry | **CONFIRMED** | LOW (perf) + LOW (robustness) | `http_client.py:66` new `httpx.Client` per attempt; no `Retry-After`; unbounded body | Hoist client; honor Retry-After; cap body size |
| C2 rate limiter thread-safety | **CONFIRMED** | LOW | `rate_limits.py:34-43` unguarded `_last_acquired`; single-threaded sequential usage today; monotonic clock correct | Add a lock or document single-thread contract |
| C3 health small-sample sensitivity | **CONFIRMED** | MEDIUM-LOW | `shadow/health.py:319-332` `_rate_check` has no sample-size floor: 1 failure of 1 symbol → failure_rate 1.0 → PAUSE_REQUIRED | Add minimum-sample floor / INSUFFICIENT_DATA below N |
| C4 disclosure regex fragility | **PARTIALLY_CONFIRMED** | LOW-MEDIUM | `disclosure_extraction.py:67-70` explicit regex matches negated/alleviated phrasing ("**no** substantial doubt … going concern", "substantial doubt … has been alleviated") as EXPLICIT_DISCLOSURE_FOUND; cover-page checkbox filter exists (`:95-111`) | Add negation/alleviation guard window like the checkbox filter |
| C5 SEC point-in-time availability | **PARTIALLY_CONFIRMED / NEEDS_RUNTIME_EVIDENCE** | LOW | `corporate_status_adapters.py` filters via `available_by=as_of` on `acceptanceDateTime`; company-facts enforcement asserted in `fundamentals.py:6` docstring but only verifiable against live EDGAR | Verify in the opt-in SEC smoke |
| C6 hardcoded cache metadata | **CONFIRMED** | INFORMATIONAL | `http_client.py:123` `"cache_status": "MISS"` — honest (no HTTP-layer cache exists); typed as plain string | Document enum; wire real value if caching added |
| D1 Reddit cap duplication | **DESIGN_TRADEOFF** | LOW | `config/scoring.yaml:15` is authoritative; `analysis/scorer.py:92` hardcodes 0.10 as an architectural *ceiling on the config value* — two-tier by design | Name the constant; cross-reference the ADR |
| D2 config loader side effects | **CONFIRMED** | LOW | `config.py:137-138` `mkdir` on every `load_config` | Move directory creation to first DB/data use |
| D3 permissive booleans | **PARTIALLY_CONFIRMED** | MEDIUM-LOW | paper_books (`_strict_bool`) and paper_runtime (`type(...) is bool`) strict; **`research/scheduled_research_config.py:105-113`** uses `bool()` for `enabled`, `submit_paper_orders`, `allow_live_promotion` — quoted `"false"` in YAML would enable | Use strict bool parsing there too |
| D4 `hash_config` default=str | **CONFIRMED** | LOW-MEDIUM | `hashing.py:15`; Path/set/object reprs → environment-specific or unstable hashes; current callers pass mostly str/Decimal-str | Reject unsupported types instead of stringifying |
| D5 config/doc drift | **CONFIRMED** | MEDIUM | `README.md:3` "No real orders are placed, prepared, previewed, or staged anywhere in this codebase" vs Milestone 11 external paper preview/submit | Update README safety banner to name the external paper boundary |
| E1 transport threads not joined | **PARTIALLY_CONFIRMED** | LOW | `process_client.py:70-73` daemon pump threads never joined (self-terminate at child EOF); `terminate()`'s final `kill()` (`:116`) has no follow-up `wait()` → possible zombie | Join with timeout; `wait()` after `kill()` |
| E2 runtime secret boundary | **ALREADY_FIXED / verified** | — | Allowlist (`cli.py:247-251`); explicit env-file only (`configuration.py:53-78`); stderr suppressed with count only (`process_client.py:184-193`) | None |
| E3 protocol/payload bounds | **PARTIALLY_CONFIRMED** | LOW-MEDIUM | 64 KB bounds both directions; strict request_id/operation correlation. Gap: a late response after a timeout stays in the stdout queue and fails the *next* request (no drain/resync) → new finding N3 | Restart runtime (or drain) after any request timeout |
| E4 broker numeric normalization | **ALREADY_FIXED** | — | `external_broker.py:122-134` `_exact_int` rejects fractional/NaN/inf; fill quantities re-validated (`:786-801`) | None |
| F1 recommendations.run_id no FK | **CONFIRMED** | LOW | `trading_schema.py:148` nullable `run_id`, no REFERENCES — recommendations can validly exist without a screening run (manual `analyze`) | Document intent; add FK only if manual flow is retired |
| F2 `paper_cash_state` runtime DDL | **CONFIRMED** (legacy) | LOW | `paper/ledger.py:67-71` CREATE TABLE inside constructor | Retire with legacy subsystem |
| F3 append-only claims vs triggers | **FALSE_POSITIVE** | — | `paper_books_schema.py:806-893` UPDATE+DELETE `RAISE(ABORT)` triggers verified per table; orders restricted to status-only updates | None |
| F4 FK coverage | **PARTIALLY_CONFIRMED** | LOW | 28 FK references in paper_books schema; some cross-schema links (events→intents by id pair) app-enforced due to write ordering | Note deliberate gaps inline |
| G1 bare exception handling | **PARTIALLY_CONFIRMED** | LOW | Broad `except Exception` sites are annotated boundary isolators (`execute_paper_recommendation.py:131`, `scheduled_integration.py:406`); `Exception` not `BaseException`; reconciliation persists evidence before re-raising (`external_broker.py:1108-1132`) | None urgent |
| G2 recovery lookup swallowed | **CONFIRMED** | LOW-MEDIUM | `submit_credentialed_paper_order.py:137-140` `except Exception: recovered = None` — lookup failure detail never persisted; outcome still safely SUBMISSION_UNKNOWN | Persist sanitized lookup error in the failure record |
| G3 ambiguous retry evidence | **ALREADY_FIXED** (11.1) | — | `external_broker.py:909-949`: fresh authoritative NOT_FOUND bound to exact ambiguous event + attempt + payload hash + fingerprint, unconsumed, consumed on use, bounded by `maximum_retry_attempts`, under order lease | None |
| G4 order-scope concurrency | **PARTIALLY_CONFIRMED** | MEDIUM (via A3) | Order lease + event insert conflict detection (`_append_event`:217-223) + `scope_sequence`; residual: cross-order reservation race (A3); lease TTL 30 s ≤ request timeout 30 s (N8) | Fix A3; raise lease TTL above request timeout |
| G5 campaign recovery / recurring activation | **NOT_REPRODUCIBLE** (no defect found) | — | Queue claim/finalize under BEGIN IMMEDIATE (`recurring_scheduler.py:753-822`); frozen-state hash re-verified at claim; singleton lease with TTL + heartbeat. Reviewed at moderate depth | — |
| H1 `_strip()` alignment | **FALSE_POSITIVE** | — | Input contract is a dense close list; `_strip` removes only the warmup prefix, and MACD/TRIX re-align explicitly (`indicators.py:97-129`); CLI fails fast on non-numeric | Optionally assert no interior Nones |
| H2 `closes()` flexible shapes | **PARTIALLY_CONFIRMED** | LOW | `macro_pillar.py:143-150` mixed lists / missing keys raise raw TypeError/KeyError; NaN accepted | Validate + fail with clear message |
| H3 Bollinger population stdev | **FALSE_POSITIVE** | — | `indicators.py:139` `pstdev` with explicit "population, like TradingView" comment — documented convention | None |
| H4 `score_trend` asymmetry | **DESIGN_TRADEOFF** | LOW | `score.py:38-51`: banding is symmetric (+3→2 / −3→−2); the actual asymmetry is that exact ties score bearish (`else: pts -= 1`) — conservative, undocumented | Document tie policy |
| H5 EMA rebound semantics | **FALSE_POSITIVE** | — | `indicators.py:188-196` comment states daily-close semantics; no intraday claim anywhere | None |

### A1 reproduction (offline, temp DB, real modules)

```text
python 3.14.5rc1; conn from storage.database.connect() → isolation_level == ''
INSERT (uncommitted) → conn.in_transaction == True
lease.acquire(...)   → sqlite3.OperationalError: cannot start a transaction within a transaction
after raise          → conn.in_transaction STILL True  (BEGIN at lease.py:75 is outside try/except → no rollback)
second connection    → sqlite3.OperationalError: database is locked (after 5 s busy timeout)
```

Also demonstrated: a helper's `conn.commit()` silently commits the caller's unrelated pending rows. The two-real-connection lease race itself behaves correctly (`LeaseHandle` + `LeaseConflict`).

Trigger conditions today: any code path that leaves uncommitted DML on a connection before calling `lease.acquire/renew/release`, `recurring_scheduler` lease/queue functions, or `external_broker`'s fill transaction. Most repository writes auto-commit, so the defect is **latent**; `_release_claims_after_failure` (`recurring_scheduler.py:824-831`, caller-committed) and any future `commit=False` usage are the realistic triggers. Existing tests never exercise "pending transaction + manual BEGIN", which is why it went uncaught.

---

## Newly discovered findings

| ID | Finding | Severity | Confidence | Subsystem | Blocker for |
|---|---|---|---|---|---|
| N1 | Local simulated fill application is non-atomic; crash between `save_fill` and position/cash application permanently loses the fill's effects, and `fill_exists` blocks replay | **HIGH** | HIGH | paper_books/execution.py | Unattended recurring local paper (P1) |
| N2 | `BEGIN IMMEDIATE` raise outside `try` wedges the connection (aspect of A1; also applies to `renew`/`release`/`force_release`) | MEDIUM | HIGH | shadow/lease.py | P1 |
| N3 | Stale runtime response after a timeout poisons the next request (strict correlation raises, but no drain/restart) | MEDIUM | HIGH | runtime/client/process_client.py | External paper ops robustness (P2) |
| N4 | `_release_claims_after_failure` performs DML relying on a later caller commit — an uncommitted-transaction window that can trigger A1 | LOW | MEDIUM | paper_books/recurring_scheduler.py | P2 |
| N5 | Pyright baseline of 1824 errors with a non-blocking CI gate lets new type regressions land invisibly | LOW | HIGH | CI | P2 |
| N6 | Untracked, unwired `research/openai_compatible_provider.py` in the working tree (not in HEAD; unreviewed) | LOW | HIGH | research | P3 |
| N7 | Legacy `PaperLedger` CLI commands (`paper-status`, `execute-paper-recommendation`, `sync-paper-orders`, `reconcile-paper`) remain operator-reachable beside paper_books — dual-ledger confusion risk | LOW | HIGH | cli.py / paper | P3 |
| N8 | External order lease TTL (30 s) ≤ runtime request timeout (30 s): the lease can expire while a slow broker call is in flight; event-chain uniqueness backstops forking, but the failure mode is an avoidable EVENT_CHAIN_CONFLICT | LOW | MEDIUM | external_broker.py | P2 |

### N1 detail (HIGH)

`paper_books/execution.py::submit_and_simulate` (lines 120-135): `repo.save_fill(conn, fill)` commits; then `positions.apply_buy_fill`, `cash_ledger.settle_buy`, and `release_reservation` each commit separately. Expected invariant: a fill's lot, position, and cash effects are applied atomically-with-or-not-at-all relative to the fill row. A crash (or exception) after the first commit leaves a persisted fill with **no** lot, position, or cash settlement; on re-run, `fill_exists` returns True and the function reports FILLED without repairing anything — permanent divergence between `paper_book_fills` and positions/cash, silently. The external path already solved this exact problem (`external_broker.py:833-867`: `BEGIN IMMEDIATE` + `commit=False` on every sub-write + single commit); the same pattern should be applied to the local path. Existing tests cover success and duplicate-replay, not the crash boundary between the writes — which is why it survived. Cross-book comparison/reconciliation would eventually flag the drift, but nothing repairs it.

---

## P0 findings — must fix before any real Alpaca paper smoke

None identified. The 11.1 external path's duplicate-order, oversell, reservation, ambiguous-retry, and credential-boundary controls were each verified in source. (A1/A3 below are strongly recommended before the smoke anyway, since they touch the same connections, but neither produces a duplicated broker order, oversell, or credential exposure on the paths a supervised manual smoke exercises.)

## P1 findings — before unattended recurring local paper operation

- **N1** — atomic local fill application (see above).
- **A1/N2** — connection transaction discipline: `isolation_level=None` (or in-transaction assertion) plus moving `BEGIN IMMEDIATE` inside the `try` in `shadow/lease.py`.
- **A3** — wrap reservation check+insert (`cash_ledger.reserve_for_order`, `positions.reserve_shares_for_sell`) in `BEGIN IMMEDIATE`.
- **B1 (active-path aspect)** — decide and document settlement semantics for the paper_books ledger (currently instant settlement; fine for paper simulation only if stated).

## P2 findings — before broader evaluation

- **C3** — sample-size floor for provider-failure health (avoids spurious PAUSE_REQUIRED on 1-symbol cycles).
- **N3** — restart or drain the runtime transport after any request timeout.
- **N8** — order-lease TTL > request timeout.
- **C1** — reuse one `httpx.Client`; honor `Retry-After`; bound response size.
- **D3** — strict booleans in `scheduled_research_config.py`.
- **G2** — persist sanitized recovery-lookup failures.
- **N4, N5** — commit discipline in queue release; ratchet the pyright baseline.

## P3 findings — maintainability / policy clarification

- **D5** — README safety-banner correction (arguably do this immediately; it is one line).
- **N7 / A2 / B2 / F2** — retire or clearly quarantine the legacy `paper/ledger.py` subsystem and its CLI commands.
- **C4** — negation/alleviation guard in going-concern extraction.
- **D1, D2, D4, H2, H4, A4, F1, F4** — as tabled above.

---

## False positives and resolved concerns

- **F3** — every table documented as append-only/immutable has enforcing UPDATE+DELETE `RAISE(ABORT)` triggers; `paper_book_orders` and `paper_book_position_lots` restrict updates to exactly the mutable columns.
- **H1/H3/H5** — indicator concerns rest on contracts the code doesn't have (interior-None series, sample-stdev reference, intraday semantics); each is either impossible by input contract or explicitly documented.
- **B3 (active path)** — partial valuation with `SOURCE_UNAVAILABLE` / `POINT_IN_TIME_UNSAFE` / stale statuses; never fabricates a price, never fails the whole portfolio.
- **B4/B5/E4/G3/E2** — corrected in Milestones 11/11.1 exactly as their code comments claim; verified rather than assumed.
- **C6** — honest constant, not a lie: there is no HTTP-layer cache to report on.
- **B2 (active path)** — window-scoped running peak; no future data. Look-ahead exists only in the legacy snapshot writer.

## Test and CI gaps

- No crash-boundary test for local fill application (N1) — tests assert success and idempotent replay only.
- No test opens a manual transaction on a connection with pending DML (A1 class).
- No negated-phrase fixtures for going-concern extraction (C4).
- Two-real-connection tests exist for the shadow lease but not for cash/share reservation races (A3).
- Pyright is informational-only in CI with a 1824-error baseline (N5); no secret-scanning or dependency-audit job; no migration smoke against a previous-milestone database file (A4).

## Recommended remediation sequence

1. D5 README safety-banner correction (one line, immediate).
2. A1/N2 connection transaction discipline + lease `try` fix, with a pending-transaction regression test.
3. N1 atomic local fill application (port the external path's BEGIN IMMEDIATE/commit=False pattern), with a crash-boundary test.
4. A3 reservation atomicity.
5. N8 lease TTL, N3 transport drain-on-timeout, C3 health floor, D3 strict booleans, G2 lookup evidence.
6. Legacy subsystem retirement (N7/A2/B2/F2) and the P3 tail.

## Operational go/no-go assessment

| Capability | Assessment |
|---|---|
| Research-only analysis | **READY** |
| Local simulated paper trading | **READY_WITH_LIMITATIONS** (N1 crash boundary; supervised use acceptable) |
| Manual soak campaigns | **READY_WITH_LIMITATIONS** (same caveat) |
| Recurring local paper scheduler | **READY_WITH_LIMITATIONS** (fix N1 + A1 before *unattended* operation) |
| Manual external Alpaca paper execution | **READY_WITH_LIMITATIONS** (single operator, disabled-by-default config, A1/A3 remediation recommended) |
| Real Alpaca paper smoke readiness | **READY_WITH_LIMITATIONS** (no P0; run supervised, after items 1–2 above) |
| Live trading | **KEEP_DISABLED / NOT_IMPLEMENTED** (structurally unavailable by design — keep it that way) |

## Appendix: evidence and reproductions

- **A1 repro** — scratchpad scripts `repro_a1.py` / `repro_a1_lease.py` (session scratchpad, temp SQLite): demonstrates (a) `BEGIN IMMEDIATE` failure inside an implicit transaction, (b) unrolled-back wedged connection blocking a second connection, (c) helper `commit()` committing caller work, (d) correct two-connection lease conflict behavior.
- **A3 repro** — `repro_a1_race.py`: sequential two-connection interleave is caught by the in-call re-check (`InsufficientCashError`); the residual window is between one call's check and its own commit, closable only with a surrounding write transaction.
- **Fill-atomicity evidence** — contrast `paper_books/execution.py:120-135` (four separate auto-commits) with `paper_books/external_broker.py:833-867` (one `BEGIN IMMEDIATE`, `commit=False` sub-writes, single commit, rollback on exception).
- **Baseline commands** — `git rev-parse HEAD`, `git status --short`, `git log --oneline -15`, both pytest suites, `pyright`; outputs summarized in "Repository and test baseline".
- No brokers, credentials, networks, real databases, or schedulers were touched; no repository file other than this report and `.codex/scratchpads/full-codebase-audit.md` was created or modified.
