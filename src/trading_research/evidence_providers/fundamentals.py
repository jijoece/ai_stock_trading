"""Normalizes real SEC XBRL company facts into fundamentals fields
(docs/milestone-6.md Step 8). Every value keeps its source concept, unit,
reporting period, and filed date — nothing is computed when a required input
is missing, zero is never treated as missing and missing is never treated as
zero, and only values whose `filed_at` was already known as of the request's
`as_of` are ever used (enforced upstream by `sec_provider.get_company_facts`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .models import CompanyFactValue

# Concept aliases: try in order, use the first with any values (docs/milestone-6.md
# Step 8: "reject incompatible units" — every alias here reports in USD).
_CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
    "net_income": ("NetIncomeLoss",),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "cash": ("CashAndCashEquivalentsAtCarryingValue",),
    "long_term_debt": ("LongTermDebtNoncurrent", "LongTermDebt"),
    "shares_outstanding": ("CommonStockSharesOutstanding",),
}


@dataclass(frozen=True)
class NormalizedFundamental:
    field_name: str
    value: Decimal
    unit: str
    period_start: date | None
    period_end: date
    fiscal_period: str | None
    form_type: str
    filed_at: date
    concept: str


_ANNUAL_PERIOD_MIN_DAYS = 350
_ANNUAL_PERIOD_MAX_DAYS = 380


def _is_annual_period(value: CompanyFactValue) -> bool:
    """A 10-K's XBRL data tags prior-period *quarterly* comparatives with the
    same `fp="FY"` default as the filing's own annual figures (confirmed
    against real SEC data during implementation — Apple's 10-K includes
    quarter-length 'Revenues' entries all marked fp=FY). `fiscal_period`
    alone is therefore not a reliable annual/quarterly discriminator; period
    *duration* is. A bug found via the real SEC smoke path, not a unit test —
    see the Milestone 6 scratchpad's "Bugs discovered and fixed" section."""
    if value.period_start is None:
        return False
    days = (value.period_end - value.period_start).days
    return _ANNUAL_PERIOD_MIN_DAYS <= days <= _ANNUAL_PERIOD_MAX_DAYS


def _latest_annual_then_any(values: tuple[CompanyFactValue, ...]) -> CompanyFactValue | None:
    """Prefer the most recent true annual-duration value; fall back to the
    most recent value of any period if no annual value exists."""
    if not values:
        return None
    annual = [v for v in values if v.form_type.startswith("10-K") and _is_annual_period(v)]
    pool = annual if annual else list(values)
    return max(pool, key=lambda v: (v.period_end, v.filed_at))


def normalize_fundamentals(facts: tuple[CompanyFactValue, ...]) -> dict[str, NormalizedFundamental]:
    by_concept: dict[str, list[CompanyFactValue]] = {}
    for fact in facts:
        by_concept.setdefault(fact.concept, []).append(fact)

    result: dict[str, NormalizedFundamental] = {}
    for field_name, aliases in _CONCEPT_ALIASES.items():
        for concept in aliases:
            candidates = tuple(by_concept.get(concept, ()))
            chosen = _latest_annual_then_any(candidates)
            if chosen is not None:
                result[field_name] = NormalizedFundamental(
                    field_name=field_name, value=chosen.value, unit=chosen.unit,
                    period_start=chosen.period_start, period_end=chosen.period_end,
                    fiscal_period=chosen.fiscal_period, form_type=chosen.form_type,
                    filed_at=chosen.filed_at, concept=concept,
                )
                break  # first alias with data wins — never merges across aliases

    revenue_concept = next((c for c in _CONCEPT_ALIASES["revenue"] if c in by_concept), None)
    if revenue_concept:
        annual_revenues = sorted(
            (v for v in by_concept[revenue_concept] if v.form_type.startswith("10-K") and _is_annual_period(v)),
            key=lambda v: v.period_end,
        )
        # De-duplicate by period_end (restatements can add a later filed_at for
        # the same period — keep the earliest-filed value for reproducibility;
        # unlike other fields, growth explicitly needs two *distinct* periods).
        by_period: dict[date, CompanyFactValue] = {}
        for v in annual_revenues:
            by_period.setdefault(v.period_end, v)
        distinct = sorted(by_period.values(), key=lambda v: v.period_end)
        if len(distinct) >= 2:
            latest, prior = distinct[-1], distinct[-2]
            if prior.value != 0:
                growth = (latest.value - prior.value) / prior.value
                result["revenue_growth_yoy"] = NormalizedFundamental(
                    field_name="revenue_growth_yoy", value=growth, unit="ratio",
                    period_start=prior.period_end, period_end=latest.period_end,
                    fiscal_period="FY", form_type=latest.form_type, filed_at=latest.filed_at,
                    concept=f"{revenue_concept}(derived_growth)",
                )

    if "gross_profit" in result and "revenue" in result and result["revenue"].value != 0 and result["gross_profit"].period_end == result["revenue"].period_end:
        gm = result["gross_profit"]
        rv = result["revenue"]
        result["gross_margin"] = NormalizedFundamental(
            field_name="gross_margin", value=gm.value / rv.value, unit="ratio",
            period_start=gm.period_start, period_end=gm.period_end, fiscal_period=gm.fiscal_period,
            form_type=gm.form_type, filed_at=gm.filed_at, concept="GrossProfit/Revenues(derived)",
        )
    if "operating_income" in result and "revenue" in result and result["revenue"].value != 0 and result["operating_income"].period_end == result["revenue"].period_end:
        oi = result["operating_income"]
        rv = result["revenue"]
        result["operating_margin"] = NormalizedFundamental(
            field_name="operating_margin", value=oi.value / rv.value, unit="ratio",
            period_start=oi.period_start, period_end=oi.period_end, fiscal_period=oi.fiscal_period,
            form_type=oi.form_type, filed_at=oi.filed_at, concept="OperatingIncomeLoss/Revenues(derived)",
        )

    return result
