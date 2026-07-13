"""Real SEC EDGAR client (docs/milestone-6.md Step 6).

No API key required — only a compliant identification `User-Agent` header,
per SEC's fair-access policy (https://www.sec.gov/os/webmaster-faq#developers).
Three official, structured JSON endpoints are used; nothing here scrapes a
rendered filing page:

* `https://www.sec.gov/files/company_tickers.json` — ticker -> CIK resolution
* `https://data.sec.gov/submissions/CIK##########.json` — recent filing metadata
* `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` — normalized
  XBRL financial concepts

Point-in-time rule: a filing or company-fact value is only "available" as of
its own `acceptanceDateTime` (filings) or `filed` date (company facts) — never
its `reportDate`/period end, which can be months before the value was
actually published. `list_filings`/`get_company_facts` both accept an
`as_of`/`available_by` cutoff and silently exclude anything accepted/filed
after it (never truncate-then-fabricate; the caller only ever sees rows that
were genuinely knowable at that time).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping

from .cache import CacheKey, ProviderCache, TTL_COMPANY_FACTS, TTL_FILINGS
from .errors import MalformedProviderResponseError, ProviderConfigurationError
from .http_client import HttpJsonClient
from .models import CompanyFactValue, FilingRecord

TICKER_INDEX_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik10}.json"
COMPANY_FACTS_URL_TEMPLATE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"

DEFAULT_USER_AGENT = "agentic-trading-desk-milestone6 research (contact: research@example.com)"


def _parse_accepted_at(raw: str) -> datetime:
    # SEC format: "2026-06-17T22:40:43.000Z"
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


class SecEdgarClient:
    """Real SEC EDGAR client. `http_client` is injected so tests never open a
    real socket (see `http_client.HttpJsonClient`'s `transport` parameter)."""

    def __init__(self, *, http_client: HttpJsonClient, cache: ProviderCache | None = None, user_agent: str = DEFAULT_USER_AGENT):
        if not user_agent or "@" not in user_agent:
            raise ProviderConfigurationError(
                "SEC EDGAR requires a compliant User-Agent identification header including contact information"
            )
        self._http = http_client
        self._cache = cache
        self._user_agent = user_agent
        self._cik_index: Mapping[str, str] | None = None

    def resolve_cik(self, symbol: str) -> str | None:
        """Returns a zero-padded 10-digit CIK string, or None if the ticker
        is not in SEC's public ticker index."""
        symbol = symbol.upper()
        if self._cik_index is None:
            key = CacheKey.build(provider="sec-edgar", operation="ticker_index", symbol="__ALL__")
            cached = self._cache.get(key) if self._cache else None
            if cached is not None:
                self._cik_index = cached
            else:
                payload, _meta = self._http.get_json(TICKER_INDEX_URL, operation="ticker_index", symbol="__ALL__")
                if not isinstance(payload, dict):
                    raise MalformedProviderResponseError("SEC ticker index response was not a JSON object")
                index: dict[str, str] = {}
                for entry in payload.values():
                    ticker = str(entry.get("ticker", "")).upper()
                    cik = entry.get("cik_str")
                    if ticker and cik is not None:
                        index[ticker] = str(cik).zfill(10)
                self._cik_index = index
                if self._cache:
                    self._cache.set(key, index, ttl_seconds=None)
        return self._cik_index.get(symbol)

    def list_filings(self, symbol: str, *, available_by: datetime, cik: str | None = None) -> tuple[FilingRecord, ...]:
        cik = cik or self.resolve_cik(symbol)
        if cik is None:
            return ()
        key = CacheKey.build(provider="sec-edgar", operation="submissions", symbol=symbol, cik=cik)
        payload = self._cache.get(key) if self._cache else None
        if payload is None:
            payload, _meta = self._http.get_json(SUBMISSIONS_URL_TEMPLATE.format(cik10=cik), operation="submissions", symbol=symbol)
            if self._cache:
                self._cache.set(key, payload, ttl_seconds=TTL_FILINGS)
        if not isinstance(payload, dict) or "filings" not in payload:
            raise MalformedProviderResponseError(f"SEC submissions response for CIK {cik} missing 'filings'")

        recent = payload["filings"].get("recent", {})
        required_fields = ("accessionNumber", "filingDate", "acceptanceDateTime", "form", "primaryDocument")
        if not all(f in recent for f in required_fields):
            raise MalformedProviderResponseError(f"SEC submissions response for CIK {cik} missing required recent-filing fields")

        records: list[FilingRecord] = []
        count = len(recent["accessionNumber"])
        for i in range(count):
            accepted_raw = recent["acceptanceDateTime"][i]
            if not accepted_raw:
                continue
            accepted_at = _parse_accepted_at(accepted_raw)
            if accepted_at > available_by:
                continue  # not yet available as of the requested cutoff — no look-ahead
            report_date_raw = recent.get("reportDate", [None] * count)[i]
            form_type = recent["form"][i]
            records.append(FilingRecord(
                accession_number=recent["accessionNumber"][i],
                cik=cik,
                symbol=symbol.upper(),
                form_type=form_type,
                filing_date=date.fromisoformat(recent["filingDate"][i]),
                accepted_at=accepted_at,
                report_period=date.fromisoformat(report_date_raw) if report_date_raw else None,
                primary_document_url=(
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                    f"{recent['accessionNumber'][i].replace('-', '')}/{recent['primaryDocument'][i]}"
                    if recent["primaryDocument"][i] else None
                ),
                is_amendment=form_type.endswith("/A"),
            ))
        return tuple(sorted(records, key=lambda r: r.accepted_at))

    def get_company_facts(self, symbol: str, *, as_of: datetime, cik: str | None = None) -> tuple[CompanyFactValue, ...]:
        cik = cik or self.resolve_cik(symbol)
        if cik is None:
            return ()
        key = CacheKey.build(provider="sec-edgar", operation="companyfacts", symbol=symbol, cik=cik)
        payload = self._cache.get(key) if self._cache else None
        if payload is None:
            payload, _meta = self._http.get_json(COMPANY_FACTS_URL_TEMPLATE.format(cik10=cik), operation="companyfacts", symbol=symbol)
            if self._cache:
                self._cache.set(key, payload, ttl_seconds=TTL_COMPANY_FACTS)
        if not isinstance(payload, dict) or "facts" not in payload:
            raise MalformedProviderResponseError(f"SEC company-facts response for CIK {cik} missing 'facts'")

        gaap = payload["facts"].get("us-gaap", {})
        values: list[CompanyFactValue] = []
        for concept, concept_data in gaap.items():
            units = concept_data.get("units", {})
            for unit_name, entries in units.items():
                for entry in entries:
                    filed_raw = entry.get("filed")
                    if not filed_raw:
                        continue
                    filed_at = date.fromisoformat(filed_raw)
                    if datetime(filed_at.year, filed_at.month, filed_at.day, tzinfo=timezone.utc) > as_of:
                        continue  # restated/late-filed value not yet known as of as_of — no look-ahead
                    try:
                        value = Decimal(str(entry["val"]))
                    except (InvalidOperation, KeyError):
                        continue
                    values.append(CompanyFactValue(
                        concept=concept, unit=unit_name, value=value,
                        period_start=date.fromisoformat(entry["start"]) if entry.get("start") else None,
                        period_end=date.fromisoformat(entry["end"]),
                        fiscal_year=entry.get("fy"), fiscal_period=entry.get("fp"),
                        form_type=entry.get("form", ""), filed_at=filed_at, frame=entry.get("frame"),
                        accession_number=entry.get("accn"),
                    ))
        return tuple(values)
