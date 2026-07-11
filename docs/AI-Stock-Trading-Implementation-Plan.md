# AI Stock Trading — Implementation Plan

**Date:** 2026-07-11
**Companion to:** [AI-Driven-Stock-Trading-Architecture.md](AI-Driven-Stock-Trading-Architecture.md)
**Sizing:** S (≤ half day) · M (1–2 days) · L (3–5 days) · XL (1–2 weeks)

Global security requirements applying to **every** story below:
- No broker write tool is ever called; no real-order code path exists before Milestone 5 (which is gated, not scheduled).
- All external text (Reddit, news, web) passes the prompt-injection filter before any LLM exposure and is stored raw.
- No credentials in source control; all secrets via `.env` (gitignored) with log redaction.
- Fail closed: missing/stale critical state ⇒ `ANALYSIS_INCOMPLETE`/`NO_ACTION`, recorded.
- All deterministic financial calculations ship with unit tests in the same PR.

---

## Milestone 0 — Foundation hardening (mostly complete)

**Goal:** safe substrate: read-only MCP posture, config, logging, storage.

| # | Story / task | Details | Size | Depends on | Acceptance criteria |
|---|---|---|---|---|---|
| 0.1 | ✅ MCP capability inventory | `scripts/inventory_mcp_tools.py`, snapshots in `research-input/` | M | — | Both servers' tools classified read vs. write with reasons |
| 0.2 | ✅ Tool policy | `config/tool_policy.yaml` allowlist + denylist, fail-closed classifier | S | 0.1 | Unknown tool ⇒ prohibited |
| 0.3 | ✅ Config + logging | typed `Config`, `.env.example`, secret redaction | S | — | Secrets never appear in logs |
| 0.4 | ✅ Research storage | SQLite schema + repositories | S | — | Idempotent schema apply |
| 0.5 | Remove stale user-scope Reddit MCP entry from `~/.claude.json` | Contains plaintext Reddit credentials for a deleted directory | S | — | Entry gone; credentials rotated on Reddit side |
| 0.6 | Register read-only Reddit app | Client id/secret only (no username/password) → unblocks live collection (anonymous = 403, confirmed) | S | — | `search_reddit` returns data through MCP with creds set |
| 0.7 | Pin Reddit MCP version | `npx reddit-mcp-server@1.5.1` in MCP config | S | — | Version pinned; upgrade requires inventory re-diff (0.1) |

## Milestone 1 — Deterministic core + paper ledger (Phase 1 build)

**Goal:** end-to-end: universe → screen → score → risk → frozen recommendation → paper fill → basic evaluation, on mocked/fixture data, fully tested.

**Epic 1A — Reference data & schemas**

| # | Story | Details | Size | Depends on | Acceptance |
|---|---|---|---|---|---|
| 1A.1 | Trading schema | `storage/trading_schema.py`: securities, price_bars, fundamentals, corporate_events, earnings_calendar, sec_filings, news_items, reddit_* (3), screening_runs, candidate_scores, recommendations (+factors), model/prompt_versions, simulated_* (4), approvals, real_orders (reserved), evaluation_results, benchmark_results, agent_runs, tool_calls | M | 0.4 | Idempotent DDL; FK integrity; unit test creates all tables |
| 1A.2 | Verified ticker universe | `universe/tickers.py` + seed data; ambiguous-symbol list (AI, IT, ON, ALL, SO, A, FOR, ARE, …) | M | 1A.1 | Unknown symbol rejected; ambiguity flag correct; test coverage |
| 1A.3 | Recommendation JSON schema | `schemas/recommendation.schema.json`, draft-07, additionalProperties:false | S | — | Valid/invalid fixtures pass/fail validation in tests |

**Epic 1B — Reddit deterministic analytics (on stored/fixture data)**

| # | Story | Details | Size | Depends on | Acceptance |
|---|---|---|---|---|---|
| 1B.1 | Ticker mention parser | `analysis/ticker_extractor.py`: cashtag + contextual bare-symbol matching against universe | M | 1A.2 | False-positive words (AI/IT/FOR…) not matched without context; cashtags matched; tests |
| 1B.2 | Sentiment aggregation interface | `analysis/sentiment.py`: deterministic aggregates (counts, unique authors, growth, engagement-weighted, duplicates) over classified records; classification itself pluggable (mock now, Claude API in M2) | M | 1B.1 | Aggregates reproducible from fixtures; LLM does no counting; tests |
| 1B.3 | Live collection (creds-gated) | Wire existing `reddit_adapter.py` collector to store reddit_posts/comments with injection annotation | M | 0.6, 1A.1 | Raw records stored with timestamps + dedup keys; 403 handled as recorded incident |

**Epic 1C — Screening, scoring, risk**

| # | Story | Details | Size | Depends on | Acceptance |
|---|---|---|---|---|---|
| 1C.1 | Screener | `analysis/screener.py` + `config/screening.yaml` hard gates (price, mcap, dollar volume, OTC, distress/dilution/runway/earnings/volatility/halt/spread flags) | M | 1A.1 | Each gate individually tested; failures recorded with reasons |
| 1C.2 | Composite scorer | `analysis/scorer.py`: pillar weights from config; Reddit capped 10%; factor contributions persisted | M | 1C.1, 1B.2 | Score reconstructible from stored factors; cap enforced in code + test |
| 1C.3 | Risk engine | `risk/position_sizing.py`: sizing, stop, target, R:R, concentration, exposure caps, earnings/liquidity restrictions; fail-closed `IncompleteStateError` | M | 1A.1 | Boundary tests (zero risk/share, cash caps, missing inputs ⇒ error); property check shares×risk ≤ max risk |
| 1C.4 | Recommendation builder | Frozen, schema-validated rows incl. not-acted candidates, model/prompt versions | S | 1A.3, 1C.2, 1C.3 | No UPDATE path; `ANALYSIS_INCOMPLETE` on risk failure |

**Epic 1D — Paper ledger & evaluation basics**

| # | Story | Details | Size | Depends on | Acceptance |
|---|---|---|---|---|---|
| 1D.1 | Paper ledger | `paper/ledger.py`: simulated orders/fills (spread+slippage model), T+1 settled cash, positions, snapshots, idempotency keys | L | 1A.1, 1C.4 | Fill math tested; unsettled cash unusable; duplicate order rejected |
| 1D.2 | Evaluator (per-rec) | 1/5/20-day returns, MFE/MAE, stop/target triggering from frozen values | M | 1D.1 | Known-answer fixtures; outcome data strictly after rec timestamp |
| 1D.3 | Mock adapters | `mcp/mock_adapters.py`: MockRobinhood/MockReddit replaying fixture JSON | S | — | Full pipeline runs offline in CI |
| 1D.4 | CLI | `cli.py`: `analyze <ticker>` (mocked/read-only data), `run-screen`, `paper-status`, `evaluate` | M | 1C.4, 1D.1, 1D.3 | `analyze` on one ticker end-to-end with mocks; no network required |

## Milestone 2 — Signal maturation (Phase 2)

| # | Story | Details | Size | Depends on | Acceptance |
|---|---|---|---|---|---|
| 2.1 | Market-data source selection + collector | Resolve open question (coverage of sub-$25 names); daily incremental bar/fundamental pulls | L | 1A.1 | Bars cached immutably; staleness tracked |
| 2.2 | SEC/EDGAR ingestion | Filings metadata + risk flags (going concern, dilution) | L | 1A.1 | Flags feed screener gates |
| 2.3 | Claude API sentiment classifier | Small model, quoted excerpts, JSON schema output, retry-on-invalid, prompt caching; skip-unchanged | M | 1B.2 | Invalid output rejected+retried; token spend logged per run |
| 2.4 | Rationale writer | Larger model writes prose from frozen numbers only | S | 2.3 | Rationale references only stored factor values |
| 2.5 | Benchmark suite | SPY, equal-weight, random-from-screen (seeded distribution), MA-crossover baseline | M | 1D.2, 2.1 | Random baseline reported as distribution |
| 2.6 | Daily watchlist report | Markdown from DB rows; scheduled via launchd/cron | S | 2.5 | Report reproducible from DB alone |
| 2.7 | Regime/sector/score-band slicing + with/without-Reddit split | Evaluation slices per architecture §18 | M | 2.5 | Reddit-weight-0 re-ranking comparison produced |

## Milestone 3 — Validation & research (Phase 3)

| # | Story | Details | Size | Depends on | Acceptance |
|---|---|---|---|---|---|
| 3.1 | 6-month evidence accumulation | Run daily; freeze everything; monthly evaluation reports | XL (calendar) | M2 | ≥100 frozen recommendations |
| 3.2 | VectorBT factor research (optional) | Vectorized studies over accumulated data | L | 3.1 | Weight changes only via config PR with backtest evidence |
| 3.3 | Risk review & go/no-go | Written review against architecture §25 criteria | M | 3.1 | Signed decision doc; default answer is "not yet" |

## Milestone 4 — Gated real-trading design (only if 3.3 passes — not scheduled)

| # | Story | Details | Size | Depends on | Acceptance |
|---|---|---|---|---|---|
| 4.1 | Approval service | Hash-pinned payloads, TTL, state-drift invalidation (architecture §12) | L | 3.3 | Drifted state ⇒ approval invalid; expired ⇒ unexecutable; no standing-rule representation |
| 4.2 | Separate execution profile | Distinct process + credentials; Agentic-account only; immutable audit chain | L | 4.1 | Research pipeline physically cannot execute |
| 4.3 | Real-order tables + reconciliation | `real_orders` activation, fill reconciliation, kill switch | M | 4.2 | Duplicate-order impossible via idempotency keys |

## Recommended implementation order

0.5 → 0.6 → 0.7 (quick safety wins) → 1A.1 → 1A.2 → 1A.3 → 1B.1 → 1C.1 → 1C.3 → 1B.2 → 1C.2 → 1C.4 → 1D.1 → 1D.2 → 1D.3 → 1D.4 → (M1 complete: full offline pipeline) → 2.1 → 2.2 → 2.3 → 2.4 → 2.5 → 2.6 → 2.7 → M3 calendar time → gate.

**Test requirements summary:** every epic lands with pytest coverage of its deterministic math; mock-adapter integration test keeps CI offline; schema-validation fixtures for every JSON producer; fail-closed paths explicitly tested.

**Complexity totals:** M0 remaining ≈ 3×S; M1 ≈ 3×S + 8×M + 1×L; M2 ≈ 2×S + 3×M + 2×L; M3 ≈ 1×M + 1×L + calendar time; M4 ≈ 1×M + 2×L (gated).

---

*Research and evaluation only. Not financial advice. Milestone 4 is a design placeholder, not an authorization — it requires the evidence gate in Milestone 3.3 and explicit human sign-off.*
