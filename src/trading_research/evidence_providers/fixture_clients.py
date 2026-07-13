"""Fixture-mode raw clients (docs/milestone-6.md Step 21: "fixture mode
remains available"). These satisfy the same raw-client shapes
(`get_price_history`/`get_company_facts`) the real `AlpacaMarketDataClient`/
`SecEdgarClient` expose, driven by `research/fixtures.py`'s existing
fixture-symbol data — so `run-research-cycle --provider-mode fixture` can
exercise the full scheduled-cycle path offline, with no network and no
credentials, for the same four symbols Milestone 5's fixture path already
recognizes.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from ..research.fixtures import fixture_deterministic_factors, is_fixture_symbol
from .models import CompanyFactValue, PriceBar


class FixtureMarketDataClient:
    def get_price_history(self, symbol: str, *, start: date, end: date, as_of: datetime) -> tuple[PriceBar, ...]:
        if not is_fixture_symbol(symbol):
            return ()
        bars = []
        base_price = Decimal("100.00")
        d = start
        while d <= end:
            if d.weekday() < 5:
                bars.append(PriceBar(
                    symbol=symbol, session_date=d, open=base_price, high=base_price + Decimal("1"),
                    low=base_price - Decimal("1"), close=base_price, volume=1_000_000, adjusted=False,
                    provider="fixture",
                ))
                base_price += Decimal("0.05")
            d += timedelta(days=1)
        return tuple(bars)

    def get_quote(self, symbol: str, *, as_of: datetime):
        return None


class FixtureSecClient:
    def get_company_facts(self, symbol: str, *, as_of: datetime) -> tuple[CompanyFactValue, ...]:
        factors = fixture_deterministic_factors(symbol)
        if not factors:
            return ()
        period_end = as_of.date() - timedelta(days=30)
        period_start = period_end - timedelta(days=365)
        filed_at = period_end + timedelta(days=20)
        values = []
        concept_map = {
            "revenue_growth_yoy": None,  # derived field, not a raw concept — handled specially below
            "gross_margin": None,
            "operating_margin": None,
        }
        # Fixture data only carries ratios directly (no raw revenue/profit
        # dollar figures), so this synthesizes plausible Revenues/GrossProfit/
        # OperatingIncomeLoss dollar values whose *ratios* reproduce the
        # fixture's documented values exactly — normalize_fundamentals()
        # recomputes gross_margin/operating_margin/revenue_growth_yoy from
        # these, never hand-waving the derived fields directly.
        base_revenue = Decimal("100000000")
        prior_revenue = base_revenue / (Decimal(str(1 + factors.get("revenue_growth_yoy", 0.0))) or Decimal("1"))
        values.append(CompanyFactValue(
            concept="Revenues", unit="USD", value=base_revenue, period_start=period_start, period_end=period_end,
            fiscal_year=period_end.year, fiscal_period="FY", form_type="10-K", filed_at=filed_at, frame=None,
        ))
        values.append(CompanyFactValue(
            concept="Revenues", unit="USD", value=prior_revenue.quantize(Decimal("1")),
            period_start=period_start - timedelta(days=365), period_end=period_start, fiscal_year=period_start.year,
            fiscal_period="FY", form_type="10-K", filed_at=filed_at, frame=None,
        ))
        if "gross_margin" in factors:
            values.append(CompanyFactValue(
                concept="GrossProfit", unit="USD", value=(base_revenue * Decimal(str(factors["gross_margin"]))).quantize(Decimal("1")),
                period_start=period_start, period_end=period_end, fiscal_year=period_end.year, fiscal_period="FY",
                form_type="10-K", filed_at=filed_at, frame=None,
            ))
        if "operating_margin" in factors:
            values.append(CompanyFactValue(
                concept="OperatingIncomeLoss", unit="USD", value=(base_revenue * Decimal(str(factors["operating_margin"]))).quantize(Decimal("1")),
                period_start=period_start, period_end=period_end, fiscal_year=period_end.year, fiscal_period="FY",
                form_type="10-K", filed_at=filed_at, frame=None,
            ))
        return tuple(values)

    def list_filings(self, symbol: str, *, available_by: datetime):
        return ()
