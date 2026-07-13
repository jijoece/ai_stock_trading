"""Unit tests for evidence_providers/evidence_adapters.py's fail-closed
paths and portfolio_context.py — Milestone 6 docs/milestone-6.md Step 22
category A/G. No network."""
from __future__ import annotations

from datetime import datetime, timezone

from trading_research.evidence_providers.evidence_adapters import RealNewsEvidenceProvider, RealSentimentEvidenceProvider
from trading_research.evidence_providers.news_provider import UnconfiguredNewsProvider
from trading_research.evidence_providers.portfolio_context import LedgerPortfolioContextProvider
from trading_research.evidence_providers.sentiment_provider import RedditSentimentSource
from trading_research.paper.ledger import PaperLedger
from trading_research.storage.database import connect

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)


def test_unconfigured_news_provider_fails_closed_to_missing_data():
    provider = RealNewsEvidenceProvider(UnconfiguredNewsProvider())
    bundle = provider.fetch("AAPL", AS_OF)
    assert bundle.evidence_items == ()
    assert len(bundle.missing_data_reasons) == 1
    assert "not configured" in bundle.missing_data_reasons[0] or "unavailable" in bundle.missing_data_reasons[0]


def test_unconfigured_sentiment_provider_fails_closed_to_missing_data():
    source = RedditSentimentSource(credentials_configured=False)
    provider = RealSentimentEvidenceProvider(source)
    bundle = provider.fetch("AAPL", AS_OF)
    assert bundle.evidence_items == ()
    assert bundle.missing_data_reasons
    assert bundle.source_records[0].status == "missing"
    assert bundle.source_records[0].error_code == "reddit_unavailable"


def test_configured_sentiment_source_without_fetch_callable_still_fails_closed():
    source = RedditSentimentSource(credentials_configured=True, fetch_records=None)
    result = source.fetch("AAPL", AS_OF)
    assert result.net_sentiment is None
    assert result.missing_data_reasons


def test_portfolio_context_no_position_returns_zero_not_none(tmp_path):
    conn = connect(tmp_path / "ledger.sqlite3")
    ledger = PaperLedger(conn, starting_cash=50_000.0)
    provider = LedgerPortfolioContextProvider(ledger)
    context = provider.fetch("AAPL", AS_OF)
    assert context is not None
    assert context["existing_position_shares"] == 0
    assert context["available_settled_cash"] == 50_000.0
    assert "account" not in str(context).lower() or "account_id" not in context  # no account identifiers


def test_portfolio_context_reflects_existing_position(tmp_path):
    conn = connect(tmp_path / "ledger.sqlite3")
    ledger = PaperLedger(conn, starting_cash=50_000.0)
    ledger.submit_and_fill("AAPL", "buy", 10, bid=99.0, ask=101.0, idempotency_key="k1", now=AS_OF)
    provider = LedgerPortfolioContextProvider(ledger)
    context = provider.fetch("AAPL", AS_OF)
    assert context["existing_position_shares"] == 10
    assert context["open_position_count"] == 1
