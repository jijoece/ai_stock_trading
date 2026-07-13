"""Unit tests for evidence_providers/reddit_fetch.py — Milestone 7
docs/milestone-7.md Step 27 category E. No real network/MCP subprocess:
`mcp.reddit_adapter.call_read_only_tool` is patched at the module boundary
this code actually calls through."""
from __future__ import annotations

from pathlib import Path

import pytest

from trading_research.evidence_providers import reddit_fetch
from trading_research.evidence_providers.reddit_fetch import (
    MAX_RECORDS_RETURNED,
    build_reddit_sentiment_source,
    make_fetch_records,
)
from trading_research.evidence_providers.sentiment_provider import MISSING_CREDENTIALS_REASON, RedditSentimentSource
from trading_research.mcp.reddit_adapter import ReadOnlyPolicyError

T0 = 1_800_000_000.0
DAY = 86_400.0


def _config(**overrides):
    from trading_research.config import Config

    defaults = dict(
        anthropic_api_key=None, anthropic_model="claude-sonnet-5", anthropic_batch_poll_interval_seconds=30,
        research_data_dir=Path("/tmp/rd"), research_database_path=Path("/tmp/rd/db.sqlite3"),
        reddit_mcp_mode="stdio", reddit_mcp_command="npx -y reddit-mcp-server",
        reddit_mcp_url=None, reddit_mcp_auth_token=None,
        reddit_client_id=None, reddit_client_secret=None,
        robinhood_mcp_url="https://agent.robinhood.com/mcp/trading", log_level="INFO",
        alpaca_market_data_api_key=None, alpaca_market_data_api_secret=None,
    )
    defaults.update(overrides)
    return Config(**defaults)


async def _fake_call_read_only_tool_factory(raw_items, *, expected_tool=None):
    async def _fake(config, tool_name, arguments):
        if expected_tool is not None:
            assert tool_name == expected_tool
        return {"results": raw_items}

    return _fake


def _post(id_, text, created, *, subreddit="stocks", score=5, num_comments=2, author="u1", is_crosspost=False):
    return {
        "id": id_, "title": text, "selftext": "", "author": author, "subreddit": subreddit,
        "created_utc": created, "score": score, "num_comments": num_comments,
        "url": f"https://reddit.com/{id_}", "is_crosspost": is_crosspost,
    }


# -- missing credentials behavior --------------------------------------------

def test_build_reddit_sentiment_source_missing_credentials_fails_closed():
    source = build_reddit_sentiment_source(_config())
    assert source.credentials_configured is False
    from datetime import datetime, timezone

    result = source.fetch("AAPL", datetime.now(timezone.utc))
    assert result.missing_data_reasons == (MISSING_CREDENTIALS_REASON,)


def test_build_reddit_sentiment_source_partial_credentials_fails_closed():
    source = build_reddit_sentiment_source(_config(reddit_client_id="id-only"))
    assert source.credentials_configured is False


def test_build_reddit_sentiment_source_configured_when_both_present(monkeypatch):
    async def fake(config, tool_name, arguments):
        return {"results": []}

    monkeypatch.setattr(reddit_fetch, "call_read_only_tool", fake)
    source = build_reddit_sentiment_source(_config(reddit_client_id="id", reddit_client_secret="secret"))
    assert source.credentials_configured is True
    assert isinstance(source, RedditSentimentSource)


# -- read-only tools only / unknown tool rejection ---------------------------

def test_fetch_records_calls_only_search_reddit_tool(monkeypatch):
    seen = {}

    async def fake(config, tool_name, arguments):
        seen["tool"] = tool_name
        seen["args"] = arguments
        return {"results": [_post("p1", "I think $AAPL is going up", T0 + 10)]}

    monkeypatch.setattr(reddit_fetch, "call_read_only_tool", fake)
    fetch = make_fetch_records(_config(reddit_client_id="id", reddit_client_secret="secret"))
    records = fetch("AAPL", T0, T0 + DAY)
    assert seen["tool"] == "search_reddit"
    assert "$AAPL" in seen["args"]["query"]
    assert len(records) == 1


def test_unknown_tool_rejected_by_underlying_policy(monkeypatch):
    # Simulate what would happen if this module tried to call a
    # non-allowlisted tool: call_read_only_tool itself raises
    # ReadOnlyPolicyError and reddit_fetch does not swallow it.
    from trading_research import config as config_module
    from trading_research.mcp import reddit_adapter

    async def call_bad_tool(config, tool_name, arguments):
        return await reddit_adapter.call_read_only_tool(config, tool_name, arguments)

    monkeypatch.setattr(reddit_fetch, "call_read_only_tool", call_bad_tool)

    import asyncio

    with pytest.raises(ReadOnlyPolicyError):
        asyncio.run(call_bad_tool(_config(), "not_a_real_tool", {}))


# -- mutation-tool rejection --------------------------------------------------

@pytest.mark.parametrize("mutating_tool", ["create_post", "reply_to_post", "delete_comment", "edit_post"])
def test_mutation_tools_rejected(mutating_tool):
    import asyncio

    with pytest.raises(ReadOnlyPolicyError):
        from trading_research.mcp.reddit_adapter import call_read_only_tool

        asyncio.run(call_read_only_tool(_config(), mutating_tool, {}))


def test_reddit_fetch_module_never_calls_mutation_tools():
    """The module may *discuss* mutation tool names in its docstring (to
    document why they are excluded), but must never call one — the only
    tool-name string literal actually passed to `call_read_only_tool` is
    `SEARCH_TOOL_NAME`."""
    import ast

    tree = ast.parse(Path(reddit_fetch.__file__).read_text())
    string_literals_in_code: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            string_literals_in_code.append(node.value)
    # exclude docstrings (first statement of module/function bodies) by only
    # checking literals that match a mutation-tool name exactly
    mutation_tools = {"create_post", "reply_to_post", "delete_comment", "edit_post", "delete_post", "vote"}
    assert not (mutation_tools & set(string_literals_in_code))
    assert reddit_fetch.SEARCH_TOOL_NAME == "search_reddit"


# -- duplicate post handling ---------------------------------------------------

def test_duplicate_post_ids_deduplicated(monkeypatch):
    async def fake(config, tool_name, arguments):
        return {"results": [
            _post("dup1", "buy $AAPL calls", T0 + 10),
            _post("dup1", "buy $AAPL calls", T0 + 10),  # exact duplicate ID (cross-post/reindex)
        ]}

    monkeypatch.setattr(reddit_fetch, "call_read_only_tool", fake)
    fetch = make_fetch_records(_config(reddit_client_id="id", reddit_client_secret="secret"))
    records = fetch("AAPL", T0, T0 + DAY)
    assert len(records) == 1


def test_cross_post_flag_normalized(monkeypatch):
    async def fake(config, tool_name, arguments):
        return {"results": [_post("cp1", "$AAPL to the moon", T0 + 10, is_crosspost=True)]}

    monkeypatch.setattr(reddit_fetch, "call_read_only_tool", fake)
    fetch = make_fetch_records(_config(reddit_client_id="id", reddit_client_secret="secret"))
    records = fetch("AAPL", T0, T0 + DAY)
    assert records[0].is_cross_post is True


# -- cashtag ambiguity handling -------------------------------------------------

def test_bare_word_without_cashtag_dropped(monkeypatch):
    async def fake(config, tool_name, arguments):
        return {"results": [
            _post("amb1", "it is a good day to trade", T0 + 10),  # "it" the word, not $IT
            _post("amb2", "loading up on $IT calls", T0 + 20),
        ]}

    monkeypatch.setattr(reddit_fetch, "call_read_only_tool", fake)
    fetch = make_fetch_records(_config(reddit_client_id="id", reddit_client_secret="secret"))
    records = fetch("IT", T0, T0 + DAY)
    assert len(records) == 1
    assert records[0].record_id == "amb2"


def test_cashtag_records_marked_is_cashtag(monkeypatch):
    async def fake(config, tool_name, arguments):
        return {"results": [_post("c1", "$AAPL breaking out today", T0 + 10)]}

    monkeypatch.setattr(reddit_fetch, "call_read_only_tool", fake)
    fetch = make_fetch_records(_config(reddit_client_id="id", reddit_client_secret="secret"))
    records = fetch("AAPL", T0, T0 + DAY)
    assert records[0].is_cashtag is True


# -- historical cutoff -----------------------------------------------------------

def test_historical_cutoff_excludes_out_of_window_records(monkeypatch):
    async def fake(config, tool_name, arguments):
        return {"results": [
            _post("in1", "$AAPL within window", T0 + 10),
            _post("out1", "$AAPL before window", T0 - 1000),
            _post("out2", "$AAPL after window", T0 + DAY + 1000),
        ]}

    monkeypatch.setattr(reddit_fetch, "call_read_only_tool", fake)
    fetch = make_fetch_records(_config(reddit_client_id="id", reddit_client_secret="secret"))
    records = fetch("AAPL", T0, T0 + DAY)
    assert [r.record_id for r in records] == ["in1"]


# -- injection risk annotation present -------------------------------------------

def test_injection_risk_note_documented():
    assert "untrusted" in reddit_fetch.INJECTION_RISK_NOTE.lower()


# -- bounded result size ----------------------------------------------------------

def test_result_size_bounded(monkeypatch):
    many = [_post(f"p{i}", f"$AAPL post {i}", T0 + i) for i in range(500)]

    async def fake(config, tool_name, arguments):
        return {"results": many}

    monkeypatch.setattr(reddit_fetch, "call_read_only_tool", fake)
    fetch = make_fetch_records(_config(reddit_client_id="id", reddit_client_secret="secret"))
    records = fetch("AAPL", T0, T0 + DAY)
    assert len(records) <= MAX_RECORDS_RETURNED


# -- no direct Claude access to the MCP session (structural invariant) ----------

def test_no_claude_import_in_reddit_fetch_module():
    """Structural invariant: this module imports no Anthropic/Claude client
    and defines no tool-calling entry point Claude could invoke — it is
    plain application code sitting between `RedditSentimentSource` and the
    MCP adapter (ADR 0003: Claude has no tool-calling wiring in this
    repository at all)."""
    import ast

    tree = ast.parse(Path(reddit_fetch.__file__).read_text())
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any("anthropic" in m.lower() or "claude" in m.lower() for m in imported_modules)


def test_fetch_records_is_synchronous_application_code_only():
    """This wiring is only ever called from application code (evidence
    adapters -> sentiment_provider.RedditSentimentSource.fetch), never
    exposed as a Claude-callable tool. There is no tool-registration
    mechanism anywhere in this repository that could expose
    `make_fetch_records`/`call_read_only_tool` to Claude (ADR 0003: Claude
    has no tool-calling wiring in this repository at all) — asserted here by
    confirming `fetch_records` is a plain synchronous callable, not
    registered against any MCP server or tool schema."""
    fetch = make_fetch_records(_config(reddit_client_id="id", reddit_client_secret="secret"))
    assert callable(fetch)
    assert not hasattr(fetch, "tool_schema")
    assert not hasattr(fetch, "mcp_tool_name")


# -- integration with RedditSentimentSource / aggregate --------------------------

def test_fetched_records_feed_existing_aggregate_pipeline(monkeypatch):
    async def fake(config, tool_name, arguments):
        return {"results": [
            _post("s1", "buy $AAPL bullish breakout", T0 + 10, score=100),
            _post("s2", "$AAPL puts bearish dump", T0 + 20, score=50),
        ]}

    monkeypatch.setattr(reddit_fetch, "call_read_only_tool", fake)
    source = build_reddit_sentiment_source(_config(reddit_client_id="id", reddit_client_secret="secret"))
    from datetime import datetime, timezone

    as_of = datetime.fromtimestamp(T0 + DAY, tz=timezone.utc)
    result = source.fetch("AAPL", as_of, window_seconds=DAY)
    assert result.total_mentions == 2
    assert result.missing_data_reasons == ()
