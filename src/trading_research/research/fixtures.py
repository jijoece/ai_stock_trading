"""Fixture-backed evidence providers for the Milestone 5 vertical slice.

docs/milestone-5.md: "Use one to five fixture-backed symbols for the initial
integration path... Any real external provider must be optional and
separately tested." Everything here is offline and deterministic — same
`(symbol, as_of)` always produces the same `EvidenceBundle` content (though
`retrieved_at`/`as_of` timestamps on the resulting `SourceRecord`s are
derived from the caller-supplied `as_of`, not wall-clock time, which is what
keeps snapshot hashing reproducible in tests).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Mapping

from .evidence import EvidenceBundle, build_evidence_snapshot
from .models import EvidenceItem, EvidenceSnapshot, SourceRecord

# One deliberately thin symbol (XXXX) is included so tests/CLI can exercise
# the missing-data / ANALYSIS_INCOMPLETE path without a second code path.
_FIXTURE_SYMBOLS: Mapping[str, dict] = {
    "AAPL": {
        "fundamentals": {"revenue_growth_yoy": 0.08, "gross_margin": 0.46, "operating_margin": 0.30},
        "headline": "Quarterly results beat consensus revenue estimates on services growth",
        "risk": "Regulatory scrutiny of App Store practices continues in the EU",
    },
    "MSFT": {
        "fundamentals": {"revenue_growth_yoy": 0.15, "gross_margin": 0.69, "operating_margin": 0.44},
        "headline": "Cloud segment growth accelerates on AI infrastructure demand",
        "risk": "Capital expenditure guidance raised materially for data-center buildout",
    },
    "SHEL": {
        "fundamentals": {"revenue_growth_yoy": -0.04, "gross_margin": 0.21, "operating_margin": 0.09},
        "headline": "Refining margins compress on softer product demand",
        "risk": "Exposure to commodity price volatility and carbon-transition policy risk",
    },
    "XXXX": {
        "fundamentals": {},
        "headline": "",
        "risk": "",
    },
}


def fixture_symbols() -> tuple[str, ...]:
    return tuple(_FIXTURE_SYMBOLS.keys())


def is_fixture_symbol(symbol: str) -> bool:
    return symbol in _FIXTURE_SYMBOLS


def fixture_deterministic_factors(symbol: str) -> dict[str, float]:
    return dict(_FIXTURE_SYMBOLS.get(symbol, {}).get("fundamentals", {}))


def fixture_sentiment_metrics(symbol: str) -> dict[str, float]:
    if not is_fixture_symbol(symbol) or symbol == "XXXX":
        return {}
    factors = fixture_deterministic_factors(symbol)
    growth = factors.get("revenue_growth_yoy", 0.0)
    return {"net_sentiment": max(-1.0, min(1.0, growth * 2)), "sample_size": 12}


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class FixtureFundamentalsProvider:
    def fetch(self, symbol: str, as_of: datetime) -> EvidenceBundle:
        fundamentals = _FIXTURE_SYMBOLS.get(symbol, {}).get("fundamentals", {})
        source_id = f"fixture-fundamentals-{symbol}"
        published_at = as_of - timedelta(days=30)
        source = SourceRecord(
            source_id=source_id, source_type="fundamentals", provider="fixture", source_locator=None,
            retrieved_at=as_of, published_at=published_at, effective_at=published_at,
            available_at=as_of - timedelta(days=25), content_hash=_content_hash(repr(sorted(fundamentals.items()))),
            status="ok" if fundamentals else "missing", is_stale=False, point_in_time_safe=True,
            error_code=None if fundamentals else "no_fixture_data",
        )
        if not fundamentals:
            return EvidenceBundle(
                source_records=(source,), evidence_items=(),
                missing_data_reasons=(f"no fixture fundamentals available for {symbol}",),
            )
        items = tuple(
            EvidenceItem(
                evidence_id=f"{source_id}-{key}", source_id=source_id, category="fundamentals",
                title=f"{symbol} {key.replace('_', ' ')}", summary=f"{key}={value}",
                normalized_values={key: value}, as_of=published_at, confidence="high", stale=False,
                conflict_group=None,
            )
            for key, value in sorted(fundamentals.items())
        )
        return EvidenceBundle(source_records=(source,), evidence_items=items)


class FixtureNewsProvider:
    def fetch(self, symbol: str, as_of: datetime) -> EvidenceBundle:
        headline = _FIXTURE_SYMBOLS.get(symbol, {}).get("headline", "")
        source_id = f"fixture-news-{symbol}"
        published_at = as_of - timedelta(days=1)
        source = SourceRecord(
            source_id=source_id, source_type="news", provider="fixture", source_locator=None,
            retrieved_at=as_of, published_at=published_at, effective_at=published_at,
            available_at=as_of - timedelta(hours=12), content_hash=_content_hash(headline),
            status="ok" if headline else "missing", is_stale=False, point_in_time_safe=True,
            error_code=None if headline else "no_fixture_data",
        )
        if not headline:
            return EvidenceBundle(
                source_records=(source,), evidence_items=(), missing_data_reasons=(f"no fixture news available for {symbol}",),
            )
        item = EvidenceItem(
            evidence_id=f"{source_id}-1", source_id=source_id, category="news", title=headline, summary=headline,
            normalized_values={}, as_of=published_at, confidence="medium", stale=False, conflict_group=None,
        )
        return EvidenceBundle(source_records=(source,), evidence_items=(item,))


class FixtureFilingRiskProvider:
    def fetch(self, symbol: str, as_of: datetime) -> EvidenceBundle:
        risk_text = _FIXTURE_SYMBOLS.get(symbol, {}).get("risk", "")
        source_id = f"fixture-filing-{symbol}"
        published_at = as_of - timedelta(days=90)
        source = SourceRecord(
            source_id=source_id, source_type="filing", provider="fixture", source_locator=None,
            retrieved_at=as_of, published_at=published_at, effective_at=published_at,
            available_at=as_of - timedelta(days=85), content_hash=_content_hash(risk_text),
            status="ok" if risk_text else "missing", is_stale=False, point_in_time_safe=True,
            error_code=None if risk_text else "no_fixture_data",
        )
        if not risk_text:
            return EvidenceBundle(source_records=(source,), evidence_items=())
        item = EvidenceItem(
            evidence_id=f"{source_id}-1", source_id=source_id, category="filing", title="Risk-factor excerpt",
            summary=risk_text, normalized_values={}, as_of=published_at, confidence="high", stale=False,
            conflict_group=None,
        )
        return EvidenceBundle(source_records=(source,), evidence_items=(item,))


def build_fixture_snapshot(
    symbol: str,
    as_of: datetime,
    *,
    config_hash: str,
    git_sha: str,
    clock,
) -> EvidenceSnapshot:
    return build_evidence_snapshot(
        symbol, as_of,
        deterministic_factors=fixture_deterministic_factors(symbol),
        sentiment_metrics=fixture_sentiment_metrics(symbol),
        providers=(FixtureFundamentalsProvider(), FixtureNewsProvider(), FixtureFilingRiskProvider()),
        portfolio_context_provider=None, config_hash=config_hash, git_sha=git_sha, clock=clock,
    )
