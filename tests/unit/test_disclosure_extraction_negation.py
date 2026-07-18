"""Milestone 11.3 Part 29: going-concern negation/alleviation handling —
"no substantial doubt", "has been alleviated", "no longer raise substantial
doubt" must not be classified as an active EXPLICIT_DISCLOSURE_FOUND."""
from __future__ import annotations

from datetime import datetime, timezone

from trading_research.evidence_providers.disclosure_extraction import (
    OUTCOME_AMBIGUOUS_DISCLOSURE,
    OUTCOME_EXPLICIT_DISCLOSURE_FOUND,
    extract_disclosure,
)
from trading_research.evidence_providers.filing_documents import STATUS_OK, FilingDocument

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)


def _doc(text: str) -> FilingDocument:
    return FilingDocument(
        accession_number="acc-1", source_url="https://www.sec.gov/Archives/edgar/data/1/acc-1/doc.htm",
        content_hash="h" * 64, text=text, retrieved_at=AS_OF, byte_length=len(text),
        truncated=False, status=STATUS_OK,
    )


def test_true_positive_still_found_no_negation_nearby():
    """Sanity/regression: the negation handling must not suppress a genuine
    explicit finding just because an unrelated 'no' appears far away."""
    text = (
        "There is no assurance the Company will complete its financing plan. "
        "Separately, management has concluded that there is substantial doubt about "
        "the Company's ability to continue as a going concern within one year."
    )
    result = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert result.outcome == OUTCOME_EXPLICIT_DISCLOSURE_FOUND


def test_direct_negation_before_substantial_doubt_is_not_found():
    text = (
        "Management has concluded that there is no substantial doubt about the "
        "Company's ability to continue as a going concern."
    )
    result = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert result.outcome != OUTCOME_EXPLICIT_DISCLOSURE_FOUND


def test_alleviation_after_match_is_not_found():
    text = (
        "The conditions that previously raised substantial doubt about the Company's "
        "ability to continue as a going concern have since been alleviated by the "
        "completion of a $50 million financing."
    )
    result = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert result.outcome != OUTCOME_EXPLICIT_DISCLOSURE_FOUND


def test_no_longer_raises_before_match_is_not_found():
    text = (
        "Conditions no longer raise substantial doubt about the Company's ability to "
        "continue as a going concern."
    )
    result = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert result.outcome != OUTCOME_EXPLICIT_DISCLOSURE_FOUND


def test_negated_finding_falls_back_to_ambiguous_not_dropped_silently():
    """A negated mention still surfaces for human review (AMBIGUOUS) rather
    than silently vanishing into a bare not-found — conservative
    classification, per spec."""
    text = (
        "Management has concluded that there is no substantial doubt about the "
        "Company's ability to continue as a going concern."
    )
    result = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert result.outcome == OUTCOME_AMBIGUOUS_DISCLOSURE


def test_negation_detection_survives_html_residue_and_line_breaks():
    text = (
        "Management has concluded that there is\n<br/>\nno   \n\n substantial\n"
        "doubt about the   Company's\nability to continue as a going concern."
    )
    result = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert result.outcome != OUTCOME_EXPLICIT_DISCLOSURE_FOUND


def test_negation_detection_survives_table_cell_punctuation():
    text = (
        "| Risk Factor | Assessment |\n"
        "| --- | --- |\n"
        "| Going concern | there is no substantial doubt about the Company's ability "
        "to continue as a going concern. |"
    )
    result = extract_disclosure(_doc(text), disclosure_type="going_concern")
    assert result.outcome != OUTCOME_EXPLICIT_DISCLOSURE_FOUND
