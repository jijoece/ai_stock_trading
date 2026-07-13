"""Category A: evidence-model tests (docs/milestone-5.md Step 20.A)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_research.research.errors import EvidenceValidationError
from trading_research.research.evidence import build_evidence_snapshot, canonical_snapshot_payload, compute_snapshot_id
from trading_research.research.evidence_validation import validate_snapshot_preconditions
from trading_research.research.fixtures import (
    FixtureFilingRiskProvider,
    FixtureFundamentalsProvider,
    FixtureNewsProvider,
    build_fixture_snapshot,
)
from trading_research.research.models import EvidenceItem, EvidenceSnapshot, SourceRecord

NOW = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)


def _source(source_id="src-1", **overrides) -> SourceRecord:
    defaults = dict(
        source_id=source_id, source_type="fundamentals", provider="fixture", source_locator=None,
        retrieved_at=NOW, published_at=NOW, effective_at=NOW, available_at=NOW, content_hash="abc",
        status="ok", is_stale=False, point_in_time_safe=True, error_code=None,
    )
    defaults.update(overrides)
    return SourceRecord(**defaults)


def _item(evidence_id="ev-1", source_id="src-1", **overrides) -> EvidenceItem:
    defaults = dict(
        evidence_id=evidence_id, source_id=source_id, category="fundamentals", title="t", summary="s",
        normalized_values={"revenue_growth_yoy": 0.1}, as_of=NOW, confidence="high", stale=False, conflict_group=None,
    )
    defaults.update(overrides)
    return EvidenceItem(**defaults)


def test_source_record_requires_tz_aware_timestamps():
    with pytest.raises(EvidenceValidationError):
        SourceRecord(
            source_id="s", source_type="news", provider="fixture", source_locator=None,
            retrieved_at=datetime(2026, 1, 1), published_at=None, effective_at=None, available_at=None,
            content_hash="x", status="ok", is_stale=False, point_in_time_safe=True, error_code=None,
        )


def test_evidence_item_rejects_unknown_confidence():
    with pytest.raises(EvidenceValidationError):
        _item(confidence="super-high")


def test_snapshot_rejects_evidence_item_with_unknown_source_id():
    with pytest.raises(EvidenceValidationError):
        EvidenceSnapshot(
            snapshot_id="snap-x", symbol="AAPL", as_of=NOW, created_at=NOW,
            source_records=(_source(source_id="src-1"),),
            evidence_items=(_item(source_id="src-does-not-exist"),),
            deterministic_factors={}, sentiment_metrics={}, portfolio_context=None,
            missing_data_reasons=(), conflict_reasons=(), point_in_time_safe=True,
            config_hash="c" * 64, git_sha="deadbeef",
        )


def test_deterministic_snapshot_id_same_content_same_hash():
    snap1 = build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)
    snap2 = build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert snap1.snapshot_id == snap2.snapshot_id  # created_at differs, content doesn't


def test_snapshot_id_changes_when_content_changes():
    snap_aapl = build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)
    snap_msft = build_fixture_snapshot("MSFT", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)
    assert snap_aapl.snapshot_id != snap_msft.snapshot_id


def test_snapshot_id_matches_manual_recomputation():
    snap = build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)
    payload = canonical_snapshot_payload(
        symbol=snap.symbol, as_of=snap.as_of, source_records=snap.source_records, evidence_items=snap.evidence_items,
        deterministic_factors=snap.deterministic_factors, sentiment_metrics=snap.sentiment_metrics,
        portfolio_context=snap.portfolio_context, missing_data_reasons=snap.missing_data_reasons,
        conflict_reasons=snap.conflict_reasons, point_in_time_safe=snap.point_in_time_safe,
        config_hash=snap.config_hash, git_sha=snap.git_sha,
    )
    assert compute_snapshot_id(payload) == snap.snapshot_id


def test_point_in_time_safe_false_when_any_source_unsafe():
    snap = build_evidence_snapshot(
        "AAPL", NOW, deterministic_factors={}, sentiment_metrics={},
        providers=[FixtureFundamentalsProvider(), FixtureNewsProvider()],
        portfolio_context_provider=None, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW,
    )
    assert snap.point_in_time_safe is True

    class _UnsafeProvider:
        def fetch(self, symbol, as_of):
            from trading_research.research.evidence import EvidenceBundle

            return EvidenceBundle(source_records=(_source(source_id="unsafe-1", point_in_time_safe=False),), evidence_items=())

    unsafe_snap = build_evidence_snapshot(
        "AAPL", NOW, deterministic_factors={}, sentiment_metrics={},
        providers=[FixtureFundamentalsProvider(), _UnsafeProvider()],
        portfolio_context_provider=None, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW,
    )
    assert unsafe_snap.point_in_time_safe is False


def test_stale_evidence_is_explicit_not_silently_dropped():
    stale_item = _item(evidence_id="ev-stale", stale=True)
    snap = EvidenceSnapshot(
        snapshot_id="snap-y", symbol="AAPL", as_of=NOW, created_at=NOW, source_records=(_source(),),
        evidence_items=(stale_item,), deterministic_factors={}, sentiment_metrics={}, portfolio_context=None,
        missing_data_reasons=(), conflict_reasons=(), point_in_time_safe=True, config_hash="c" * 64, git_sha="sha1",
    )
    assert snap.evidence_by_id("ev-stale").stale is True


def test_conflicting_sources_retained_not_resolved():
    a = _item(evidence_id="ev-a", conflict_group="revenue-growth")
    b = _item(evidence_id="ev-b", conflict_group="revenue-growth", normalized_values={"revenue_growth_yoy": -0.1})
    snap = EvidenceSnapshot(
        snapshot_id="snap-z", symbol="AAPL", as_of=NOW, created_at=NOW, source_records=(_source(),),
        evidence_items=(a, b), deterministic_factors={}, sentiment_metrics={}, portfolio_context=None,
        missing_data_reasons=(), conflict_reasons=("revenue_growth_yoy disagreement between sources",),
        point_in_time_safe=True, config_hash="c" * 64, git_sha="sha1",
    )
    assert len(snap.evidence_items) == 2
    assert snap.conflict_reasons == ("revenue_growth_yoy disagreement between sources",)


def test_missing_publication_and_availability_time_allowed_but_explicit():
    source = _source(published_at=None, available_at=None)
    assert source.published_at is None and source.available_at is None


def test_snapshot_immutability_is_a_frozen_dataclass():
    snap = build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)
    with pytest.raises(AttributeError):
        snap.symbol = "MSFT"  # type: ignore[misc]


def test_no_cross_symbol_evidence_in_a_single_snapshot():
    aapl_snap = build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)
    assert all(item.evidence_id.startswith("fixture-") for item in aapl_snap.evidence_items)
    # every evidence_id was minted from an AAPL-scoped source_id
    assert all("AAPL" in item.source_id for item in aapl_snap.evidence_items)


def test_canonical_serialization_is_order_independent():
    a = _item(evidence_id="ev-a")
    b = _item(evidence_id="ev-b", source_id="src-2")
    src2 = _source(source_id="src-2")
    payload1 = canonical_snapshot_payload(
        symbol="AAPL", as_of=NOW, source_records=(_source(), src2), evidence_items=(a, b),
        deterministic_factors={"x": 1.0, "y": 2.0}, sentiment_metrics={}, portfolio_context=None,
        missing_data_reasons=(), conflict_reasons=(), point_in_time_safe=True, config_hash="c" * 64, git_sha="sha1",
    )
    payload2 = canonical_snapshot_payload(
        symbol="AAPL", as_of=NOW, source_records=(src2, _source()), evidence_items=(b, a),
        deterministic_factors={"y": 2.0, "x": 1.0}, sentiment_metrics={}, portfolio_context=None,
        missing_data_reasons=(), conflict_reasons=(), point_in_time_safe=True, config_hash="c" * 64, git_sha="sha1",
    )
    assert compute_snapshot_id(payload1) == compute_snapshot_id(payload2)


def test_missing_required_evidence_fails_snapshot_precondition():
    snap = build_fixture_snapshot("XXXX", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)
    assert snap.missing_data_reasons  # thin fixture
    reasons = validate_snapshot_preconditions(
        snap, require_point_in_time_safe=True, fail_on_stale_required_evidence=True,
    )
    assert reasons


def test_deterministic_truncation_is_stable_and_preserves_source_ids():
    class _ManyItemsProvider:
        def fetch(self, symbol, as_of):
            from trading_research.research.evidence import EvidenceBundle

            source = _source(source_id="many-source")
            items = tuple(
                _item(evidence_id=f"ev-{i:03d}", source_id="many-source", category="news")
                for i in range(10)
            )
            return EvidenceBundle(source_records=(source,), evidence_items=items)

    snap = build_evidence_snapshot(
        "AAPL", NOW, deterministic_factors={}, sentiment_metrics={}, providers=[_ManyItemsProvider()],
        portfolio_context_provider=None, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW,
        max_evidence_items=100, max_items_per_source_category=3,
    )
    assert len(snap.evidence_items) == 3
    kept_ids = sorted(i.evidence_id for i in snap.evidence_items)
    assert kept_ids == ["ev-000", "ev-001", "ev-002"]
    # source record survives even though most of its items were truncated
    assert any(s.source_id == "many-source" for s in snap.source_records)
