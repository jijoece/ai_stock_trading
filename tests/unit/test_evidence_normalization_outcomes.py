"""Unit tests for evidence_providers/normalization.py — Milestone 6
docs/milestone-6.md Step 22 category G (evidence-snapshot outcomes)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading_research.evidence_providers.normalization import (
    BLOCKING_OUTCOMES,
    OUTCOME_COMPLETE,
    OUTCOME_COMPLETE_WITH_CONFLICTS,
    OUTCOME_INCOMPLETE_REQUIRED_DATA,
    OUTCOME_POINT_IN_TIME_UNSAFE,
    classify_snapshot_outcome,
)
from trading_research.research.models import EvidenceItem, EvidenceSnapshot, SourceRecord

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)


def _source(source_id, *, available_at, point_in_time_safe=True, status="ok"):
    return SourceRecord(
        source_id=source_id, source_type="fundamentals", provider="test", source_locator=None,
        retrieved_at=AS_OF, published_at=available_at, effective_at=available_at, available_at=available_at,
        content_hash="h", status=status, is_stale=False, point_in_time_safe=point_in_time_safe, error_code=None,
    )


def _item(evidence_id, source_id, category="fundamentals"):
    return EvidenceItem(
        evidence_id=evidence_id, source_id=source_id, category=category, title="t", summary="s",
        normalized_values={}, as_of=AS_OF, confidence="high", stale=False,
    )


def _snapshot(sources, items, *, missing=(), conflicts=(), point_in_time_safe=True):
    return EvidenceSnapshot(
        snapshot_id="snap-1", symbol="AAPL", as_of=AS_OF, created_at=AS_OF, source_records=tuple(sources),
        evidence_items=tuple(items), deterministic_factors={}, sentiment_metrics={}, portfolio_context=None,
        missing_data_reasons=tuple(missing), conflict_reasons=tuple(conflicts), point_in_time_safe=point_in_time_safe,
        config_hash="c", git_sha="g",
    )


def test_complete_snapshot_with_required_category_present():
    src = _source("s1", available_at=AS_OF - timedelta(days=1))
    snap = _snapshot([src], [_item("e1", "s1")])
    outcome, reasons = classify_snapshot_outcome(snap, required_categories=("fundamentals",))
    assert outcome == OUTCOME_COMPLETE
    assert reasons == ()


def test_point_in_time_unsafe_takes_priority():
    src = _source("s1", available_at=AS_OF + timedelta(days=1), point_in_time_safe=False)
    snap = _snapshot([src], [_item("e1", "s1")], point_in_time_safe=False)
    outcome, _ = classify_snapshot_outcome(snap, required_categories=("fundamentals",))
    assert outcome == OUTCOME_POINT_IN_TIME_UNSAFE
    assert outcome in BLOCKING_OUTCOMES


def test_missing_required_category_blocks():
    snap = _snapshot([], [], missing=("no fundamentals available",))
    outcome, _ = classify_snapshot_outcome(snap, required_categories=("fundamentals",))
    assert outcome == OUTCOME_INCOMPLETE_REQUIRED_DATA
    assert outcome in BLOCKING_OUTCOMES


def test_conflicts_with_otherwise_complete_data():
    src = _source("s1", available_at=AS_OF - timedelta(days=1))
    snap = _snapshot([src], [_item("e1", "s1")], conflicts=("fundamentals disagree across sources",))
    outcome, reasons = classify_snapshot_outcome(snap, required_categories=("fundamentals",))
    assert outcome == OUTCOME_COMPLETE_WITH_CONFLICTS
    assert outcome not in BLOCKING_OUTCOMES
    assert reasons == ("fundamentals disagree across sources",)


def test_provider_unavailable_when_all_missing_reasons_are_configuration_related():
    from trading_research.evidence_providers.normalization import OUTCOME_PROVIDER_UNAVAILABLE

    snap = _snapshot([], [], missing=("real news provider unavailable: no API key is configured",))
    outcome, _ = classify_snapshot_outcome(snap, required_categories=())
    assert outcome == OUTCOME_PROVIDER_UNAVAILABLE
