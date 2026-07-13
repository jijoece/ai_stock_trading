"""Read-only portfolio-context evidence source (docs/milestone-6.md Step 11).

Reads the existing `paper/ledger.py::PaperLedger` — no broker access, no
account mutation, no new state. Portfolio context is deliberately *not* a
`FundamentalsEvidenceProvider`-style Protocol member because
`research/evidence.py::build_evidence_snapshot` already carries a dedicated
`portfolio_context_provider: PortfolioContextProvider | None` parameter,
separate from company evidence — this class satisfies that Protocol exactly
(`fetch(symbol, as_of) -> Mapping[str, Any] | None`).

No account identifiers appear anywhere in the returned mapping (the paper
ledger has none to redact — it is a single local simulated account).
"""
from __future__ import annotations

from datetime import datetime

from ..paper.ledger import PaperLedger


class LedgerPortfolioContextProvider:
    """Real, local (no network) portfolio-context provider. Returns `None`
    only if the ledger genuinely cannot be read — never a fabricated zero
    position (docs/milestone-6.md: "unavailable portfolio state causes
    explicit missing context")."""

    def __init__(self, ledger: PaperLedger):
        self._ledger = ledger

    def fetch(self, symbol: str, as_of: datetime) -> dict | None:
        try:
            positions = self._ledger.positions()
            settled_cash = self._ledger.settled_cash(as_of)
            total_cash = self._ledger.total_cash()
        except Exception:
            return None  # fail closed to explicit missing context, never fabricated

        symbol_position = next((p for p in positions if p["symbol"] == symbol.upper()), None)
        position_cost_basis = sum(p["qty"] * p["avg_cost"] for p in positions)
        denominator = total_cash + position_cost_basis
        symbol_weight = (
            (symbol_position["qty"] * symbol_position["avg_cost"] / denominator)
            if symbol_position and denominator > 0 else 0.0
        )

        return {
            "snapshot_as_of": as_of.isoformat(),
            "existing_position_shares": symbol_position["qty"] if symbol_position else 0,
            "existing_position_avg_cost": symbol_position["avg_cost"] if symbol_position else None,
            "portfolio_weight_cost_basis": round(symbol_weight, 6),
            "available_settled_cash": round(settled_cash, 2),
            "available_total_cash": round(total_cash, 2),
            "open_position_count": len(positions),
            "note": (
                "portfolio_weight_cost_basis uses cost basis, not live market value "
                "(no live-marking price source is wired into this context provider); "
                "Claude cannot use this context to compute a final order quantity — "
                "deterministic risk/position sizing owns that exclusively."
            ),
        }
