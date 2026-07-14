"""Derives `CorporateStatusEvidence` (Step 4) deterministically from SEC
EDGAR filing-history metadata (docs/milestone-7.md Step 5).

Reuses `sec_provider.SecEdgarClient.list_filings`, which already calls the
official `data.sec.gov/submissions/CIK##########.json` endpoint and already
enforces point-in-time safety (any filing whose `acceptanceDateTime` is
after the requested `available_by`/`as_of` is excluded before this module
ever sees it — see `sec_provider.py`'s own point-in-time-rule docstring and
`test_list_filings_excludes_future_accepted_filing`). This module adds no
new HTTP client method: `list_filings` already targets the submissions
endpoint (a different endpoint from `companyfacts`, which
`RealFundamentalsEvidenceProvider` uses), and it already returns the exact
metadata this step needs (accession numbers, form types, filing/accepted
dates, source URLs) with nothing further to extract from the raw payload.

Everything below is deterministic, rule-based classification over already
point-in-time-filtered `FilingRecord` tuples — no network call, no model
call, no editorializing ("filings appear to have stopped" is surfaced as an
explicit `reporting_status` + `reporting_status_reason`, never phrased or
treated as "the company is defunct").
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from .corporate_status import (
    REPORTING_STATUS_ACTIVE,
    REPORTING_STATUS_INACTIVE,
    REPORTING_STATUS_SOURCE_UNAVAILABLE,
    REPORTING_STATUS_UNKNOWN,
    STATUS_CONFIRMED,
    STATUS_NOT_FOUND_IN_SEARCHED_SOURCES,
    STATUS_SOURCE_UNAVAILABLE,
    COMPLETENESS_COMPLETE,
    COMPLETENESS_PARTIAL,
    COMPLETENESS_UNAVAILABLE,
    CorporateRiskSignal,
    CorporateStatusEvidence,
    FilingReference,
    SourceRecord,
)
from .disclosure_extraction import (
    OUTCOME_AMBIGUOUS_DISCLOSURE,
    OUTCOME_DOCUMENT_UNAVAILABLE,
    OUTCOME_EXPLICIT_DISCLOSURE_FOUND,
    OUTCOME_SEARCH_INCOMPLETE,
    extract_disclosure,
)
from .errors import ProviderError
from .filing_documents import FilingDocument, FilingDocumentClient
from .models import FilingRecord
from .sec_provider import SecEdgarClient

ANNUAL_FORM_TYPES = ("10-K", "10-K405", "20-F")
QUARTERLY_FORM_TYPES = ("10-Q",)

# Forms whose presence is a documented, official signal of the category
# named — never inferred from an absent form, only from a *present* one
# (docs/milestone-7.md Step 5: "no broad inference from an absent form").
BANKRUPTCY_ITEM_MARKERS = ("BANKRUPTCY", "CHAPTER 11", "CHAPTER 7", "RECEIVERSHIP")
DELISTING_FORM_TYPES = ("25", "25-NSE")  # Form 25: notification of delisting/removal from listing
REGISTRATION_TERMINATION_FORM_TYPES = ("15", "15-12B", "15-12G", "15-15D")  # Form 15: termination of registration

# A gap between the two most recent filings' `accepted_at` beyond this many
# days is surfaced as a reporting-inactivity signal — a fact, not a
# conclusion (see module docstring: never "the company is defunct").
REPORTING_INACTIVITY_GAP_DAYS = 545  # ~18 months; generous vs. the ~90-180 day 10-Q/10-K cadence
LATE_FILING_GRACE_DAYS = 15  # beyond the typical filing-deadline extension window


def _content_hash(*parts: object) -> str:
    import hashlib

    return hashlib.sha256(repr(parts).encode()).hexdigest()


def _to_filing_reference(f: FilingRecord) -> FilingReference:
    return FilingReference(
        accession_number=f.accession_number,
        form_type=f.form_type,
        filing_date=f.filing_date,
        accepted_at=f.accepted_at,
        source_url=f.primary_document_url,
        is_amendment=f.is_amendment,
    )


def _base_form_type(form_type: str) -> str:
    """Strips a trailing "/A" amendment suffix for category matching
    (e.g. "10-K/A" -> "10-K") — the amendment is still the *same* filing
    category, and `is_amendment`/the verbatim `form_type` on the
    `FilingReference` are preserved for the caller; only category
    membership tests use the base form."""
    return form_type[:-2] if form_type.endswith("/A") else form_type


def _latest_of(filings: tuple[FilingRecord, ...], form_types: tuple[str, ...]) -> FilingRecord | None:
    matches = [f for f in filings if _base_form_type(f.form_type) in form_types]
    if not matches:
        return None
    return max(matches, key=lambda f: f.accepted_at)


def _dedupe_by_accession(filings: tuple[FilingRecord, ...]) -> tuple[FilingRecord, ...]:
    """Duplicate accession numbers (the same filing appearing twice in a
    submissions payload) collapse to one entry — never double-counted as
    two separate filings for gap/lateness analysis."""
    seen: dict[str, FilingRecord] = {}
    for f in filings:
        seen.setdefault(f.accession_number, f)
    return tuple(sorted(seen.values(), key=lambda f: f.accepted_at))


def derive_corporate_status(
    symbol: str,
    *,
    sec_client: SecEdgarClient,
    as_of: datetime,
    cik: str | None = None,
) -> CorporateStatusEvidence:
    """Builds `CorporateStatusEvidence` from SEC filing-history metadata.

    Never raises for an "expected" failure mode (SEC provider unavailable,
    unresolvable ticker) — those become `SOURCE_UNAVAILABLE`/`UNKNOWN`
    statuses on the result instead, matching the rest of this package's
    "fail closed into an explicit status, not an exception" convention
    (`evidence_adapters.py`).
    """
    try:
        raw_filings = sec_client.list_filings(symbol, available_by=as_of, cik=cik)
    except ProviderError as exc:
        source = SourceRecord(
            source_id=f"sec-submissions-{symbol}",
            source_type="corporate_status",
            provider="sec-edgar",
            source_locator=None,
            retrieved_at=as_of,
            available_at=None,
            content_hash=_content_hash(symbol, "error", type(exc).__name__),
            status="error",
        )
        return CorporateStatusEvidence(
            symbol=symbol.upper(),
            as_of=as_of,
            reporting_status=REPORTING_STATUS_SOURCE_UNAVAILABLE,
            reporting_status_reason=f"SEC submissions endpoint unavailable: {exc}",
            earliest_reliable_filing_date=None,
            operating_history_years=None,
            latest_annual_filing=None,
            latest_quarterly_filing=None,
            late_filing_notices=(),
            bankruptcy_signals=(
                CorporateRiskSignal(signal_type="bankruptcy", status=STATUS_SOURCE_UNAVAILABLE, basis=str(exc)),
            ),
            delisting_signals=(
                CorporateRiskSignal(signal_type="delisting", status=STATUS_SOURCE_UNAVAILABLE, basis=str(exc)),
            ),
            registration_status_signals=(
                CorporateRiskSignal(signal_type="registration_status", status=STATUS_SOURCE_UNAVAILABLE, basis=str(exc)),
            ),
            shell_company_signals=(
                CorporateRiskSignal(signal_type="shell_company", status=STATUS_SOURCE_UNAVAILABLE, basis=str(exc)),
            ),
            going_concern_signals=(
                CorporateRiskSignal(signal_type="going_concern", status=STATUS_SOURCE_UNAVAILABLE, basis=str(exc)),
            ),
            completeness_status=COMPLETENESS_UNAVAILABLE,
            sources=(source,),
        )

    filings = _dedupe_by_accession(raw_filings)
    source = SourceRecord(
        source_id=f"sec-submissions-{symbol}",
        source_type="corporate_status",
        provider="sec-edgar",
        source_locator=None,
        retrieved_at=as_of,
        available_at=as_of,
        content_hash=_content_hash(symbol, tuple(f.accession_number for f in filings)),
        status="ok",
    )

    if not filings:
        return CorporateStatusEvidence(
            symbol=symbol.upper(),
            as_of=as_of,
            reporting_status=REPORTING_STATUS_UNKNOWN,
            reporting_status_reason="no SEC filing history found for this symbol as of as_of (unresolved ticker or genuinely no filings)",
            earliest_reliable_filing_date=None,
            operating_history_years=None,
            latest_annual_filing=None,
            latest_quarterly_filing=None,
            late_filing_notices=(),
            bankruptcy_signals=(
                CorporateRiskSignal(signal_type="bankruptcy", status=STATUS_NOT_FOUND_IN_SEARCHED_SOURCES, basis="no filings available to search"),
            ),
            delisting_signals=(
                CorporateRiskSignal(signal_type="delisting", status=STATUS_NOT_FOUND_IN_SEARCHED_SOURCES, basis="no filings available to search"),
            ),
            registration_status_signals=(
                CorporateRiskSignal(signal_type="registration_status", status=STATUS_NOT_FOUND_IN_SEARCHED_SOURCES, basis="no filings available to search"),
            ),
            shell_company_signals=(
                CorporateRiskSignal(signal_type="shell_company", status=STATUS_NOT_FOUND_IN_SEARCHED_SOURCES, basis="no filings available to search"),
            ),
            going_concern_signals=(
                CorporateRiskSignal(signal_type="going_concern", status=STATUS_NOT_FOUND_IN_SEARCHED_SOURCES, basis="no filings available to search"),
            ),
            completeness_status=COMPLETENESS_UNAVAILABLE,
            sources=(source,),
        )

    earliest = filings[0]
    latest = filings[-1]
    earliest_date = earliest.filing_date

    latest_annual = _latest_of(filings, ANNUAL_FORM_TYPES)
    latest_quarterly = _latest_of(filings, QUARTERLY_FORM_TYPES)

    # Operating-history proxy: years between the earliest reliable filing
    # date and as_of. See operating_history.py's module docstring for the
    # full semantic disclaimer — this is a *public-reporting-history* proxy,
    # not company age.
    days_history = (as_of.date() - earliest_date).days
    operating_history_years = (
        Decimal(days_history) / Decimal(365) if days_history >= 0 else None
    )

    # Reporting-inactivity signal: a gap since the most recent filing.
    gap_days = (as_of.date() - latest.filing_date).days
    if gap_days > REPORTING_INACTIVITY_GAP_DAYS:
        reporting_status = REPORTING_STATUS_INACTIVE
        reporting_status_reason = (
            f"no SEC filing observed for {gap_days} days prior to as_of "
            f"(most recent known filing: {latest.form_type} accepted {latest.accepted_at.isoformat()}); "
            "this surfaces a reporting-inactivity fact only, not a conclusion about company viability"
        )
    else:
        reporting_status = REPORTING_STATUS_ACTIVE
        reporting_status_reason = None

    # Late-filing notices: NT 10-K / NT 10-Q ("notification of late filing")
    # form types are an explicit, official SEC signal — not inferred.
    late_filing_notices = tuple(
        _to_filing_reference(f) for f in filings if f.form_type in ("NT 10-K", "NT 10-Q", "NT 10-K/A", "NT 10-Q/A")
    )

    bankruptcy_signals = _derive_bankruptcy_signals(filings)
    delisting_signals = _derive_delisting_signals(filings)
    registration_signals = _derive_registration_signals(filings)
    shell_signals = _derive_shell_signals(filings)
    going_concern_signals = _derive_going_concern_placeholder_signals(filings)

    completeness_status = COMPLETENESS_COMPLETE if (latest_annual or latest_quarterly) else COMPLETENESS_PARTIAL

    return CorporateStatusEvidence(
        symbol=symbol.upper(),
        as_of=as_of,
        reporting_status=reporting_status,
        reporting_status_reason=reporting_status_reason,
        earliest_reliable_filing_date=earliest_date,
        operating_history_years=operating_history_years,
        latest_annual_filing=_to_filing_reference(latest_annual) if latest_annual else None,
        latest_quarterly_filing=_to_filing_reference(latest_quarterly) if latest_quarterly else None,
        late_filing_notices=late_filing_notices,
        bankruptcy_signals=bankruptcy_signals,
        delisting_signals=delisting_signals,
        registration_status_signals=registration_signals,
        shell_company_signals=shell_signals,
        going_concern_signals=going_concern_signals,
        completeness_status=completeness_status,
        sources=(source,),
    )


def _derive_bankruptcy_signals(filings: tuple[FilingRecord, ...]) -> tuple[CorporateRiskSignal, ...]:
    """Filing-metadata alone (form type, no filing text) cannot confirm a
    bankruptcy disclosure — only `disclosure_extraction.py`, reading actual
    filing text, can reach `CONFIRMED`. At this metadata-only layer the
    honest result is always `NOT_FOUND_IN_SEARCHED_SOURCES` (searched form
    types/metadata; no explicit bankruptcy-context form found) rather than
    a fabricated `CONFIRMED`/false-negative pair."""
    matches = [f for f in filings if f.form_type in ("8-K",)]
    return (
        CorporateRiskSignal(
            signal_type="bankruptcy",
            status=STATUS_NOT_FOUND_IN_SEARCHED_SOURCES,
            basis=(
                f"searched {len(filings)} filing(s) by form type/metadata only; "
                f"{len(matches)} 8-K filing(s) present but item-level disclosure text was not inspected at this layer "
                "(see disclosure_extraction.py for text-level confirmation)"
            ),
            evidence_refs=tuple(_to_filing_reference(f) for f in matches),
        ),
    )


def _derive_delisting_signals(filings: tuple[FilingRecord, ...]) -> tuple[CorporateRiskSignal, ...]:
    matches = [f for f in filings if f.form_type in DELISTING_FORM_TYPES]
    if matches:
        return (
            CorporateRiskSignal(
                signal_type="delisting",
                status=STATUS_CONFIRMED,
                basis=f"Form {matches[-1].form_type} (notification of removal from listing) accepted {matches[-1].accepted_at.isoformat()}",
                evidence_refs=tuple(_to_filing_reference(f) for f in matches),
            ),
        )
    return (
        CorporateRiskSignal(
            signal_type="delisting",
            status=STATUS_NOT_FOUND_IN_SEARCHED_SOURCES,
            basis=f"no Form 25/25-NSE found among {len(filings)} searched filing(s)",
        ),
    )


def _derive_registration_signals(filings: tuple[FilingRecord, ...]) -> tuple[CorporateRiskSignal, ...]:
    matches = [f for f in filings if f.form_type in REGISTRATION_TERMINATION_FORM_TYPES]
    if matches:
        return (
            CorporateRiskSignal(
                signal_type="registration_status",
                status=STATUS_CONFIRMED,
                basis=f"Form {matches[-1].form_type} (termination of registration) accepted {matches[-1].accepted_at.isoformat()}",
                evidence_refs=tuple(_to_filing_reference(f) for f in matches),
            ),
        )
    return (
        CorporateRiskSignal(
            signal_type="registration_status",
            status=STATUS_NOT_FOUND_IN_SEARCHED_SOURCES,
            basis=f"no Form 15/15-12B/15-12G/15-15D found among {len(filings)} searched filing(s)",
        ),
    )


def _derive_shell_signals(filings: tuple[FilingRecord, ...]) -> tuple[CorporateRiskSignal, ...]:
    """Filing metadata alone cannot confirm shell-company status (SEC does
    not expose a dedicated form type for this) — always
    `NOT_FOUND_IN_SEARCHED_SOURCES` at this layer; explicit disclosure
    extraction (Step 6) is the only path to `CONFIRMED`."""
    return (
        CorporateRiskSignal(
            signal_type="shell_company",
            status=STATUS_NOT_FOUND_IN_SEARCHED_SOURCES,
            basis=(
                f"no dedicated SEC form type exists for shell-company status; searched {len(filings)} filing(s) "
                "by metadata only — see disclosure_extraction.py for text-level shell-company disclosure search"
            ),
        ),
    )


def _derive_going_concern_placeholder_signals(filings: tuple[FilingRecord, ...]) -> tuple[CorporateRiskSignal, ...]:
    """Going-concern disclosures live in filing *text* (typically the audit
    opinion or MD&A of a 10-K/10-Q), never in filing metadata — this layer
    always returns `NOT_FOUND_IN_SEARCHED_SOURCES` (metadata searched, text
    not inspected), matching Step 6's requirement that "not found" never
    silently becomes "confirmed absent". `disclosure_extraction.py` is the
    only module that can produce `EXPLICIT_DISCLOSURE_FOUND`."""
    annual = [f for f in filings if f.form_type in ANNUAL_FORM_TYPES]
    return (
        CorporateRiskSignal(
            signal_type="going_concern",
            status=STATUS_NOT_FOUND_IN_SEARCHED_SOURCES,
            basis=(
                f"{len(annual)} annual filing(s) present in metadata; going-concern status requires text-level "
                "extraction (see disclosure_extraction.py) — metadata alone can never confirm or rule this out"
            ),
            evidence_refs=tuple(_to_filing_reference(f) for f in annual[-3:]),
        ),
    )


# --- Step 4: corporate-status evidence-provider boundary -----------------


class CorporateStatusEvidenceProvider(Protocol):
    """Smallest safe extension point (docs/milestone-7.md Step 4): a typed,
    point-in-time-safe `CorporateStatusEvidence` result, distinct from the
    generic `research/evidence.py::EvidenceBundle` provider shape used by
    the fundamentals/market/news/sentiment/filing Protocols (those return an
    already-normalized bundle; this one returns the full typed result so
    `research/evidence_completeness.py::evaluate_completeness` and
    `storage/corporate_status_repositories.py` can consume it directly,
    without lossy round-tripping through `EvidenceBundle` first)."""

    def fetch(self, symbol: str, as_of: datetime) -> CorporateStatusEvidence: ...


class SecCorporateStatusProvider:
    """Real (or fixture, via an injected fixture `sec_client`) corporate-status
    provider. `filing_document_client=None` (the fixture-mode default) skips
    Step 6's text-level disclosure composition entirely and returns the
    metadata-only `derive_corporate_status` result — deterministic, offline,
    no HTTP. A real caller supplies a real `FilingDocumentClient` to enable
    bounded text-level going-concern/shell-company/bankruptcy confirmation."""

    def __init__(
        self,
        sec_client: SecEdgarClient,
        *,
        filing_document_client: FilingDocumentClient | None = None,
        cik: str | None = None,
    ):
        self._sec_client = sec_client
        self._filing_document_client = filing_document_client
        self._cik = cik

    def fetch(self, symbol: str, as_of: datetime) -> CorporateStatusEvidence:
        return build_corporate_status_with_disclosures(
            symbol, sec_client=self._sec_client, filing_document_client=self._filing_document_client,
            as_of=as_of, cik=self._cik,
        )


# --- Step 6: bounded metadata + disclosure-extraction composition --------

MAX_DISCLOSURE_DOCUMENTS = 2


def _merge_disclosure_signal(
    original: CorporateRiskSignal, document: FilingDocument, *, disclosure_type: str, filing_ref: FilingReference,
) -> CorporateRiskSignal:
    """Applies `extract_disclosure` to one bounded document and merges the
    result into `original` (a metadata-only signal). Only an
    `EXPLICIT_DISCLOSURE_FOUND` outcome may upgrade the signal to
    `CONFIRMED`; every other outcome (not-found/ambiguous/search-incomplete/
    document-unavailable) leaves `original.status` unchanged and only
    appends an audit note to `basis` — never downgrades, never converts a
    "not found" into a confirmed negative, never fabricates
    `SOURCE_UNAVAILABLE` from one unavailable document (docs/milestone-7.md
    Step 6: "extraction failures fail closed")."""
    extraction = extract_disclosure(document, disclosure_type=disclosure_type)

    if extraction.outcome == OUTCOME_EXPLICIT_DISCLOSURE_FOUND:
        return CorporateRiskSignal(
            signal_type=original.signal_type,
            status=STATUS_CONFIRMED,
            basis=(
                f"text-level extraction ({extraction.extraction_rule_version}) found an explicit disclosure in "
                f"{filing_ref.form_type} accession {filing_ref.accession_number}"
                f"{', section=' + extraction.section if extraction.section else ''}"
            ),
            evidence_refs=(filing_ref,),
        )
    if extraction.outcome == OUTCOME_AMBIGUOUS_DISCLOSURE:
        note = (
            f"; text-level extraction ({extraction.extraction_rule_version}) found ambiguous/hedged language in "
            f"{filing_ref.form_type} accession {filing_ref.accession_number} — flagged for human review, not confirmed"
        )
    elif extraction.outcome == OUTCOME_SEARCH_INCOMPLETE:
        note = (
            f"; text-level search of {filing_ref.form_type} accession {filing_ref.accession_number} was incomplete "
            "(document text truncated) — metadata-only result preserved, explicit SEARCH_INCOMPLETE"
        )
    elif extraction.outcome == OUTCOME_DOCUMENT_UNAVAILABLE:
        note = (
            f"; filing document unavailable for text-level search ({filing_ref.form_type} accession "
            f"{filing_ref.accession_number}) — metadata-only result preserved, explicit DOCUMENT_UNAVAILABLE"
        )
    else:  # EXPLICIT_DISCLOSURE_NOT_FOUND_IN_SEARCHED_SECTIONS
        note = (
            f"; text-level extraction ({extraction.extraction_rule_version}) searched {filing_ref.form_type} "
            f"accession {filing_ref.accession_number}, no explicit disclosure found — not converted to a confirmed absence"
        )
    return CorporateRiskSignal(
        signal_type=original.signal_type, status=original.status, basis=original.basis + note,
        evidence_refs=original.evidence_refs,
    )


def build_corporate_status_with_disclosures(
    symbol: str,
    *,
    sec_client: SecEdgarClient,
    filing_document_client: FilingDocumentClient | None,
    as_of: datetime,
    cik: str | None = None,
) -> CorporateStatusEvidence:
    """Composes Step 5's metadata-only `derive_corporate_status` with Step 6's
    bounded, deterministic text-level disclosure extraction.

    Retrieves at most `MAX_DISCLOSURE_DOCUMENTS` filing documents total: the
    latest annual filing's text (reused for both the going-concern and
    shell-company searches, since they are typically disclosed in the same
    document) and the most recent 8-K referenced by the bankruptcy signal's
    own metadata-derived `evidence_refs`, if any. No new HTTP client method
    is used beyond `filing_documents.py::FilingDocumentClient.get_document`,
    which is already bounded (`MAX_DOCUMENT_BYTES`/`MAX_RETAINED_TEXT_CHARS`).
    `filing_document_client=None` returns the unmodified metadata-only base
    (fixture-mode/offline path — deterministic, no network).
    """
    base = derive_corporate_status(symbol, sec_client=sec_client, as_of=as_of, cik=cik)
    if filing_document_client is None:
        return base

    going_concern = base.going_concern_signals
    shell_company = base.shell_company_signals
    bankruptcy = base.bankruptcy_signals
    documents_fetched = 0

    if base.latest_annual_filing is not None and documents_fetched < MAX_DISCLOSURE_DOCUMENTS and base.latest_annual_filing.source_url:
        document = filing_document_client.get_document(
            accession_number=base.latest_annual_filing.accession_number,
            source_url=base.latest_annual_filing.source_url, retrieved_at=as_of,
        )
        documents_fetched += 1
        if going_concern:
            going_concern = (
                _merge_disclosure_signal(
                    going_concern[0], document, disclosure_type="going_concern", filing_ref=base.latest_annual_filing,
                ),
            )
        if shell_company:
            shell_company = (
                _merge_disclosure_signal(
                    shell_company[0], document, disclosure_type="shell_company", filing_ref=base.latest_annual_filing,
                ),
            )

    if bankruptcy and bankruptcy[0].evidence_refs and documents_fetched < MAX_DISCLOSURE_DOCUMENTS:
        latest_8k = max(bankruptcy[0].evidence_refs, key=lambda r: r.accepted_at)
        if latest_8k.source_url:
            document = filing_document_client.get_document(
                accession_number=latest_8k.accession_number, source_url=latest_8k.source_url, retrieved_at=as_of,
            )
            documents_fetched += 1
            bankruptcy = (
                _merge_disclosure_signal(
                    bankruptcy[0], document, disclosure_type="bankruptcy", filing_ref=latest_8k,
                ),
            )

    return dataclasses.replace(
        base, going_concern_signals=going_concern, shell_company_signals=shell_company, bankruptcy_signals=bankruptcy,
    )
