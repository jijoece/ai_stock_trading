"""Offline, deterministic end-to-end proof of Milestone 9 (docs/milestone-9.md
Section 14):

    persistent test database
    -> several fixture scheduled cycles across multiple market days
    -> integrate cycles into both isolated books
    -> process pending entries
    -> open positions
    -> trigger at least one profit-target exit
    -> trigger at least one stop-loss exit
    -> create SELL intents
    -> simulate fills
    -> update isolated cash and positions
    -> create daily snapshots
    -> reconcile both books
    -> compute metrics
    -> produce soak report
    -> evaluate readiness
    -> rerun same lifecycle dates
    -> prove idempotency
    -> prove no cross-book contamination
    -> prove no live execution

Fixture providers only. No Claude, no network call anywhere in this file.
"""
from __future__ import annotations

import ast
import socket
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.paper_books import cash_ledger, cli_support, execution, positions
from trading_research.paper_books.config import (
    ExecutionSection,
    ExitsSection,
    LifecycleSection,
    PaperBookDefinition,
    PaperBooksConfiguration,
    PendingOrdersSection,
    RiskSection,
    ScheduledIntegrationSection,
    SoakSection,
    ValuationSection,
)
from trading_research.paper_books.lifecycle import run_paper_book_lifecycle
from trading_research.evaluation.price_provider import DeterministicPriceProvider
from trading_research.recommendations.builder import FrozenRecommendation, SIDE_BUY_CANDIDATE, STATUS_ACTIVE
from trading_research.research.evidence_completeness import STATUS_COMPLETE_FOR_SCREENING
from trading_research.research.models import EvidenceItem, EvidenceSnapshot, SourceRecord
from trading_research.research.scheduled_cycle import SymbolCycleResult
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect
from trading_research.storage.research_cycle_repositories import SQLiteResearchCycleRepository, save_symbol_evidence_status
from trading_research.storage.research_repositories import save_evidence_snapshot
from trading_research.storage.trading_repositories import save_frozen_recommendation

DAY0 = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)  # Monday — pre-existing MSFT position
DAY1 = datetime(2026, 1, 6, 20, 0, tzinfo=timezone.utc)  # cycle-1 (AAPL) integrated + MSFT stop-loss triggers
DAY2 = datetime(2026, 1, 7, 20, 0, tzinfo=timezone.utc)  # cycle-2 (GOOG, incomplete) + AAPL profit-target triggers
DAY3 = datetime(2026, 1, 8, 20, 0, tzinfo=timezone.utc)  # pending exit SELL orders resolve


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("Milestone 9 offline e2e must never open a real socket")

    monkeypatch.setattr(socket.socket, "connect", _blocked)


def _config() -> PaperBooksConfiguration:
    return PaperBooksConfiguration(
        version=1, enabled=True,
        baseline=PaperBookDefinition(enabled=True, book_id="BASELINE", starting_cash_usd=Decimal("100000.00")),
        enhanced=PaperBookDefinition(enabled=True, book_id="ENHANCED", starting_cash_usd=Decimal("120000.00")),
        execution=ExecutionSection(provider="local_simulated", allow_external_paper_broker=False, allow_live_broker=False),
        risk=RiskSection(
            max_position_weight=Decimal("0.9"), max_order_notional_usd=Decimal("1000000.00"),
            max_daily_new_notional_usd=Decimal("1000000.00"), minimum_cash_buffer_weight=Decimal("0.02"),
            max_open_positions=20, max_symbol_concentration_weight=Decimal("0.9"),
            reject_stale_market_price_seconds=999999,
        ),
        valuation=ValuationSection(price_source="evidence_snapshot", maximum_price_age_seconds=999999, missing_price_policy="MARK_UNVALUED"),
        scheduled_integration=ScheduledIntegrationSection(enabled=True),
        lifecycle=LifecycleSection(
            enabled=True, pending_orders=PendingOrdersSection(expire_after_market_days=5),
            exits=ExitsSection(
                enabled=True, stop_loss_percent=Decimal("0.08"), profit_target_percent=Decimal("0.15"),
                maximum_holding_market_days=20, exit_on_recommendation_reversal=True,
            ),
            soak=SoakSection(minimum_completed_cycles=2, minimum_market_days=2),
        ),
        config_hash="m9-e2e-config-hash", raw={},
    )


def _evidence_snapshot(symbol: str, close: Decimal, as_of: datetime, bid: Decimal, ask: Decimal) -> EvidenceSnapshot:
    source = SourceRecord(
        source_id=f"src-{symbol}", source_type="market", provider="fixture-market", source_locator=None,
        retrieved_at=as_of, published_at=as_of, effective_at=as_of, available_at=as_of, content_hash="hash",
        status="ok", is_stale=False, point_in_time_safe=True, error_code=None,
    )
    item = EvidenceItem(
        evidence_id=f"{symbol}-market", source_id=source.source_id, category="market", title="market",
        summary="market", normalized_values={"latest_close": float(close), "bid": float(bid), "ask": float(ask)},
        as_of=as_of, confidence="high", stale=False,
    )
    return EvidenceSnapshot(
        snapshot_id=f"snap-m9-{symbol}", symbol=symbol, as_of=as_of, created_at=as_of, source_records=(source,),
        evidence_items=(item,), deterministic_factors={}, sentiment_metrics={}, portfolio_context=None,
        missing_data_reasons=(), conflict_reasons=(), point_in_time_safe=True, config_hash="cfg", git_sha="sha",
    )


def _rec_payload(rec_id: str, symbol: str, shares: int, entry_price: float, ts: datetime) -> dict:
    return {
        "rec_id": rec_id, "run_id": f"run-{rec_id}", "symbol": symbol, "side": SIDE_BUY_CANDIDATE,
        "ts": ts.isoformat(), "price_at_rec": entry_price, "score": 85.0, "confidence": "high",
        "status": STATUS_ACTIVE, "acted": False, "rationale_text": "m9 e2e test", "factors": [],
        "model_version": "test-v1", "prompt_version": "test-v1", "config_hash": "cfg", "git_sha": "sha",
        "risk_plan": {
            "shares": shares, "entry_price": entry_price, "stop_price": entry_price * 0.9,
            "target_price": entry_price * 1.2, "risk_per_share": entry_price * 0.1,
            "dollars_at_risk": shares * entry_price * 0.1, "position_value": shares * entry_price,
            "reward_risk": 2.0, "warnings": [],
        },
    }


def _seed_cycle_1_aapl(conn) -> str:
    """Cycle 1: a real BUY-candidate cycle for AAPL, both arms, both books
    (BOTH_SEPARATE_PAPER_BOOKS) — bid/ask crafted so the local-simulated
    limit order actually crosses immediately (Milestone 8.1's own tier-1
    OBSERVED market-simulation-input path, reused unmodified)."""
    cycle_id = "m9-cycle-aapl"
    snapshot = _evidence_snapshot("AAPL", Decimal("100.00"), DAY1, Decimal("98.00"), Decimal("98.50"))
    save_evidence_snapshot(conn, snapshot)
    baseline_payload = _rec_payload("m9-rec-b-aapl", "AAPL", shares=100, entry_price=100.0, ts=DAY1)
    enhanced_payload = _rec_payload("m9-rec-e-aapl", "AAPL", shares=100, entry_price=100.0, ts=DAY1)
    save_frozen_recommendation(conn, FrozenRecommendation(payload=baseline_payload))
    save_frozen_recommendation(conn, FrozenRecommendation(payload=enhanced_payload))

    cycle_repo = SQLiteResearchCycleRepository(conn)
    cycle_repo.save_cycle_started(cycle_id, "m9-universe", DAY1, "cfg-hash", "SHADOW_ENHANCED", "fixture", DAY1)
    cycle_repo.save_symbol_result(
        cycle_id,
        SymbolCycleResult(
            symbol="AAPL", status="COMPLETED", snapshot_id=snapshot.snapshot_id, research_run_id=None,
            experiment_id="m9-experiment", baseline_recommendation_id="m9-rec-b-aapl",
            enhanced_recommendation_id="m9-rec-e-aapl", baseline_paper_submitted=False,
        ),
        DAY1, DAY1,
    )
    cycle_repo.mark_cycle_finished(cycle_id, "COMPLETED", DAY1)
    save_symbol_evidence_status(conn, {
        "cycle_id": cycle_id, "symbol": "AAPL", "snapshot_id": snapshot.snapshot_id,
        "corporate_status_evidence_id": None, "completeness_result_id": None,
        "screening_completeness": STATUS_COMPLETE_FOR_SCREENING, "research_completeness": STATUS_COMPLETE_FOR_SCREENING,
        "blocking_categories_json": "[]", "policy_version": "test-v1", "created_at": DAY1.isoformat(),
    })
    return cycle_id


def _seed_cycle_2_incomplete(conn) -> str:
    """Cycle 2: a second, real scheduled cycle on a later market day whose
    evidence is deliberately incomplete — both arms correctly SKIPPED, never
    fabricated as executable. Proves "several fixture cycles across
    multiple market days" and "explicit cycle integration only" without
    adding a third executable position to reason about."""
    cycle_id = "m9-cycle-goog-incomplete"
    cycle_repo = SQLiteResearchCycleRepository(conn)
    cycle_repo.save_cycle_started(cycle_id, "m9-universe", DAY2, "cfg-hash", "SHADOW_ENHANCED", "fixture", DAY2)
    cycle_repo.save_symbol_result(
        cycle_id,
        SymbolCycleResult(
            symbol="GOOG", status="COMPLETED", snapshot_id=None, research_run_id=None,
            experiment_id="m9-experiment", baseline_recommendation_id=None,
            enhanced_recommendation_id=None, baseline_paper_submitted=False,
        ),
        DAY2, DAY2,
    )
    cycle_repo.mark_cycle_finished(cycle_id, "COMPLETED", DAY2)
    return cycle_id


def _open_pre_existing_msft_position(conn, cfg, book_id: str, quantity: Decimal) -> None:
    """Represents a position that existed before Milestone 9 soak tracking
    began — built via the same persistence primitives Milestone 8's own
    offline e2e test uses (`positions.apply_buy_fill` + `cash_ledger.settle_buy`),
    not through the lifecycle service."""
    cash_ledger.open_book(conn, book_id=book_id, starting_cash_usd=cfg.book(book_id).starting_cash_usd, config_hash=cfg.config_hash, clock=lambda: DAY0)
    fill_id = f"prior-fill-msft-{book_id}"
    intent_id = f"prior-intent-msft-{book_id}"
    from trading_research.paper_books.models import PaperBookOrderIntent

    prior_intent = PaperBookOrderIntent(
        paper_order_intent_id=intent_id, book_id=book_id, experiment_arm=book_id, cycle_id="cyc-prior",
        recommendation_id="rec-prior-msft", symbol="MSFT", side="BUY", order_type="LIMIT", quantity=quantity,
        limit_price=Decimal("200.00"), notional_usd=quantity * Decimal("200.00"), time_in_force="DAY", as_of=DAY0,
        risk_decision_id="rd-prior", portfolio_snapshot_id="snap-prior", config_hash=cfg.config_hash, created_at=DAY0,
        status="FILLED",
    )
    repo.save_order_intent(conn, prior_intent)
    repo.save_fill(conn, {
        "book_id": book_id, "fill_id": fill_id, "paper_order_intent_id": intent_id, "symbol": "MSFT", "side": "BUY",
        "simulated_market_price": Decimal("200.00"), "limit_price": Decimal("200.00"), "fill_quantity": quantity,
        "fill_price": Decimal("200.00"), "fees_usd": Decimal("0"), "slippage_usd": Decimal("0"),
        "fill_timestamp": DAY0, "simulation_rule_version": "v1",
    })
    positions.apply_buy_fill(conn, book_id, "MSFT", fill_id, quantity, Decimal("200.00"), DAY0)
    cash_ledger.settle_buy(conn, book_id, fill_id, quantity * Decimal("200.00"), Decimal("0"), Decimal("0"), DAY0)


def test_milestone_9_offline_end_to_end(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "m9_e2e.sqlite3"
        conn = connect(db_path)
        cfg = _config()
        monkeypatch.setattr(cli_support, "load_paper_books_config", lambda: cfg)

        # -- pre-existing MSFT positions in both books (different quantity,
        #    proving independent starting state) -------------------------
        _open_pre_existing_msft_position(conn, cfg, "BASELINE", Decimal("100"))
        _open_pre_existing_msft_position(conn, cfg, "ENHANCED", Decimal("150"))
        assert repo.load_position(conn, "BASELINE", "MSFT")["quantity"] == "100"
        assert repo.load_position(conn, "ENHANCED", "MSFT")["quantity"] == "150"

        price_provider = DeterministicPriceProvider()
        # MSFT: day1 close breaches the 8% stop-loss threshold (200*0.92=184).
        price_provider.register("MSFT", DAY1.date(), Decimal("170"))
        # MSFT: day2 close is high enough to cross the day1 SELL's limit price.
        price_provider.register("MSFT", DAY2.date(), Decimal("172"))
        # AAPL: day2 close breaches the 15% profit target (98.6*1.15≈113.4).
        price_provider.register("AAPL", DAY2.date(), Decimal("130"))
        # AAPL: day3 close is high enough to cross the day2 SELL's limit price.
        price_provider.register("AAPL", DAY3.date(), Decimal("132"))

        cycle1_id = _seed_cycle_1_aapl(conn)

        # -- Day 1: explicit cycle integration + MSFT stop-loss triggers ----
        result1 = run_paper_book_lifecycle(
            conn, as_of=DAY1, paper_books_config=cfg, price_provider=price_provider,
            integrate_cycle_ids=(cycle1_id,),
        )
        assert result1.processed_cycle_ids == (cycle1_id,)
        assert result1.failure_reasons == ()
        assert set(result1.books_processed) == {"BASELINE", "ENHANCED"}
        # AAPL entered in both books, isolated risk sizing/positions.
        baseline_aapl = repo.load_position(conn, "BASELINE", "AAPL")
        enhanced_aapl = repo.load_position(conn, "ENHANCED", "AAPL")
        assert baseline_aapl is not None and enhanced_aapl is not None
        msft_exits = [d for d in result1.exit_decisions if d["symbol"] == "MSFT"]
        assert all(d["decision"] == "EXIT_STOP_LOSS" for d in msft_exits)
        assert len(msft_exits) == 2  # both books

        cycle2_id = _seed_cycle_2_incomplete(conn)

        # -- Day 2: second cycle integrated (correctly a no-op — incomplete
        #    evidence), AAPL profit target triggers, MSFT pending SELL fills --
        result2 = run_paper_book_lifecycle(
            conn, as_of=DAY2, paper_books_config=cfg, price_provider=price_provider,
            integrate_cycle_ids=(cycle2_id,),
        )
        assert result2.processed_cycle_ids == (cycle2_id,)
        assert repo.load_position(conn, "BASELINE", "GOOG") is None  # never fabricated
        aapl_exits = [d for d in result2.exit_decisions if d["symbol"] == "AAPL"]
        assert all(d["decision"] == "EXIT_PROFIT_TARGET" for d in aapl_exits)
        assert len(aapl_exits) == 2
        assert result2.pending_orders_filled >= 1  # MSFT SELL from day1 resolves

        # -- Day 3: AAPL pending SELL resolves -------------------------------
        result3 = run_paper_book_lifecycle(conn, as_of=DAY3, paper_books_config=cfg, price_provider=price_provider)
        assert result3.pending_orders_filled >= 1

        for book_id in ("BASELINE", "ENHANCED"):
            msft = repo.load_position(conn, book_id, "MSFT")
            aapl = repo.load_position(conn, book_id, "AAPL")
            assert Decimal(msft["quantity"]) == 0, f"{book_id} MSFT should be fully exited"
            assert Decimal(aapl["quantity"]) == 0, f"{book_id} AAPL should be fully exited"
            assert Decimal(msft["realized_pnl_usd"]) < 0  # stop-loss realized a loss
            assert Decimal(aapl["realized_pnl_usd"]) > 0  # profit target realized a gain

        # -- reconciliation + snapshots + metrics persisted, per book -------
        for book_id in ("BASELINE", "ENHANCED"):
            recon = repo.list_reconciliations(conn, book_id)
            assert recon and recon[-1]["status"] == "MATCHED"
            snapshots = repo.list_snapshots(conn, book_id)
            assert len(snapshots) >= 3  # one per lifecycle day

        # -- daily soak report never declares a winner -----------------------
        report = cli_support.paper_book_soak_report_cli(db_path, as_of=DAY3)
        assert "error" not in report
        assert report["status"] in ("NOT_ENOUGH_HISTORY", "RUNNING", "ATTENTION_REQUIRED", "READY_FOR_ACTIVATION_REVIEW")
        assert "winner" not in str(report).lower()
        assert report["books"]["baseline"]["reconciliation_status"] == "MATCHED"
        assert report["books"]["enhanced"]["reconciliation_status"] == "MATCHED"

        # -- readiness is deterministic and advisory-only --------------------
        readiness = cli_support.paper_book_soak_readiness_cli(db_path, as_of=DAY3)
        assert "error" not in readiness
        assert readiness["result"].startswith("READY_FOR") or readiness["result"].startswith("NOT_READY")
        assert cfg.lifecycle.enabled is True  # readiness never mutates config / activates anything

        # -- idempotency: rerunning the same lifecycle dates changes nothing --
        cash_before = {b: cash_ledger.available_cash(conn, b) for b in ("BASELINE", "ENHANCED")}
        fills_before = {b: len(repo.list_fills(conn, b)) for b in ("BASELINE", "ENHANCED")}
        rerun1 = run_paper_book_lifecycle(conn, as_of=DAY1, paper_books_config=cfg, price_provider=price_provider, integrate_cycle_ids=(cycle1_id,))
        rerun3 = run_paper_book_lifecycle(conn, as_of=DAY3, paper_books_config=cfg, price_provider=price_provider)
        assert rerun1.lifecycle_run_id == result1.lifecycle_run_id
        assert rerun3.lifecycle_run_id == result3.lifecycle_run_id
        for b in ("BASELINE", "ENHANCED"):
            assert cash_ledger.available_cash(conn, b) == cash_before[b]
            assert len(repo.list_fills(conn, b)) == fills_before[b]

        # -- no cross-book contamination: independent cash/quantities --------
        assert cash_ledger.available_cash(conn, "BASELINE") != cash_ledger.available_cash(conn, "ENHANCED")

        conn.close()


def test_no_live_execution_path_in_lifecycle_and_exit_policy():
    """Structural proof: neither `lifecycle.py` nor `exit_policy.py` imports a
    broker/live-trading module, and no `--live` flag exists anywhere in the CLI."""
    import trading_research.paper_books.lifecycle as lifecycle_mod
    import trading_research.paper_books.exit_policy as exit_policy_mod

    forbidden_modules = ("lumibot", "alpaca", "robinhood", "anthropic", "trading_paper_runtime")
    for mod in (lifecycle_mod, exit_policy_mod):
        tree = ast.parse(Path(mod.__file__).read_text(), filename=mod.__file__)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                for forbidden in forbidden_modules:
                    assert not name.startswith(forbidden), f"{mod.__name__} imports forbidden module {name!r}"

    cli_help = subprocess.run(
        [sys.executable, "-m", "trading_research.cli", "--help"], capture_output=True, text=True, check=True,
    )
    assert "--live" not in cli_help.stdout
