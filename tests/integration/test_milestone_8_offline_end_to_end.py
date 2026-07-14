"""Offline, deterministic end-to-end proof of Milestone 8's isolated paper
books (docs/milestone-8.md Step 23):

    one shared evidence snapshot
    -> baseline recommendation
    -> enhanced recommendation
    -> BASELINE book risk decision
    -> ENHANCED book risk decision
    -> independent order intents
    -> independent paper-runtime submissions (local-simulated)
    -> independent fills
    -> independent cash ledgers
    -> independent positions
    -> mark-to-market snapshots
    -> independent reconciliation
    -> performance metrics
    -> comparison result
    -> no live execution

Uses intentionally different portfolio state (different starting cash) so
the *same* symbol/recommendation produces different approved quantities
across books, and proves the difference is deterministic and persisted, not
cross-contamination (docs/milestone-8.md Step 23's explicit requirement).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.evaluation.price_provider import PricePoint
from trading_research.paper_books import (
    cash_ledger,
    comparison,
    execution,
    order_intent,
    positions,
    promotion_evidence,
    reconciliation,
    risk,
    valuation,
)
from trading_research.paper_books.config import RiskSection
from trading_research.paper_books.experiment_assignment import PaperBookExperimentAssignment, save_assignment
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect

AS_OF = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
NEXT_DAY = AS_OF + timedelta(days=1)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "milestone8_e2e.db")
        yield c
        c.close()


def _risk_config() -> RiskSection:
    return RiskSection(
        max_position_weight=Decimal("0.50"), max_order_notional_usd=Decimal("100000.00"),
        max_daily_new_notional_usd=Decimal("100000.00"), minimum_cash_buffer_weight=Decimal("0.05"),
        max_open_positions=20, max_symbol_concentration_weight=Decimal("0.50"),
        reject_stale_market_price_seconds=900,
    )


class _FakePriceProvider:
    def __init__(self, price: Decimal):
        self.price = price

    def get_close(self, symbol, as_of):
        return PricePoint(symbol=symbol, as_of=as_of, close=self.price, source="fixture")


def test_full_isolated_dual_book_pipeline(conn):
    experiment_id = "exp-m8-e2e"
    cycle_id = "cycle-m8-e2e"
    symbol = "AAPL"
    reference_price = Decimal("150.00")
    quantity_hint = Decimal("100")  # deliberately large — different books will size it differently

    # -- one shared evidence snapshot / as_of / recommendation timestamp --
    evidence_snapshot_id = "shared-evidence-snapshot-1"
    baseline_recommendation_id = "rec-baseline-1"
    enhanced_recommendation_id = "rec-enhanced-1"

    # -- identical starting cash (required for fair comparability) but
    # intentionally different *current* portfolio state: ENHANCED already
    # holds a large pre-existing MSFT position that consumes much of its
    # position/cash capacity, so the *same* AAPL recommendation is sized
    # differently across books for a deterministic, persisted portfolio
    # reason — never randomness, never cross-book contamination.
    cfg_hash = "e2e-config-hash"
    baseline_book = cash_ledger.open_book(
        conn, book_id="BASELINE", starting_cash_usd=Decimal("100000.00"), config_hash=cfg_hash, clock=lambda: AS_OF,
    )
    enhanced_book = cash_ledger.open_book(
        conn, book_id="ENHANCED", starting_cash_usd=Decimal("100000.00"), config_hash=cfg_hash, clock=lambda: AS_OF,
    )
    from trading_research.paper_books.models import PaperBookOrderIntent as _PriorIntent

    prior_intent = _PriorIntent(
        paper_order_intent_id="prior-intent-msft", book_id="ENHANCED", experiment_arm="ENHANCED", cycle_id="cyc-prior",
        recommendation_id="rec-prior-msft", symbol="MSFT", side="BUY", order_type="LIMIT", quantity=Decimal("280"),
        limit_price=Decimal("300.00"), notional_usd=Decimal("84000.00"), time_in_force="DAY", as_of=AS_OF,
        risk_decision_id="rd-prior", portfolio_snapshot_id="snap-prior", config_hash=cfg_hash, created_at=AS_OF,
        status="FILLED",
    )
    repo.save_order_intent(conn, prior_intent)
    repo.save_fill(conn, {
        "book_id": "ENHANCED", "fill_id": "prior-fill-msft", "paper_order_intent_id": "prior-intent-msft",
        "symbol": "MSFT", "side": "BUY", "simulated_market_price": Decimal("300.00"), "limit_price": Decimal("300.00"),
        "fill_quantity": Decimal("280"), "fill_price": Decimal("300.00"), "fees_usd": Decimal("0"),
        "slippage_usd": Decimal("0"), "fill_timestamp": AS_OF, "simulation_rule_version": "v1",
    })
    positions.apply_buy_fill(conn, "ENHANCED", "MSFT", "prior-fill-msft", Decimal("280"), Decimal("300.00"), AS_OF)
    cash_ledger.settle_buy(conn, "ENHANCED", "prior-fill-msft", Decimal("84000.00"), Decimal("0"), Decimal("0"), AS_OF)

    save_assignment(conn, PaperBookExperimentAssignment(
        experiment_id=experiment_id, cycle_id=cycle_id, symbol=symbol, as_of=AS_OF,
        evidence_snapshot_id=evidence_snapshot_id, baseline_recommendation_id=baseline_recommendation_id,
        enhanced_recommendation_id=enhanced_recommendation_id, baseline_book_id="BASELINE",
        enhanced_book_id="ENHANCED", baseline_intent_id=None, enhanced_intent_id=None,
    ), clock=lambda: AS_OF)

    risk_cfg = _risk_config()
    initial_price_provider = _FakePriceProvider(Decimal("300.00"))  # prices ENHANCED's prior MSFT lot
    intents = {}
    risk_decisions = {}
    for book_id, book, recommendation_id in (
        ("BASELINE", baseline_book, baseline_recommendation_id),
        ("ENHANCED", enhanced_book, enhanced_recommendation_id),
    ):
        snap = valuation.build_portfolio_snapshot(
            conn, book_id, AS_OF, price_provider=initial_price_provider, maximum_price_age_seconds=900,
        )
        context = risk.build_portfolio_context(conn, book_id, AS_OF, snap, symbol, Decimal("0"))
        decision = risk.evaluate_paper_risk(
            book_status=book.status, experiment_arm=book.experiment_arm, expected_arm=book.experiment_arm,
            context=context, requested_quantity_hint=quantity_hint, reference_price=reference_price,
            reference_price_age_seconds=0, reference_price_point_in_time_safe=True, risk_config=risk_cfg,
        )
        risk_decisions[book_id] = decision
        rd_id = order_intent.persist_risk_decision(
            conn, book_id, cycle_id, recommendation_id, symbol, decision, snap.snapshot_id, lambda: AS_OF,
        )
        intent = order_intent.build_order_intent(
            book_id=book_id, experiment_arm=book.experiment_arm, cycle_id=cycle_id,
            recommendation_id=recommendation_id, symbol=symbol, risk_decision=decision, risk_decision_id=rd_id,
            portfolio_snapshot_id=snap.snapshot_id, config_hash=cfg_hash, as_of=AS_OF, clock=lambda: AS_OF,
        )
        intents[book_id] = intent

    # -- same recommendation timestamp/reference price, different approved sizes --
    assert risk_decisions["BASELINE"].approved_quantity != risk_decisions["ENHANCED"].approved_quantity
    # ENHANCED's pre-existing MSFT position consumes much of its own
    # position/cash capacity, so its approved AAPL size is smaller — a
    # deterministic, persisted portfolio reason, not randomness, and not
    # cross-book contamination (BASELINE never held or sized against MSFT).
    assert risk_decisions["ENHANCED"].approved_quantity < risk_decisions["BASELINE"].approved_quantity

    # -- independent, book-aware order intent IDs --------------------------
    assert intents["BASELINE"].paper_order_intent_id != intents["ENHANCED"].paper_order_intent_id

    # -- independent local-simulated paper-runtime submissions/fills -------
    # A conservative limit order (limit == reference_price exactly) only
    # crosses when the simulated market is genuinely at-or-below that price
    # net of spread/slippage — set bid/ask below reference_price so both
    # books' orders actually fill this cycle.
    market = execution.MarketSimulationInput(bid=Decimal("148.90"), ask=Decimal("149.10"))
    outcomes = {}
    for book_id in ("BASELINE", "ENHANCED"):
        outcomes[book_id] = execution.submit_and_simulate(conn, intents[book_id], market, AS_OF)
        assert outcomes[book_id]["status"] == "FILLED"

    # -- independent cash ledgers -------------------------------------------
    baseline_cash = cash_ledger.available_cash(conn, "BASELINE")
    enhanced_cash = cash_ledger.available_cash(conn, "ENHANCED")
    assert baseline_cash != enhanced_cash
    assert baseline_cash < Decimal("100000.00")
    assert enhanced_cash < Decimal("16000.00")  # 100000 - 84000 (MSFT) - AAPL cost

    # -- independent positions -----------------------------------------------
    baseline_position = repo.load_position(conn, "BASELINE", symbol)
    enhanced_position = repo.load_position(conn, "ENHANCED", symbol)
    assert Decimal(baseline_position["quantity"]) == risk_decisions["BASELINE"].approved_quantity
    assert Decimal(enhanced_position["quantity"]) == risk_decisions["ENHANCED"].approved_quantity
    assert baseline_position["quantity"] != enhanced_position["quantity"]

    # -- mark-to-market snapshots (both books, both days) --------------------
    provider = _FakePriceProvider(Decimal("155.00"))
    valuation.build_portfolio_snapshot(conn, "BASELINE", NEXT_DAY, price_provider=provider, maximum_price_age_seconds=900)
    valuation.build_portfolio_snapshot(conn, "ENHANCED", NEXT_DAY, price_provider=provider, maximum_price_age_seconds=900)

    # -- independent reconciliation ------------------------------------------
    baseline_recon = reconciliation.reconcile_book(conn, "BASELINE", NEXT_DAY)
    enhanced_recon = reconciliation.reconcile_book(conn, "ENHANCED", NEXT_DAY)
    assert baseline_recon["status"] == reconciliation.STATUS_MATCHED
    assert enhanced_recon["status"] == reconciliation.STATUS_MATCHED

    # -- performance metrics + comparison + promotion evidence --------------
    cmp = comparison.build_comparison(
        conn, experiment_id, "BASELINE", "ENHANCED", AS_OF - timedelta(hours=1), NEXT_DAY + timedelta(hours=1),
        clock=lambda: NEXT_DAY,
    )
    assert cmp.comparable is True
    enhanced_metrics = repo.load_daily_metrics(conn, "ENHANCED", cmp.enhanced_metrics_id)["metrics"]
    result, reasons = promotion_evidence.evaluate_promotion_evidence(
        cmp, enhanced_metrics, cycle_count=1, min_comparable_cycles=1, min_trading_days=1, min_closed_trades=0,
        operational_health_ok=True, reconciliation_ok=True,
    )
    assert isinstance(result, str)
    assert result != "PROMOTED" and "AUTO_PROMOT" not in result  # no automatic-promotion status value ever exists

    # -- no cross-book contamination proof -----------------------------------
    baseline_fills = repo.list_fills(conn, "BASELINE")
    enhanced_fills = repo.list_fills(conn, "ENHANCED")
    assert {f["fill_id"] for f in baseline_fills}.isdisjoint({f["fill_id"] for f in enhanced_fills})
    assert repo.load_position(conn, "BASELINE", symbol)["quantity"] != "0"
    assert repo.load_position(conn, "ENHANCED", symbol)["quantity"] != "0"


def test_no_live_execution_path_exists():
    """Structural proof: nothing in the paper_books package imports a
    broker/live-trading module, and no CLI flag named --live exists."""
    import ast

    import trading_research.paper_books as pkg

    package_dir = Path(pkg.__file__).parent
    forbidden_modules = ("lumibot", "alpaca", "robinhood", "anthropic", "trading_paper_runtime")
    for py_file in package_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                for forbidden in forbidden_modules:
                    assert not name.startswith(forbidden), f"{py_file.name} imports forbidden module {name!r}"

    cli_help = subprocess.run(
        [sys.executable, "-m", "trading_research.cli", "--help"], capture_output=True, text=True, check=True,
    )
    assert "--live" not in cli_help.stdout
