"""Unit tests for evidence_providers/disclosure_extraction.py —
docs/milestone-7.md Step 27 categories A/B (disclosure extraction is
exercised alongside both, since it consumes `FilingDocument` and produces
the going-concern/shell/bankruptcy text-level outcomes)."""
from __future__ import annotations

from datetime import datetime, timezone

from trading_research.evidence_providers.disclosure_extraction import (
    EXTRACTION_RULE_VERSION,
    OUTCOME_AMBIGUOUS_DISCLOSURE,
    OUTCOME_DOCUMENT_UNAVAILABLE,
    OUTCOME_EXPLICIT_DISCLOSURE_FOUND,
    OUTCOME_EXPLICIT_DISCLOSURE_NOT_FOUND,
    OUTCOME_SEARCH_INCOMPLETE,
    extract_disclosure,
)
from trading_research.evidence_providers.filing_documents import STATUS_DOCUMENT_UNAVAILABLE, STATUS_OK, FilingDocument

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)


def _doc(text: str, *, status=STATUS_OK, truncated=False) -> FilingDocument:
    return FilingDocument(
        accession_number="acc-1", source_url="https://www.sec.gov/Archives/edgar/data/1/acc-1/doc.htm",
        content_hash="h" * 64, text=text, retrieved_at=AS_OF, byte_length=len(text),
        truncated=truncated, status=status,
    )


# --- explicit going-concern disclosure --------------------------------------

def test_explicit_going_concern_disclosure_found():
    text = (
        "Management has concluded that there is substantial doubt about the Company's "
        "ability to continue as a going concern within one year of the date of issuance."
    )
    result = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert result.outcome == OUTCOME_EXPLICIT_DISCLOSURE_FOUND
    assert result.excerpt_hash is not None
    assert result.excerpt_snippet is not None
    assert len(result.excerpt_snippet) <= 200
    assert result.extraction_rule_version == EXTRACTION_RULE_VERSION
    assert result.availability_time == AS_OF


# --- disclosure absent from searched sections --------------------------------

def test_going_concern_absent_from_searched_sections():
    text = "The Company reported strong revenue growth and no unusual risk factors this quarter."
    result = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert result.outcome == OUTCOME_EXPLICIT_DISCLOSURE_NOT_FOUND
    # Never claim confirmed-absent.
    assert result.outcome != "CONFIRMED_ABSENT"
    assert result.excerpt_hash is None


def test_ambiguous_going_concern_mention_flagged():
    text = "The auditors discussed the going concern basis of accounting used in preparing these statements."
    result = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert result.outcome == OUTCOME_AMBIGUOUS_DISCLOSURE
    assert result.excerpt_snippet is not None


# --- document unavailable -----------------------------------------------------

def test_document_unavailable_status_propagates():
    doc = _doc("", status=STATUS_DOCUMENT_UNAVAILABLE)
    result = extract_disclosure(doc, disclosure_type="going_concern")
    assert result.outcome == OUTCOME_DOCUMENT_UNAVAILABLE


def test_empty_text_is_document_unavailable():
    doc = _doc("", status=STATUS_OK)
    result = extract_disclosure(doc, disclosure_type="going_concern")
    assert result.outcome == OUTCOME_DOCUMENT_UNAVAILABLE


# --- search incomplete (truncated document) -----------------------------------

def test_truncated_document_with_no_match_is_search_incomplete():
    text = "Ordinary business discussion with no relevant disclosure phrase present here."
    doc = _doc(text, truncated=True)
    result = extract_disclosure(doc, disclosure_type="going_concern")
    assert result.outcome == OUTCOME_SEARCH_INCOMPLETE


def test_truncated_document_with_explicit_match_still_found():
    text = "substantial doubt about the Company's ability to continue as a going concern"
    doc = _doc(text, truncated=True)
    result = extract_disclosure(doc, disclosure_type="going_concern")
    # An explicit match found before truncation still counts as found.
    assert result.outcome == OUTCOME_EXPLICIT_DISCLOSURE_FOUND


# --- shell-company disclosure --------------------------------------------------

def test_shell_company_disclosure_found():
    text = "The registrant is a shell company as defined in Rule 12b-2 of the Exchange Act."
    result = extract_disclosure(_doc(text), disclosure_type="shell_company")
    assert result.outcome == OUTCOME_EXPLICIT_DISCLOSURE_FOUND


def test_shell_company_disclosure_not_found():
    text = "The Company operates a full-scale manufacturing business with 500 employees."
    result = extract_disclosure(_doc(text), disclosure_type="shell_company")
    assert result.outcome == OUTCOME_EXPLICIT_DISCLOSURE_NOT_FOUND


# --- bankruptcy disclosure -----------------------------------------------------

def test_bankruptcy_disclosure_found():
    text = "On March 1, 2026, the Company filed a voluntary petition for Chapter 11 bankruptcy protection."
    result = extract_disclosure(_doc(text), disclosure_type="bankruptcy")
    assert result.outcome == OUTCOME_EXPLICIT_DISCLOSURE_FOUND


def test_bankruptcy_disclosure_not_found():
    text = "The Company maintains a strong balance sheet with no near-term liquidity concerns."
    result = extract_disclosure(_doc(text), disclosure_type="bankruptcy")
    assert result.outcome == OUTCOME_EXPLICIT_DISCLOSURE_NOT_FOUND


# --- section extraction ---------------------------------------------------------

def test_section_identified_when_heading_present():
    text = (
        "Item 7. Management's Discussion and Analysis of Financial Condition\n\n"
        "There is substantial doubt about the Company's ability to continue as a going concern."
    )
    result = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert result.section is not None
    assert "item 7" in result.section.lower()


def test_section_none_when_no_heading_pattern():
    text = "substantial doubt about the Company's ability to continue as a going concern appears here with no heading"
    result = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert result.section is None


# --- extraction rule version -----------------------------------------------------

def test_extraction_rule_version_recorded_on_every_result():
    text = "no relevant disclosure here"
    result = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert result.extraction_rule_version == EXTRACTION_RULE_VERSION
    assert result.extraction_rule_version != ""


# --- deterministic (not an LLM call) ---------------------------------------------

def test_extraction_is_pure_and_deterministic():
    text = "substantial doubt about the Company's ability to continue as a going concern"
    r1 = extract_disclosure(_doc(text), disclosure_type="going_concern")
    r2 = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert r1.outcome == r2.outcome
    assert r1.excerpt_hash == r2.excerpt_hash


def test_module_never_imports_claude_or_anthropic():
    import inspect
    import trading_research.evidence_providers.disclosure_extraction as mod

    source = inspect.getsource(mod)
    assert "import anthropic" not in source
    assert "claude_client" not in source.lower()
