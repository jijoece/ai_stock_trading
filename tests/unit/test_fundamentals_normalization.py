"""Unit tests for evidence_providers/fundamentals.py — Milestone 6
docs/milestone-6.md Step 22 category D."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from trading_research.evidence_providers.fundamentals import normalize_fundamentals
from trading_research.evidence_providers.models import CompanyFactValue


def _annual(concept, value, period_end, filed_at, form_type="10-K", fiscal_period="FY"):
    return CompanyFactValue(
        concept=concept, unit="USD", value=Decimal(str(value)),
        period_start=period_end - timedelta(days=365),  # a true ~365-day annual period
        period_end=period_end, fiscal_year=period_end.year, fiscal_period=fiscal_period,
        form_type=form_type, filed_at=filed_at, frame=None,
    )


def test_quarterly_period_tagged_fp_fy_is_not_treated_as_annual():
    """Regression test for the real bug found via the SEC smoke path: a
    10-K's XBRL data tags quarterly comparatives with fp='FY' too — period
    *duration*, not fp, must decide annual-vs-quarterly."""
    quarterly_but_tagged_fy = CompanyFactValue(
        concept="Revenues", unit="USD", value=Decimal("50000"), period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30), fiscal_year=2026, fiscal_period="FY", form_type="10-K",
        filed_at=date(2026, 11, 1), frame=None,
    )
    true_annual = _annual("Revenues", 200000, date(2026, 9, 30), date(2026, 11, 1))
    normalized = normalize_fundamentals((quarterly_but_tagged_fy, true_annual))
    assert normalized["revenue"].value == Decimal("200000")  # the annual one, not the mislabeled quarter


def test_revenue_growth_yoy_uses_two_distinct_annual_periods():
    prior = _annual("Revenues", 100, date(2025, 9, 30), date(2025, 11, 1))
    latest = _annual("Revenues", 150, date(2026, 9, 30), date(2026, 11, 1))
    normalized = normalize_fundamentals((prior, latest))
    assert normalized["revenue_growth_yoy"].value == Decimal("0.5")


def test_missing_concept_is_omitted_not_zero_filled():
    facts = (_annual("Revenues", 100, date(2026, 9, 30), date(2026, 11, 1)),)
    normalized = normalize_fundamentals(facts)
    assert "cash" not in normalized
    assert "net_income" not in normalized


def test_gross_margin_requires_matching_period_end():
    revenue = _annual("Revenues", 1000, date(2026, 9, 30), date(2026, 11, 1))
    mismatched_gross_profit = _annual("GrossProfit", 500, date(2025, 9, 30), date(2025, 11, 1))
    normalized = normalize_fundamentals((revenue, mismatched_gross_profit))
    assert "gross_margin" not in normalized  # different period_end -> never silently combined


def test_gross_margin_computed_when_periods_align():
    revenue = _annual("Revenues", 1000, date(2026, 9, 30), date(2026, 11, 1))
    gross_profit = _annual("GrossProfit", 400, date(2026, 9, 30), date(2026, 11, 1))
    normalized = normalize_fundamentals((revenue, gross_profit))
    assert normalized["gross_margin"].value == Decimal("0.4")


def test_zero_value_is_not_treated_as_missing():
    facts = (_annual("NetIncomeLoss", 0, date(2026, 9, 30), date(2026, 11, 1)),)
    normalized = normalize_fundamentals(facts)
    assert "net_income" in normalized
    assert normalized["net_income"].value == Decimal("0")


def test_empty_facts_returns_empty_normalization():
    assert normalize_fundamentals(()) == {}


def test_instant_concept_uses_most_recent_filed_regardless_of_duration():
    older = CompanyFactValue(
        concept="CommonStockSharesOutstanding", unit="shares", value=Decimal("100"), period_start=None,
        period_end=date(2025, 9, 30), fiscal_year=2025, fiscal_period="FY", form_type="10-K",
        filed_at=date(2025, 11, 1), frame=None,
    )
    newer = CompanyFactValue(
        concept="CommonStockSharesOutstanding", unit="shares", value=Decimal("120"), period_start=None,
        period_end=date(2026, 9, 30), fiscal_year=2026, fiscal_period="FY", form_type="10-K",
        filed_at=date(2026, 11, 1), frame=None,
    )
    normalized = normalize_fundamentals((older, newer))
    assert normalized["shares_outstanding"].value == Decimal("120")
