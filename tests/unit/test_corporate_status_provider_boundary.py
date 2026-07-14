"""Unit tests for the Milestone 7.1 corporate-status evidence-provider
boundary (docs/milestone-7.1.md Steps 4-6): `SecCorporateStatusProvider`,
`build_corporate_status_with_disclosures`, `corporate_status_to_evidence_bundle`,
and the deterministic `FixtureSecClient.list_filings` correction.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_research.evidence_providers.corporate_status import (
    STATUS_CONFIRMED,
    STATUS_NOT_FOUND_IN_SEARCHED_SOURCES,
)
from trading_research.evidence_providers.corporate_status_adapters import (
    CorporateStatusEvidenceProvider,
    SecCorporateStatusProvider,
    build_corporate_status_with_disclosures,
)
from trading_research.evidence_providers.evidence_adapters import (
    PrefetchedEvidenceProvider,
    corporate_status_to_evidence_bundle,
)
from trading_research.evidence_providers.filing_documents import FilingDocument, FilingDocumentCache, FilingDocumentClient
from trading_research.evidence_providers.fixture_clients import FixtureSecClient

AS_OF = datetime(2026, 7, 1, 23, 0, tzinfo=timezone.utc)


class _StaticDocumentClient:
    """Test double for `FilingDocumentClient` — returns a pre-scripted
    document for every accession number, no network."""

    def __init__(self, text: str, *, status: str = "OK"):
        self._text = text
        self._status = status

    def get_document(self, *, accession_number, source_url, retrieved_at):
        return FilingDocument(
            accession_number=accession_number, source_url=source_url, content_hash="deadbeef",
            text=self._text, retrieved_at=retrieved_at, byte_length=len(self._text), truncated=False,
            status=self._status,
        )


def test_fixture_provider_satisfies_protocol_shape():
    provider: CorporateStatusEvidenceProvider = SecCorporateStatusProvider(FixtureSecClient())
    evidence = provider.fetch("AAPL", AS_OF)
    assert evidence.symbol == "AAPL"


def test_fixture_sec_client_returns_deterministic_filings_covering_the_offline_path():
    filings = FixtureSecClient().list_filings("AAPL", available_by=AS_OF)
    form_types = {f.form_type for f in filings}
    assert "10-K" in form_types  # annual
    assert "10-Q" in form_types  # quarterly
    assert "10-K/A" in form_types  # amendment
    assert "NT 10-Q" in form_types  # late-filing notice
    assert "8-K" in form_types  # risk-signal fixture
    assert all(f.accepted_at <= AS_OF for f in filings), "future filing must be excluded (point-in-time)"
    accepted_times = [f.accepted_at for f in filings]
    assert accepted_times == sorted(accepted_times), "stable deterministic ordering"


def test_fixture_sec_client_unknown_symbol_returns_empty():
    assert FixtureSecClient().list_filings("ZZZZ_NOT_A_FIXTURE", available_by=AS_OF) == ()


def test_metadata_only_composition_when_no_filing_document_client():
    evidence = build_corporate_status_with_disclosures(
        "AAPL", sec_client=FixtureSecClient(), filing_document_client=None, as_of=AS_OF,
    )
    assert evidence.going_concern_signals[0].status == STATUS_NOT_FOUND_IN_SEARCHED_SOURCES
    assert evidence.shell_company_signals[0].status == STATUS_NOT_FOUND_IN_SEARCHED_SOURCES


def test_disclosure_composition_upgrades_going_concern_to_confirmed_on_explicit_match():
    doc_client = _StaticDocumentClient(
        "Item 7. There is substantial doubt about the Company's ability to continue as a going concern.",
    )
    evidence = build_corporate_status_with_disclosures(
        "AAPL", sec_client=FixtureSecClient(), filing_document_client=doc_client, as_of=AS_OF,
    )
    assert evidence.going_concern_signals[0].status == STATUS_CONFIRMED
    assert "text-level extraction" in evidence.going_concern_signals[0].basis


def test_disclosure_composition_document_unavailable_preserves_metadata_only_status():
    doc_client = _StaticDocumentClient("", status="DOCUMENT_UNAVAILABLE")
    evidence = build_corporate_status_with_disclosures(
        "AAPL", sec_client=FixtureSecClient(), filing_document_client=doc_client, as_of=AS_OF,
    )
    assert evidence.going_concern_signals[0].status == STATUS_NOT_FOUND_IN_SEARCHED_SOURCES
    assert "DOCUMENT_UNAVAILABLE" in evidence.going_concern_signals[0].basis


def test_disclosure_composition_never_converts_not_found_to_confirmed_absence():
    doc_client = _StaticDocumentClient("Ordinary business discussion, nothing relevant here.")
    evidence = build_corporate_status_with_disclosures(
        "AAPL", sec_client=FixtureSecClient(), filing_document_client=doc_client, as_of=AS_OF,
    )
    assert evidence.going_concern_signals[0].status == STATUS_NOT_FOUND_IN_SEARCHED_SOURCES
    assert "no explicit disclosure found" in evidence.going_concern_signals[0].basis


def test_disclosure_composition_bounded_to_two_documents(monkeypatch):
    calls = []

    class _CountingDocumentClient(_StaticDocumentClient):
        def get_document(self, *, accession_number, source_url, retrieved_at):
            calls.append(accession_number)
            return super().get_document(accession_number=accession_number, source_url=source_url, retrieved_at=retrieved_at)

    build_corporate_status_with_disclosures(
        "AAPL", sec_client=FixtureSecClient(), filing_document_client=_CountingDocumentClient("no matches here"), as_of=AS_OF,
    )
    assert len(calls) <= 2


def test_corporate_status_to_evidence_bundle_produces_stable_ids_and_bounded_content():
    evidence = build_corporate_status_with_disclosures(
        "AAPL", sec_client=FixtureSecClient(), filing_document_client=None, as_of=AS_OF,
    )
    bundle1 = corporate_status_to_evidence_bundle(evidence)
    bundle2 = corporate_status_to_evidence_bundle(evidence)
    ids1 = sorted(i.evidence_id for i in bundle1.evidence_items)
    ids2 = sorted(i.evidence_id for i in bundle2.evidence_items)
    assert ids1 == ids2, "stable evidence IDs — same input always produces the same IDs"
    assert len(bundle1.source_records) == 1
    for item in bundle1.evidence_items:
        assert len(item.summary) < 2000  # bounded content, never full filing text
    # NOT_FOUND_IN_SEARCHED_SOURCES retained verbatim, never rendered as "no risk".
    summaries = " ".join(i.summary for i in bundle1.evidence_items)
    assert "NOT_FOUND_IN_SEARCHED_SOURCES" in summaries


def test_corporate_status_bundle_labels_earliest_filing_as_proxy_not_operating_history():
    evidence = build_corporate_status_with_disclosures(
        "AAPL", sec_client=FixtureSecClient(), filing_document_client=None, as_of=AS_OF,
    )
    bundle = corporate_status_to_evidence_bundle(evidence)
    earliest_item = next(i for i in bundle.evidence_items if i.evidence_id.endswith("earliest-filing"))
    assert "proxy" in earliest_item.summary.lower()
    assert "operating history" in earliest_item.summary.lower()


def test_prefetched_evidence_provider_returns_fixed_bundle_regardless_of_args():
    evidence = build_corporate_status_with_disclosures(
        "AAPL", sec_client=FixtureSecClient(), filing_document_client=None, as_of=AS_OF,
    )
    bundle = corporate_status_to_evidence_bundle(evidence)
    provider = PrefetchedEvidenceProvider(bundle)
    assert provider.fetch("AAPL", AS_OF) is bundle
    assert provider.fetch("MSFT", AS_OF) is bundle  # static — args are ignored by design


def test_snapshot_hashing_includes_corporate_status():
    """Two snapshots built with different corporate-status evidence must
    hash differently — corporate-status evidence participates in canonical
    snapshot hashing (docs/milestone-7.1.md Step 5)."""
    from trading_research.research.evidence import build_evidence_snapshot

    evidence_a = build_corporate_status_with_disclosures(
        "AAPL", sec_client=FixtureSecClient(), filing_document_client=None, as_of=AS_OF,
    )
    bundle_a = corporate_status_to_evidence_bundle(evidence_a)

    class _UnavailableSecClient(FixtureSecClient):
        def list_filings(self, symbol, *, available_by, cik=None):
            from trading_research.evidence_providers.errors import ProviderError

            raise ProviderError("unavailable")

    evidence_b = build_corporate_status_with_disclosures(
        "AAPL", sec_client=_UnavailableSecClient(), filing_document_client=None, as_of=AS_OF,
    )
    bundle_b = corporate_status_to_evidence_bundle(evidence_b)

    snap_a = build_evidence_snapshot(
        "AAPL", AS_OF, deterministic_factors={}, sentiment_metrics={},
        providers=(PrefetchedEvidenceProvider(bundle_a),), portfolio_context_provider=None,
        config_hash="h", git_sha="s", clock=lambda: AS_OF,
    )
    snap_b = build_evidence_snapshot(
        "AAPL", AS_OF, deterministic_factors={}, sentiment_metrics={},
        providers=(PrefetchedEvidenceProvider(bundle_b),), portfolio_context_provider=None,
        config_hash="h", git_sha="s", clock=lambda: AS_OF,
    )
    assert snap_a.snapshot_id != snap_b.snapshot_id
