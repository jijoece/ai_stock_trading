"""Deterministic, keyword/regex-based disclosure extraction over bounded
filing text (docs/milestone-7.md Step 6).

This is NOT an LLM call — every rule here is a versioned, regex/keyword
match over already-sanitized `FilingDocument.text`
(`filing_documents.py::sanitize_html` output). `extraction_rule_version`
identifies the exact rule set used, so a later rule change never silently
reclassifies a previously-extracted result.

PROMPT-INJECTION / UNTRUSTED-INPUT WARNING: the filing text scanned here
originates from a third-party filer, not this codebase — it is untrusted
input. This module never sends filing text to Claude or any other model;
if that is ever added in a future task, the same untrusted-input handling
`research/evidence_validation.py::render_evidence_item` already applies to
other evidence text (explicit delimiters + injection-risk annotation) must
be applied here too — not assumed to already be safe because extraction
here is regex-based.

Outcome vocabulary (Step 6) — never conflates "not found" with "confirmed
absent":

    EXPLICIT_DISCLOSURE_FOUND                          — an unambiguous
        going-concern (or other) disclosure phrase was matched.
    EXPLICIT_DISCLOSURE_NOT_FOUND_IN_SEARCHED_SECTIONS  — searched, no
        match; NEVER interpreted as "no going-concern issue exists".
    SEARCH_INCOMPLETE                                    — the document
        text available was truncated/partial, so the search could not
        cover the full filing.
    DOCUMENT_UNAVAILABLE                                 — no filing text
        was retrievable at all.
    AMBIGUOUS_DISCLOSURE                                 — a hedged/partial
        phrase matched (e.g. discussion of the going-concern *standard*
        without asserting substantial doubt) that a human should review.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from .filing_documents import FilingDocument, MAX_RETAINED_TEXT_CHARS

EXTRACTION_RULE_VERSION = "disclosure-extraction-v1"

OUTCOME_EXPLICIT_DISCLOSURE_FOUND = "EXPLICIT_DISCLOSURE_FOUND"
OUTCOME_EXPLICIT_DISCLOSURE_NOT_FOUND = "EXPLICIT_DISCLOSURE_NOT_FOUND_IN_SEARCHED_SECTIONS"
OUTCOME_SEARCH_INCOMPLETE = "SEARCH_INCOMPLETE"
OUTCOME_DOCUMENT_UNAVAILABLE = "DOCUMENT_UNAVAILABLE"
OUTCOME_AMBIGUOUS_DISCLOSURE = "AMBIGUOUS_DISCLOSURE"

OUTCOME_VALUES = (
    OUTCOME_EXPLICIT_DISCLOSURE_FOUND,
    OUTCOME_EXPLICIT_DISCLOSURE_NOT_FOUND,
    OUTCOME_SEARCH_INCOMPLETE,
    OUTCOME_DOCUMENT_UNAVAILABLE,
    OUTCOME_AMBIGUOUS_DISCLOSURE,
)

_SNIPPET_MAX_CHARS = 200  # short, bounded snippet only — never the full excerpt

# Explicit going-concern disclosure phrasing (conservative — modeled on the
# standard PCAOB/AICPA audit-opinion and MD&A language pattern
# "substantial doubt about its/the Company's ability to continue as a
# going concern"). Deliberately narrow: matches the standardized phrase,
# not generic financial-distress language.
_GOING_CONCERN_EXPLICIT_RE = re.compile(
    r"substantial\s+doubt\b.{0,80}?\bability\s+to\s+continue\s+as\s+a\s+going\s+concern",
    re.IGNORECASE | re.DOTALL,
)
# Hedged/ambiguous: mentions "going concern" near doubt-adjacent language
# but doesn't match the explicit standardized phrase above — flagged for
# human review rather than silently classified either way.
_GOING_CONCERN_AMBIGUOUS_RE = re.compile(
    r"going\s+concern",
    re.IGNORECASE,
)

_SHELL_COMPANY_EXPLICIT_RE = re.compile(
    r"\bshell\s+compan(?:y|ies)\b",
    re.IGNORECASE,
)

# Every 10-K/10-Q cover page carries the standard SEC checkbox question
# ("Indicate by check mark whether the registrant is a shell company...")
# regardless of whether the actual answer is Yes or No — a bare regex match
# on "shell company" therefore matches nearly every filing's cover page
# whether or not the filer is actually a shell company (confirmed against
# real AAPL 10-K text during Milestone 7.1 real validation: this produced a
# false CONFIRMED for a company that is obviously not a shell company).
# `_looks_like_cover_page_checkbox_context` filters these boilerplate
# matches out — never treats "the form asked the question" as "the answer
# was yes" (the same fail-closed posture as every other status in this
# module, applied to a false-positive risk instead of a false-negative one).
_COVER_PAGE_CHECKBOX_MARKERS = (
    "indicate by check mark",
    "check the appropriate box",
    "check if the registrant",
)


def _looks_like_cover_page_checkbox_context(text: str, match: re.Match, *, window_chars: int = 200) -> bool:
    preceding = text[max(0, match.start() - window_chars):match.start()].lower()
    return any(marker in preceding for marker in _COVER_PAGE_CHECKBOX_MARKERS)


def _find_first_valid_explicit_match(text: str, pattern: re.Pattern) -> re.Match | None:
    for candidate in pattern.finditer(text):
        if not _looks_like_cover_page_checkbox_context(text, candidate):
            return candidate
    return None

_BANKRUPTCY_EXPLICIT_RE = re.compile(
    r"\b(?:filed|filing)\s+(?:a\s+)?(?:voluntary\s+)?petition\s+(?:for|under)\s+(?:chapter\s+(?:7|11)|bankruptcy)",
    re.IGNORECASE,
)

_RULES: dict[str, tuple[re.Pattern, re.Pattern | None]] = {
    "going_concern": (_GOING_CONCERN_EXPLICIT_RE, _GOING_CONCERN_AMBIGUOUS_RE),
    "shell_company": (_SHELL_COMPANY_EXPLICIT_RE, None),
    "bankruptcy": (_BANKRUPTCY_EXPLICIT_RE, None),
}


@dataclass(frozen=True)
class DisclosureExtractionResult:
    disclosure_type: str
    outcome: str
    accession_number: str
    source_url: str
    section: str | None
    excerpt_hash: str | None
    excerpt_snippet: str | None
    availability_time: datetime
    extraction_rule_version: str

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOME_VALUES:
            raise ValueError(f"DisclosureExtractionResult.outcome={self.outcome!r} must be one of {OUTCOME_VALUES}")


def _excerpt(text: str, match: re.Match) -> tuple[str, str]:
    start = max(0, match.start() - 40)
    end = min(len(text), match.end() + 40)
    snippet = text[start:end].strip()
    snippet = snippet[:_SNIPPET_MAX_CHARS]
    excerpt_hash = hashlib.sha256(text[match.start():match.end()].encode()).hexdigest()
    return excerpt_hash, snippet


def extract_disclosure(
    document: FilingDocument,
    *,
    disclosure_type: str,
    rule_version: str = EXTRACTION_RULE_VERSION,
) -> DisclosureExtractionResult:
    """Deterministically scans `document.text` for `disclosure_type`
    ("going_concern", "shell_company", "bankruptcy"). Never claims absence
    was "confirmed" — a non-match is always
    `EXPLICIT_DISCLOSURE_NOT_FOUND_IN_SEARCHED_SECTIONS`, distinct from a
    genuine confirmed-absent conclusion (which this function never emits;
    that would require an authoritative negative statement, not just the
    absence of a positive one)."""
    if disclosure_type not in _RULES:
        raise ValueError(f"unknown disclosure_type: {disclosure_type!r}")

    if document.status == "DOCUMENT_UNAVAILABLE" or not document.text:
        return DisclosureExtractionResult(
            disclosure_type=disclosure_type, outcome=OUTCOME_DOCUMENT_UNAVAILABLE,
            accession_number=document.accession_number, source_url=document.source_url,
            section=None, excerpt_hash=None, excerpt_snippet=None,
            availability_time=document.retrieved_at, extraction_rule_version=rule_version,
        )

    explicit_re, ambiguous_re = _RULES[disclosure_type]
    explicit_match = _find_first_valid_explicit_match(document.text, explicit_re)
    if explicit_match:
        excerpt_hash, snippet = _excerpt(document.text, explicit_match)
        return DisclosureExtractionResult(
            disclosure_type=disclosure_type, outcome=OUTCOME_EXPLICIT_DISCLOSURE_FOUND,
            accession_number=document.accession_number, source_url=document.source_url,
            section=_guess_section(document.text, explicit_match),
            excerpt_hash=excerpt_hash, excerpt_snippet=snippet,
            availability_time=document.retrieved_at, extraction_rule_version=rule_version,
        )

    # Search-incomplete takes priority over a non-match verdict: if the
    # retained text was truncated, an absent match does not mean the full
    # filing lacks the disclosure — it means the searched portion did.
    if document.truncated:
        return DisclosureExtractionResult(
            disclosure_type=disclosure_type, outcome=OUTCOME_SEARCH_INCOMPLETE,
            accession_number=document.accession_number, source_url=document.source_url,
            section=None, excerpt_hash=None, excerpt_snippet=None,
            availability_time=document.retrieved_at, extraction_rule_version=rule_version,
        )

    if ambiguous_re is not None:
        ambiguous_match = ambiguous_re.search(document.text)
        if ambiguous_match:
            excerpt_hash, snippet = _excerpt(document.text, ambiguous_match)
            return DisclosureExtractionResult(
                disclosure_type=disclosure_type, outcome=OUTCOME_AMBIGUOUS_DISCLOSURE,
                accession_number=document.accession_number, source_url=document.source_url,
                section=_guess_section(document.text, ambiguous_match),
                excerpt_hash=excerpt_hash, excerpt_snippet=snippet,
                availability_time=document.retrieved_at, extraction_rule_version=rule_version,
            )

    return DisclosureExtractionResult(
        disclosure_type=disclosure_type, outcome=OUTCOME_EXPLICIT_DISCLOSURE_NOT_FOUND,
        accession_number=document.accession_number, source_url=document.source_url,
        section=None, excerpt_hash=None, excerpt_snippet=None,
        availability_time=document.retrieved_at, extraction_rule_version=rule_version,
    )


_SECTION_HEADING_RE = re.compile(
    r"(item\s+\d+[a-z]?\.?\s*[^\n]{0,80})", re.IGNORECASE,
)


def _guess_section(text: str, match: re.Match) -> str | None:
    """Best-effort section identification: looks backward from the match
    for the nearest 'Item N...' heading-like text. Returns None (never a
    fabricated section name) when no heading pattern is found before the
    match."""
    preceding = text[: match.start()]
    headings = list(_SECTION_HEADING_RE.finditer(preceding))
    if not headings:
        return None
    return headings[-1].group(1).strip()[:120]
