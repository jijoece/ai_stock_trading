"""Operating-history derivation (docs/milestone-7.md Step 7).

WHAT THIS PROXY MEANS — read before using this value anywhere:

`operating_history_years` derived here is a **public-reporting-history
proxy**: the number of years between a company's *earliest reliable SEC
filing date* (as observed in the SEC EDGAR submissions history available
as of the requested `as_of`) and `as_of` itself.

It is explicitly **NOT**:

* company age (a company can operate privately for years before its
  earliest SEC filing — this proxy only sees the reporting history);
* exchange-listing history (a company can be SEC-registered and file
  reports before or after any particular exchange listing, and can move
  between exchanges/OTC without changing this value);
* a measure of operational continuity (a reporting gap does not reduce
  this value — see `corporate_status_adapters.py`'s separate
  reporting-inactivity signal for that).

`derivation_method` on the result names exactly how the value was computed
(currently only one method: `EARLIEST_SEC_FILING_DATE`), and
`earliest_known_source` retains the filing reference the value was derived
from, so a caller can audit or refute the derivation without re-fetching
data.

Returns `UNKNOWN` (never `0` and never a fabricated small/large default)
when the evidence is insufficient to establish any public-reporting
history — e.g. no filings found at all, or the underlying corporate-status
evidence itself reports `SOURCE_UNAVAILABLE`.

Per docs/milestone-7.md Step 7's explicit instruction, this value is
**not** wired into `analysis/screener.py` or
`services/analyze_candidate.py::CandidateInput`/`FundamentalSnapshot` in
this task — see the module-level `INTEGRATION_NOTE` below for what proving
semantic compatibility would require before that wiring is safe.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .corporate_status import CorporateStatusEvidence, FilingReference, REPORTING_STATUS_SOURCE_UNAVAILABLE

DERIVATION_METHOD_EARLIEST_SEC_FILING_DATE = "EARLIEST_SEC_FILING_DATE"

OUTCOME_DERIVED = "DERIVED"
OUTCOME_UNKNOWN = "UNKNOWN"

INTEGRATION_NOTE = (
    "Before wiring OperatingHistoryResult.value_years into "
    "FundamentalSnapshot.operating_history_years (models/trading_models.py) or "
    "analysis/screener.py, a future task must: (1) confirm the screener's existing "
    "gate semantics for operating_history_years assume company age or listing history "
    "rather than public-reporting history and adjust its threshold/interpretation "
    "accordingly, or explicitly document that the proxy is an accepted substitute; "
    "(2) add tests proving the screener does not silently pass/fail differently for "
    "a company with a long private operating history but short SEC-reporting history "
    "(e.g. a recent IPO of an old company) than it would with the 'true' semantic the "
    "gate was designed against; (3) decide whether screener callers should see the "
    "OperatingHistoryResult.outcome (DERIVED vs UNKNOWN) directly, since today the "
    "field is silently None when unknown. Until that is done, this module's output "
    "must not flow into any screening/scoring path."
)


@dataclass(frozen=True)
class OperatingHistoryResult:
    symbol: str
    as_of: datetime
    outcome: str
    value_years: Decimal | None
    derivation_method: str | None
    earliest_known_source: FilingReference | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.outcome not in (OUTCOME_DERIVED, OUTCOME_UNKNOWN):
            raise ValueError(f"OperatingHistoryResult.outcome={self.outcome!r} must be DERIVED or UNKNOWN")
        if self.outcome == OUTCOME_DERIVED and self.value_years is None:
            raise ValueError("OperatingHistoryResult with outcome=DERIVED must have a non-None value_years")


def derive_operating_history(evidence: CorporateStatusEvidence) -> OperatingHistoryResult:
    """Pure function over an already-built `CorporateStatusEvidence` —
    performs no I/O itself. Reuses
    `CorporateStatusEvidence.earliest_reliable_filing_date` /
    `.operating_history_years` (already computed in
    `corporate_status_adapters.derive_corporate_status`) rather than
    re-deriving them, but packages the result with the explicit semantic
    documentation and derivation-method/source provenance this step
    requires that the bare `CorporateStatusEvidence` fields do not carry on
    their own."""
    if evidence.reporting_status == REPORTING_STATUS_SOURCE_UNAVAILABLE:
        return OperatingHistoryResult(
            symbol=evidence.symbol, as_of=evidence.as_of, outcome=OUTCOME_UNKNOWN,
            value_years=None, derivation_method=None, earliest_known_source=None,
            reason="corporate-status source was unavailable; no reliable filing history to derive from",
        )

    if evidence.earliest_reliable_filing_date is None or evidence.operating_history_years is None:
        return OperatingHistoryResult(
            symbol=evidence.symbol, as_of=evidence.as_of, outcome=OUTCOME_UNKNOWN,
            value_years=None, derivation_method=None, earliest_known_source=None,
            reason="no earliest reliable SEC filing date found in searched sources",
        )

    earliest_source = evidence.latest_annual_filing or evidence.latest_quarterly_filing
    # Prefer a FilingReference whose filing_date equals the evidence's
    # earliest_reliable_filing_date if one of the two available references
    # matches it; otherwise retain whichever reference is available as a
    # best-effort source pointer (never fabricate a synthetic reference).
    for candidate in (evidence.latest_annual_filing, evidence.latest_quarterly_filing):
        if candidate is not None and candidate.filing_date == evidence.earliest_reliable_filing_date:
            earliest_source = candidate
            break

    return OperatingHistoryResult(
        symbol=evidence.symbol, as_of=evidence.as_of, outcome=OUTCOME_DERIVED,
        value_years=evidence.operating_history_years,
        derivation_method=DERIVATION_METHOD_EARLIEST_SEC_FILING_DATE,
        earliest_known_source=earliest_source,
        reason=None,
    )
