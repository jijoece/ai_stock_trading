# Full Codebase Audit

## Repository baseline
- HEAD: bec1463 (Milestone 11.1 external paper execution safety corrections); branch agent/milestone-11-1-external-paper-safety
- Dirty: M docs/pitfalls_and_improvements.md; untracked docs/codebase-analysis-pitfalls.md, src/trading_research/research/openai_compatible_provider.py (unwired)
- Tests: main 1714 passed / 15 skipped (all opt-in real-provider smokes); paper_runtime 47 passed
- Pyright 1.1.411: 1824 errors, 0 warnings (pre-existing baseline; CI runs it non-blocking by documented decision)
- CI: .github/workflows/ci.yml — main-tests, paper-runtime-tests, type-check (non-blocking); offline-only, no credentials

## Architecture map
- config (env .env + YAML per subsystem) → evidence providers (HttpJsonClient, SEC/Alpaca/Reddit adapters) → scheduled research cycle (lease + cycle idempotency) → recommendations (research_schema) → shadow ops (health/pause/alerts/budget) → paper_books (append-only cash ledger + FIFO lots + reservations) → risk/exit decisions → local sim execution (execution.py) → soak campaign / activation review → recurring scheduler (singleton lease + BEGIN IMMEDIATE queue claims) → external paper (external_broker.py event chain + order-scope lease → RuntimeClient stdio JSON → paper_runtime subprocess w/ Alpaca creds) → reconciliation → metrics.
- Competing subsystems: legacy paper/ledger.py (paper_cash_state JSON row, simulated_* tables) still reachable via CLI (paper-status, execute-paper-recommendation, sync-paper-orders, reconcile-paper) vs paper_books ledger (active).
- Live trading: DisabledLiveExecutionGateway only — structurally unavailable.
- Credential boundary: cli.py `_paper_runtime_command_env` allowlist; runtime loads only PAPER_RUNTIME_ENV_FILE (no dotenv scan). Sound.

## Candidate-finding validation (summary)
A1 CONFIRMED (MED/HIGH-conf) — default isolation_level; BEGIN IMMEDIATE raises inside pending implicit txn; lease.py:75 raise is OUTSIDE try → no rollback, connection wedged (repro'd); helper commit() silently commits caller work.
A2 CONFIRMED, legacy scope — PaperLedger _load_cash/_save_cash RMW race; still on CLI paths.
A3 PARTIALLY_CONFIRMED — reserve_for_order / reserve_shares_for_sell check-then-insert not in one write txn; order lease doesn't serialize *different* orders on one book; narrow window.
A4 DESIGN_TRADEOFF — documented no-framework; additive _ensure_columns; SCHEMA_VERSION not persisted; one ad hoc rename.
B1 PARTIALLY — legacy T+1 = calendar days (Fri→Sat) CONFIRMED; active book ledger models NO settlement delay at all (undocumented semantics).
B2 CONFIRMED legacy only — MAX(equity) no as_of; active metrics use in-window running peak (no look-ahead).
B3 ALREADY_FIXED — valuation.py partial statuses, never fabricates price; legacy fail-closed is by design.
B4 ALREADY_FIXED/verified — per-fill clamped releases, FILLED-no-details keeps reservation, terminal releases idempotent.
B5 ALREADY_FIXED/verified — fail-closed share reservation, OVERSELL check, already_reserved ceiling=confirmed qty.
B6 PARTIALLY — external fill application atomic (BEGIN IMMEDIATE, commit=False); LOCAL sim path is NOT (→ new N1); post-fill state event append is separate txn (reconciliation surfaces lag).
C1 CONFIRMED — new httpx.Client per attempt; no Retry-After; unbounded body; request_url persisted (no secrets in URLs today — auth via headers).
C2 CONFIRMED (LOW) — not thread-safe; single-threaded sequential use; monotonic clock ok.
C3 CONFIRMED — no sample-size floor in _rate_check; 1/1 failure → PAUSE_REQUIRED; fail-closed direction, ops noise.
C4 PARTIALLY — explicit regex matches negated/alleviated phrasing ("no substantial doubt…") as FOUND; cover-page filter present; conservative direction.
C5 PARTIALLY/NEEDS_RUNTIME_EVIDENCE — corporate status uses acceptanceDateTime via available_by=as_of; company-facts enforcement claimed upstream, not runtime-verified.
C6 CONFIRMED (INFO) — cache_status always "MISS"; honest (no cache exists at HTTP layer).
D1 DESIGN_TRADEOFF — scoring.yaml authoritative; hardcoded 0.10 is an architectural ceiling ON the config value, deliberate.
D2 CONFIRMED (LOW) — load_config mkdirs on load.
D3 PARTIALLY — paper_books/paper_runtime strict; scheduled_research_config.py uses bool() for enabled/submit_paper_orders/allow_live_promotion.
D4 CONFIRMED (LOW-MED) — default=str: Paths/sets/reprs → unstable/environment-specific hashes possible.
D5 CONFIRMED (MED) — README: "No real orders are placed, prepared, previewed, or staged anywhere in this codebase" contradicts Milestone 11 external Alpaca paper preview/submit.
E1 PARTIALLY (LOW) — daemon pump threads unjoined (self-terminate at child EOF); final kill() has no wait (zombie possible).
E2 ALREADY_FIXED/verified — env allowlist + explicit PAPER_RUNTIME_ENV_FILE only.
E3 PARTIALLY — strict correlation + 64KB bounds both directions; but a late response after a timeout stays queued and poisons the NEXT request (ProtocolViolationError; no drain/resync).
E4 ALREADY_FIXED — _exact_int fail-closed on fractional/NaN/inf (11.1 Part 13).
F1 CONFIRMED (LOW) — recommendations.run_id nullable, no FK; likely intentional (manual analyze flow).
F2 CONFIRMED legacy — paper_cash_state DDL inside PaperLedger.__init__; retire with legacy subsystem.
F3 FALSE_POSITIVE — append-only triggers verified in paper_books_schema (update+delete RAISE ABORT per table; orders status-only).
F4 PARTIALLY (LOW) — 28 FK refs in paper_books; some cross-schema links app-enforced.
G1 PARTIALLY (LOW) — broad excepts exist, all annotated at adapter/isolation boundaries; Exception not BaseException.
G2 CONFIRMED (LOW-MED) — recovery lookup failure swallowed (`except Exception: recovered=None`), detail never persisted; safe direction, evidence lost.
G3 ALREADY_FIXED — retry requires fresh, unconsumed, authoritative NOT_FOUND bound to exact ambiguous event/attempt/hash/fingerprint; bounded attempts; consumed after use.
G4 PARTIALLY — order-scope lease + event-chain uniqueness + scope_sequence verified; cross-order cash race remains (=A3); lease TTL 30s ≤ request timeout 30s (could expire mid-call; event-chain conflict backstops).
G5 NOT_REPRODUCIBLE (no defect found) — queue claim/finalize under BEGIN IMMEDIATE; frozen-state hash re-checked at claim; reviewed at moderate depth only.
H1 FALSE_POSITIVE — dense-input contract keeps alignment; fragile only if reused with interior Nones (CLI fails fast on None).
H2 PARTIALLY (LOW) — mixed/malformed lists raise unhelpfully; CLI-scoped.
H3 FALSE_POSITIVE — pstdev documented as TradingView convention.
H4 DESIGN_TRADEOFF — banding symmetric; ties score bearish (conservative, undocumented).
H5 FALSE_POSITIVE — daily-close semantics documented in code comment.

## Newly discovered findings
N1 HIGH — execution.py::submit_and_simulate applies fill via 4+ auto-commit steps; crash between save_fill and position/cash application permanently loses effects; fill_exists blocks replay. P1 blocker for unattended recurring local operation.
N2 MED — folded into A1: BEGIN IMMEDIATE raise outside try wedges connection (lease.py acquire/renew/release/force_release; scheduler sites are inside try).
N3 MED — RuntimeClient stale-response poisoning after timeout (no queue drain/restart guidance).
N4 LOW — _release_claims_after_failure DML relies on caller commit (uncommitted-txn window feeds A1).
N5 LOW — pyright 1824-error baseline, non-blocking CI gate.
N6 LOW — untracked, unwired openai_compatible_provider.py in working tree (not part of HEAD; unreviewed dead code risk).
N7 LOW (P3) — legacy PaperLedger CLI paths still operator-reachable alongside paper_books (operator-confusion/dual-ledger drift risk).
N8 LOW — order-scope lease TTL 30s can expire during a 30s runtime call.

## Test and CI review
- 1714 tests; 15 skips all opt-in credentialed smokes (correct). Two-real-connection tests exist for shadow lease. Gaps: no crash-boundary test for local sim fill application (N1); no test for BEGIN-IMMEDIATE-with-pending-txn (A1); no negated-phrase test for going-concern regex (C4).

## Security boundaries
- Runtime env allowlist + explicit dotenv only: sound. CLI error sanitization present. No secrets in provider URLs today (headers). MCP/reddit unrelated to execution path.

## Financial and point-in-time integrity
- Active valuation is PIT-safe with explicit unsafe/stale/missing statuses. Legacy snapshot drawdown look-ahead confirmed (legacy only). Active book: no settlement modeling (documented gap).

## Persistence and concurrency
- Append-only triggers verified. Main risks: A1 (latent wedge), A3 (narrow cross-order reservation race), N1 (local sim crash hole).

## External broker boundary
- Paper endpoint pinned (base_url exact match, health must prove paper + real_money_disabled); fingerprint history binding; one-account-one-book enforced; long-only LIMIT/DAY; duplicate detection; retry evidence gating. No P0 found in the 11.1 path.

## Operational readiness
- Research-only: READY. Local sim: READY_WITH_LIMITATIONS (N1). Soak: READY_WITH_LIMITATIONS. Recurring scheduler: READY_WITH_LIMITATIONS (fix N1 first for unattended). Manual external Alpaca paper: READY_WITH_LIMITATIONS (A1/A3 remediation recommended; keep single-operator + disabled-by-default). Real paper smoke: READY_WITH_LIMITATIONS. Live: KEEP_DISABLED / NOT_IMPLEMENTED (structural).

## Final classification
CONFIRMED 13 · PARTIALLY_CONFIRMED 12 · ALREADY_FIXED 6 · DESIGN_TRADEOFF 3 · FALSE_POSITIVE 4 · NOT_REPRODUCIBLE 1 · NEEDS_RUNTIME_EVIDENCE (C5, partially) 1-overlap
