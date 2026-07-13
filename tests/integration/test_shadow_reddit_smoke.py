"""Opt-in real Reddit-sentiment smoke test (docs/milestone-7.md Step 28,
"Real Reddit smoke"). Gated on BOTH `RUN_REDDIT_SENTIMENT_TESTS=true` AND
real `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` credentials being present.

At the time this test was authored, `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET`
are absent in this environment (confirmed via `.env` presence check, boolean
only) — this test is therefore expected to SKIP, not run, in that state.

Uses only the existing read-only wiring
(`evidence_providers/reddit_fetch.py::build_reddit_sentiment_source`, which
itself only ever calls the allowlisted `search_reddit` MCP tool via
`mcp/reddit_adapter.py::call_read_only_tool` — no mutation tool is reachable
from this test or anything it calls).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.reddit_sentiment_real

_RUN_FLAG = os.environ.get("RUN_REDDIT_SENTIMENT_TESTS", "").strip().lower() == "true"
_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")

_SKIP_REASON = (
    "opt-in real Reddit-sentiment smoke test: set RUN_REDDIT_SENTIMENT_TESTS=true AND real "
    "REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET to run it — both credentials are absent in this "
    "environment, so this test is environmentally pending, not implemented-and-broken"
)


@pytest.mark.skipif(not (_RUN_FLAG and _CLIENT_ID and _CLIENT_SECRET), reason=_SKIP_REASON)
def test_real_reddit_sentiment_read_only_bounded():
    from trading_research.config import load_config
    from trading_research.evidence_providers.reddit_fetch import build_reddit_sentiment_source

    config = load_config()
    assert config.reddit_client_id and config.reddit_client_secret, "credentials must be loaded from the real environment"

    source = build_reddit_sentiment_source(config)
    assert source.credentials_configured is True

    now = datetime.now(timezone.utc)
    result = source.fetch("AAPL", now, window_seconds=86_400.0)

    # --- Read-only call succeeded (no mutation tool is reachable at all —
    # structurally guaranteed by call_read_only_tool's own allowlist, not
    # re-verified here since this test never imports a mutation path).
    assert isinstance(result.records, tuple)

    # --- Bounded results + timestamps + normalized sentiment shape.
    assert len(result.records) <= 200  # MAX_RECORDS_RETURNED
    for record in result.records:
        assert record.created_at <= now, "no future-dated Reddit record may be returned"

    # --- No mutation: the fetch call path used is read-only by construction.
    print(
        f"Real Reddit-sentiment smoke result: symbol=AAPL record_count={len(result.records)} "
        f"net_sentiment={result.net_sentiment} total_mentions={result.total_mentions} "
        f"unique_authors={result.unique_authors} missing_data_reasons={result.missing_data_reasons}"
    )
