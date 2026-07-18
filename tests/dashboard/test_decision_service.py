from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dashboard.models.view_models import DashboardOutcome
from dashboard.services.decision_service import DecisionFilters, DecisionService


def test_lists_bounded_decisions_and_maps_persisted_outcomes(dashboard_database: Path):
    decisions = DecisionService(dashboard_database).list_decisions(limit=25)

    assert len(decisions) == 2
    by_symbol = {item.symbol: item for item in decisions}
    assert by_symbol["ABC"].final_outcome is DashboardOutcome.BOUGHT_OR_SUBMITTED
    assert by_symbol["ABC"].paper_order_status == "FILLED"
    assert by_symbol["XYZ"].final_outcome is DashboardOutcome.REJECTED
    assert by_symbol["XYZ"].primary_reason_code == "REJECTED_MAX_OPEN_POSITIONS"


def test_parameterized_filters_cover_date_symbol_outcome_and_reason(dashboard_database: Path):
    service = DecisionService(dashboard_database)
    result = service.list_decisions(DecisionFilters(
        start_date=date(2026, 7, 17),
        end_date=date(2026, 7, 17),
        symbol="xyz",
        outcome=DashboardOutcome.REJECTED,
        primary_reason="rejected_max_open_positions",
    ))

    assert [item.symbol for item in result] == ["XYZ"]
    with pytest.raises(ValueError, match="symbol"):
        service.list_decisions(DecisionFilters(symbol="ABC' OR 1=1 --"))


def test_rejects_unbounded_result_limit(dashboard_database: Path):
    with pytest.raises(ValueError, match="limit"):
        DecisionService(dashboard_database).list_decisions(limit=201)


def test_bought_detail_exposes_only_whitelisted_structured_fields(dashboard_database: Path):
    detail = DecisionService(dashboard_database).get_decision_detail("cycle-1", "ABC")

    assert detail is not None
    assert detail.bull_thesis == "Structured bull case for ABC"
    assert detail.bear_case == "Structured bear case for ABC"
    assert detail.evidence_references == ("evidence-abc",)
    assert detail.reference_price == Decimal("100.0")
    assert detail.limit_price == Decimal("99.50")
    assert detail.quantity == Decimal("9.95")
    assert detail.fill_status == "FILLED"
    assert "raw_response" not in repr(detail)


def test_not_bought_detail_explains_stable_block(dashboard_database: Path):
    detail = DecisionService(dashboard_database).get_decision_detail("cycle-1", "XYZ")

    assert detail is not None
    assert detail.summary.final_outcome is DashboardOutcome.REJECTED
    assert detail.summary.primary_reason_code == "REJECTED_MAX_OPEN_POSITIONS"
    assert detail.failed_stage == "Risk and policy evaluation"
    assert detail.observed_value == "10"
    assert detail.required_threshold == "8"
    assert detail.block_category == "Deterministic"
