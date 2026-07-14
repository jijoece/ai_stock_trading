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
from .models import CompanyFactValue, FilingRecord, PriceBar


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

    def list_filings(self, symbol: str, *, available_by: datetime, cik: str | None = None) -> tuple[FilingRecord, ...]:
        """Deterministic fixture filing history (docs/milestone-7.md Step 10),
        anchored relative to `available_by` (exactly like `get_company_facts`
        anchors its synthesized period dates) so the same fixture symbol
        produces a stable, point-in-time-safe filing set regardless of which
        `as_of`/`available_by` a given test or cycle happens to use. Covers
        the full offline corporate-status path: an annual filing, a
        quarterly filing, an amendment, a late-filing notice, a historical
        earliest filing, a future filing that point-in-time filtering must
        exclude, and one risk-signal fixture (an 8-K, exercising the
        bankruptcy-signal metadata search)."""
        if not is_fixture_symbol(symbol):
            return ()
        cik = cik or "0000000001"

        def _mk(form_type: str, days_before_available: int, suffix: str, *, is_amendment: bool = False, report_period_days_before: int | None = None) -> FilingRecord:
            accepted = available_by - timedelta(days=days_before_available)
            accession = f"0000000001-{accepted.year:04d}-{suffix}"
            return FilingRecord(
                accession_number=accession, cik=cik, symbol=symbol.upper(), form_type=form_type,
                filing_date=accepted.date(), accepted_at=accepted,
                report_period=(accepted - timedelta(days=report_period_days_before)).date() if report_period_days_before else None,
                primary_document_url=(
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/primary.htm"
                ),
                is_amendment=is_amendment,
            )

        candidates = (
            _mk("10-K", 400, "000010", report_period_days_before=430),                       # latest annual filing
            _mk("10-Q", 100, "000009", report_period_days_before=130),                        # latest quarterly filing
            _mk("10-K/A", 395, "000011", is_amendment=True, report_period_days_before=430),   # amendment
            _mk("NT 10-Q", 105, "000008"),                                                    # late-filing notice
            _mk("10-K", 365 * 5, "000001", report_period_days_before=365 * 5 + 30),           # historical earliest filing
            _mk("8-K", 200, "000007"),                                                        # risk-signal fixture (bankruptcy search)
            _mk("10-Q", -30, "000099"),                                                        # future filing — must be excluded
        )
        return tuple(sorted((f for f in candidates if f.accepted_at <= available_by), key=lambda f: f.accepted_at))
