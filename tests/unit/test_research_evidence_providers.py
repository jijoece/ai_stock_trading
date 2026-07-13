"""Category B: evidence-provider tests (docs/milestone-5.md Step 20.B)."""
from __future__ import annotations

from datetime import datetime, timezone

from trading_research.research.evidence import EvidenceBundle, build_evidence_snapshot
from trading_research.research.fixtures import (
    FixtureFilingRiskProvider,
    FixtureFundamentalsProvider,
    FixtureNewsProvider,
    build_fixture_snapshot,
    fixture_deterministic_factors,
    fixture_sentiment_metrics,
    fixture_symbols,
    is_fixture_symbol,
)

NOW = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)


def test_fixture_symbols_are_deterministic_and_known():
    assert is_fixture_symbol("AAPL")
    assert not is_fixture_symbol("NOPE")
    assert set(fixture_symbols()) >= {"AAPL", "MSFT", "SHEL", "XXXX"}


def test_fundamentals_provider_missing_data_for_thin_symbol():
    bundle = FixtureFundamentalsProvider().fetch("XXXX", NOW)
    assert bundle.evidence_items == ()
    assert bundle.missing_data_reasons


def test_fundamentals_provider_has_data_for_known_symbol():
    bundle = FixtureFundamentalsProvider().fetch("AAPL", NOW)
    assert bundle.evidence_items
    assert not bundle.missing_data_reasons
    assert all(item.source_id == bundle.source_records[0].source_id for item in bundle.evidence_items)


def test_news_provider_source_provenance_has_all_four_timestamps():
    bundle = FixtureNewsProvider().fetch("AAPL", NOW)
    source = bundle.source_records[0]
    assert source.retrieved_at is not None
    assert source.published_at is not None
    assert source.effective_at is not None
    assert source.available_at is not None
    assert source.available_at <= NOW  # available before or at the snapshot's as_of


def test_filing_provider_returns_empty_bundle_not_error_when_no_fixture_risk_text():
    bundle = FixtureFilingRiskProvider().fetch("XXXX", NOW)
    assert bundle.evidence_items == ()
    assert bundle.source_records  # source record recorded even when empty, for provenance


def test_deterministic_factors_and_sentiment_metrics_are_symbol_specific():
    aapl_factors = fixture_deterministic_factors("AAPL")
    msft_factors = fixture_deterministic_factors("MSFT")
    assert aapl_factors != msft_factors
    assert fixture_sentiment_metrics("XXXX") == {}


def test_item_limit_enforced_across_providers_combined():
    snap = build_evidence_snapshot(
        "AAPL", NOW, deterministic_factors=fixture_deterministic_factors("AAPL"),
        sentiment_metrics=fixture_sentiment_metrics("AAPL"),
        providers=[FixtureFundamentalsProvider(), FixtureNewsProvider(), FixtureFilingRiskProvider()],
        portfolio_context_provider=None, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW,
        max_evidence_items=2,
    )
    assert len(snap.evidence_items) == 2


def test_provider_failure_recorded_as_missing_data_reason_not_exception():
    class _FailingProvider:
        def fetch(self, symbol, as_of):
            return EvidenceBundle(source_records=(), evidence_items=(), missing_data_reasons=("upstream provider unavailable",))

    snap = build_evidence_snapshot(
        "AAPL", NOW, deterministic_factors={}, sentiment_metrics={}, providers=[_FailingProvider()],
        portfolio_context_provider=None, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW,
    )
    assert "upstream provider unavailable" in snap.missing_data_reasons


def test_conflicting_values_from_two_providers_are_both_retained():
    class _OptimisticProvider:
        def fetch(self, symbol, as_of):
            from trading_research.research.models import EvidenceItem, SourceRecord

            source = SourceRecord(
                source_id="optimistic", source_type="news", provider="test", source_locator=None,
                retrieved_at=as_of, published_at=as_of, effective_at=as_of, available_at=as_of,
                content_hash="a", status="ok", is_stale=False, point_in_time_safe=True, error_code=None,
            )
            item = EvidenceItem(
                evidence_id="optimistic-1", source_id="optimistic", category="analyst", title="bullish take",
                summary="growth accelerating", normalized_values={"revenue_growth_yoy": 0.30}, as_of=as_of,
                confidence="medium", stale=False, conflict_group="revenue_growth_yoy",
            )
            return EvidenceBundle(source_records=(source,), evidence_items=(item,))

    class _PessimisticProvider:
        def fetch(self, symbol, as_of):
            from trading_research.research.models import EvidenceItem, SourceRecord

            source = SourceRecord(
                source_id="pessimistic", source_type="news", provider="test", source_locator=None,
                retrieved_at=as_of, published_at=as_of, effective_at=as_of, available_at=as_of,
                content_hash="b", status="ok", is_stale=False, point_in_time_safe=True, error_code=None,
            )
            item = EvidenceItem(
                evidence_id="pessimistic-1", source_id="pessimistic", category="analyst", title="bearish take",
                summary="growth decelerating", normalized_values={"revenue_growth_yoy": -0.10}, as_of=as_of,
                confidence="medium", stale=False, conflict_group="revenue_growth_yoy",
            )
            return EvidenceBundle(
                source_records=(source,), evidence_items=(item,), conflict_reasons=("revenue_growth_yoy: optimistic vs pessimistic analyst views",),
            )

    snap = build_evidence_snapshot(
        "AAPL", NOW, deterministic_factors={}, sentiment_metrics={},
        providers=[_OptimisticProvider(), _PessimisticProvider()], portfolio_context_provider=None,
        config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW,
    )
    assert len(snap.evidence_items) == 2
    assert snap.conflict_reasons
