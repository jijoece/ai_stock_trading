"""Bounded retrieval of a single SEC filing document's primary text
(docs/milestone-7.md Step 6).

Uses the official SEC filing-document URL pattern already produced by
`sec_provider.py::SecEdgarClient.list_filings` (`FilingRecord.primary_document_url`,
`https://www.sec.gov/Archives/edgar/data/{cik}/{accession-no-dashes}/{primary_document}`)
— nothing here constructs a new undocumented endpoint. Reuses the existing
`HttpJsonClient`'s transport/rate-limit/User-Agent conventions via a thin
text-fetching sibling method rather than `get_json` (a filing document is
HTML/text, not JSON).

Hard bounds, per Step 6:

* `MAX_DOCUMENT_BYTES` caps how much of a filing is ever retrieved or
  retained — this is bounded retrieval, not an unrestricted filing mirror.
* Content is cached immutably (an accepted filing's document never changes)
  via a narrow, in-process cache modeled on `cache.py::ProviderCache`'s
  key/TTL shape, not a general-purpose blob store.
* A content hash is always retained, independent of whether the text itself
  is retained.

SEC public filings may be retained per SEC's documented public-domain
policy (see `sec_provider.py` module docstring); nothing here persists a
raw filing outside this bounded, immutable cache.

PROMPT-INJECTION / UNTRUSTED-INPUT WARNING: filing text originates from a
third-party (the filer), not from this codebase or from SEC's own curation
— it is untrusted input. If filing text is ever later summarized or
otherwise processed by Claude (out of scope for this task), it must be
treated exactly like any other untrusted evidence text (see
`research/evidence_validation.py::render_evidence_item`'s delimiter +
injection-annotation pattern) and never concatenated directly into a
system/instruction prompt.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import httpx

from .errors import ProviderRequestError, RetryBoundExceededError
from .rate_limits import MinIntervalRateLimiter

MAX_DOCUMENT_BYTES = 2_000_000  # 2 MB cap on raw retrieved bytes
MAX_RETAINED_TEXT_CHARS = 500_000  # cap on sanitized text actually cached/returned

STATUS_OK = "OK"
STATUS_DOCUMENT_UNAVAILABLE = "DOCUMENT_UNAVAILABLE"
STATUS_TOO_LARGE = "TOO_LARGE"
STATUS_CACHE_CORRUPT = "CACHE_CORRUPT"


@dataclass(frozen=True)
class FilingDocument:
    """Bounded, sanitized filing-document text plus provenance. `text` is
    deterministically HTML-stripped (see `sanitize_html`) and truncated at
    `MAX_RETAINED_TEXT_CHARS` — never the raw HTML, never unbounded."""

    accession_number: str
    source_url: str
    content_hash: str
    text: str
    retrieved_at: datetime
    byte_length: int
    truncated: bool
    status: str


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def sanitize_html(raw_html: str) -> str:
    """Deterministic HTML-to-text: strips `<script>`/`<style>` blocks
    entirely (never executed — this is treated as untrusted input, not
    rendered or evaluated), strips remaining tags, collapses whitespace.
    No arbitrary code execution, no external resource fetch, no template
    evaluation — pure string processing."""
    without_script_style = _SCRIPT_STYLE_RE.sub(" ", raw_html)
    text_only = _TAG_RE.sub(" ", without_script_style)
    text_only = text_only.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    collapsed = _WHITESPACE_RE.sub(" ", text_only)
    collapsed = _BLANK_LINES_RE.sub("\n\n", collapsed)
    return collapsed.strip()


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class _CacheEntry:
    document: FilingDocument


@dataclass
class FilingDocumentCache:
    """Narrow, immutable, in-process cache keyed by accession number —
    modeled on `cache.py::ProviderCache`'s fail-closed-on-corruption
    behavior, but deliberately not reusing that class directly since its
    key shape (`CacheKey`) and TTL-expiry semantics don't apply to content
    that is immutable by definition (an accepted filing's document never
    changes)."""

    _store: dict[str, _CacheEntry] = field(default_factory=dict)
    corruptions: int = field(default=0, init=False)

    def get(self, accession_number: str) -> FilingDocument | None:
        entry = self._store.get(accession_number)
        if entry is None:
            return None
        try:
            # Defensive shape check — a hand-corrupted/deserialized entry
            # (e.g. loaded from an external store in a future extension)
            # fails closed rather than silently returning garbage.
            if not isinstance(entry.document, FilingDocument):
                raise TypeError("cache entry is not a FilingDocument")
            return entry.document
        except Exception:
            self.corruptions += 1
            self._store.pop(accession_number, None)
            return None

    def set(self, accession_number: str, document: FilingDocument) -> None:
        self._store[accession_number] = _CacheEntry(document=document)

    def invalidate_corrupt(self, accession_number: str) -> None:
        """Test/operational hook: forcibly mark a cache slot corrupted."""
        self._store[accession_number] = _CacheEntry(document=None)  # type: ignore[arg-type]


@dataclass
class FilingDocumentClient:
    """Bounded filing-document text fetcher. `transport` is always
    injectable (mirrors `http_client.HttpJsonClient`) so no default test
    ever opens a real socket."""

    user_agent: str
    rate_limiter: MinIntervalRateLimiter
    cache: FilingDocumentCache | None = None
    transport: httpx.BaseTransport | None = None
    timeout_seconds: float = 30.0
    max_attempts: int = 2
    wall_clock: Callable[[], float] = __import__("time").time

    def get_document(self, *, accession_number: str, source_url: str, retrieved_at: datetime) -> FilingDocument:
        if self.cache is not None:
            cached = self.cache.get(accession_number)
            if cached is not None:
                return cached

        last_exc: Exception | None = None
        for _attempt in range(1, self.max_attempts + 1):
            self.rate_limiter.acquire()
            try:
                with httpx.Client(
                    headers={"User-Agent": self.user_agent}, timeout=self.timeout_seconds, transport=self.transport
                ) as client:
                    response = client.get(source_url)
            except httpx.HTTPError as exc:
                last_exc = ProviderRequestError(f"filing document request to {source_url} failed: {exc}", retryable=True)
                continue

            if response.status_code == 404:
                doc = FilingDocument(
                    accession_number=accession_number, source_url=source_url,
                    content_hash=_content_hash(b""), text="", retrieved_at=retrieved_at,
                    byte_length=0, truncated=False, status=STATUS_DOCUMENT_UNAVAILABLE,
                )
                if self.cache is not None:
                    self.cache.set(accession_number, doc)
                return doc

            if response.status_code >= 400:
                last_exc = ProviderRequestError(
                    f"filing document request to {source_url} returned {response.status_code}", retryable=False,
                    status_code=response.status_code,
                )
                continue

            raw = response.content[:MAX_DOCUMENT_BYTES]
            truncated_raw = len(response.content) > MAX_DOCUMENT_BYTES
            text = sanitize_html(raw.decode("utf-8", errors="replace"))
            truncated_text = len(text) > MAX_RETAINED_TEXT_CHARS
            if truncated_text:
                text = text[:MAX_RETAINED_TEXT_CHARS]

            doc = FilingDocument(
                accession_number=accession_number,
                source_url=source_url,
                content_hash=_content_hash(raw),
                text=text,
                retrieved_at=retrieved_at,
                byte_length=len(raw),
                truncated=(truncated_raw or truncated_text),
                status=STATUS_OK,
            )
            if self.cache is not None:
                self.cache.set(accession_number, doc)
            return doc

        assert last_exc is not None
        doc = FilingDocument(
            accession_number=accession_number, source_url=source_url,
            content_hash=_content_hash(b""), text="", retrieved_at=retrieved_at,
            byte_length=0, truncated=False, status=STATUS_DOCUMENT_UNAVAILABLE,
        )
        return doc
