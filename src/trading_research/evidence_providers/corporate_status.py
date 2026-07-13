"""Corporate-status evidence model (docs/milestone-7.md Step 4).

`CorporateStatusEvidence` is a frozen-dataclass evidence contract, analogous
to `research/models.py::EvidenceSnapshot` but scoped to the specific
questions "is this company still an active, reliable public reporter, and
does it show any distress/shell/going-concern signal?" — questions today's
`FundamentalSnapshot.operating_history_years` /
`.bankruptcy_or_distress` / `.going_concern_warning` /
`.shell_company_flag` fields (`models/trading_models.py`) are always `None`
for, per ADR 0004 Decision 7.

Status vocabulary (docs/milestone-7.md Step 4) — used wherever a signal or
overall reporting status needs an explicit epistemic state, never a
silently-defaulted boolean:

    CONFIRMED                      — an official source explicitly states this.
    NOT_FOUND_IN_SEARCHED_SOURCES  — searched, not present; NOT "false".
    UNKNOWN                        — insufficient evidence to say either way.
    SOURCE_UNAVAILABLE             — the provider itself could not be reached.
    POINT_IN_TIME_UNSAFE           — evidence exists but is not safely
                                      knowable as of the requested `as_of`.
    CONFLICTING                    — sources disagree.

Hard rule (docs/milestone-7.md, "Hard safety boundaries"): code in this
module must never convert `NOT_FOUND_IN_SEARCHED_SOURCES` into `FALSE`, and
must never synthesize a "healthy company" default when evidence is merely
absent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


def _require_tz_aware(value: datetime | None, name: str) -> None:
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


# --- Status vocabulary -------------------------------------------------

STATUS_CONFIRMED = "CONFIRMED"
STATUS_NOT_FOUND_IN_SEARCHED_SOURCES = "NOT_FOUND_IN_SEARCHED_SOURCES"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
STATUS_POINT_IN_TIME_UNSAFE = "POINT_IN_TIME_UNSAFE"
STATUS_CONFLICTING = "CONFLICTING"

CORPORATE_STATUS_VALUES = (
    STATUS_CONFIRMED,
    STATUS_NOT_FOUND_IN_SEARCHED_SOURCES,
    STATUS_UNKNOWN,
    STATUS_SOURCE_UNAVAILABLE,
    STATUS_POINT_IN_TIME_UNSAFE,
    STATUS_CONFLICTING,
)

# Overall reporting-status values (distinct axis from a single signal's
# status above — this describes "is the company currently an active SEC
# filer as of as_of", not a single risk signal).
REPORTING_STATUS_ACTIVE = "ACTIVE"
REPORTING_STATUS_INACTIVE = "INACTIVE"
REPORTING_STATUS_UNKNOWN = "UNKNOWN"
REPORTING_STATUS_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"

REPORTING_STATUS_VALUES = (
    REPORTING_STATUS_ACTIVE,
    REPORTING_STATUS_INACTIVE,
    REPORTING_STATUS_UNKNOWN,
    REPORTING_STATUS_SOURCE_UNAVAILABLE,
)

COMPLETENESS_COMPLETE = "COMPLETE"
COMPLETENESS_PARTIAL = "PARTIAL"
COMPLETENESS_UNAVAILABLE = "UNAVAILABLE"

COMPLETENESS_VALUES = (COMPLETENESS_COMPLETE, COMPLETENESS_PARTIAL, COMPLETENESS_UNAVAILABLE)


def _require_enum(value: str, allowed: tuple[str, ...], name: str) -> None:
    if value not in allowed:
        raise ValueError(f"{name}={value!r} must be one of {allowed}")


@dataclass(frozen=True)
class FilingReference:
    """Provenance for one specific SEC filing referenced by corporate-status
    evidence. Always carries the accession number, form type, and both the
    filing date and the accepted/available timestamp actually used for
    point-in-time filtering — never just a bare form-type string."""

    accession_number: str
    form_type: str
    filing_date: date
    accepted_at: datetime
    source_url: str | None
    is_amendment: bool = False

    def __post_init__(self) -> None:
        _require_tz_aware(self.accepted_at, "FilingReference.accepted_at")
        if not self.accession_number:
            raise ValueError("FilingReference.accession_number must be non-empty")


@dataclass(frozen=True)
class SourceRecord:
    """Minimal provenance record for a corporate-status evidence source.
    Deliberately a local, narrower type rather than importing
    `research/models.py::SourceRecord` — that type is bound to the
    evidence-snapshot content-hashing contract (`EvidenceSnapshot`
    validates every `EvidenceItem.source_id` against it); corporate-status
    evidence is a separate contract per ADR 0005 Decision 10 and does not
    need to satisfy that validation. Adapters that want to feed a
    `CorporateStatusEvidence` result into an `EvidenceBundle` do the mapping
    explicitly, not implicitly via shared identity."""

    source_id: str
    source_type: str
    provider: str
    source_locator: str | None
    retrieved_at: datetime
    available_at: datetime | None
    content_hash: str
    status: str

    def __post_init__(self) -> None:
        _require_tz_aware(self.retrieved_at, "SourceRecord.retrieved_at")
        _require_tz_aware(self.available_at, "SourceRecord.available_at")
        if not self.source_id:
            raise ValueError("SourceRecord.source_id must be non-empty")


@dataclass(frozen=True)
class CorporateRiskSignal:
    """One discrete corporate-status signal (bankruptcy, delisting,
    registration-termination, shell-company, going-concern, ...).

    `status` uses the module's explicit status vocabulary. `basis`
    describes *how* the signal was derived (e.g. "form_type=15 filed",
    "no matching form type found in searched filing history") so a
    downstream reader — human or code — can audit why a given status was
    reached without re-deriving it. `evidence_refs` retains the filings
    (if any) that back the signal."""

    signal_type: str
    status: str
    basis: str
    evidence_refs: tuple[FilingReference, ...] = ()

    def __post_init__(self) -> None:
        _require_enum(self.status, CORPORATE_STATUS_VALUES, f"CorporateRiskSignal({self.signal_type}).status")


@dataclass(frozen=True)
class CorporateStatusEvidence:
    """Normalized, point-in-time-safe corporate-status evidence for one
    symbol as of one `as_of` timestamp (docs/milestone-7.md Step 4).

    Every field that can legitimately be unknown carries an explicit status
    rather than a favorable default — see module docstring. `sources`
    retains full provenance for every filing/document consulted, even when
    the ultimate conclusion is `UNKNOWN` or `NOT_FOUND_IN_SEARCHED_SOURCES`,
    so an operator or later code can inspect exactly what was and was not
    searched.
    """

    symbol: str
    as_of: datetime
    reporting_status: str
    reporting_status_reason: str | None
    earliest_reliable_filing_date: date | None
    operating_history_years: Decimal | None
    latest_annual_filing: FilingReference | None
    latest_quarterly_filing: FilingReference | None
    late_filing_notices: tuple[FilingReference, ...]
    bankruptcy_signals: tuple[CorporateRiskSignal, ...]
    delisting_signals: tuple[CorporateRiskSignal, ...]
    registration_status_signals: tuple[CorporateRiskSignal, ...]
    shell_company_signals: tuple[CorporateRiskSignal, ...]
    going_concern_signals: tuple[CorporateRiskSignal, ...]
    completeness_status: str
    sources: tuple[SourceRecord, ...]

    def __post_init__(self) -> None:
        _require_tz_aware(self.as_of, "CorporateStatusEvidence.as_of")
        _require_enum(self.reporting_status, REPORTING_STATUS_VALUES, "CorporateStatusEvidence.reporting_status")
        _require_enum(self.completeness_status, COMPLETENESS_VALUES, "CorporateStatusEvidence.completeness_status")
        if not self.symbol:
            raise ValueError("CorporateStatusEvidence.symbol must be non-empty")

    def has_any_critical_uncertainty(self) -> bool:
        """True when the overall reporting status itself, or any risk
        signal, is in a state that must fail-closed a screening decision
        (`UNKNOWN`, `CONFLICTING`, `SOURCE_UNAVAILABLE`, or
        `POINT_IN_TIME_UNSAFE`) — used by
        `research/evidence_completeness.py` (Step 11), not duplicated
        there."""
        if self.reporting_status in (REPORTING_STATUS_UNKNOWN, REPORTING_STATUS_SOURCE_UNAVAILABLE):
            return True
        blocking = (STATUS_UNKNOWN, STATUS_CONFLICTING, STATUS_SOURCE_UNAVAILABLE, STATUS_POINT_IN_TIME_UNSAFE)
        for group in (
            self.bankruptcy_signals,
            self.delisting_signals,
            self.registration_status_signals,
            self.shell_company_signals,
            self.going_concern_signals,
        ):
            for signal in group:
                if signal.status in blocking:
                    return True
        return False
