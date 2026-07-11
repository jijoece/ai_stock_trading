# AI Stock Trading — Research Sources

**Date accessed for all sources: 2026-07-11** unless noted.
**Companion to:** [AI-Driven-Stock-Trading-Architecture.md](AI-Driven-Stock-Trading-Architecture.md)

Reliability scale: **High** (primary/official/verified locally) · **Medium** (reputable secondary, corroborated) · **Low** (single anecdote/opinion, uncorroborated).

**Limitation disclosed up front:** the requirement asked for extensive research *through the configured Reddit MCP*. Anonymous Reddit API access is blocked (HTTP 403) from this environment — verified live twice on 2026-07-11 (`search_reddit`, `browse_subreddit`, `get_top_posts` all fail; only the local health check succeeds). Community-practice research therefore came from web search (including academic studies of Reddit data) rather than direct subreddit retrieval. Direct MCP-based subreddit research becomes possible once a read-only Reddit app credential is configured (Implementation Plan story 0.6). Reddit-derived claims below are labeled accordingly.

---

## S1 — Local repository and session inspection (primary evidence)

- **Source:** this working directory; live Claude Code session tool lists; `research-input/robinhood-tools.json`, `research-input/reddit-tools.json`, `research-input/mcp-inventory-summary.md` (captured 2026-07-11T10:17Z); scratchpads `.claude/scratchpads/deep-dive/20260711-*`.
- **Type:** primary (local code + live session).
- **Key findings:** repo = clone of Oft3r/agentic-trading-desk (MIT) extended with a research pipeline (`src/trading_research`); Robinhood MCP exposes 46 tools (29 read / 17 write); Reddit MCP exposes 23 tools (17 read / 6 write); `.claude/settings.local.json` allowlists only read-only Robinhood tools; no tests/paper ledger/risk module existed before this session; a stale user-scope MCP entry in `~/.claude.json` contains plaintext Reddit credentials (hygiene issue, flagged).
- **Reliability:** High (verified directly). **Verified.**
- **Influence:** grounds §§4–8 of the architecture; PoC scope; security action item 0.5.

## S2 — Live Reddit MCP probes

- **Source:** `mcp__reddit__test_reddit_mcp_server` and `mcp__reddit__search_reddit` calls, 2026-07-11 (this session and the 09:50 session).
- **Type:** primary (live tool execution).
- **Key findings:** server v1.5.1 healthy, read-only mode; every real Reddit API call → HTTP 403 anonymously; registered client id/secret required for any live retrieval.
- **Reliability:** High. **Verified** (reproduced twice, independent sessions).
- **Influence:** architecture §7, decision criterion 5; Implementation Plan 0.6; the research-method limitation above.

## S3 — jordanburke/reddit-mcp-server (configured server)

- **Source:** https://github.com/jordanburke/reddit-mcp-server
- **Type:** source code / repository documentation (primary for the tool itself).
- **Key findings:** TypeScript, MIT, v1.5.1 (July 2026), 182★, actively maintained; 10 read + 6 write tools; documented auth tiers (anonymous ~10 req/min, client-credential 60–100 req/min, username/password for writes); Safe Mode, duplicate detection, bot-disclosure features; Responsible Builder Policy compliance noted.
- **Reliability:** High for capabilities (matches our live tool inventory); Medium for advertised anonymous rate tier (contradicted in practice by S2's 403 — contradiction called out rather than resolved silently).
- **Influence:** architecture §8 comparison; keep-and-pin recommendation.

## S4 — adhikasp/mcp-reddit (alternative)

- **Source:** https://github.com/adhikasp/mcp-reddit
- **Type:** source code / repository documentation.
- **Key findings:** Python, MIT, 413★ but only 16 commits and no releases; minimal tool surface (hot-thread fetching, post retrieval); pagination/search/rate limits undocumented; read-only by design.
- **Reliability:** High (repo speaks for itself). **Verified.**
- **Influence:** rejected as replacement (architecture §8): less capable and less maintained than the configured server despite higher star count.

## S5 — Robinhood Agentic Trading (official)

- **Sources:** https://robinhood.com/us/en/support/articles/agentic-trading-overview/ · https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/
- **Type:** official vendor documentation (primary).
- **Key findings:** official MCP at `agent.robinhood.com/mcp/trading`, OAuth; reads span all accounts, trades confined to a dedicated "Agentic account"; **no paper/simulated mode**; agents *can* be configured to execute without per-trade confirmation; user remains responsible for all agent trades; no published rate limits; Robinhood's own risk language: AI agents "can make errors, misinterpret instructions, act on incomplete or outdated information."
- **Reliability:** High. **Verified** (vendor primary source).
- **Influence:** architecture §6 (no headless production dependency; interactive read-only use), §12–13 (we must build our own paper mode and refuse no-confirmation execution), decision criteria 4 and 12.

## S6 — MCP security: prompt injection and tool poisoning

- **Sources:** https://www.practical-devsecops.com/mcp-security-vulnerabilities/ · https://www.truefoundry.com/blog/blog-mcp-tool-poisoning-gateway-defense (CVE-2025-54136) · https://arxiv.org/html/2603.22489v1 (MCP threat modeling)
- **Type:** security vendor analyses + peer-review-track preprint (secondary, corroborating each other).
- **Key findings:** documented real incidents — Supabase/Cursor prompt-injection data exfiltration (mid-2025), first malicious MCP package in the wild (Sept 2025); tool poisoning = supply-chain attack on agent context; mitigations: least-privilege tool scoping, version pinning, treating MCP servers as third-party code, gateway/attestation defenses.
- **Reliability:** Medium-High (multiple independent sources agree; incidents publicly documented). **Verified as reported incidents;** specific CVE details not independently reproduced.
- **Influence:** architecture §11 threat table; tool-policy fail-closed design; version pinning (Plan 0.7).

## S7 — Backtesting framework landscape

- **Sources:** https://python.financial/ · https://autotradelab.com/blog/backtrader-vs-nautilusttrader-vs-vectorbt-vs-zipline-reloaded · https://bullalert.ai/blog/best-python-backtest-engines-2026/ · https://alphagaindaily.com/en/blog/backtrader-vs-zipline-vs-quantconnect
- **Type:** practitioner comparison articles (secondary; multiple independent sources, consistent picture).
- **Key findings:** Backtrader in long-term-maintenance stall since ~2023; zipline-reloaded maintained but maintenance-only (Pipeline API still uniquely good for factor research); VectorBT fastest for vectorized research; NautilusTrader/LEAN are the production-grade engines; consensus that heavy engines are overkill for research-stage projects.
- **Reliability:** Medium (opinionated secondary sources, but independently consistent). **Corroborated, partially anecdotal.**
- **Influence:** architecture §2/§9 Option D rejection; VectorBT as optional phase-3 adjunct (Plan 3.2); build-own-ledger decision (§13).

## S8 — Human-in-the-loop approval patterns

- **Sources:** https://developers.cloudflare.com/agents/concepts/agentic-patterns/human-in-the-loop/ · https://matheuspalma.com/blog/human-in-the-loop-llm-tool-approval-production · https://arxiv.org/pdf/2605.19337 ("Agentic Trading: When LLM Agents Meet Financial Markets")
- **Type:** vendor pattern documentation + practitioner writeup + academic preprint.
- **Key findings:** pre-execution approval for consequential actions; approval TTLs (≈24h for sensitive operations); **pin the approved payload by hash and refuse execution on drift** (documented production bug class: arguments mutating between approval and execution); audit trails; escalation triggers.
- **Reliability:** Medium-High (independent sources converge). **Verified as documented practice.**
- **Influence:** architecture §12 approval design (hash pinning, TTL, drift invalidation), decision criterion 12.

## S9 — Reddit/WSB sentiment predictive value (academic)

- **Sources:** https://arxiv.org/pdf/2508.02089 (social-media sentiment for prediction) · https://dl.acm.org/doi/10.1145/3614419.3643993 ("Highly Regarded Investors? Mining Predictive Value from the Collective Intelligence of Reddit's WallStreetBets", ACM WebSci 2024) · https://arxiv.org/pdf/2507.22922 (ChatGPT-annotated Reddit sentiment) · https://alphaarchitect.com/wallstreetbets/
- **Type:** peer-reviewed paper + preprints + quant-research blog summarizing the literature.
- **Key findings:** evidence is **mixed and regime-dependent** — some studies find short-horizon signal (notably in bull markets); others find weak correlation or that price predicts sentiment rather than the reverse; WSB content is anonymous, speculative, hype-prone, and post-GameStop inflows likely degraded signal quality; reflexivity (followers acting on signals) distorts measured predictive value.
- **Reliability:** Medium-High (academic, but findings conflict — the *conflict itself* is the finding). **Verified that the literature is mixed;** no single directional claim treated as fact.
- **Influence:** the ≤10% sentiment cap (architecture §14–15), with/without-Reddit evaluation split (§18), manipulation detectors (§15).

## S10 — Reddit community-practice research (attempted via MCP)

- **Source:** intended: r/algotrading, r/ClaudeAI, r/mcp, r/quant, r/LocalLLaMA via configured Reddit MCP.
- **Type:** would be Low reliability (sentiment/anecdote) by policy.
- **Key findings:** **not retrievable this session** — blocked by S2's 403; targeted web searches for indexed Reddit threads on LLM-trading-bot experience returned no directly usable threads. No Reddit-anecdote claims are therefore made in the architecture; where the requirement asked "how are developers currently doing X," the answer draws on S5–S8 primary/secondary sources instead.
- **Reliability:** N/A (gap). **Documented as a gap, not silently filled.**
- **Influence:** research-method limitation; Plan 0.6 unblocks a follow-up pass; architecture treats community anecdotes as absent rather than assumed.

## S11 — Reddit Responsible Builder Policy

- **Source:** https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy (referenced by the configured MCP server's own instructions).
- **Type:** official platform policy (primary).
- **Key findings:** retrieved Reddit data must not train AI models without approval, must not be resold/redistributed; no de-anonymization; no vote/karma manipulation; bots must disclose automation.
- **Reliability:** High. **Verified.**
- **Influence:** architecture §23 risk 5; collection design stores data locally for analysis only, no redistribution.

---

## Summary of how sources shaped the recommendation

| Decision | Driven by |
|---|---|
| Hybrid Option C (Python production, Claude Code interactive) | S1, S5, S7, S8 |
| Robinhood MCP read-only, no headless production dependency, refuse no-confirmation execution | S1, S5 |
| Keep jordanburke MCP, pin version, add read-only creds; no custom MCP | S1, S2, S3, S4 |
| Reddit sentiment ≤10% + with/without split + manipulation detectors | S9, S11 |
| Hash-pinned, TTL'd human approval; separate execution profile | S8, S5, S6 |
| Own SQLite paper ledger; VectorBT later; no heavy framework | S5 (no paper mode), S7 |
| Fail-closed tool policy, version pinning, injection filtering | S6, S1, S2 |

*All URLs accessed 2026-07-11. Claims from secondary sources are labeled; contradictions (S3 vs S2 on anonymous access; internal conflict within S9) are surfaced rather than resolved silently.*
