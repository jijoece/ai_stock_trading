# Deep Dive Scratchpad — Implement docs/trading_desk_requirement.md

## Investigation Question
Implement the full requirement spec in docs/trading_desk_requirement.md:
(a) investigate repo + configured MCPs, (b) research community practice
(Reddit MCP + web), (c) produce docs/AI-Driven-Stock-Trading-Architecture.md,
docs/AI-Stock-Trading-Implementation-Plan.md, docs/AI-Stock-Trading-Research-Sources.md,
(d) add the optional safe proof-of-concept (no live trading), with tests.
User directive: run without pausing for permission; write scratchpad after every step.

## Mode
agent-architecture + mcp-analysis + implementation-plan (continuation of
20260711-095011-ai-trading-architecture.md — inherit its Known Facts; do not redo
repo/MCP inventory work that is already captured in research-input/).

## Current Status
2026-07-11 11:20 — COMPLETE. All 3 docs written; PoC implemented; 47/47 tests pass
(pytest 0.29s, .venv); CLI verified end-to-end (`analyze SOFI` → schema-valid
frozen recommendation JSON, exit 0; `paper-status` works; fail-closed paths
covered by tests). .env.example updated with REDDIT_CLIENT_ID/SECRET gap note.

## Known Facts (inherited + verified this session)
- Repo = clone of Oft3r/agentic-trading-desk (MIT), extended by prior session:
  pyproject.toml (anthropic/mcp/jsonschema/PyYAML/dotenv; pytest dev extra;
  pythonpath=src), src/trading_research/{config,logging_config}.py,
  models/ (4 modules), mcp/ (adapter+classifier+inventory), collection/
  (prompt_injection_filter only), storage/ (migrations/database/repositories),
  schemas/ (4 JSON schemas), config/tool_policy.yaml, research-input/ inventories,
  data/research.sqlite3.
- EMPTY: tests/ (absent), src/trading_research/{batches,processing,reporting}/ ,
  prompts/, research-results/*.
- Robinhood MCP: official hosted HTTP (agent.robinhood.com), OAuth, 46 tools
  (29 read allowed / 17 write prohibited per config/tool_policy.yaml). No paper
  trading. Writes cannot be removed server-side → client-side allowlist
  (.claude/settings.local.json) + tool_policy.yaml enforce read-only posture.
- Reddit MCP: jordanburke/reddit-mcp-server via npx (stdio), 23 tools (17 read /
  6 write). Prior session: anonymous read = HTTP 403 → needs REDDIT_CLIENT_ID/SECRET.
- Three required docs do NOT exist yet in docs/ (only batch_processing.md +
  trading_desk_requirement.md).
- CLAUDE.md mandates deterministic scripts for all indicator math.

## Assumptions
- Prior session's MCP inventories (research-input/*.json, 2026-07-11T10:17Z) are
  current. Safe to reuse — same day, no config change since.
- Reddit 403 may persist; retry once via MCP this session; fallback = WebSearch/
  WebFetch for community research (documented as limitation).

## Unknowns
- Does Reddit MCP now have credentials? (retry pending)
- Robinhood MCP official docs (rate limits, scopes) — web check pending.
- GitHub state of jordanburke/reddit-mcp-server vs adhikasp/mcp-reddit.
- Backtesting framework maintenance status (Backtrader/VectorBT/Zipline/LEAN).

## Hypotheses (inherited)
- H1: Hybrid Option C (Python deterministic core + Claude Code as dev/manual
  interface + bounded API reasoning) is the recommended architecture.
- H2: Configured reddit-mcp-server ok for interactive research; deterministic
  mention analytics belong in Python (direct API/PRAW or stored MCP output).
- H3: Multi-agent not justified; single orchestrator + deterministic tools.

## Plan (ordered)
1. DONE Inspect src/ + tests/ state (10:37). No tests exist yet.
2. Retry Reddit MCP read (search_reddit). [next]
3. Reddit research topics via MCP if working, else WebSearch fallback.
4. Web: Robinhood MCP docs, reddit MCP repos comparison, Oft3r repo, backtesting fw.
5. Write 3 docs (architecture, implementation plan, research sources).
6. PoC gap fill: ticker universe + mention parser + sentiment aggregation iface +
   paper ledger + risk module + recommendation schema + mock adapters + CLI + tests.
7. Run full test suite; final report.

## Sources to Check
- Reddit (via MCP if authed): r/algotrading, r/ClaudeAI, r/mcp, r/quant, r/LocalLLaMA.
- Web: Robinhood MCP docs; GitHub repos above; backtesting framework activity.

## MCPs Used
- reddit.test_reddit_mcp_server (10:40) → server ok, read-only mode, v1.5.1.
- reddit.search_reddit("Claude MCP trading Robinhood", year, 15) → HTTP 403.
  CONFIRMS prior finding: anonymous Reddit API read is blocked; REDDIT_CLIENT_ID/
  SECRET required. Falling back to WebSearch (incl. site:reddit.com) — documented
  as limitation in final docs.
- robinhood-trading: NOT called this session (read inventory reused from
  research-input/, captured 10:17 today). No write tool will ever be called.

## Websites Reviewed
(appended as gathered)

## Files Reviewed
- docs/trading_desk_requirement.md (the spec)
- prior scratchpad 20260711-095011 (state + findings inherited)
- research-input/mcp-inventory-summary.md, pyproject.toml

## Commands Run
- new_scratchpad.py; find repo tree; ls -R src tests config prompts.

## Evidence Collected
- E1 (10:37, local fs): tests/ absent; batches/, processing/, reporting/, prompts/
  empty → PoC must add tests + missing modules. pyproject has pytest config ready.
- E2 (10:45, github.com/jordanburke/reddit-mcp-server): v1.5.1 Jul 2026, MIT,
  TypeScript, 182★/30 forks, actively maintained. 10 read + 6 write tools. Auth
  tiers: anonymous ~10 req/min (BLOCKED in our env — 403), client id/secret
  60-100 req/min, user+pass for writes. Safe Mode, duplicate detection, bot
  disclosure. = the configured server.
- E3 (10:45, github.com/adhikasp/mcp-reddit): Python, MIT, 413★, read-only,
  minimal tool surface (fetch_hot_threads + post fetch), 16 commits, no releases,
  sparse docs/undocumented pagination → less capable & less maintained than E2.
- E4 (10:47, robinhood.com agentic-trading-overview + newsroom): OFFICIAL product.
  OAuth via agent.robinhood.com/mcp/trading. Reads span ALL accounts; trades ONLY
  in dedicated "Agentic account" funded separately. NO paper/sim mode. Agents CAN
  be configured to execute without per-trade confirmation (⚠) — user remains
  responsible. No published rate limits. Up to 10 self-directed accounts.
- E5 (10:48, arxiv 2508.02089 + ACM WebSci'24 "Highly Regarded Investors?" +
  arxiv 2507.22922 + alphaarchitect.com): academic evidence on WSB/Reddit sentiment
  is MIXED — some short-horizon signal in bull markets; other studies find weak
  correlation and price→sentiment (reverse) causality; anonymity/hype/reflexivity
  degrade signal. → supports spec's ≤10% score weight; treat as supplementary only.
- E6 (10:49, practical-devsecops, truefoundry CVE-2025-54136, arxiv 2603.22489):
  MCP prompt injection + tool poisoning are real (Supabase Cursor incident mid-2025;
  first malicious MCP package Sept 2025). Mitigations: least-privilege allowlists,
  treat MCP servers as third-party code, scope consent, gateway defenses.
- E7 (10:50, python.financial + autotradelab + bullalert): backtrader in LTS/stalled
  since 2023; zipline-reloaded maintenance-only; VectorBT = fast vectorized research;
  NautilusTrader/LEAN = production-grade but heavy. → Phase-1: own SQLite ledger
  (simple, auditable); VectorBT optional later for factor research; don't adopt a
  heavy framework prematurely.
- E8 (10:51, cloudflare HITL docs, matheuspalma.com, arxiv 2605.19337 "Agentic
  Trading: When LLM Agents Meet Financial Markets"): HITL patterns = pre-execution
  approval for consequential actions; approval TTL (24h for sensitive); PIN THE
  APPROVED PAYLOAD BY HASH and refuse execution on drift; audit trail. Directly
  informs approvals design (spec §5 human approval).
- E9 (10:42, live MCP call): search_reddit → HTTP 403 under anonymous mode.
  Documented gap: register read-only Reddit app for live collection.

## Websites Reviewed
- github.com/jordanburke/reddit-mcp-server (2026-07-11) — E2
- github.com/adhikasp/mcp-reddit (2026-07-11) — E3
- robinhood.com/us/en/support/articles/agentic-trading-overview/ (2026-07-11) — E4
- robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/ (2026-07-11) — E4
- arxiv.org/pdf/2508.02089, dl.acm.org/doi/10.1145/3614419.3643993,
  arxiv.org/pdf/2507.22922, alphaarchitect.com/wallstreetbets/ (2026-07-11) — E5
- practical-devsecops.com/mcp-security-vulnerabilities/,
  truefoundry.com/blog/blog-mcp-tool-poisoning-gateway-defense,
  arxiv.org/html/2603.22489v1 (2026-07-11) — E6
- python.financial, autotradelab.com, bullalert.ai backtest comparisons (2026-07-11) — E7
- developers.cloudflare.com/agents/.../human-in-the-loop/,
  matheuspalma.com/blog/human-in-the-loop-llm-tool-approval-production,
  arxiv.org/pdf/2605.19337 (2026-07-11) — E8

## Key Findings
(appended)

## Risks / Caveats
- Reddit content = untrusted sentiment, injection risk; never fact.
- No orders/watchlist writes ever this session (spec + skill hard rule).

## Decisions / Conclusions
- D1 (user directive, 10:41): IGNORE the batch-processing implementation from the
  prior scratchpad. batches/ stays out of scope; scripts/{submit,check,download}_batch.py
  and docs/batch_processing.md are pre-existing and left untouched; Batch API is
  mentioned in docs only as an optional cost lever, not core architecture.
- D2 (11:10): All 3 required docs written (architecture 25 sections / plan with
  milestones+sizing / sources S1–S11 with reliability labels). Hybrid Option C
  recommended; reddit sentiment ≤10%; hash-pinned HITL approvals; no multi-agent.
- D3 (11:12): PoC proposed file changes (ALL NEW FILES — nothing overwritten,
  no live-trading capability anywhere, mocks only, per spec §"Optional PoC"):
  * src/trading_research/universe/{__init__,tickers}.py — verified ticker universe
    + ambiguous-symbol handling (AI, IT, ON, ALL, SO, A, FOR, ARE…)
  * src/trading_research/analysis/{__init__,ticker_extractor,sentiment}.py —
    mention parser (cashtag + context-confirmed bare symbols); deterministic
    sentiment aggregation interface (pluggable classifier, mock impl)
  * src/trading_research/risk/{__init__,position_sizing}.py — fail-closed
    deterministic risk engine (IncompleteStateError on missing/stale state)
  * src/trading_research/paper/{__init__,ledger}.py — SQLite paper ledger:
    idempotent orders, spread+slippage fills, T+1 settled cash, positions,
    snapshots. NO broker interaction.
  * src/trading_research/storage/trading_schema.py — full trading DDL (spec §data
    model incl. approvals + reserved real_orders table with NO writer code)
  * src/trading_research/mcp/mock_adapters.py — MockRobinhood/MockReddit fixtures
  * src/trading_research/cli.py — `analyze <ticker>` end-to-end on mocked data,
    `paper-status`
  * schemas/recommendation.schema.json — frozen recommendation contract
  * tests/unit/*.py — extractor, risk, sentiment, ledger, schema, trading_schema
  Verified against spec: no order placement, no MCP write calls, conventions
  preserved (dataclasses, sqlite3, from __future__ import annotations).

## Open Questions
(appended)

## Key Findings (final)
- Reddit MCP anonymous read BLOCKED (403) — re-confirmed live this session (E9);
  read-only Reddit app creds are the phase-1 unblocker (documented, .env.example).
- Robinhood MCP official: no paper mode; writes only in dedicated Agentic account;
  can be configured for no-confirmation execution — architecture refuses that.
- Reddit/WSB sentiment academic evidence is mixed → ≤10% cap justified (E5).
- MCP injection/tool-poisoning incidents are real (E6) → pin versions, fail-closed
  tool policy, injection filter.
- Backtesting frameworks: build own SQLite ledger now; VectorBT optional later (E7).
- HITL best practice: hash-pinned approval payloads + TTL + drift invalidation (E8).

## Final Summary Draft
Deliverables (all committed to working tree, nothing pushed):
- docs/AI-Driven-Stock-Trading-Architecture.md — 25 sections, Mermaid diagram,
  all 15 decision criteria answered. Recommendation: hybrid Option C.
- docs/AI-Stock-Trading-Implementation-Plan.md — milestones 0–4, epics, stories,
  sizes, dependencies, acceptance criteria, ordering; M4 gated not scheduled.
- docs/AI-Stock-Trading-Research-Sources.md — S1–S11 with reliability labels,
  verified-vs-anecdotal flags, contradiction notes, MCP-research limitation.
- PoC (all NEW files): universe/tickers.py, analysis/{ticker_extractor,sentiment}.py,
  risk/position_sizing.py, paper/ledger.py, storage/trading_schema.py,
  mcp/mock_adapters.py, cli.py, schemas/recommendation.schema.json,
  tests/unit/ (5 modules, 47 tests, all passing).
- Safety verified: no broker write path exists; real_orders table reserved with
  no writer; mocks only; reddit sentiment weight structurally capped at 0.10 in
  schema; risk engine fail-closed (IncompleteStateError); ledger idempotent.
Limitations: Reddit-MCP community research blocked by 403 (gap documented in
sources doc S10); market-data source for scheduled pipeline is an open question;
PoC scoring is illustrative, not the real model.
