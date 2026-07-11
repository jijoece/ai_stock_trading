# Deep Dive Scratchpad — AI-Driven Stock Trading Architecture

## Investigation Question
Safest, most effective, most maintainable architecture for an AI-assisted stock
research → paper-trading → (later) human-approved trading app on:
Claude Code + Robinhood MCP + Reddit MCP + deterministic Python, local-first.
Deliverables: docs/AI-Driven-Stock-Trading-Architecture.md,
docs/AI-Stock-Trading-Implementation-Plan.md, docs/AI-Stock-Trading-Research-Sources.md.

## Mode
agent-architecture + mcp-analysis + implementation-plan

## Current Status
Started 2026-07-11 09:50 UTC. Repo + MCP inspection done. Reddit/web research next.

UPDATE 2026-07-11 (implementation phase, paused mid-build at user request to checkpoint):
Moved from pure research into full pipeline implementation per the detailed
`/deep-dive` implementation spec (safe local-first research pipeline: Claude Code +
Batch API + Reddit MCP + Robinhood MCP inventory + Python deterministic processing,
no live trading). See "Implementation Progress" section below for exact file-level
status. Resuming from there — do not re-scaffold what's already listed as done.

Environment facts discovered during implementation setup:
- No pyproject.toml/requirements.txt/package.json existed before this session — repo
  was scripts/ + SKILL.md + CLAUDE.md + README.md only. Now has pyproject.toml.
- `anthropic` (0.116.0), `mcp` (1.28.1), `jsonschema` (4.26.0), `PyYAML` (6.0.3),
  `python-dotenv` (1.2.2) already present in the global Python 3.14 env (installed
  during this session, no venv created yet — TODO: consider venv per CLAUDE
  instructions' dev workflow, currently using system python3 to move faster).
- `npx` available (v11.16.0) → real STDIO MCP client for `npx -y reddit-mcp-server`
  is feasible, not just a stub.
- `ANTHROPIC_API_KEY` is NOT set in this environment → Batch API submission will be
  implemented + unit-tested with mocks, but cannot be executed live from here.
  Must be clearly labeled "implemented but not externally tested / blocked by
  missing credentials" in the final response, per spec's acceptance criteria.
- Noticed the user's IDE has `docs/batch_processing.md` open — that file does not
  exist and is not part of the planned doc set (planned: docs/AI-Driven-Stock-
  Trading-Architecture.md, docs/AI-Stock-Trading-Implementation-Plan.md,
  docs/AI-Stock-Trading-Research-Sources.md, docs/RESEARCH-PIPELINE.md,
  docs/SECURITY.md per spec §19). Not creating it unless asked — flagging only.

## Implementation Progress (file-level checklist)

DONE:
- pyproject.toml (deps: anthropic, mcp, jsonschema, PyYAML, python-dotenv; pytest dev extra)
- .env.example (all vars from spec §5, no real secrets)
- .gitignore updated (venv, pycache, .env, generated data/research-input/research-results content, .DS_Store)
- Directory scaffold: src/trading_research/{models,mcp,collection,batches,processing,storage,reporting}/,
  config/, schemas/, prompts/, docs/, tests/{unit,integration,fixtures}/, data/, research-input/,
  research-results/{raw,validated,failed,retries}/ (all with .gitkeep where needed)
- src/trading_research/__init__.py
- src/trading_research/config.py — typed Config dataclass, load_config(), ConfigError,
  require_anthropic_key(), HTTPS validation for REDDIT_MCP_URL when mode=http
- src/trading_research/logging_config.py — RedactingFormatter + JsonRedactingFormatter,
  register_secret()/redact() (regex + verbatim secret scrubbing), get_logger()
- src/trading_research/models/{capability_models,source_models,batch_models,research_models}.py
  — dataclasses matching the 4 JSON schemas below, incl. make_custom_id(run_id, workstream_id, version)
- schemas/{batch_workstream_result,mcp_capability_inventory,reddit_record,consolidated_research}.schema.json
  — draft-07, additionalProperties:false where the spec's JSON shape is closed
- src/trading_research/storage/{migrations.py (DDL, all tables from spec §13),
  database.py (connect/session), repositories.py (plain functions, not full ORM)}
- config/tool_policy.yaml — explicit allowlist + denylist_patterns for robinhood and
  reddit servers, fail-closed unknown fallback documented in comments

IN PROGRESS / NEXT (resume here):
- src/trading_research/mcp/tool_classifier.py — deterministic classifier reading
  config/tool_policy.yaml (denylist pattern match > allowlist exact match > read-name
  heuristic (explain only, does not grant access) > unknown=prohibited fail-closed)
- src/trading_research/mcp/capability_inventory.py — builds McpCapabilityInventory for
  robinhood + reddit. Plan: Robinhood is HTTP+OAuth hosted MCP with no sandbox — no
  programmatic OAuth flow available in this sandbox, so inventory will be built from
  the REAL tool list already confirmed live in this Claude Code session (documented
  in "Known Facts" above, captured 2026-07-11) run through the same classifier code,
  labeled as a dated live-session snapshot rather than an automated fetch. Reddit
  inventory: build a real STDIO MCP client (using `mcp` python SDK, subprocess to
  `npx -y reddit-mcp-server`) and call list_tools() for real — this one CAN be
  automated end-to-end.
- src/trading_research/mcp/reddit_adapter.py — real stdio client wrapper (Mode A);
  Mode B (remote HTTPS) stub with HTTPS/timeout/retry validation per spec §7.
- src/trading_research/collection/{reddit_collector.py, reddit_normalizer.py,
  ticker_extractor.py, prompt_injection_filter.py} — prompt_injection_filter should
  implement the exact pattern list from spec §8.
- Then: run a REAL read-only Reddit collection (a few subreddits/topics from
  config/reddit_sources.yaml defaults) to get genuine data for later batch input.
- Then: batches/ (client, request_builder, workstreams, submit, status, download,
  retry), processing/ (parser, validator, dedup, conflict_detector, evidence_classifier,
  consolidator), reporting/ (packet_builder, markdown_exporter, source_catalog),
  cli.py wiring all subcommands, scripts/*.py thin wrappers, config/{research_workstreams.yaml,
  reddit_sources.yaml, scoring_policy.yaml}, prompts/*.md templates.
- Tests (pytest, unit + integration w/ mocks, fixtures dir).
- Attempt real MCP inventory execution + real Reddit collection execution; batch
  submission will be coded+unit-tested only (no API key available) unless the user
  supplies ANTHROPIC_API_KEY.
- Final docs (5 files per spec §19) — written last, synthesizing everything above
  plus the mcp-analysis/repo-analysis findings already in "Known Facts".
- Final response per deep-dive skill's report format + spec §28 required structure
  (summary, repo assessment, files added/modified, run commands, test results,
  limitations, security guarantees, no-live-trading confirmation, next phase).

## Key Finding — Reddit MCP anonymous mode is BLOCKED in this environment (2026-07-11)
Built a real MCP stdio client (src/trading_research/mcp/reddit_adapter.py, using the
official `mcp` Python SDK against `npx -y reddit-mcp-server`) and exercised it live:
- `test_reddit_mcp_server` (local health check, no Reddit API call) → succeeds:
  "Reddit Client: ✓ Initialized", "Write Access: ✗ Read-only mode", version 1.5.1.
- Every tool that actually calls the Reddit API — `browse_subreddit`, `get_subreddit_info`,
  `get_top_posts`, `get_trending_subreddits`, `search_reddit` — fails with
  `HTTP 403` under anonymous mode (no REDDIT_CLIENT_ID/SECRET configured at project
  scope, matching what was already noted in "Known Facts").
- This directly overturns the earlier Assumption ("reddit-mcp-server anonymous read
  works"). CONFIRMED instead: anonymous/unauthenticated read access to Reddit's API
  is blocked (at least from this environment's network), and a real registered Reddit
  app (REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET, script or installed app type) is
  required for the configured reddit-mcp-server to retrieve any content — the
  read/write distinction is about mutating Reddit, not about whether credentials are
  needed at all.
- Impact on architecture recommendation: an "anonymous read-only Reddit MCP" is not
  a reliable zero-credential integration path as currently configured. Phase 1
  should register a read-only Reddit app (client id/secret only, no username/password
  needed since we never write) and treat REDDIT_CLIENT_ID/SECRET as required
  Reddit-side config alongside REDDIT_MCP_* vars — update .env.example accordingly
  if the user wants live Reddit collection to actually work.
- Consequence for this implementation run: collection/reddit_collector.py is fully
  implemented and was executed for real against the live MCP, but returned zero
  records for every configured subreddit/topic due to the 403s above. This is
  labeled in the final response as "implemented and executed, blocked by missing
  Reddit app credentials" — not silently faked with placeholder data.

## Test/dependency install notes
`pip3 install anthropic mcp jsonschema pyyaml python-dotenv` already run successfully
in this session (global python3.14, not venv) — see Environment facts above.

## Known Facts (verified locally)
- Repo = clone of https://github.com/Oft3r/agentic-trading-desk (MIT). 4 commits.
  Contents: CLAUDE.md, SKILL.md (trading skill), README.md, scripts/{indicators,score,macro_pillar}.py
  (stdlib-only, deterministic; EMA/RSI-Wilder/MACD/TRIX/Bollinger; three-pillar scoring -6..+6).
- No .mcp.json in repo. No tests. No DB. No paper-trading ledger. No docs/ dir.
- .claude/settings.local.json: permission allowlist = READ-ONLY Robinhood tools only
  (get_equity_quotes/fundamentals/historicals/positions/accounts, earnings_results) + python3 + some web.
  No place_/cancel_/review_ order tools in allowlist → permission layer currently gates writes.
- .claude/skills: deep-dive, run-agentic-trading-desk.
- MCP servers (project scope in ~/.claude.json "projects"):
  - robinhood-trading: HTTP https://agent.robinhood.com/mcp/trading (OFFICIAL Robinhood hosted MCP).
  - reddit: `npx reddit-mcp-server` (= jordanburke/reddit-mcp-server, TypeScript, npm), env {} at project scope.
- STALE user-scope reddit entry in ~/.claude.json → uv run of ~/workspace/reddit-mcp (dir DELETED),
  env contains plaintext REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD → credential hygiene issue.
- Robinhood MCP tool inventory (confirmed from live session tool list):
  READ: get_accounts, get_earnings_calendar, get_earnings_results, get_equity_fundamentals,
  get_equity_historicals, get_equity_orders, get_equity_positions, get_equity_quotes,
  get_equity_tax_lots, get_equity_technical_indicators, get_equity_tradability,
  get_index_quotes, get_indexes, get_option_chains/historicals/instruments/orders/positions/quotes,
  get_option_watchlist, get_pnl_trade_history, get_popular_watchlists, get_portfolio,
  get_realized_pnl, get_scans, get_watchlist_items, get_watchlists, search.
  WRITE/ORDER: place_equity_order, place_option_order, cancel_equity_order, cancel_option_order,
  review_equity_order, review_option_order (preview), add/remove watchlist (+option), create/update
  watchlist, follow/unfollow_watchlist, create_scan, run_scan, update_scan_config/filters.
  → No paper-trading tools. Hosted server → write tools cannot be removed server-side by us;
  restriction must be client-side (permissions deny/allowlist).
- Reddit MCP tool inventory (confirmed): search_reddit (subreddit scoping, sorts incl. relevance/top/new,
  time_filter hour..all, pagination via `after`, limit≤100), browse_subreddit (hot/new/rising/top/controversial),
  get_top_posts, get_reddit_post, get_post_comments (limit≤500, sorts), get_more_comments,
  get_subreddit_info/rules, get_user_info/posts/comments, get_trending_subreddits, get_post_flairs,
  get_me/get_my_overview/get_my_saved, test_reddit_mcp_server; WRITE: create_post, reply_to_post,
  edit_post/comment, delete_post/comment → NOT read-only as configured.
- Session context: SKILL.md notes Polymarket previously rejected due to prompt-injection risk;
  Investing.com chosen as macro source.

## Assumptions
- Robinhood official MCP auth = OAuth to user's account; no paper/sandbox mode (verify via docs).
- reddit-mcp-server anonymous read works (tool descriptions say read tools work anonymously).
- Robinhood MCP rate limits undocumented/not discoverable.

## Unknowns / To verify
- Robinhood MCP official docs: scopes, read-only option, rate limits. → web
- jordanburke/reddit-mcp-server vs adhikasp/mcp-reddit: maintenance, license, output structure. → GitHub
- Community experience: Claude Code as trading orchestrator, LLM trading bot failures,
  reddit-sentiment efficacy, HITL approval patterns. → Reddit research

## Hypotheses
- H1: Hybrid (Option C): Python deterministic core + Claude Code as dev/manual interface +
  bounded API reasoning is the recommended design.
- H2: Configured reddit-mcp-server suffices for interactive research, but deterministic mention
  analytics need direct Reddit API/PRAW in Python (bulk counts through an LLM is the wrong layer).
- H3: Multi-agent not justified beyond a few bounded roles; single orchestrator + deterministic tools.

## Sources to Check
- GitHub: jordanburke/reddit-mcp-server, adhikasp/mcp-reddit, Oft3r/agentic-trading-desk,
  backtrader / vectorbt / zipline-reloaded / LEAN activity.
- Official: Robinhood MCP docs; Reddit Responsible Builder Policy (cited in MCP instructions).
- Reddit: r/algotrading, r/ClaudeAI, r/mcp, r/quant, r/LocalLLaMA.

## Evidence Collected
(appended as gathered)

## Key Findings
- Both configured MCPs expose write tools; safety must be enforced by the Claude Code permission
  allowlist (already partially in place) + no write creds for reddit at project scope.

## Risks / Caveats
- Reddit content = untrusted (injection/manipulation). Sentiment, not fact.

## Open Questions
- (tracked in Unknowns)

## Final Summary Draft
(pending)
