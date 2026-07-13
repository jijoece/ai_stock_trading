"""Real, point-in-time-aware evidence providers (Milestone 6, docs/milestone-6.md).

Nothing in this package writes a recommendation, calls an execution service, or
mutates broker/ledger state — it only retrieves and normalizes raw facts. The
adapter classes in `evidence_adapters.py` satisfy the existing
`research/evidence.py` Protocols (`FundamentalsEvidenceProvider`,
`MarketEvidenceProvider`, `NewsEvidenceProvider`, `SentimentEvidenceProvider`,
`FilingEvidenceProvider`, `PortfolioContextProvider`) — `research/evidence.py`
and `build_evidence_snapshot` require zero changes to consume a real provider.
"""
from __future__ import annotations
