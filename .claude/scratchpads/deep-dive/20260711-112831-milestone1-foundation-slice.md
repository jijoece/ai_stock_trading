# Deep Dive: milestone1-foundation-slice

## Investigation Question
milestone1-foundation-slice

## Mode
<mcp-analysis | repo-analysis | trading-research | agent-architecture | risk-review | website-research | implementation-plan | general>

## Current Status
Started 2026-07-11 11:28 UTC

## Known Facts
-

## Assumptions
-

## Unknowns
-

## Hypotheses
-

## Sources to Check
- [ ] local code/files
- [ ] Git history
- [ ] MCP servers
- [ ] websites
- [ ] official documentation
- [ ] brokerage documentation
- [ ] market data sources
- [ ] news/social/reddit sources

## MCPs Used
-

## Websites Reviewed
-

## Files Reviewed
-

## Commands Run
-

## Evidence Collected
-

## Key Findings
-

## Risks / Caveats
-

## Decisions / Conclusions
-

## Open Questions
-

## Final Summary Draft


## Investigation Question
Complete Milestone 1 foundation slice (1A.1 schema, 1A.2 universe, 1A.3 rec JSON schema, 1B.1 ticker parser) per docs/AI-Stock-Trading-Implementation-Plan.md — offline, no broker/LLM/network.

## Mode
implementation-plan (continuation of existing partial implementation)

## Known Facts (verified)
- All four target modules already exist with substantial implementations; tests exist for extractor, schema+cli, ledger, sizing, sentiment.
- pyproject: pytest only; no formatter/linter/type-checker configured. Deps: anthropic, mcp, jsonschema, PyYAML, dotenv.
- BUG FOUND: migrations.py and trading_schema.py BOTH define `recommendations` (different shapes). Live data/research.sqlite3 has the research shape (workstream_id...) — trading `recommendations` (rec_id PK) silently never created => FK targets broken for recommendation_factors/simulated_orders/approvals/evaluation_results. All tables empty; DB gitignored => safe documented rename migration.
- trading_schema has NO indexes, no immutability enforcement, no real_orders write guard, no config_hash/git_sha on recommendations, no errors/data-quality extension (arch §17 last bullet).
- tickers.py: no is_active/source on Security, no normalize/require API.
- extractor: no confidence category / rejection_reason / span end.
- rec JSON schema: missing missing_data_reasons, config_hash, git_sha; no rule blocking risk_plan on analysis_incomplete; no fixtures on disk.

## Decisions
- Rename research-run `recommendations` -> `research_recommendations` in migrations.py + legacy-rename guard run by both apply_schema and apply_trading_schema (documented arch-inconsistency fix; no writers existed, tables empty).
- Enforce recommendation immutability + real_orders reservation with SQLite triggers (fail-closed at DB level).
- Extend `errors` with severity + data_quality via idempotent ALTERs (per arch §17).
- Schema draft-07 kept; add allOf/if-then: analysis_incomplete => risk_plan null + missing_data_reasons >=1; no_action => risk_plan null.
- CLI computes config_hash from NON-SECRET config fields only; git_sha via `git rev-parse` with 'unknown' fallback (offline-safe).
- Confidence categories (deterministic): cashtag=high; bare unambiguous=medium; bare ambiguous confirmed=low; rejected ambiguous=rejected + rejection_reason.

## Current Status (updated 2026-07-11, mid-implementation)
Implementation ~60% done. Completed so far:

1. **Collision fix (migrations.py)** — research-run `recommendations` renamed to
   `research_recommendations` (SCHEMA_VERSION 2); added
   `rename_legacy_research_recommendations()` guard that migrates legacy DBs by
   shape detection (has workstream_id, lacks rec_id) and fails loudly if both
   tables exist. Verified safe: all affected tables in data/research.sqlite3 had
   0 rows; DB is gitignored; no code read/wrote the research recommendations table.
2. **trading_schema.py (1A.1)** — TRADING_SCHEMA_VERSION 2. Added:
   - `config_hash`, `git_sha` columns on recommendations (reproducibility)
   - `span_start`, `confidence`, `rejection_reason` on reddit_ticker_mentions
   - `errors` table (shared shape) + severity/data_quality extension per arch §17
   - 18 indexes incl. UNIQUE(record_type, record_id, symbol, span_start) on mentions
   - Triggers: frozen recommendations immutable (UPDATE/DELETE abort);
     real_orders fully write-blocked (INSERT/UPDATE/DELETE abort) — reserved table
   - `_ensure_columns()` idempotent ALTERs for pre-existing DBs
   - apply order: rename guard → DDL → column upgrades → indexes → triggers
3. **database.py** — connect() now applies BOTH schemas (research + trading).
4. **tickers.py (1A.2)** — added `normalize_symbol()` (strip, lstrip $, upper,
   reject empty/whitespace), `UnknownSymbolError`, `TickerUniverse.require()`;
   Security gained `is_active` + `source`; is_valid now rejects inactive + OTC;
   from_csv reads is_active/source; seed gained GONEQ (inactive example);
   universe/__init__.py exports updated.
5. **ticker_extractor.py (1B.1)** — TickerMention gained `end`, `confidence`,
   `rejection_reason`. Deterministic categories: cashtag=high; bare
   unambiguous=medium; bare ambiguous+context=low; rejected ambiguous=rejected
   with reason string.
6. **recommendation.schema.json (1A.3)** — added required config_hash (sha256
   pattern) + git_sha (hex or 'unknown'); missing_data_reasons array; allOf
   if/then rules: analysis_incomplete ⇒ risk_plan null + missing_data_reasons
   ≥1; side no_action/analysis_incomplete ⇒ risk_plan null.
7. **cli.py** — emits config_hash (sha256 of NON-SECRET config only), git_sha
   (subprocess with 'unknown' fallback), missing_data_reasons on all
   fail-closed paths.
8. **Fixtures** — 12 files in tests/fixtures/recommendations/ (3 valid_*, 9
   invalid_* incl. incomplete-with-order-details, account-identifier,
   overweight-reddit, unfrozen, wrong-disclaimer).

## Remaining
- New tests: test_trading_schema.py (FK/uniqueness/triggers/indexes/legacy
  migration/no-real_orders-write-path), test_tickers.py, extractor additions,
  test_recommendation_fixtures.py
- Developer doc (schema init, universe, ambiguity, validation, running tests)
- Full pytest run + diff review

## Final Status: COMPLETE
All 4 stories (1A.1, 1A.2, 1A.3, 1B.1) + tests + docs implemented. 102/102 tests pass.
Full diff reviewed — no secrets, no unrelated changes, changes confined to
src/trading_research/{storage,universe,analysis}, cli.py, schemas/, tests/, docs/.


## Real bug found and fixed during test-writing
universe/tickers.py::name_tokens() included the ticker symbol's own lowercase
spelling in company-name tokens (e.g. "ON Semiconductor" -> tokens include
"on"). This let any bare mention of the word "on" (e.g. "Turn it ON")
self-confirm as a ticker mention via the company-name co-mention rule,
defeating the ambiguity guard the extractor requires per the spec ("Turn it
ON" must NOT match). Fixed by excluding sec.symbol.lower() from name_tokens.
Caught by test_false_positive_turn_it_on while adding required false-positive
test coverage from the task spec.
