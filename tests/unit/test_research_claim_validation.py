"""Category G: claim-validation tests (docs/milestone-5.md Step 20.G)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_research.research.claim_validation import validate_claim, validate_decision, validate_role_report
from trading_research.research.models import EvidenceItem, EvidenceSnapshot, ResearchClaim, ResearchDecision, RoleResearchReport, SourceRecord

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _source(source_id="src-1", point_in_time_safe=True) -> SourceRecord:
    return SourceRecord(
        source_id=source_id, source_type="fundamentals", provider="fixture", source_locator=None,
        retrieved_at=NOW, published_at=NOW, effective_at=NOW, available_at=NOW, content_hash="abc",
        status="ok", is_stale=False, point_in_time_safe=point_in_time_safe, error_code=None,
    )


def _item(evidence_id="ev-1", source_id="src-1", stale=False, normalized_values=None) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id, source_id=source_id, category="fundamentals", title="t", summary="s",
        normalized_values=normalized_values or {"revenue_growth_yoy": 0.08}, as_of=NOW, confidence="high",
        stale=stale, conflict_group=None,
    )


def _snapshot(symbol="AAPL", items=None, sources=None, snapshot_id="snap-1") -> EvidenceSnapshot:
    return EvidenceSnapshot(
        snapshot_id=snapshot_id, symbol=symbol, as_of=NOW, created_at=NOW,
        source_records=tuple(sources or [_source()]), evidence_items=tuple(items or [_item()]),
        deterministic_factors={}, sentiment_metrics={}, portfolio_context=None, missing_data_reasons=(),
        conflict_reasons=(), point_in_time_safe=True, config_hash="c" * 64, git_sha="sha1",
    )


def _claim(evidence_ids=("ev-1",), numeric_value=None, unit=None, importance="medium") -> ResearchClaim:
    return ResearchClaim(
        claim_id="c1", claim_type="growth", statement="grew", evidence_ids=evidence_ids,
        numeric_value=numeric_value, unit=unit, importance=importance,
    )


def test_valid_evidence_reference_passes():
    snap = _snapshot()
    result = validate_claim(_claim(), snap)
    assert result.valid


def test_unknown_evidence_id_rejected():
    snap = _snapshot()
    result = validate_claim(_claim(evidence_ids=("ev-does-not-exist",)), snap)
    assert not result.valid
    assert "unknown evidence_id" in result.reasons[0]


def test_stale_evidence_rejected():
    snap = _snapshot(items=[_item(stale=True)])
    result = validate_claim(_claim(), snap)
    assert not result.valid


def test_point_in_time_unsafe_source_rejected():
    snap = _snapshot(sources=[_source(point_in_time_safe=False)])
    result = validate_claim(_claim(), snap)
    assert not result.valid


def test_numeric_claim_matching_evidence_within_tolerance_passes():
    snap = _snapshot(items=[_item(normalized_values={"revenue_growth_yoy": 0.080001})])
    result = validate_claim(_claim(numeric_value=Decimal("0.08")), snap)
    assert result.valid


def test_numeric_claim_rounding_tolerance_boundary():
    snap = _snapshot(items=[_item(normalized_values={"x": 100.0})])
    result_within = validate_claim(_claim(numeric_value=Decimal("101.9")), snap)  # 1.9% off
    assert result_within.valid
    result_outside = validate_claim(_claim(numeric_value=Decimal("110")), snap)  # 10% off
    assert not result_outside.valid


def test_unsupported_numeric_claim_rejected():
    snap = _snapshot(items=[_item(normalized_values={"revenue_growth_yoy": 0.08})])
    result = validate_claim(_claim(numeric_value=Decimal("99.9")), snap)
    assert not result.valid


def test_cross_snapshot_citation_rejected():
    snap_a = _snapshot(symbol="AAPL", snapshot_id="snap-a", items=[_item(evidence_id="ev-a")])
    snap_b = _snapshot(symbol="MSFT", snapshot_id="snap-b", items=[_item(evidence_id="ev-b")])
    claim_citing_other_snapshot = _claim(evidence_ids=("ev-b",))
    result = validate_claim(claim_citing_other_snapshot, snap_a)
    assert not result.valid


def test_unsupported_qualitative_claim_with_no_evidence_ids_rejected():
    result = validate_claim(_claim(evidence_ids=()), _snapshot())
    assert not result.valid


def test_material_unsupported_claim_forces_report_invalid():
    snap = _snapshot()
    report = RoleResearchReport(
        report_id="r1", research_run_id="run-1", role="fundamental", symbol="AAPL", snapshot_id="snap-1",
        stance="BULLISH", summary="s", claims=(_claim(evidence_ids=("ev-unknown",), importance="high"),),
        catalysts=(), risks=(), uncertainties=(), missing_data_reasons=(), model_name="m", prompt_version="v1",
    )
    result = validate_role_report(report, snap)
    assert result.material_claim_unsupported is True
    assert not result.is_valid


def test_low_importance_unsupported_claim_does_not_force_invalid():
    snap = _snapshot()
    report = RoleResearchReport(
        report_id="r1", research_run_id="run-1", role="fundamental", symbol="AAPL", snapshot_id="snap-1",
        stance="BULLISH", summary="s", claims=(_claim(evidence_ids=("ev-unknown",), importance="low"),),
        catalysts=(), risks=(), uncertainties=(), missing_data_reasons=(), model_name="m", prompt_version="v1",
    )
    result = validate_role_report(report, snap)
    assert result.material_claim_unsupported is False
    assert result.rejected_claims


def test_complete_decision_with_missing_required_evidence_rejected():
    snap = EvidenceSnapshot(
        snapshot_id="snap-1", symbol="AAPL", as_of=NOW, created_at=NOW, source_records=(_source(),),
        evidence_items=(_item(),), deterministic_factors={}, sentiment_metrics={}, portfolio_context=None,
        missing_data_reasons=("fundamentals unavailable for a required field",), conflict_reasons=(),
        point_in_time_safe=True, config_hash="c" * 64, git_sha="sha1",
    )
    decision = ResearchDecision(
        decision_id="d1", research_run_id="run-1", symbol="AAPL", snapshot_id="snap-1", rating="BUY",
        confidence=Decimal("0.9"), thesis="t", bull_case="bull", bear_case="bear", catalysts=(), risks=(),
        invalidation_conditions=(), claims=(), evidence_ids=("ev-1",), missing_data_reasons=(),
        model_name="m", prompt_version="v1",
    )
    result = validate_decision(decision, snap)
    assert not result.is_valid
    assert result.consistency_reasons


def test_decision_fabricated_citation_rejected():
    snap = _snapshot()
    decision = ResearchDecision(
        decision_id="d1", research_run_id="run-1", symbol="AAPL", snapshot_id="snap-1", rating="BUY",
        confidence=Decimal("0.9"), thesis="t", bull_case="bull", bear_case="bear", catalysts=(), risks=(),
        invalidation_conditions=(), claims=(), evidence_ids=("ev-fabricated",), missing_data_reasons=(),
        model_name="m", prompt_version="v1",
    )
    result = validate_decision(decision, snap)
    assert not result.is_valid
    assert any("fabricated" in r for r in result.consistency_reasons)


def test_analysis_incomplete_with_missing_data_reasons_is_consistent():
    snap = _snapshot()
    decision = ResearchDecision(
        decision_id="d1", research_run_id="run-1", symbol="AAPL", snapshot_id="snap-1",
        rating="ANALYSIS_INCOMPLETE", confidence=Decimal("0"), thesis="t", bull_case="", bear_case="",
        catalysts=(), risks=(), invalidation_conditions=(), claims=(), evidence_ids=(),
        missing_data_reasons=("fundamentals unavailable",), model_name="m", prompt_version="v1",
    )
    result = validate_decision(decision, snap)
    assert result.is_valid


def test_validate_role_report_rejects_snapshot_mismatch():
    snap = _snapshot(snapshot_id="snap-1")
    report = RoleResearchReport(
        report_id="r1", research_run_id="run-1", role="fundamental", symbol="AAPL", snapshot_id="snap-DIFFERENT",
        stance="BULLISH", summary="s", claims=(), catalysts=(), risks=(), uncertainties=(),
        missing_data_reasons=(), model_name="m", prompt_version="v1",
    )
    with pytest.raises(ValueError):
        validate_role_report(report, snap)
