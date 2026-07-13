"""Unit tests for evidence_providers/filing_documents.py —
docs/milestone-7.md Step 27 category B. No real network: every request is
served by `httpx.MockTransport`, matching tests/unit/test_sec_provider.py's
pattern."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from trading_research.evidence_providers.filing_documents import (
    MAX_DOCUMENT_BYTES,
    MAX_RETAINED_TEXT_CHARS,
    STATUS_DOCUMENT_UNAVAILABLE,
    STATUS_OK,
    FilingDocumentCache,
    FilingDocumentClient,
    sanitize_html,
)
from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)
OFFICIAL_URL = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000010/aapl-10k.htm"


def _client(handler, *, cache=None) -> FilingDocumentClient:
    transport = httpx.MockTransport(handler)
    return FilingDocumentClient(
        user_agent="test-agent contact@example.com",
        rate_limiter=MinIntervalRateLimiter(0.0, sleep_fn=lambda s: None),
        transport=transport, cache=cache,
    )


# --- official locator --------------------------------------------------

def test_uses_official_sec_archives_url_pattern():
    seen_urls = []

    def handler(request):
        seen_urls.append(str(request.url))
        return httpx.Response(200, text="<html><body>Hello</body></html>")

    client = _client(handler)
    client.get_document(accession_number="0000320193-26-000010", source_url=OFFICIAL_URL, retrieved_at=AS_OF)
    assert seen_urls == [OFFICIAL_URL]
    assert seen_urls[0].startswith("https://www.sec.gov/Archives/edgar/data/")


# --- document-size cap ---------------------------------------------------

def test_document_size_cap_truncates_large_document():
    big_html = "<html><body>" + ("A" * (MAX_DOCUMENT_BYTES + 1000)) + "</body></html>"

    def handler(request):
        return httpx.Response(200, text=big_html)

    client = _client(handler)
    doc = client.get_document(accession_number="acc-1", source_url=OFFICIAL_URL, retrieved_at=AS_OF)
    assert doc.status == STATUS_OK
    assert doc.byte_length <= MAX_DOCUMENT_BYTES
    assert doc.truncated is True
    assert len(doc.text) <= MAX_RETAINED_TEXT_CHARS


# --- HTML normalization ----------------------------------------------------

def test_html_normalization_strips_tags_and_scripts():
    raw = "<html><head><script>alert('x')</script></head><body><p>Hello&nbsp;World</p></body></html>"
    text = sanitize_html(raw)
    assert "<script>" not in text
    assert "<p>" not in text
    assert "alert" not in text
    assert "Hello" in text
    assert "World" in text


def test_html_normalization_collapses_whitespace_and_blank_lines():
    raw = "<p>Line1</p>\n\n\n\n<p>Line2</p>"
    text = sanitize_html(raw)
    assert "\n\n\n" not in text


# --- section extraction is exercised in test_disclosure_extraction.py,
# since section identification only makes sense in the context of a match.

# --- content hash ------------------------------------------------------

def test_content_hash_is_deterministic_and_stable():
    def handler(request):
        return httpx.Response(200, text="<html><body>Same content</body></html>")

    client1 = _client(handler)
    client2 = _client(handler)
    doc1 = client1.get_document(accession_number="acc-1", source_url=OFFICIAL_URL, retrieved_at=AS_OF)
    doc2 = client2.get_document(accession_number="acc-1", source_url=OFFICIAL_URL, retrieved_at=AS_OF)
    assert doc1.content_hash == doc2.content_hash
    assert len(doc1.content_hash) == 64  # sha256 hex digest


def test_content_hash_differs_for_different_content():
    def handler_a(request):
        return httpx.Response(200, text="<html>A</html>")

    def handler_b(request):
        return httpx.Response(200, text="<html>B</html>")

    doc_a = _client(handler_a).get_document(accession_number="acc-a", source_url=OFFICIAL_URL, retrieved_at=AS_OF)
    doc_b = _client(handler_b).get_document(accession_number="acc-b", source_url=OFFICIAL_URL, retrieved_at=AS_OF)
    assert doc_a.content_hash != doc_b.content_hash


# --- cache ---------------------------------------------------------------

def test_cache_avoids_second_network_call():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text="<html><body>Cached content</body></html>")

    cache = FilingDocumentCache()
    client = _client(handler, cache=cache)
    doc1 = client.get_document(accession_number="acc-1", source_url=OFFICIAL_URL, retrieved_at=AS_OF)
    doc2 = client.get_document(accession_number="acc-1", source_url=OFFICIAL_URL, retrieved_at=AS_OF)
    assert calls["n"] == 1
    assert doc1.content_hash == doc2.content_hash


# --- corrupted cache -------------------------------------------------------

def test_corrupted_cache_entry_falls_back_to_refetch():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text="<html><body>Fresh content</body></html>")

    cache = FilingDocumentCache()
    cache.invalidate_corrupt("acc-1")
    assert cache.corruptions == 0  # not yet triggered
    client = _client(handler, cache=cache)
    doc = client.get_document(accession_number="acc-1", source_url=OFFICIAL_URL, retrieved_at=AS_OF)
    # Corrupted entry must fail closed to a cache miss, not raise or return
    # garbage, and the client must fall through to a real fetch.
    assert cache.corruptions == 1
    assert calls["n"] == 1
    assert doc.status == STATUS_OK


# --- injection annotation present -------------------------------------------

def test_module_documents_injection_risk_and_untrusted_input():
    import trading_research.evidence_providers.filing_documents as mod
    assert "prompt-injection" in mod.__doc__.lower() or "prompt_injection" in mod.__doc__.lower()
    assert "untrusted" in mod.__doc__.lower()


# --- document unavailable ---------------------------------------------------

def test_404_produces_document_unavailable_status():
    def handler(request):
        return httpx.Response(404, text="not found")

    client = _client(handler)
    doc = client.get_document(accession_number="acc-1", source_url=OFFICIAL_URL, retrieved_at=AS_OF)
    assert doc.status == STATUS_DOCUMENT_UNAVAILABLE
    assert doc.text == ""


def test_repeated_5xx_produces_document_unavailable_after_bounded_retries():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    client = _client(handler)
    doc = client.get_document(accession_number="acc-1", source_url=OFFICIAL_URL, retrieved_at=AS_OF)
    assert doc.status == STATUS_DOCUMENT_UNAVAILABLE
    assert calls["n"] == client.max_attempts  # bounded, never infinite


# --- no unrestricted full filing passed to Claude ---------------------------

def test_no_claude_or_anthropic_import_in_filing_documents_module():
    """Structural guard: this module must never import or reference Claude
    directly, matching Step 6's "do not send unrestricted full filings to
    Claude" boundary."""
    import trading_research.evidence_providers.filing_documents as mod
    import inspect

    source = inspect.getsource(mod)
    assert "anthropic" not in source.lower()
    assert "claude" not in source.lower() or "claude" in mod.__doc__.lower()  # only doc mentions are fine
