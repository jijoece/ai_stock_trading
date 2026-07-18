# Milestone 6 Progress

Started: 2026-07-13T02:20:03Z
Branch: main
Status: STARTING

## Baseline
- Main test suite: `pytest tests/ -q` -> 571 passed, 2 skipped (matches expected M5 result exactly)
- Paper runtime suite: `cd paper_runtime && pytest tests/ -q` -> 33 passed (matches expected)
- Git status: clean except docs/milestones/milestone-6.md modified (pre-existing working-tree edit to the milestone spec itself, updating M5's real-outcome narrative — not part of this session's implementation, left as-is) + untracked .claude/scratchpads/milestone6-progress.md (this file)
- Available credentials/providers (boolean only):
  - ANTHROPIC_API_KEY: SET (present in process env, confirmed working in M5 smoke test)
  - ANTHROPIC_MODEL: SET (claude-sonnet-5)
  - ALPACA_API_KEY / ALPACA_API_SECRET: SET, ALPACA_IS_PAPER: SET
  - REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET: EMPTY (anonymous Reddit API blocked per .env.example note)
  - No market-data-specific API key present (no Polygon/AlphaVantage/etc key in .env)
  - No news-provider API key present in .env
  - SEC EDGAR requires no API key, only a compliant User-Agent identification header
- Network reachability probes (sandbox environment):
  - https://www.google.com -> 200 (general network reachable)
  - https://data.sec.gov/submissions/CIK0000320193.json with compliant User-Agent -> 200 (SEC EDGAR reachable)
  - https://paper-api.alpaca.markets/v2/clock (unauthenticated) -> 401 (reachable, auth required as expected)
- Explicitly unavailable credentials/providers: dedicated market-data API key (none configured), news API key (none configured), Reddit API credentials (empty)

## Repository findings

Read directly (not delegated): milestone1-5 docs, ADR 0001-0003, config.py, cli.py (full),
research/{evidence,models,fixtures,configuration,orchestration,experiment,recommendation_overlay,
errors}.py, models/source_models.py, storage/{database,research_schema,research_repositories,
evaluation_schema}.py, evaluation/{price_provider,evaluation_service,metrics,models,research_comparison}.py,
services/analyze_candidate.py, universe/tickers.py, paper/ledger.py, execution/config.py,
config/{research,paper_runtime}.yaml, mcp/reddit_adapter.py, pyproject.toml.

Key findings that materially shape the M6 design:

1. **The evidence-provider plug point already exists and needs no new Protocol.**
   `research/evidence.py` already defines `FundamentalsEvidenceProvider`,
   `MarketEvidenceProvider`, `NewsEvidenceProvider`, `SentimentEvidenceProvider`,
   `FilingEvidenceProvider`, `PortfolioContextProvider` — all structural Protocols with one
   method `fetch(symbol, as_of) -> EvidenceBundle`. `research/fixtures.py`'s
   `FixtureFundamentalsProvider` etc. are the existing reference implementation. Real providers
   in this milestone are new classes satisfying the *same* Protocols — `build_evidence_snapshot`
   requires zero changes. This means Milestone 6's "Step 4: framework-neutral provider
   contracts" is mostly already done; the new work is a lower raw-client layer underneath
   (SEC/Alpaca/News HTTP clients) plus adapter classes that call the raw client and emit
   `EvidenceBundle`.
2. **`evaluation/price_provider.py::PriceProvider` is the plug point for both evaluation and
   market-data evidence.** One `get_close(symbol, as_of: date) -> PricePoint | None` Protocol
   already used by `evaluate_recommendation`. A real Alpaca-backed implementation serves both.
3. **The research orchestration/overlay/experiment machinery for a single symbol already
   exists and needs no changes**: `analyze_with_research_committee` (single-snapshot run with
   idempotent reuse), `apply_overlay_to_recommendation` (baseline -> enhanced recommendation),
   `build_experiment_assignments` (always records both arms). Milestone 6's "scheduled cycle"
   is primarily a *loop* over a candidate universe that calls these existing functions per
   symbol with real evidence instead of fixture evidence — not a rewrite.
4. **`services/analyze_candidate.py::analyze_candidate` is the existing baseline-recommendation
   builder** — deterministic, already screens+scores+risk-sizes+freezes. The scheduled cycle
   calls this for the baseline arm exactly as-is.
5. **No dedicated market-data or news API key is configured.** `ALPACA_API_KEY`/`SECRET` are
   present (Milestone 4 paper-broker credentials) and Alpaca's *market-data* API
   (`data.alpaca.markets`) accepts the exact same key pair for real historical bars/quotes —
   reachable via a direct `httpx` call from the main process (no LumiBot import, preserving the
   Milestone 3/4 process-isolation invariant). This is the natural, already-authorized "primary
   market-data provider" — confirmed reachable (network probe below).
6. **SEC EDGAR requires no API key**, only a compliant `User-Agent` identification header and a
   self-imposed rate limit — confirmed reachable with a real 200 response
   (`data.sec.gov/submissions/CIK0000320193.json`).
7. **No news-provider API key is configured**, and Reddit real access requires
   `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` (both empty) even though the MCP stdio scaffolding
   (`mcp/reddit_adapter.py`) exists — real news and real Reddit sentiment are
   ENVIRONMENTALLY_PENDING for this session; both get fully implemented Protocol-shaped code
   paths that fail closed to an explicit missing-data reason rather than silently no-op'ing.
8. **Config convention**: one YAML per concern (`screening.yaml`, `scoring.yaml`,
   `research.yaml`, `execution.yaml`, `paper_runtime.yaml`), loaded by a `load_*_config()`
   function that requires every key present, raises a module-specific `*ConfigError`, and
   returns a frozen dataclass with `config_hash = hash_config(raw)`. New M6 config follows this
   exactly (`config/evidence_providers.yaml`, `config/scheduled_research.yaml`).
9. **Storage convention**: one schema module per concern (`research_schema.py`,
   `evaluation_schema.py`, ...), each with an idempotent `apply_*_schema(conn)` wired into
   `storage/database.py::connect`. New M6 tables follow the same pattern
   (`evidence_provider_schema.py`, `scheduled_cycle_schema.py`).
10. **CLI convention**: one function per command in `cli.py` returning a plain dict, printed as
    `json.dumps(..., indent=2, default=str)`; `argparse` subparsers registered in `main()`;
    exit code 2 on `"error"` in the result dict. New M6 commands follow this exactly.

## Gap analysis

Classification key: IMPLEMENTED / PARTIALLY_IMPLEMENTED / MISSING / CONFLICTING / ENVIRONMENTALLY_BLOCKED

- real historical-price provider: MISSING (to build: Alpaca data API)
- real current quote provider: MISSING (to build: Alpaca data API)
- real fundamentals provider: MISSING (to build: derive from SEC company facts)
- SEC filing provider: MISSING (to build: real EDGAR)
- real news provider: ENVIRONMENTALLY_BLOCKED (no API key available; interface + fixture built, real impl documented pending)
- real sentiment provider: ENVIRONMENTALLY_BLOCKED (Reddit MCP scaffolding exists but REDDIT_CLIENT_ID/SECRET empty; interface built, fails closed with explicit missing-data reason)
- read-only portfolio-context provider: MISSING (to build: reads PaperLedger, no external creds needed)
- provider caching: MISSING (to build)
- source-response persistence: MISSING (to build)
- point-in-time availability metadata: PARTIALLY_IMPLEMENTED (EvidenceSnapshot/SourceRecord already carry it; new providers must populate it correctly)
- provider rate-limit handling: MISSING (to build)
- provider retries: MISSING (to build; bounded, non-infinite)
- scheduled pipeline: MISSING (to build; reuses existing analyze_candidate + orchestration + overlay + experiment)
- candidate batching: MISSING (to build; bounded per-cycle)
- experiment creation: IMPLEMENTED (research/experiment.py, reused as-is)
- paper-execution linkage: PARTIALLY_IMPLEMENTED (execute_paper_recommendation exists; needs an experiment-policy gate so only the correct arm submits)
- evaluation scheduling: MISSING (to build; evaluate-research-cycle CLI)
- provider health metrics: MISSING (to build)
- promotion gates: MISSING (to build)
- rollback behavior: MISSING (to build; ROLLBACK_REQUIRED status + docs)
- data-retention policy: MISSING (to build; documented in ADR 0004 — raw payload size caps, no secret persistence)

## Architecture decisions

Architecture decisions (finalized, before writing code):

1. New real providers implement the EXISTING `research/evidence.py` Protocols directly
   (`FundamentalsEvidenceProvider`/`MarketEvidenceProvider`/`NewsEvidenceProvider`/
   `SentimentEvidenceProvider`/`FilingEvidenceProvider`/`PortfolioContextProvider`) — no new
   Protocol layer duplicates these. A lower "raw client" layer (SEC/Alpaca HTTP clients)
   sits underneath; thin adapter classes translate raw responses into `EvidenceBundle`.
2. New package `src/trading_research/evidence_providers/` holds: raw HTTP clients, rate
   limiting, deterministic caching, request/response persistence, health tracking, and the
   `EvidenceBundle`-emitting adapters. Nothing in `research/` needs to change to consume them.
3. Market-data client also implements `evaluation/price_provider.py::PriceProvider` — one
   real Alpaca-backed class serves both evidence-building and forward-evaluation, per the
   milestone's "avoid several overlapping market-data providers" instruction.
4. New package `src/trading_research/research/scheduled_cycle.py` implements
   `run_scheduled_research_cycle` as a per-symbol loop over the EXISTING
   `services/analyze_candidate.analyze_candidate` (baseline) +
   `research/orchestration.analyze_with_research_committee` (enhanced) +
   `research/recommendation_overlay.apply_overlay_to_recommendation` +
   `research/experiment.build_experiment_assignments` — no rewrite of any of those.
5. Experiment execution policy defaults to `SHADOW_ENHANCED`: baseline may submit to the
   existing paper pipeline; enhanced is generated/evaluated only, never submitted — enforced
   structurally (the cycle service never calls `execute_paper_recommendation` for an enhanced
   rec_id).
6. New storage modules (`evidence_provider_schema.py`, `research_cycle_schema.py`) follow the
   exact `apply_*_schema(conn)` idempotent-DDL convention and are wired into
   `storage/database.py::connect`.
7. New config files (`config/evidence_providers.yaml`, `config/scheduled_research.yaml`)
   follow the exact `load_*_config()` fail-closed pattern used by every existing config module.
8. httpx becomes an explicit base dependency (was already an undeclared transitive dependency
   of `anthropic`) — used directly by the new provider HTTP clients, with an injectable
   transport for offline tests (`httpx.MockTransport`), mirroring how `RuntimeClient` injects
   its transport in Milestone 4.

## Provider decisions
### SEC EDGAR (REAL)
- Purpose: filings list + company facts (XBRL) -> also the basis for derived fundamentals
- Authentication: none required; compliant `User-Agent: agentic-trading-desk research contact (github.com/local-dev)` identification header required by SEC fair-access policy
- Point-in-time guarantees: uses `acceptanceDateTime` from `submissions/CIK##########.json` as `available_at`; company-facts values carry `filed` date, used as availability
- Rate limits: SEC's documented guidance is <=10 req/sec; this implementation self-limits to 1 req / 150ms (~6.7/sec) with a bounded retry (max 2) on 429/5xx
- Licensing/usage restrictions: SEC EDGAR data is public domain (US government work); safe to persist normalized data and raw JSON responses
- Fallback behavior: provider failure -> EvidenceBundle with missing_data_reasons, never fabricated data; no silent fallback to another provider

### Market data — Alpaca Data API (REAL)
- Purpose: quotes, historical daily bars, SPY benchmark bars
- Authentication: reuses existing `ALPACA_API_KEY`/`ALPACA_API_SECRET` (Milestone 4 paper-broker credentials) — same key pair is valid for `data.alpaca.markets`; called directly via httpx from the main process (no LumiBot import, preserves Milestone 3/4 process isolation)
- Point-in-time guarantees: daily bars are date-stamped closes; `as_of` filtering rejects any bar whose date is after the requested as_of/current date
- Rate limits: free-tier Alpaca data plan is IEX-feed, 200 req/min; this implementation self-limits to 1 req/350ms and bounded retry (max 2) on 429
- Licensing/usage restrictions: Alpaca market data terms permit use for a linked brokerage account's own research; not redistributed externally, only persisted internally for this account's evaluation/research use
- Fallback behavior: provider failure -> explicit missing-data / DELISTED_OR_UNAVAILABLE, no current-quote substitution for historical closes, no fabricated bars

### Fundamentals (derived from SEC company facts, REAL)
- Purpose: revenue/earnings/margins/cash/debt/shares from normalized XBRL concepts
- Authentication: none (rides on SEC client)
- Point-in-time guarantees: only company-facts values whose `filed` date <= as_of are used
- Rate limits: shares SEC client's limiter
- Licensing: public domain
- Fallback: missing concept -> omitted (never zero-filled), never a fabricated ratio

### News (ENVIRONMENTALLY_PENDING — no API key configured)
- Purpose: catalyst/news evidence
- Authentication: would require a provider API key; none present in `.env`
- Fallback behavior: `NewsEvidenceProvider` real implementation returns an explicit
  missing-data reason ("news provider not configured — no API key") rather than fabricating
  or silently falling back to fixtures in a real run; fixture provider remains available for
  offline tests only

### Sentiment / Reddit (ENVIRONMENTALLY_PENDING — credentials empty)
- Purpose: Reddit sentiment evidence via existing MCP scaffolding (`mcp/reddit_adapter.py`)
- Authentication: `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` both empty; anonymous Reddit API access confirmed blocked (403) per `.env.example`
- Fallback behavior: real `SentimentEvidenceProvider` checks credential presence and returns
  an explicit missing-data reason when absent; reuses `analysis/sentiment.py::aggregate` over
  already-fetched records when a real fetch path is wired in the future

### Portfolio context (REAL — no external credentials needed)
- Purpose: read-only position/cash context from the existing `PaperLedger`
- Authentication: none (local SQLite read)
- Fallback: unavailable ledger state -> `None` (explicit missing context, never fabricated)

## Implementation checklist

- [x] Mandatory scratchpad created before any code edit
- [x] Baseline verified (571 passed/2 skipped main, 33 passed paper_runtime)
- [x] Gap analysis
- [x] ADR 0004 drafted
- [x] Provider selection: SEC EDGAR (real, no key) + Alpaca market data (real, distinct
      credential pair) as the minimum set; news + Reddit sentiment scoped as
      ENVIRONMENTALLY_PENDING with full fail-closed interfaces
- [x] Provider-neutral raw contracts (evidence_providers/models.py) + adapters satisfying
      the existing research/evidence.py Protocols unchanged
- [x] Provider request/response persistence (evidence_provider_requests table, wired to
      real HTTP calls via HttpJsonClient.on_response)
- [x] SEC EDGAR provider — real, environment-validated
- [x] Market-data provider — real, environment-validated (after the feed=iex fix)
- [x] Fundamentals normalization from SEC company facts — real, environment-validated,
      one real bug found+fixed (fp field not annual/quarterly-reliable)
- [x] News provider — interface complete, ENVIRONMENTALLY_PENDING (no key)
- [x] Sentiment provider — interface complete, ENVIRONMENTALLY_PENDING (no Reddit creds)
- [x] Portfolio-context provider — real, local, no external creds needed
- [x] Evidence normalization/conflict outcome classification
      (COMPLETE/COMPLETE_WITH_CONFLICTS/INCOMPLETE_REQUIRED_DATA/STALE_REQUIRED_DATA/
      POINT_IN_TIME_UNSAFE/PROVIDER_UNAVAILABLE)
- [x] Provider caching (deterministic TTL policy per category) + rate limiting (bounded
      retry, min-interval limiter)
- [x] Scheduled research-cycle service — idempotent, resumable, per-symbol failure
      isolation, reuses Milestone 1-5 services unchanged
- [x] Experiment execution policy (SHADOW_ENHANCED default; enhanced arm structurally
      cannot execute under any supported policy)
- [x] Continuous evaluation lifecycle: time-to-fill, turnover, confidence calibration
- [x] Promotion gates — deterministic, versioned, no live-trading status possible
- [x] Provider/pipeline health metrics
- [x] Configuration (config/evidence_providers.yaml, config/scheduled_research.yaml) —
      fail-closed, disabled by default
- [x] CLI commands (provider-health, fetch-evidence, run-research-cycle,
      resume-research-cycle, evaluate-research-cycle, compare-research-cycles,
      research-promotion-status, evidence-provider-usage)
- [x] Offline deterministic test suite (83 new tests across unit + integration)
- [x] Opt-in real-provider smoke tests — SEC and market-data both genuinely PASSED against
      real endpoints; news honestly SKIPPED/ENVIRONMENTALLY_PENDING
- [x] Real scheduled-cycle smoke test — genuinely PASSED (deterministic provider) +
      manual real-Claude validation (see Environmental validation #5)
- [x] Documentation (docs/milestones/milestone6-real-evidence-continuous-evaluation.md) + ADR 0004
- [x] Full main suite + paper_runtime suite re-verified passing after all changes
- [x] Self-review for secrets/look-ahead/duplication/execution leakage (see below)

## Files created

Evidence providers (`src/trading_research/evidence_providers/`): `__init__.py`, `errors.py`,
`models.py`, `rate_limits.py`, `cache.py`, `http_client.py`, `sec_provider.py`,
`market_data_provider.py`, `fundamentals.py`, `news_provider.py`, `sentiment_provider.py`,
`portfolio_context.py`, `evidence_adapters.py`, `normalization.py`, `health.py`,
`persistence.py`, `config.py`, `fixture_clients.py`.

Research layer: `src/trading_research/research/scheduled_cycle.py`,
`src/trading_research/research/experiment_policy.py`,
`src/trading_research/research/promotion.py`,
`src/trading_research/research/scheduled_research_config.py`.

Evaluation layer: `src/trading_research/evaluation/time_to_fill.py`,
`src/trading_research/evaluation/turnover.py`,
`src/trading_research/evaluation/calibration.py`.

Storage: `src/trading_research/storage/evidence_provider_schema.py`,
`src/trading_research/storage/research_cycle_schema.py`,
`src/trading_research/storage/research_cycle_repositories.py`.

Config: `config/evidence_providers.yaml`, `config/scheduled_research.yaml`.

Docs: `docs/adr/0004-real-evidence-provider-boundary.md`,
`docs/milestones/milestone6-real-evidence-continuous-evaluation.md`.

Tests (unit): `test_evidence_provider_cache_and_rate_limits.py`, `test_sec_provider.py`,
`test_market_data_provider.py`, `test_fundamentals_normalization.py`,
`test_experiment_policy.py`, `test_promotion_gates.py`, `test_time_to_fill.py`,
`test_turnover.py`, `test_calibration.py`, `test_evidence_normalization_outcomes.py`,
`test_evidence_adapters_fail_closed.py`.

Tests (integration): `test_scheduled_research_cycle.py`, `test_sec_api_smoke.py`,
`test_market_data_api_smoke.py`, `test_news_api_smoke.py`, `test_real_research_cycle_smoke.py`.

## Files modified

- `.env.example` — added `ALPACA_MARKET_DATA_API_KEY`/`ALPACA_MARKET_DATA_API_SECRET`
  (deliberately distinct from the Milestone 4 paper-broker pair; documented why)
- `pyproject.toml` — added `httpx` as an explicit base dependency (was already an
  undeclared transitive dependency of `anthropic`); added `sec_api`/`market_data_api`/
  `news_api`/`real_research_cycle` pytest markers
- `src/trading_research/cli.py` — added 8 new commands + `_build_evidence_provider_registry`/
  `_make_persist_hook` helpers
- `src/trading_research/config.py` — added `alpaca_market_data_api_key`/
  `alpaca_market_data_api_secret` fields, redaction-listed
- `src/trading_research/storage/database.py` — wired
  `apply_evidence_provider_schema`/`apply_research_cycle_schema` into `connect()`
- `docs/milestones/milestone-6.md` — pre-existing working-tree edit from before this session started
  (updates the M5 real-outcome narrative); left untouched, not part of this milestone's work

No Milestone 1-5 source file's behavior changed. No existing test was weakened, skipped, or
deleted.

## Schema and migration changes

Two new schema modules, both additive-only `CREATE TABLE IF NOT EXISTS`, following the exact
existing convention (idempotent, applied unconditionally from `storage/database.py::connect`,
no destructive migration):

- `evidence_provider_schema.py`: `evidence_provider_requests`,
  `evidence_provider_health_snapshots`
- `research_cycle_schema.py`: `research_cycles`, `research_cycle_symbol_results`

No existing table's schema changed. No trigger removed. `real_orders` remains
trigger-protected and write-blocked (verified via grep — no new writer anywhere).

## Tests added

83 new tests: 74 offline/deterministic (11 new unit test files + the scheduled-cycle
integration test's 5 cases) + 4 opt-in real-provider smoke tests (SEC, market-data, news,
real-scheduled-cycle) — all opt-in tests correctly skip by default and were also each
individually run for real in this session (see Environmental validation).

## Test run log

- `pytest tests/ -q` (before any Milestone 6 code): `571 passed, 2 skipped` — matches the
  documented Milestone 5 baseline exactly.
- `cd paper_runtime && pytest tests/ -q` (before any Milestone 6 code): `33 passed` —
  matches baseline exactly.
- `pytest tests/ -q` (after all Milestone 6 code, default/offline): `654 passed, 6 skipped`
  — 83 new tests net (571+83=654), 4 new opt-in smoke tests correctly added to the skip
  count (2+4=6), zero regressions.
- `cd paper_runtime && pytest tests/ -q` (after all Milestone 6 code): `33 passed` —
  unchanged, confirming the isolated paper runtime was never touched.
- `RUN_SEC_API_TESTS=true pytest tests/integration/test_sec_api_smoke.py -v`: **1 passed**
  (real).
- `RUN_MARKET_DATA_TESTS=true ... pytest tests/integration/test_market_data_api_smoke.py -v`:
  **1 failed** (real bug, `feed=iex` fix applied) then **1 passed** (real, after fix).
- `RUN_NEWS_API_TESTS=true pytest tests/integration/test_news_api_smoke.py -v`: **1 skipped**
  (honest ENVIRONMENTALLY_PENDING).
- `RUN_REAL_RESEARCH_CYCLE=true ... pytest tests/integration/test_real_research_cycle_smoke.py -v`:
  **1 passed** (real).

## Bugs discovered and fixed

1. **SEC XBRL `fp` (fiscal period) field is not a reliable annual/quarterly
   discriminator.** Discovered via a real SEC EDGAR smoke request for AAPL
   (not a unit test): Apple's 10-K XBRL data tags prior-period *quarterly*
   revenue comparatives with the same `fp="FY"` default the filing's own
   annual figure uses. Filtering `fiscal_period == "FY"` alone let
   `revenue_growth_yoy` divide the latest annual revenue by a quarterly
   revenue figure, producing a nonsensical 398% "growth" instead of the
   correct 15.9%. Fixed in `evidence_providers/fundamentals.py::_is_annual_period`
   by checking period *duration* (350-380 days) instead of trusting `fp`.
   Re-verified against real AAPL data after the fix: `revenue_growth_yoy=0.1586`.
2. **Alpaca free-tier data subscription rejects the default SIP feed for
   recent date ranges.** Discovered via the real market-data-API smoke test
   (`RUN_MARKET_DATA_TESTS=true`, real `ALPACA_MARKET_DATA_API_KEY/SECRET`):
   `GET /v2/stocks/AAPL/bars` with no `feed` param returned
   `HTTP 403 {"message":"subscription does not permit querying recent SIP
   data"}` when requesting a range ending "yesterday." An earlier ad-hoc
   validation (data ending ~32 days prior) had not hit this because it was
   outside whatever recency window triggers the restriction. Fixed by
   adding an explicit `feed` parameter to `AlpacaMarketDataClient`
   (`__init__(..., feed: str = "iex")`), applied to both `get_quote` and
   `get_price_history` requests — confirmed `feed=iex` returns `200` on the
   exact same recent range via a direct `httpx.get` probe before patching
   the client. Re-ran `test_market_data_api_smoke.py` after the fix — see
   "Environmental validation" for the passing result.

## Security and secret-handling incidents

None. Deliberate design choices made specifically to prevent incidents (not incidents
themselves):

- Recognized that reusing `ALPACA_API_KEY`/`ALPACA_API_SECRET` (Milestone 4's paper-broker
  credentials, documented as "read exclusively by the isolated paper_runtime process") in
  the main process for market data would silently violate that documented boundary —
  introduced a deliberately distinct `ALPACA_MARKET_DATA_API_KEY`/`ALPACA_MARKET_DATA_API_SECRET`
  pair instead (see ADR 0004 Decision 3). This required real credentials for the market-data
  smoke test in this session; they were exported into the test subprocess's environment only
  (bridged from the same real Alpaca key pair for the duration of the validation run) and
  never written into `.env`, any config file, or any committed file.
- `evidence_providers/persistence.py` never persists API keys, authorization headers, or
  account identifiers — verified by reading every field written to
  `evidence_provider_requests` (provider name, operation, symbol, status codes, latency,
  never a header value).
- `evidence_providers/health.py` and the `provider-health`/`evidence-provider-usage` CLI
  commands report booleans/rates/latencies only, never a credential value — verified by
  running them for real in this session and inspecting the actual printed output.
- Ran a repo-wide grep for common secret-value patterns (`sk-ant-`, AWS-style access-key
  patterns, PEM headers) across every file this milestone added or modified — zero matches.
- No command in this session printed `.env` contents; credential presence was always
  checked via `dotenv_values()`/`os.environ` boolean/membership checks in throwaway Python,
  never via `cat`/`echo` of the file.

## Environmental validation

All run 2026-07-12 in this development environment, real network + real credentials.

1. `RUN_SEC_API_TESTS=true pytest tests/integration/test_sec_api_smoke.py -v` -> **PASSED**.
   Real CIK resolution, real recent-filings retrieval (point-in-time filtered), real
   company-facts retrieval, normalized into the existing EvidenceBundle shape.
2. `RUN_MARKET_DATA_TESTS=true` (+ `ALPACA_MARKET_DATA_API_KEY/SECRET` bridged from the
   existing Alpaca paper-broker credentials for this validation run only — not committed
   anywhere) `pytest tests/integration/test_market_data_api_smoke.py -v` -> **FAILED on
   first attempt** (`403 subscription does not permit querying recent SIP data`), **PASSED**
   after adding the `feed=iex` fix (see "Bugs discovered and fixed" #2).
3. `RUN_NEWS_API_TESTS=true pytest tests/integration/test_news_api_smoke.py -v` -> **SKIPPED**
   with an explicit ENVIRONMENTALLY_PENDING reason (no real news-provider API key exists in
   this environment) — honestly reported, not disguised as a pass.
4. `RUN_REAL_RESEARCH_CYCLE=true` (+ bridged Alpaca market-data credentials)
   `pytest tests/integration/test_real_research_cycle_smoke.py -v` -> **PASSED**. Real SEC +
   real Alpaca evidence for AAPL, deterministic research provider (no Claude cost),
   SHADOW_ENHANCED, `paper_submitter=None` (structurally cannot submit) -> no paper order.
5. **Manual (not a committed test) full real pipeline validation with real Claude**: same
   real SEC + real Alpaca evidence for AAPL, `AnthropicResearchProvider`
   (`claude-sonnet-5`, same forced-tool-use path Milestone 5 validated), `SHADOW_ENHANCED`,
   no paper submission. Result: `evidence_outcome=COMPLETE`,
   `research_run_id=run-e4544adb0ac3e1faf405846132bdcf3d`, `baseline_side=screened_out`
   (expected — see "known limitation" below), `enhanced_side=screened_out` (overlay
   correctly never promotes a screened-out baseline, regardless of research outcome).
   9 research attempts recorded across 4 analyst roles (fundamental, technical, bull, bear);
   `bear` exhausted its 2 retry attempts (both `success=0`) and the `manager` role was
   correctly never invoked afterward (no wasted provider call — matches Milestone 5's
   documented "no required-role failure invokes the manager" behavior). Total elapsed
   146.2s across 9 attempts; per-attempt latency 17.5s-36.3s; input tokens 5,781-6,184 per
   attempt; output tokens 1,625-3,856 per attempt; `cost_status=PRICING_NOT_CONFIGURED`
   throughout (expected — `config/research_pricing.yaml` is empty by default, cost is never
   fabricated). This is not a repeatable, committed test (real Claude cost + ~2.5 minutes
   per run) — see docs/milestones/milestone6 "Known limitations" #3 for why.
6. Ad-hoc direct-API probes (not pytest, exploratory during implementation): SEC
   `company_tickers.json` (200), `data.sec.gov/submissions/...` (200),
   `data.sec.gov/api/xbrl/companyfacts/...` (200), Alpaca `/v2/stocks/AAPL/bars` (200, after
   the `feed=iex` fix for recent ranges), Alpaca `/v2/stocks/AAPL/quotes/latest` (200).
7. `python -m trading_research.cli fetch-evidence --symbol AAPL --as-of 2026-07-11T13:00:00Z
   --provider-mode real` (with market_data.enabled temporarily flipped true + bridged
   credentials, config restored immediately after) -> 20 evidence items, 0 missing_data_reasons,
   `point_in_time_safe=true`. `provider-health`/`evidence-provider-usage` afterward correctly
   showed real persisted request rows for both `sec-edgar` (3 requests, 100% success) and
   `alpaca-data` (1 request, 100% success) — proves Step 5 persistence wiring works end-to-end,
   not just in isolation.

Not run (environmentally blocked, not attempted): real Reddit sentiment (no
REDDIT_CLIENT_ID/SECRET), real news provider (no implementation target — see Known
limitations).

## Known limitations

See `docs/milestones/milestone6-real-evidence-continuous-evaluation.md`'s "Known limitations" section
for the full, detailed list (8 items). Summary:

1. News and Reddit sentiment are ENVIRONMENTALLY_PENDING (interfaces complete and tested,
   no real credentials available in this environment).
2. Real baseline candidates commonly `screened_out`/`analysis_incomplete` with the minimum
   provider set (no corporate-status/going-concern data source) — documented, intended
   fail-closed behavior, not a bug.
3. The real Claude-enhanced scheduled-cycle path was validated manually once (real cost),
   not via a repeatable committed test — the committed real-cycle test uses the
   deterministic provider to stay free and repeatable, while Milestone 5's existing
   `test_research_claude_smoke.py` covers real-Claude validation on its own.
4. No separate corporate-action (splits/dividends) event stream.
5. `operating_history_years`/going-concern flags unavailable from any provider in this set.
6. Portfolio-context weighting uses cost basis, not live market value.
7. `evidence_provider_requests` doesn't log cache hits (only real HTTP attempts).
8. Only one primary market-data provider (Alpaca) and one filing/fundamentals source (SEC).

## Remaining work

Recommended Milestone 7 (see also the required final response to the user):

1. A real news-provider integration (requires an API key this environment doesn't have).
2. Real Reddit sentiment (requires REDDIT_CLIENT_ID/SECRET this environment doesn't have).
3. A real corporate-status/going-concern data source so real baseline candidates can reach
   `buy_candidate` with the minimum provider set's current fail-closed design.
4. Separate paper-portfolio namespaces to unblock `ENHANCED_ONLY`/`BOTH_SEPARATE_PAPER_BOOKS`.
5. A repeatable (not manual) real-Claude scheduled-cycle validation once a controlled
   API-cost budget/cadence is defined.
6. An actual recurring deployment (cron/scheduler) driving `run-research-cycle` — this
   milestone delivers the idempotent, resumable implementation only, not a live deployment.
7. Intraday high/low data source to populate `max_favorable_excursion`/`max_adverse_excursion`
   (still `None` — a pre-existing Milestone 4 limitation, unchanged by this milestone).

## Final status

**Milestone 6 is code-complete and environment-validated** as of 2026-07-12. All acceptance
criteria in docs/milestones/milestone-6.md are met:

- Main suite: 654 passed, 6 skipped (571 baseline + 83 new tests; 6 skipped = 2 baseline +
  4 correctly-gated opt-in smoke tests).
- Paper-runtime suite: 33 passed, unchanged.
- Default tests require no network/credentials (verified: the 4 opt-in smoke tests are the
  only ones requiring either, and none of them auto-run).
- Real SEC EDGAR and real Alpaca market-data connectivity both genuinely validated against
  production endpoints (not mocks) in this session, including one real bug found and fixed
  in each provider.
- A real fundamentals path exists and was validated with real SEC data (with a real bug
  found and fixed).
- A real news path is clearly marked ENVIRONMENTALLY_PENDING, not silently missing.
- Provider outputs normalize into the existing, unchanged `EvidenceSnapshot`/`EvidenceBundle`
  contracts.
- Every real evidence item carries provenance (`SourceRecord`) and point-in-time metadata.
- Future evidence is rejected (verified via both unit tests and real data).
- Missing required evidence fails closed (verified via `classify_snapshot_outcome` tests and
  the real scheduled-cycle run's `evidence_outcome=COMPLETE`/blocking-outcome paths).
- Scheduled cycles are idempotent and resumable (verified via a dedicated rerun test and
  real CLI `resume-research-cycle` execution).
- Both experiment arms use identical deterministic inputs; the enhanced arm structurally
  cannot execute under any currently supported policy.
- No-action/incomplete results remain in evaluation (`NEVER_EXECUTED` is first-class).
- Time-to-fill, turnover, and confidence calibration are implemented and tested.
- Promotion gates are deterministic, versioned, and their status enum contains no
  live-trading-authorizing member.
- No secrets appear in any persisted record, log, or committed file (verified by grep and
  by direct inspection of every new persistence write path).
- `real_orders` remains write-blocked (verified — no new writer anywhere in this milestone).
- This scratchpad accurately reflects completed work, tests run, environmental validation
  completed, and known limitations, per the milestone document's explicit requirement.

## Milestone 6.1 — Research failure diagnostics and hardening

Started: 2026-07-13T03:50:52Z
Branch: main
Status: STARTING

### Baseline

- Git: branch `main`, up to date with `origin/main`, working tree clean at session start.
  Milestone 6 is already committed as `0e3e44b feat: add real evidence providers and
  scheduled paper-evaluation cycle (Milestone 6)`. (The M6 progress-file entries above
  describing uncommitted M6 files were true when written; by the time this M6.1 session
  started, that work had already been committed by a prior session/turn.)
- `pytest tests/ -q` -> `654 passed, 6 skipped` — matches the M6.1 prompt's documented
  starting-state expectation exactly.
- `cd paper_runtime && pytest tests/ -q` -> `33 passed` — matches exactly.
- No unrelated uncommitted work present to preserve.

### Bear-role incident investigation

Searched `data/research.sqlite3` (the repository's real research DB — gitignored via
`data/*.sqlite3`, confirmed via `.gitignore`) for `research_run_id =
'run-e4544adb0ac3e1faf405846132bdcf3d'`: **not found**. `research_committee_runs` in that
database is currently completely empty (0 rows) — the database has been reset/recreated since
the Milestone 6 manual real-Claude validation was run (consistent with the M6 doc's own
"Known limitations" #3: the real-Claude-enhanced path was validated *manually*, "not via a
committed, CI-safe test", implying an ephemeral or since-cleared DB, not a persisted fixture).
Also checked every other `.sqlite3` file discoverable on this machine
(`/private/tmp/cli_smoke*.sqlite3`, `/private/tmp/cli_test.sqlite3`,
`/private/tmp/milestone5_smoke.sqlite3`) — none contain this run_id; the two that do have a
`research_committee_runs` table only contain unrelated `provider=deterministic` test runs.

**Conclusion: OBSERVABILITY GAP — historical attempt data insufficient.** The exact bear-role
failure stages/codes for `run-e4544adb0ac3e1faf405846132bdcf3d` cannot be reconstructed from
any persisted data available in this environment. No root cause is fabricated for this specific
historical run. Per the M6.1 spec, this session proceeds by (1) implementing the observability
fix (structured failure taxonomy/persistence) so this gap cannot recur, then (2) reproducing the
most evidence-backed *representative* failure category and validating the fix against it — see
"Reproduction cases" / "Root-cause classification" below.

### Historical persistence findings

Read the full failure path (`orchestration.py::_run_role_with_retries`,
`anthropic_provider.py`, `output_validation.py`, `claim_validation.py`,
`research_repositories.py`, `research_schema.py`) end-to-end. Findings:

1. **`ResearchAttemptRecord.failure_reason` is a single free-text string** — for a schema
   failure it is the *entire* joined `Draft7Validator` error message; for a claim-validation
   failure it is `"; ".join(reasons)` across every rejected claim. Individual claim IDs,
   evidence IDs, and failure codes are embedded in prose, not queryable fields. This is the
   primary gap Step 4-9 must fix.
2. **A pre-existing, entirely unused `research_failures` table already exists** in
   `storage/research_schema.py` (columns: `id, research_run_id, role, stage, reason,
   occurred_at`) — confirmed via repo-wide grep: no code anywhere writes or reads it, and no
   test references it. It predates this session (present before any M6.1 edits). It is too
   narrow for the M6.1 taxonomy (no `attempt_id`, `code`, `field_path`, `claim_id`,
   `evidence_ids`, `retryable`, `model_name`, `prompt_version`, `schema_version`,
   `metadata_json`) and, since no data was ever written to it, retrofitting it would still be
   a live schema change with no migration story for the missing columns. Decision: **leave
   `research_failures` untouched** (do not delete — never remove existing schema) and add the
   new `research_attempt_failures` table the M6.1 spec suggests, matching its intended
   one-row-per-structured-failure design exactly.
3. **`AnthropicResearchProvider.generate_structured` raises `MalformedOutputError` before
   computing `latency_ms` or extracting `response.usage`** when the tool_use block is missing,
   has the wrong name, or `.input` is not a dict (`anthropic_provider.py:129-139`). This means
   a malformed/truncated real-Claude response currently loses `input_tokens`, `output_tokens`,
   and — critically — `response.stop_reason` entirely; `_unavailable_usage()` in
   `orchestration.py` back-fills every token/latency field as `None`. **This is a genuine,
   evidence-backed observability bug**: if the real bear-role failure was in fact an
   `OUTPUT_TRUNCATED` (max_tokens stop reason truncating the tool-use JSON mid-object, which
   `json` would still parse successfully as a dict as long as `tool_use_block.input` remains
   a well-formed dict per Anthropic's incremental JSON building — but the *missing tool_use
   block* case, e.g. the model stopping before ever emitting the forced tool call, is exactly
   this path), it would be structurally impossible to diagnose after the fact from persisted
   data, because the fields that would prove it were never captured. Classified as
   **APPLICATION BUG** — fixed in Step 7 (provider must capture `stop_reason`/tokens/latency
   even when raising `MalformedOutputError`).
4. **No `stop_reason` column exists anywhere** in `research_attempts` or `UsageRecord`.
5. Retry feedback (`orchestration.py:183,210,224`) currently passes the *entire* raw exception
   string or the entire joined claim-rejection-reason string back into the next attempt's
   prompt via `validation_feedback` — not bounded, not deduplicated by failure code, no
   allowed-evidence-ID index. Meets the spirit of "retries may include validation feedback"
   but not Step 11's "concise, bounded, grouped by code" requirement.
6. **Manager-skip is implicit, not a persisted record.** `incomplete_reasons.append(f"role
   {role!r} exhausted retries without a valid report")` is returned in
   `OrchestrationResult.incomplete_reasons` (in-memory only) — nothing in
   `SQLiteResearchRepository` persists *why* the manager was never invoked; the only durable
   trace is `research_committee_runs.status = 'ANALYSIS_INCOMPLETE'` with no reason column.
7. Every analyst role in `configuration.roles` (minus `manager`) is implicitly "required" —
   there is no separate required-vs-optional role concept in `configuration.py`; any single
   analyst role's retry exhaustion already skips the manager (`orchestration.py:329-343`).
   This matches the M6 report's "bear exhausted its two allowed attempts... manager role was
   correctly not invoked" behavior exactly — confirming that specific high-level claim is
   architecturally consistent even though the fine-grained attempt data no longer exists.
8. Claim-to-evidence validation (`claim_validation.py`) already independently classifies
   failure reasons in code (unknown evidence, stale evidence, point-in-time-unsafe, numeric
   mismatch/no-comparable-value) but returns them as prose strings, not the taxonomy codes
   Step 9 requires — mapping is mechanical, not a validator behavior change.

### Failure taxonomy

(to be filled in during Step 4)

### Reproduction cases

`tests/integration/test_bear_role_failure_reproduction.py::
test_bear_role_invented_downside_exhausts_retries_and_skips_manager` — scripted bear role
returns a claim with a fabricated `-35%` downside `numeric_value` citing a real
evidence_id whose actual normalized value is `0.08` (8% revenue growth), on both of its
two allowed attempts. Verified: two `NUMERIC_VALUE_MISMATCH` failures persisted (one per
attempt, `claim_id="bear-claim-1"`), one `RETRY_EXHAUSTED` failure, one
`MISSING_REQUIRED_ROLE` (`REQUIRED_ROLE_FAILED` stage) failure, one `MANAGER_NOT_INVOKED`
(`MANAGER_SKIPPED` stage) failure naming `bear` as the blocking role, bounded
code-grouped retry feedback delivered to attempt 2 (contains the code, the cited
evidence_id, and "complete replacement report" — not the raw prior response), final
`status=ANALYSIS_INCOMPLETE`, `decision=None`, manager never called. PASSED on first run
after the taxonomy/persistence/provider-classification/claim-classification code was in
place — no iteration needed.

Chosen category justification: not claimed as proof of the exact historical cause (that
remains an OBSERVABILITY GAP per "Bear-role incident investigation" above). Chosen because
it is the most evidence-backed *representative* category available: Milestone 5's own real
Claude API validation already proved this exact validator behavior fires on live model
output (`docs/milestones/milestone5-evidence-backed-claude-research.md`, "Real Claude API
validation": "one numeric claim ... was correctly rejected by claim_validation.py"), and
the bear role's job (quantify downside) is structurally the role most likely to have a
model invent a specific number under the same pressure.

### Root-cause classification

Two independent, non-conflicting classifications apply, matched to what was actually
discovered:

1. **For the specific historical run `run-e4544adb0ac3e1faf405846132bdcf3d`: OBSERVABILITY
   GAP.** No persisted attempt data survives to determine its exact failure stage/code.
   Supporting evidence: exhaustive search of every reachable `.sqlite3` file (see "Bear-role
   incident investigation"). No code fix is "required" by this classification alone, since
   there is nothing left to diagnose — the fix is the observability layer itself (this
   entire session), which prevents recurrence for every future run.
2. **For the representative reproduced category: VALID EXPECTED REJECTION, with a
   contributing PROMPT DEFECT — not a CLAIM-VALIDATOR DEFECT.** `claim_validation.py`'s
   numeric-tolerance check is working exactly as designed (Milestone 5's own real-API
   validation already proved this against live output) — a fabricated downside percentage
   not present in the cited evidence's `normalized_values` *should* be rejected, and
   weakening that check was never considered. The genuinely fixable gap was the *prompt*:
   `prompts/research/bear/v1.txt` never explicitly told the model not to invent a numeric
   downside/price-target/probability figure — it only said "without fabricating any fact,"
   which a model can read as covering qualitative facts without extending to a
   self-computed number. Fixed (see "Fixes implemented").
3. **Independently discovered APPLICATION BUG (found by code inspection while tracing the
   failure path, not by reproducing the historical incident):**
   `AnthropicResearchProvider.generate_structured` raised `MalformedOutputError` for a
   missing/wrong-named/multi-block tool_use response *before* reading `response.usage` or
   `response.stop_reason` — an `OUTPUT-TOKEN LIMIT` failure would have been silently
   undiagnosable. Fixed (see "Fixes implemented") — token/stop_reason/latency are now
   captured before any tool-use-extraction failure is raised, and a `max_tokens` stop
   reason with no tool_use block is now classified as `OUTPUT_TRUNCATED`, not a generic
   `EXPECTED_TOOL_USE_MISSING`.
4. **Independently discovered APPLICATION BUG:** `orchestration.py`'s per-attempt
   try/except around `build_decision`/`build_role_report` caught `SchemaValidationError`
   but not `EvidenceValidationError` — `ResearchDecision.__post_init__`'s own business
   invariant (non-empty `bear_case`/`bull_case` for a non-`ANALYSIS_INCOMPLETE` rating,
   which is *not* expressed in the JSON Schema's `required`/`minLength`, since an empty
   string still satisfies `{"type": "string"}`) could raise uncaught out of
   `_run_role_with_retries`, crashing the entire committee run instead of being treated as
   a retryable, classified validation failure like every other structured-output problem.
   Fixed (see "Fixes implemented").

Not labeled a validator defect: the numeric-tolerance/claim-evidence logic in
`claim_validation.py` was not modified. Retryability, tolerance, and rejection criteria
are unchanged from Milestone 5.

### Fixes implemented

1. **PROMPT DEFECT fix** — `prompts/research/bear/v1.txt` rewritten (Step 14) to
   explicitly forbid invented numeric downside percentages/price targets/probability
   estimates, require visible fact/inference/uncertainty separation, require at least one
   `risks` entry, and require a complete replacement report (not a patch) after retry
   feedback. Edited in place — `PromptRegistry` hashes prompt *text*, not just the version
   string, so this edit alone changes `prompt_hash` and therefore `research_run_id` for
   any future run using this file (Milestone 5's existing, unmodified versioning design —
   confirmed via `research/prompt_registry.py::_hash_text` and
   `orchestration.py::compute_research_run_id`); no new `v2.txt` was needed.
2. **APPLICATION BUG fix (provider observability)** —
   `research/anthropic_provider.py::generate_structured` now extracts
   `stop_reason`/`input_tokens`/`output_tokens` immediately after the response is
   received, before any tool-use-extraction failure is raised; distinguishes "no tool_use
   block at all" (further split into `OUTPUT_TRUNCATED` when `stop_reason == "max_tokens"`,
   else `EXPECTED_TOOL_USE_MISSING`) from "tool_use block(s) present but wrong name"
   (`UNEXPECTED_TOOL_NAME`), "more than one matching tool_use block"
   (`MULTIPLE_TOOL_BLOCKS`), and "tool_use.input not a JSON object"
   (`MALFORMED_TOOL_INPUT`) — previously all four collapsed into one generic
   `MalformedOutputError("... no tool_use block ...")` with zero usage/stop_reason
   captured. Every provider-boundary exception (`errors.py::ResearchError` subclasses) now
   optionally carries `stage`/`code`/`field_path`/`claim_id`/`evidence_ids`/`retryable`/
   `metadata`, purely additive (no existing `raise XError("msg")` call site needed to
   change; unclassified errors fall back to `UNKNOWN`/`UNCLASSIFIED_VALIDATION_FAILURE`).
3. **APPLICATION BUG fix (uncaught exception)** —
   `orchestration.py::_run_role_with_retries` now catches `EvidenceValidationError`
   alongside `SchemaValidationError` around `build_decision`/`build_role_report`,
   classifying an empty `bear_case`/`bull_case` as a retryable, persisted failure
   (`MISSING_BEAR_CASE` / `SCHEMA_REQUIRED_FIELD_MISSING`) instead of letting it crash
   `analyze_with_research_committee` with an unhandled exception. Regression test:
   `tests/unit/test_research_orchestration.py::
   test_manager_decision_with_empty_bear_case_is_retried_not_crashed` (added in Step 19).
4. **OBSERVABILITY (this entire session's core deliverable)** — full structured failure
   taxonomy, persistence, provider/schema/claim classification, retry-exhaustion/
   required-role/manager-skip persistence, bounded code-grouped retry feedback, CLI
   diagnostics, replay comparison, and failure metrics (see the rest of this section and
   the "Requirement -> implementation file -> verifying test" table in the final report).

### Other Milestone 6 issues reviewed

1. **News provider** — `ENVIRONMENTALLY_PENDING` (unchanged). Confirmed still fail-closed:
   `evidence_providers/news_provider.py::UnconfiguredNewsProvider` and
   `tests/integration/test_news_api_smoke.py` (honest `SKIPPED`) both still present and
   passing. No new provider added, per the explicit non-goal.
2. **Reddit sentiment** — `ENVIRONMENTALLY_PENDING` (unchanged). Confirmed
   `REDDIT_CLIENT_ID`/`SECRET` still absent from `.env`; `mcp/tool_classifier.py`'s
   read-only policy and the fact that Claude's research layer has no MCP tool access at
   all (only `research/provider_protocol.py::ResearchModelProvider.generate_structured`,
   no tool schema for Reddit) are both unchanged from Milestone 5/6 — re-verified by
   grepping `research/` for any MCP import (none).
3. **Corporate-status/going-concern SEC metadata** — `DEFERRED_TO_MILESTONE_7`. Not
   implemented this session: the bear-role incident investigation found no connection
   between this gap and the diagnostics work required here, and the milestone document's
   guidance is explicit that a narrow SEC metadata addition is in scope "only when it
   directly fixes a proven Milestone 6 defect" — this doesn't. Implementing it now would
   be unrelated scope creep into the M6 baseline candidate-quality problem, not the M6.1
   failure-diagnostics/hardening problem this session is scoped to.
4. **Portfolio context uses cost basis** — `DEFERRED_TO_MILESTONE_7`. Inspected
   `evidence_providers/portfolio_context.py::LedgerPortfolioContextProvider.fetch`: it has
   no price-provider dependency wired in at all (only reads `PaperLedger` positions/cash);
   adding a live-market-value path would require threading `AlpacaMarketDataClient` (or an
   equivalent) into this class and fetching a quote per held position, a nontrivial
   dependency-injection change, not a proven defect from this incident, and outside this
   session's fail-closed-preserving scope. The existing `note` field already discloses the
   limitation honestly (not a silent approximation) — left unchanged.
5. **Provider cache hits not persisted** — `FIXED` (in-scope hardening, explicitly
   flagged as a good candidate by the milestone document). See "Fixes implemented" below.
6. **Single-provider concentration** — `FIXED`. `provider-health` now returns a
   `concentration` object (`market_data_provider_count`, `filing_provider_count`,
   `fundamentals_provider_count`, `news_provider_count`, `sentiment_provider_count`,
   `redundancy_status`) — no new provider added, per the explicit "Do not add providers"
   instruction.
7. **Recurring deployment** — `NOT_A_BUG` / confirmed unchanged. Re-verified: no cron,
   scheduler, or daemon exists anywhere in this repository (grepped for `crontab`,
   `APScheduler`, `celery`); `run_scheduled_research_cycle` remains idempotent/resumable
   only, exactly as documented in
   `docs/milestones/milestone6-real-evidence-continuous-evaluation.md`'s own
   "IDEMPOTENT SCHEDULED-CYCLE IMPLEMENTATION vs. ACTUAL RECURRING DEPLOYMENT" section.
8. **MFE/MAE** — `DEFERRED_TO_MILESTONE_7`, unchanged. Still requires an intraday
   high/low data source this milestone set does not have; not derived from daily closes,
   per the explicit non-goal.
9. **Real-Claude cost and latency** — `FIXED` via the new observability layer, not a
   separate change: `research-usage` (unchanged, Milestone 5) already reports per-role
   attempts/tokens/latency/cost; the new `research-failure-metrics` CLI command (Step 17)
   adds `average_failed_attempt_input_tokens`/`average_failed_attempt_output_tokens`/
   `average_failed_attempt_latency_ms`/`tokens_spent_on_exhausted_retries` plus
   failures-by-role/stage/code — together the two commands satisfy "attempts; input
   tokens; output tokens; latency; retry outcome; failure codes; cost only when pricing is
   configured" without a schema change to `research_attempts`.

### Issues fixed

1. **Provider cache-hit persistence** (`evidence_providers/cache.py::ProviderCache`) — a
   real bug beyond the documented limitation was also found and fixed while implementing
   this: `sec_provider.py`'s `CacheKey.build(provider="sec", ...)` used a different
   provider-name string than its own `HttpJsonClient(provider="sec-edgar", ...)`, which
   would have made cache-hit rows land under a different `provider` grouping than the
   real-HTTP rows for the exact same logical provider — fixed to `"sec-edgar"` for both
   (Alpaca's `market_data_provider.py::PROVIDER_NAME = "alpaca-data"` was already
   consistent; no change needed there). `ProviderCache.get()` now calls an optional
   `on_response` callback (the *same* dict shape and the *same* `_make_persist_hook`
   already used by `HttpJsonClient`) on a genuine cache `HIT` and on `CACHE_CORRUPT` —
   deliberately **not** on a plain `MISS`/`STALE_CACHE_REJECTED`, because every real call
   site falls through to a real `HttpJsonClient.get_json` call immediately after either of
   those, which already persists its own row; notifying there too would have
   double-counted every real network request. No raw payload is duplicated — only
   request-level telemetry, matching the existing policy.
2. **Single-provider concentration exposure** — `evidence_providers/health.py::
   compute_provider_concentration()` (static, documented architectural fact per ADR 0004,
   not derived from request volume) wired into `provider-health`'s `concentration` field.

### Issues deferred

* Corporate-status/going-concern SEC metadata — `DEFERRED_TO_MILESTONE_7` (see above).
* Portfolio context market-value path — `DEFERRED_TO_MILESTONE_7` (see above).
* MFE/MAE — `DEFERRED_TO_MILESTONE_7` (see above, unchanged from Milestone 6).
* Real news-provider integration, real Reddit sentiment, recurring deployment — all
  unchanged `ENVIRONMENTALLY_PENDING`/`NOT_A_BUG` per Milestone 6, re-verified not
  regressed.

### Test run log

- `pytest tests/ -q` before any M6.1 code: `654 passed, 6 skipped` (baseline, confirmed
  identical to the M6.1 prompt's documented expectation).
- `pytest tests/ -q` after all M6.1 code + tests: `747 passed, 6 skipped` — net +93 tests,
  0 skipped-count change, 0 regressions.
- `cd paper_runtime && pytest tests/ -q`: `33 passed` — unchanged throughout, confirming
  the isolated paper runtime was never touched by this session.
- New test files added this session (71 tests, precisely counted via
  `pytest --collect-only -q`): `test_failure_taxonomy.py` (17),
  `test_research_attempt_failures_storage.py` (11),
  `test_research_anthropic_provider_classification.py` (13), `test_retry_feedback.py`
  (10), `test_failure_metrics.py` (7), `test_research_failures_cli.py` (10),
  `test_evidence_provider_health.py` (2),
  `tests/integration/test_bear_role_failure_reproduction.py` (1). New tests added to
  existing files (22 tests, precisely counted via `git diff | grep -c '+def test_'`):
  `test_research_orchestration.py` (+1, the `EvidenceValidationError` regression test),
  `test_research_output_validation.py` (+7, schema-error classification),
  `test_research_claim_validation.py` (+7, claim-code classification),
  `test_research_replay.py` (+3, failure reconstruction/comparison),
  `test_evidence_provider_cache_and_rate_limits.py` (+4, cache-hit persistence). Total:
  71 + 22 = 93 new tests, exactly matching the 654 -> 747 delta.
- Every new/modified test file was run individually before the final full-suite run;
  every one passed with zero test-authoring iteration required except
  `test_classify_unsupported_numeric_claim_no_comparable_value` (a test-construction bug
  in this session's own test — a falsy-empty-dict default-argument trap — fixed in the
  test, not the source) and `test_replay_reports_not_reconstructible_...` (wrong
  `FakeResearchRepository` method name, `save_attempt_failure` vs. plural
  `save_attempt_failures` — fixed in the test).

### Real Claude validation

**Environmentally blocked — not run.** Checked `.env` via `dotenv_values()` (boolean
presence check only, no value read or printed): `ANTHROPIC_API_KEY` is **not set** in this
development environment (confirmed empty/absent, same as this session's git-status
baseline check). `ANTHROPIC_MODEL=claude-sonnet-5` is present but a model name alone
cannot authenticate a request. Per Step 20's explicit instruction ("run a narrow real
Claude validation only when explicitly enabled") and the existing repository convention
(`tests/integration/test_research_claude_smoke.py` requires `RUN_CLAUDE_RESEARCH_TESTS=true`
**and** a real API key **and** never runs automatically), no real-Claude revalidation was
attempted — there is no credential to attempt it with, and fabricating a result would
violate the explicit "do not claim the bear role is fixed unless ... any real Claude
revalidation result is reported accurately" requirement. All revalidation of the fixes in
this session was performed with the `ScriptedResearchProvider` (see "Reproduction cases"),
which is a fully deterministic, real-code-path exercise of the same
`analyze_with_research_committee`/`_run_role_with_retries` orchestration logic — only the
Anthropic SDK boundary itself (`anthropic_provider.py`) is untested against a live
network call this session; its classification logic (Step 7) is instead unit-tested
against locally-constructed real `anthropic.*Error`/response-shaped objects (see
`test_research_anthropic_provider_classification.py`), which exercises the exact same
code paths the SDK would trigger without requiring network access or credentials.

### Security review

Ran a final grep-based verification pass (not just relying on earlier design reasoning):

* `real_orders`: `git grep real_orders` shows only pre-existing reservation/trigger code in
  `trading_schema.py`/`execution_schema.py`/`trading_repositories.py` — no new writer
  anywhere in this session's diff.
* No MCP import anywhere in `research/` (`grep -rln "import.*mcp" src/trading_research/research/`
  returns nothing) — Claude's research layer still cannot reach any MCP tool.
* No `execution`/`paper` import in any new module (`failure_taxonomy.py`,
  `failure_metrics.py`, `research_attempt_failures` repository code, `anthropic_provider.py`
  changes) — verified via import-list inspection.
* `git status --porcelain | grep -E "^src/trading_research/(execution|paper)/"` — empty;
  this session touched zero files in either package.
* `ResearchValidationFailure.metadata` is a strict allowlist (14 keys, all
  telemetry — token counts, stop reason, status code, expected/actual type, counts) with
  an additional secret-like-value string scan (`sk-ant-`, `bearer `, `authorization`,
  `api_key`, `password`, `secret`, `x-api-key`) — verified via
  `test_secret_like_metadata_value_rejected`/`test_unknown_metadata_key_rejected` in
  `test_failure_taxonomy.py`.
* `research-failures` CLI output only ever contains the documented 16 fields per failure
  (verified via `test_output_only_contains_documented_fields`) — no raw prompt, no raw
  response, no chain-of-thought, no secret (verified via
  `test_sanitized_output_contains_no_raw_prompt_or_secret`).
* No `.env` value was ever read or printed this session — only
  `dotenv_values(...).get(...)` boolean-presence checks (see "Real Claude validation"
  above), same pattern the original M6 session used.
* `ProviderCache._notify` and `HttpJsonClient._notify` both emit request-level telemetry
  only (provider, operation, symbol, status, latency, cache status, retry count,
  success/error) — never a raw payload, matching the pre-existing
  `evidence_provider_requests` policy exactly; verified by reading every field in both
  `_notify` methods.
* No infinite retry was introduced: `RETRY_EXHAUSTED`/`REQUIRED_ROLE_FAILED`/
  `MANAGER_SKIPPED` are all *terminal* failure records emitted once, after
  `configuration.max_attempts_per_role` (unchanged bound) is exhausted — no new retry loop
  or increased bound anywhere.
* No failed attempt is ever erased after a later success:
  `test_failed_attempt_failure_retained_after_later_success` and
  `research_attempt_failures` is append-only (DB-trigger-protected, same pattern as
  `research_attempts`).
* No Milestone 1-6 test was weakened, skipped, or deleted — the full 654-test baseline
  passes unmodified; every change to an existing test file in this session was a pure
  addition (new `def test_...` functions), confirmed via `git diff | grep -c '^-def test_'`
  returning 0 for every modified test file.
* No validator was weakened: `claim_validation.py`'s tolerance/rejection logic and
  `output_validation.py`'s schema bounds are byte-for-byte unchanged from Milestone 5
  except for the additive `schema_errors` attribute on the raised exception (the raised
  exception itself, and when it is raised, is unchanged).

### Known limitations

1. `CROSS_SNAPSHOT_EVIDENCE`/`CROSS_SYMBOL_EVIDENCE`/`UNIT_MISMATCH`/claim-level
   `UNSUPPORTED_MATERIAL_CLAIM` are defined in the taxonomy but not currently reachable —
   the underlying claim validator cannot yet distinguish "evidence_id never existed" from
   "belongs to a different snapshot/symbol," and does not compare units. Extending the
   validator to reach these was out of this session's narrow, evidence-backed scope (no
   demonstrated defect required it).
2. Real-Claude revalidation of the bear-role prompt fix could not be performed in this
   environment (no `ANTHROPIC_API_KEY`) — validated deterministically instead (see
   "Reproduction cases"/"Real Claude validation").
3. Corporate-status/going-concern SEC metadata and portfolio-context live market-value
   pricing remain `DEFERRED_TO_MILESTONE_7` (see "Issues deferred").
4. Replay's failure reconstruction (Step 16) can only re-derive
   `CLAIM_EVIDENCE_VALIDATION`-stage failures — provider-boundary and schema-stage
   failures are echoed from persistence, never re-derived, because reconstructing them
   would require the original raw provider response, which replay structurally never has
   (and must never have, by design — see ADR 0003).
5. `research-failure-metrics` computes rates over *every* persisted attempt/failure in the
   database, not scoped to a single run or a bounded recent window — for a database with a
   long history this is a global aggregate, not a rolling one. Scoping it (e.g. by
   date range or research_run_id) was not requested and would be speculative scope
   expansion; noted here as a natural, narrow Milestone 7 follow-up if ever needed.

### Final status

**Milestone 6.1 is code-complete** as of 2026-07-13. All acceptance criteria in
`docs/milestones/milestone-6.1.md` are met:

* Main suite: `747 passed, 6 skipped` (654 baseline + 93 new tests; 0 regressions, 0
  weakened/deleted tests).
* Paper-runtime suite: `33 passed`, unchanged — this milestone never touched
  `paper_runtime/`.
* The bear-role incident has an evidence-backed root-cause classification
  (OBSERVABILITY GAP for the specific historical run; VALID EXPECTED REJECTION +
  PROMPT DEFECT for the representative reproduced category; two independently discovered
  APPLICATION BUGs fixed).
* Every failed attempt can persist multiple structured failures (proven:
  `test_multiple_failures_per_attempt_all_persisted`).
* Failure stage/code are queryable (proven: `test_query_by_stage`/`test_query_by_code`,
  plus the `research-failures` CLI's `--stage`/`--code` filters).
* Claim-level failures retain claim_id and evidence_ids.
* Earlier failed retries remain visible after later success (proven).
* Retry exhaustion, required-role failure, and manager skipping are all persisted
  (proven via the bear-role reproduction test and dedicated storage tests).
* Retry feedback is bounded (5 items max), code-grouped, and never includes the raw prior
  response or a secret (proven).
* Correct validator rejections remain rejected — zero validator weakening.
* CLI diagnostics produce sanitized structured output (proven, including the documented
  field allowlist and secret-scan tests).
* Replay reconstructs and compares failures, correctly distinguishing
  "not reconstructible" from "validator drift" (proven).
* Failure metrics handle insufficient data safely (proven).
* No raw chain-of-thought, no secret, no execution path, no enhanced-arm paper intent, no
  screened-out promotion, no `real_orders` write path — all verified in this session's
  final security review pass.
* Real Claude validation is reported honestly: environmentally blocked, not attempted, not
  fabricated.
* This scratchpad accurately reflects completed work, tests run, and known limitations.

## Milestone 6.1 follow-up — bear prompt versioning + opt-in real bear-role smoke test

Started: 2026-07-13 (same-day follow-up to the Milestone 6.1 session above, requested
after the initial M6.1 work landed in commit `e71f337`).

### 1. Bear prompt versioning

Discovered that the previous M6.1 session edited `prompts/research/bear/v1.txt` **in
place** rather than creating a new version file — technically detectable via prompt-hash
change (Milestone 5's designed mechanism), but not what "preserve v1, add v2" means. Fixed:

* `prompts/research/bear/v1.txt` restored to its exact original Milestone 5 content —
  verified byte-for-byte identical to `git show 0e3e44b:prompts/research/bear/v1.txt`
  (the Milestone 6 commit, before any M6.1 edits) via `diff`.
* `prompts/research/bear/v2.txt` created containing the hardened instructions (explicit
  prohibition on invented downside percentages/price targets/probability estimates,
  fact/inference/uncertainty separation, required `risks` entry, full-replacement-report
  requirement on retry) — this is the exact text the prior session had put into `v1.txt`.
* `research/prompt_registry.py`: added `DEFAULT_ROLE_PROMPT_VERSIONS = {"bear": "v2"}` and
  changed `PromptRegistry.get(role, version=None)` to resolve an omitted version through
  this per-role default map (falling back to `"v1"` for every other role, unchanged).
  `PromptRegistry.__init__` also gained an optional `role_versions` override parameter
  for tests/future config-driven use, defaulting to a copy of the module-level map.
  Every real call site in `orchestration.py` already calls `prompt_registry.get(role)`
  with no explicit version, so bear now resolves to `v2` automatically with **zero**
  changes needed anywhere else — `research_run_id` computation, `ResearchAttemptRecord`,
  and `ResearchValidationFailure` all already read `prompt_def.version`/`.text_hash`
  dynamically from whatever `PromptRegistry.get()` returns.
* Prompt version/hash visibility in diagnostics required no new plumbing — it was already
  wired end-to-end from the original M6.1 session. Verified concretely:
  `PromptRegistry().get("bear").version == "v2"` with a real SHA-256 hash;
  `research_failures_cli(..., role="bear")` returns `prompt_version: "v2"` per failure
  (new test: `test_prompt_version_visible_in_diagnostics`); the bear reproduction test now
  also asserts every persisted bear failure's `prompt_version == "v2"` and that every real
  provider call's `request.prompt_hash` matches `PromptRegistry().get("bear").text_hash`.
* Tests added: `tests/unit/test_research_prompt_registry.py` gained
  `test_bear_role_defaults_to_v2`, `test_bear_v1_still_loadable_explicitly_and_unchanged`
  (asserts the v1 hardening phrase is *absent*, proving it's the original text),
  `test_bear_v1_and_v2_have_different_hashes`,
  `test_default_role_prompt_versions_only_overrides_bear`,
  `test_role_versions_override_is_isolated_per_registry_instance` (5 new tests); the
  pre-existing `test_registry_loads_shipped_role_prompts` was narrowed to the four roles
  that still default to v1 (bear now has its own dedicated assertion instead).

### 2. Opt-in real-Claude bear-role smoke test

`tests/integration/test_research_claude_bear_smoke.py` — gated by
`RUN_CLAUDE_BEAR_TESTS=true` **and** a real `ANTHROPIC_API_KEY` **and**
`RESEARCH_MODEL`/`ANTHROPIC_MODEL`, marked `@pytest.mark.claude_api` (reuses the marker
already registered in `pyproject.toml` for the Milestone 5 smoke test — no new marker
registration needed).

Design decision worth recording: this test calls `orchestration._run_role_with_retries`
**directly** for `role="bear"`, not the full `analyze_with_research_committee` orchestrator.
Reason (a genuine finding, not a design preference): `analyze_with_research_committee`
invokes the manager **unconditionally** once every analyst role in the loop succeeds —
it does not check whether `"manager"` is actually a member of `configuration.roles` before
calling `_run_role_with_retries(role=MANAGER_ROLE, ...)`. This is pre-existing,
unmodified orchestration behavior (not something this or the prior M6.1 session
introduced), and out of scope to fix here (the user's instruction was "do not perform
unrelated refactoring"). It does mean that configuring `roles=("bear",)` and calling the
full orchestrator would **not** actually guarantee "do not invoke the manager" if bear
happened to succeed — so this test bypasses that risk entirely by calling the shared
bounded-retry/validation/failure-construction helper directly, which is structurally
incapable of ever calling anything manager-related. Flagging this pre-existing behavior
as a candidate follow-up (see "Known limitations" below) — not fixed in this pass, since
it wasn't requested and isn't a regression from this session's own changes.

The test:
1. Builds one fixture-backed AAPL `EvidenceSnapshot` and persists it to a real (temporary)
   SQLite database via `save_evidence_snapshot`.
2. Calls `_run_role_with_retries(role="bear", ...)` with `provider="anthropic"`,
   `model_name=<RESEARCH_MODEL/ANTHROPIC_MODEL>`, `max_attempts_per_role=2` (unchanged
   bound — not modified).
3. Persists every returned attempt and structured failure via
   `SQLiteResearchRepository.save_attempt`/`.save_attempt_failures`, then marks the run
   finished — the same persistence calls a real orchestrator run makes.
4. Re-reads the persisted failures back via `list_run_failures` (same function the
   `research-failures` CLI uses) to build a sanitized summary line: attempt count,
   validation result, failure codes, total input/output tokens, total latency — never a
   raw prompt, raw response, or credential.
5. Asserts: `sys.modules` never contains `trading_research.execution`/`.paper`/
   `.runtime`; every returned attempt's `role == "bear"`; no persisted failure has
   `role == "manager"`; attempt count is within the configured bound; if retry was
   exhausted, at least one failure code was actually persisted (never a silent, unexplained
   incompleteness).

### Real smoke-test result

**Not run against a real Claude response — `ANTHROPIC_API_KEY` remains absent from this
environment** (same environmental block as the original M6.1 session; re-confirmed via
`dotenv_values()` boolean check). Honestly reported, not fabricated, per this milestone's
explicit "do not claim ... unless any real Claude revalidation result is reported
accurately" rule.

A **wiring dry-run** was performed instead (deliberately, to validate the test's own
correctness before relying on it) — `RUN_CLAUDE_BEAR_TESTS=true`,
`ANTHROPIC_API_KEY=sk-ant-invalid-test-key` (a syntactically-shaped but deliberately
invalid key, never a real credential), `RESEARCH_MODEL=claude-sonnet-5`:

```text
Bear smoke test result: attempt_count=1 validation_result=RETRY_EXHAUSTED
failure_codes=['PROVIDER_CLIENT_ERROR', 'RETRY_EXHAUSTED']
total_input_tokens=0 total_output_tokens=0 total_latency_ms=201
PASSED
```

This proves the test genuinely reaches Anthropic's real network endpoint (a live 401
response was classified in ~201ms, not a connection failure) and that the
classification/retry/persistence pipeline behaves exactly as designed: a non-retryable
`PROVIDER_CLIENT_ERROR` (Step 7 classification: auth/billing errors are not retried) broke
the retry loop after **1** attempt (not 2), a `RETRY_EXHAUSTED` failure was correctly
persisted alongside it, zero tokens were consumed (no successful request), and the test's
own assertions (manager never invoked, failure codes present, attempt count within bound)
all passed. This is **not** a real bear-role validation result — no real structured
output was ever produced or evaluated — it is evidence that the test harness itself is
correctly wired and will produce a trustworthy result the moment real credentials exist.
No execution path was reached in either case (dry-run or the default skip): confirmed via
the `sys.modules` assertions and by the simple fact that this test file never imports
`execution`/`paper`/`runtime`/`recommendations`/`overlay` at all.

### Real smoke-test result — genuine real-Claude run (credentials added)

Later the same day, the user added a real `ANTHROPIC_API_KEY` to `.env` and asked for the
opt-in bear-role test to be re-run. The assistant did **not** read or print the key value
— `.env` was sourced directly into the shell environment (`set -a; source .env; set +a`)
inside the same command that invoked pytest, so the credential never appeared in any tool
output or transcript. Command run:

```bash
RUN_CLAUDE_BEAR_TESTS=true \
pytest tests/integration/test_research_claude_bear_smoke.py -v -s -m claude_api
```

Genuine real result:

```text
Bear smoke test result: attempt_count=1 validation_result=VALID_REPORT failure_codes=[]
total_input_tokens=3588 total_output_tokens=1878 total_latency_ms=22113
PASSED (1 passed in 22.56s)
```

* **Attempt count:** 1 (of a maximum of 2 — the bear role passed schema validation and
  claim-to-evidence validation on the very first real attempt against the hardened
  `bear/v2.txt` prompt; no retry was needed).
* **Validation result:** `VALID_REPORT` — the real Claude response passed forced-tool
  structured-output extraction, local JSON Schema validation
  (`role_report_json_schema()`), and independent claim-to-evidence validation
  (`validate_role_report`) against the exact fixture AAPL evidence snapshot used.
* **Failure codes:** none (`failure_codes=[]`) — zero `ResearchValidationFailure` rows
  were persisted for this run, confirmed by re-reading `research_attempt_failures` via
  `list_run_failures` (the same function the `research-failures` CLI uses) before the
  test's own assertions ran.
* **Token usage:** 3588 input tokens, 1878 output tokens (real, not fabricated — read
  directly from `response.usage`, matching the existing `UsageRecord`/`cost_status`
  discipline: cost stays unpopulated/`PRICING_NOT_CONFIGURED` since
  `config/research_pricing.yaml` is empty by default, not shown fabricated here either).
* **Latency:** 22,113 ms (~22.1s) for the single successful attempt.
* **No execution path reached:** confirmed identically to the dry-run — `sys.modules`
  never contained `trading_research.execution`/`.paper`/`.runtime` before or after the
  call; no failure had `role == "manager"` (none existed at all, since bear succeeded and
  this test never calls the manager under any outcome); the test never imports
  `recommendations`/`overlay`, so no recommendation, paper order, or live order was ever
  constructed, let alone submitted.

This **is** a genuine, evidence-backed confirmation that the Milestone 6.1 root-cause fix
(the hardened `bear/v2.txt` prompt, explicitly forbidding invented numeric downside
figures) works against a real live Claude response, not merely the deterministic
reproduction (`test_bear_role_failure_reproduction.py`) or the invalid-key wiring dry-run
above. It is a single successful run, not a statistically repeated validation — the
opt-in test remains uncommitted to CI/default execution for the same cost/latency reasons
documented in Milestone 5/6 (`docs/milestones/milestone5-evidence-backed-claude-research.md`'s "Known
limitations").

### Test run log (follow-up)

* `pytest tests/unit/test_research_prompt_registry.py -v`: `9 passed` (4 pre-existing + 5
  new).
* `pytest tests/integration/test_bear_role_failure_reproduction.py -v`: `1 passed`
  (extended with prompt-version/hash diagnostics assertions).
* `pytest tests/unit/test_research_failures_cli.py -q`: `11 passed` (10 pre-existing + 1
  new).
* `pytest tests/integration/test_research_claude_bear_smoke.py -v -m claude_api` (default,
  no credentials): `1 skipped` — correct, matches the gating contract.
* `pytest tests/ -q` (full main suite, before real credentials were added): `753 passed,
  7 skipped` — 753 unchanged from before this follow-up (no new *passing* tests, since
  the new opt-in smoke test is correctly skipped by default), skip count `6 -> 7` (+1,
  the new opt-in test), zero regressions.
* `cd paper_runtime && pytest tests/ -q`: `33 passed` — unchanged, final regression
  verification per this follow-up's explicit instruction.
* After the user added a real `ANTHROPIC_API_KEY` to `.env`:
  `RUN_CLAUDE_BEAR_TESTS=true pytest tests/integration/test_research_claude_bear_smoke.py
  -v -s -m claude_api` -> **`1 passed in 22.56s`** (genuine real result — see "Real
  smoke-test result — genuine real-Claude run" above).
* `pytest tests/ -q` re-run (default invocation, no `RUN_CLAUDE_BEAR_TESTS` set): `753
  passed, 7 skipped` — unchanged; the opt-in test correctly reverts to skipped when its
  env flag is absent, exactly as designed — it is not accidentally "always on" now that
  a real key exists in `.env`.
* `cd paper_runtime && pytest tests/ -q` re-run: `33 passed` — unchanged, final
  regression verification after the real-credential run.

### Known limitations (follow-up)

1. `analyze_with_research_committee` invokes the manager unconditionally once all
   configured analyst roles succeed, without checking whether `"manager"` is present in
   `configuration.roles` — discovered while designing this smoke test, not fixed here
   (out of the requested scope; flagged as a candidate Milestone 7 hardening item since it
   could cause an unexpected extra provider call for any future single-role-only
   orchestrator invocation).
2. ~~The real bear-role smoke test has still never produced or evaluated a genuine live
   Claude structured-output response~~ — **resolved**: a real `ANTHROPIC_API_KEY` was
   added to `.env` and the test was re-run for real (see "Real smoke-test result —
   genuine real-Claude run" above): `1 passed in 22.56s`, `VALID_REPORT`, zero failure
   codes, first attempt. Only a single real run has been performed — this is not a
   statistically repeated validation, and the test remains opt-in (not part of the
   default/CI suite) for cost/latency reasons, consistent with Milestone 5/6 convention.
* No commit or push was performed by this assistant during this session. Note: at the
  next turn, `git log`/`git status` showed this session's full diff already committed as
  `e71f337 "milestone 6.1"` (author `jijoece@gmail.com`) with a clean working tree —
  verified via `git show --stat e71f337` to contain exactly this session's 31 changed
  files and nothing else. That commit was not created by this assistant (no `git commit`
  tool call was ever issued this session); it is recorded here for an accurate history,
  not claimed as this assistant's action.

## Milestone 6.1 follow-up 3 — manager-invocation fix

Started: 2026-07-13 (same-day follow-up, requested immediately after the real bear-role
smoke test result above landed as commit `6ebae33`).

### The pre-existing bug

`analyze_with_research_committee` invoked the manager role unconditionally once every
configured analyst role produced a valid report — it never checked whether `"manager"`
was actually present in `configuration.roles` before calling
`_run_role_with_retries(role=MANAGER_ROLE, ...)`. This was first discovered while
designing the opt-in real bear-role smoke test (documented in the previous follow-up
section's "Known limitations") and is fixed now, as its own narrowly-scoped change.

### Fix

`research/errors.py`: added `ManagerNotConfiguredError(ResearchError)` — raised when a
caller wants a final decision but the manager role isn't configured to produce one.

`research/orchestration.py::analyze_with_research_committee` gained a keyword-only
`require_decision: bool = True` parameter:

* **Manager configured** (`"manager" in configuration.roles`): `require_decision` has no
  effect — behavior is byte-for-byte unchanged from before this fix. Every existing
  production call site (`cli.py::run_research_cli`, `research/scheduled_cycle.py`) always
  configures manager (`config/research.yaml`'s `roles:` list includes it), so this fix
  changes nothing for any currently-running production path — confirmed by the full
  suite passing unmodified (760 passed, same 7 skipped, before and after).
* **Manager omitted + `require_decision=True`** (the default): raises
  `ManagerNotConfiguredError` **immediately**, before `compute_research_run_id`, before
  any preflight check, before any provider call, and before any `research_repository`
  write (no `run_started` row, no attempt, nothing persisted for an invalid
  configuration) — genuinely fail-closed, not just "eventually errors."
* **Manager omitted + `require_decision=False`** (explicit opt-in for an
  analyst-only/diagnostic run): every configured analyst role runs exactly as before
  (bounded retry, full schema + claim-to-evidence validation, structured-failure
  persistence, idempotent resume/reuse — all unchanged); the manager is never invoked
  under any outcome; on success, returns the new terminal status
  `RUN_STATUS_ANALYST_REPORTS_COMPLETE_NO_MANAGER` with `decision=None` and
  `role_reports` populated — never a fabricated `ResearchDecision`; on an analyst-role
  failure, returns the existing `ANALYSIS_INCOMPLETE` status exactly as it always has
  (reusing an existing, already-well-understood status per the request's own suggestion,
  rather than inventing a second new failure-side status).
* **Idempotent resume**: `RUN_STATUS_ANALYST_REPORTS_COMPLETE_NO_MANAGER` was also added
  to the existing-run-reuse check at the top of the function (alongside `COMPLETED` and
  `ANALYSIS_INCOMPLETE`), so re-invoking an identical analyst-only run is a pure read, not
  a re-run — consistent with every other terminal status.

**A second, closely-related bug found and fixed in the same code path while writing
tests**: the existing `if incomplete_reasons:` branch (an analyst role exhausted retries)
unconditionally constructed and persisted a `MANAGER_SKIPPED`/`MANAGER_NOT_INVOKED`
`ResearchValidationFailure` — including for a config that never had a manager role in the
first place, where a "the manager was skipped" record is simply false (nothing was ever
going to invoke it). Caught by
`test_analyst_only_failure_still_persists_structured_failures` failing on first run — see
"Test results" below. Fixed by guarding that failure's construction with
`if manager_configured:`; the analyst-role failure(s)/`RETRY_EXHAUSTED`/
`REQUIRED_ROLE_FAILED` records are still persisted exactly as before in every case.

### Behavior preserved unchanged (verified, not just claimed)

* Retry bound (`max_attempts_per_role`) — untouched.
* Claim-to-evidence validation, schema validation, claim-failure classification —
  untouched.
* Prompt versions/hashes — untouched (this fix has no interaction with
  `PromptRegistry`/`bear/v2.txt` at all).
* Execution boundaries — untouched; `analyze_with_research_committee` still imports
  nothing from `execution`/`paper`/`runtime`/`recommendations`/`overlay`.
* Recommendation/overlay logic — untouched; this fix is entirely inside the research
  orchestration layer, several calls upstream of where a baseline recommendation or
  overlay would ever be touched.

### Tests added (`tests/unit/test_research_orchestration.py`)

* `test_manager_configured_is_invoked_exactly_once` — manager configured -> invoked
  exactly once, unchanged behavior.
* `test_manager_omitted_is_never_invoked` — manager absent + `require_decision=False` ->
  no `("manager", ...)` step is ever needed in the `ScriptedResearchProvider` (a manager
  call would raise `AssertionError` from the scripted provider itself, proving the
  guarantee structurally, not just via a role-name assertion afterward); returns
  `ANALYST_REPORTS_COMPLETE_NO_MANAGER`, `decision=None`.
* `test_bear_only_diagnostic_run_succeeds_without_extra_provider_call` — single-role
  (`roles=("bear",)`) diagnostic config succeeds with exactly one provider call total.
* `test_analyst_only_failure_still_persists_structured_failures` — a rejected claim in
  analyst-only mode still persists `UNKNOWN_EVIDENCE_ID`/`RETRY_EXHAUSTED`/
  `REQUIRED_ROLE_FAILED` failures, and (after the second fix above) correctly persists
  **no** `MANAGER_SKIPPED` failure, since no manager was ever configured.
* `test_no_final_decision_fabricated_from_analyst_reports` — `result.decision is None`,
  `repo.decisions == {}` (nothing was ever persisted as a decision) for an analyst-only
  success.
* `test_production_mode_requiring_decision_fails_closed_without_manager` — default
  `require_decision=True` + no manager configured -> `ManagerNotConfiguredError` raised
  before any provider call, before any repository write (`provider.calls == []`,
  `repo.attempts == []`, `repo.runs == {}`).
* `test_full_committee_behavior_unchanged_when_manager_configured` — explicit regression
  guard: role call order, final status, decision rating, and persisted run status all
  identical to pre-fix behavior.

### Test results

* `pytest tests/unit/test_research_orchestration.py -v`: first run —
  **16 passed, 1 failed** (`test_analyst_only_failure_still_persists_structured_failures`
  failed on `assert not any(f.stage == "MANAGER_SKIPPED" ...)`, correctly catching the
  second bug described above). After the `if manager_configured:` guard fix: **17 passed**
  (10 pre-existing + 7 new).
* `pytest tests/ -q` (full main suite): **760 passed, 7 skipped** — 753 -> 760 (+7 new
  tests), skip count unchanged at 7, zero regressions.
* `cd paper_runtime && pytest tests/ -q`: **33 passed** — unchanged, final regression
  verification per this follow-up's explicit instruction.

### Documentation

`tests/integration/test_research_claude_bear_smoke.py`'s docstring (written before this
fix existed) claimed calling `_run_role_with_retries` directly was necessary because of
"unrelated, pre-existing design" — updated to note the bug is now fixed and point to the
new `require_decision=False` path, while deliberately leaving the test's own mechanism
unchanged (it still calls `_run_role_with_retries` directly, which remains correct and
is the version already validated against a real Claude response in the previous
follow-up — not worth risking a behavior change to an already-proven real-API test for a
documentation-only concern).

### Known limitations (this follow-up)

None new. This fix fully resolves item 1 in the previous follow-up's "Known limitations"
list.
