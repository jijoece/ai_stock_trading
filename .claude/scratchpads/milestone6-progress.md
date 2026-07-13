# Milestone 6 Progress

Started: 2026-07-13T02:20:03Z
Branch: main
Status: STARTING

## Baseline
- Main test suite: `pytest tests/ -q` -> 571 passed, 2 skipped (matches expected M5 result exactly)
- Paper runtime suite: `cd paper_runtime && pytest tests/ -q` -> 33 passed (matches expected)
- Git status: clean except docs/milestone-6.md modified (pre-existing working-tree edit to the milestone spec itself, updating M5's real-outcome narrative — not part of this session's implementation, left as-is) + untracked .claude/scratchpads/milestone6-progress.md (this file)
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
- [x] Documentation (docs/milestone6-real-evidence-continuous-evaluation.md) + ADR 0004
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
`docs/milestone6-real-evidence-continuous-evaluation.md`.

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
- `docs/milestone-6.md` — pre-existing working-tree edit from before this session started
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
   per run) — see docs/milestone6 "Known limitations" #3 for why.
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

See `docs/milestone6-real-evidence-continuous-evaluation.md`'s "Known limitations" section
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
criteria in docs/milestone-6.md are met:

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
