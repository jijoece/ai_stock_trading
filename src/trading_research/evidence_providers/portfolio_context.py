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

from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from ..models.trading_models import PortfolioAccountSnapshot, PortfolioPositionSnapshot
from ..paper.ledger import PaperLedger

FIXTURE_ACCOUNT_IDENTITY = "fixture:local"
LEDGER_ACCOUNT_IDENTITY = "paper_ledger:local"

SOURCE_FIXTURE = "fixture"
SOURCE_PAPER_LEDGER = "paper_ledger"
SOURCE_UNKNOWN = "unknown"


def fixture_portfolio_account_snapshot(as_of: datetime) -> PortfolioAccountSnapshot:
    """Explicit fixture-mode account state (docs/milestones/26.md B3) — an
    isolated, clearly-labeled stand-in never used on a real-provider path."""
    return PortfolioAccountSnapshot(
        account_equity=Decimal("100000"), settled_cash=Decimal("100000"),
        account_as_of=as_of, positions={}, positions_as_of=as_of,
        account_identity=FIXTURE_ACCOUNT_IDENTITY, account_verified=True,
        account_query_complete=True, positions_query_complete=True,
        source=SOURCE_FIXTURE,
    )


def unverified_portfolio_account_snapshot() -> PortfolioAccountSnapshot:
    return PortfolioAccountSnapshot(
        account_equity=None, settled_cash=None, account_as_of=None, positions={},
        positions_as_of=None, account_identity=None, account_verified=False,
        account_query_complete=False, positions_query_complete=False,
        source=SOURCE_PAPER_LEDGER,
    )


class LedgerPortfolioAccountSnapshotProvider:
    """Real, local (no network) authoritative account-snapshot source for
    the deterministic exposure gate (docs/milestones/26.md B3/B4) — distinct
    from `LedgerPortfolioContextProvider` below, which supplies Claude-facing
    cost-basis evidence text, not the typed snapshot the risk/selector path
    needs.

    The local ledger has no live mark price for open positions and no
    historical point-in-time reconstruction, so:
      * held positions are reported with quantity only (no market_price /
        price_as_of) — `build_portfolio_state` then correctly leaves
        exposure incomplete for any symbol actually held, never a
        fabricated zero;
      * a materially non-"now" `as_of` (a historical/backtest cycle) cannot
        be proven against the live ledger and yields an unverified snapshot
        rather than misrepresenting today's state as historical.
    """

    def __init__(
        self,
        ledger: PaperLedger,
        *,
        account_identity: str = LEDGER_ACCOUNT_IDENTITY,
        clock: Callable[[], datetime] | None = None,
        maximum_query_age_seconds: int = 300,
    ):
        self._ledger = ledger
        self._account_identity = account_identity
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._maximum_query_age_seconds = maximum_query_age_seconds

    def fetch(self, as_of: datetime) -> PortfolioAccountSnapshot:
        retrieved_at = self._clock()
        if (
            as_of.tzinfo is None
            or as_of > retrieved_at
            or (retrieved_at - as_of).total_seconds() > self._maximum_query_age_seconds
        ):
            return unverified_portfolio_account_snapshot()

        try:
            positions = self._ledger.positions()
            settled_cash = self._ledger.settled_cash(as_of)
            total_cash = self._ledger.total_cash()
        except Exception:
            return unverified_portfolio_account_snapshot()  # fail closed, never a fabricated snapshot

        position_snapshots = {
            p["symbol"]: PortfolioPositionSnapshot(quantity=int(p["qty"]), market_price=None, price_as_of=None)
            for p in positions
        }
        return PortfolioAccountSnapshot(
            account_equity=Decimal(str(total_cash)) if not positions else None,
            settled_cash=Decimal(str(settled_cash)), account_as_of=as_of,
            positions=position_snapshots, positions_as_of=as_of,
            account_identity=self._account_identity, account_verified=True,
            account_query_complete=True, positions_query_complete=True,
            source=SOURCE_PAPER_LEDGER,
        )


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
