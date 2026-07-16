from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_research.paper_books import cash_ledger, execution
from trading_research.paper_books.config import (
    ExecutionSection, ExternalBrokerSection, PaperBookDefinition,
    PaperBooksConfiguration, RiskSection, ScheduledIntegrationSection, ValuationSection,
)
from trading_research.paper_books.external_broker import (
    ExternalPaperError, STATE_FILLED, STATE_SUBMITTED, STATE_UNKNOWN,
    _safety_checks, _verify_fingerprint_history, cancel_external_paper_order,
    derive_external_order_identity, preview_external_paper_order,
    reconcile_external_paper_order, retry_external_paper_order, submit_external_paper_order,
)
from trading_research.paper_books.models import PaperBookOrderIntent, PaperRiskDecision, RISK_APPROVED
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect


NOW = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
FINGERPRINT = "acct_0123456789abcdef0123456789abcdef"


def _config(*, submission: bool = True, external_enabled: bool = True, books=("BASELINE",)):
    return PaperBooksConfiguration(
        version=1, enabled=True,
        baseline=PaperBookDefinition(True, "BASELINE", Decimal("100000")),
        enhanced=PaperBookDefinition(True, "ENHANCED", Decimal("100000")),
        execution=ExecutionSection("local_simulated", False, False),
        risk=RiskSection(
            Decimal("0.10"), Decimal("1000"), Decimal("5000"), Decimal("0.10"), 20,
            Decimal("0.10"), 900,
        ),
        valuation=ValuationSection("evidence_snapshot", 900, "MARK_UNVALUED"),
        scheduled_integration=ScheduledIntegrationSection(False), config_hash="cfg-m11", raw={},
        external_broker=ExternalBrokerSection(
            external_enabled, "alpaca_paper", submission, tuple(books), True, 300,
            Decimal("100"), ("limit",), ("day",), 1,
        ),
    )


def _seed(conn):
    cash_ledger.open_book(
        conn, book_id="BASELINE", starting_cash_usd=Decimal("100000"),
        config_hash="cfg-m11", clock=lambda: NOW,
    )
    cash_ledger.open_book(
        conn, book_id="ENHANCED", starting_cash_usd=Decimal("100000"),
        config_hash="cfg-m11", clock=lambda: NOW,
    )
    decision = PaperRiskDecision(
        RISK_APPROVED, Decimal("80"), Decimal("80"), Decimal("2"), (), "risk-v1",
    )
    repo.save_risk_decision(
        conn, "risk-1", "BASELINE", "cycle-1", "rec-1", "AAPL", decision, "snap-1", NOW,
    )
    intent = PaperBookOrderIntent(
        paper_order_intent_id="intent-1", book_id="BASELINE", experiment_arm="BASELINE",
        cycle_id="cycle-1", recommendation_id="rec-1", symbol="AAPL", side="BUY",
        order_type="LIMIT", quantity=Decimal("2"), limit_price=Decimal("40"),
        notional_usd=Decimal("80"), time_in_force="DAY", as_of=NOW,
        risk_decision_id="risk-1", portfolio_snapshot_id="snap-1", config_hash="cfg-m11",
        created_at=NOW,
    )
    repo.save_order_intent(conn, intent)
    return repo.load_order_intent(conn, "BASELINE", "intent-1")


class FakeRuntime:
    def __init__(self):
        self.submit_calls = 0
        self.cancel_calls = 0
        self.preview_calls = 0
        self.raise_submit = False
        self.create_before_raise = False
        self.order = None
        self.fills = []
        self.cash = Decimal("100000")
        self.position = Decimal("0")

    def account_check(self, book_id):
        return {
            "provider": "alpaca_paper", "environment": "paper", "book_id": book_id,
            "account_fingerprint": FINGERPRINT, "paper_endpoint_verified": True,
        }

    def preview_limit_order(self, payload):
        self.preview_calls += 1
        return {
            "provider": "alpaca_paper", "environment": "paper", "book_id": payload["book_id"],
            "client_order_id": payload["client_order_id"], "account_fingerprint": FINGERPRINT,
            "result": "APPROVED", "reasons": [],
        }

    def _make_order(self, payload):
        return {
            "provider": "alpaca_paper", "environment": "paper", "account_fingerprint": FINGERPRINT,
            "book_id": payload["book_id"], "client_order_id": payload["client_order_id"],
            "broker_order_id": "broker-1", "symbol": payload["symbol"], "side": payload["side"],
            "quantity": payload["quantity"], "limit_price": payload["limit_price"],
            "time_in_force": "DAY", "status": "ACCEPTED", "filled_quantity": 0,
            "average_fill_price": None, "submitted_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
            "rejection_code": None,
        }

    def submit_limit_order(self, payload):
        self.submit_calls += 1
        if self.order is None and (not self.raise_submit or self.create_before_raise):
            self.order = self._make_order(payload)
        if self.raise_submit:
            raise TimeoutError("ambiguous timeout")
        return dict(self.order)

    def get_order_by_client_order_id(self, book_id, client_order_id):
        return dict(self.order) if self.order and self.order["client_order_id"] == client_order_id else None

    def list_order_fills(self, book_id, client_order_id):
        return [dict(fill) for fill in self.fills]

    def add_fill(self, quantity):
        quantity = Decimal(str(quantity))
        index = len(self.fills) + 1
        self.fills.append({
            "fill_id": f"fill-{index}", "broker_order_id": "broker-1",
            "client_order_id": self.order["client_order_id"], "book_id": "BASELINE",
            "symbol": "AAPL", "side": "BUY", "quantity": str(quantity), "price": "40",
            "filled_at": NOW.isoformat(), "account_fingerprint": FINGERPRINT,
        })
        self.cash -= quantity * Decimal("40")
        self.position += quantity
        cumulative = sum(Decimal(fill["quantity"]) for fill in self.fills)
        self.order["filled_quantity"] = int(cumulative)
        self.order["average_fill_price"] = "40"
        self.order["status"] = "FILLED" if cumulative == 2 else "PARTIALLY_FILLED"

    def get_external_positions(self, book_id):
        positions = [] if self.position == 0 else [{
            "symbol": "AAPL", "quantity": str(self.position), "average_entry_price": "40",
            "market_value": str(self.position * 40), "as_of": NOW.isoformat(),
        }]
        return {"book_id": book_id, "account_fingerprint": FINGERPRINT, "positions": positions}

    def get_external_account_snapshot(self, book_id):
        return {
            "provider": "alpaca_paper", "environment": "paper", "book_id": book_id,
            "account_fingerprint": FINGERPRINT, "cash": str(self.cash), "equity": "100000",
            "buying_power": None, "currency": "USD", "as_of": NOW.isoformat(),
        }

    def cancel_external_order(self, book_id, client_order_id, account_fingerprint):
        self.cancel_calls += 1
        self.order["status"] = "CANCELLED"
        return dict(self.order)


def _preview(conn, runtime, cfg):
    return preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        runtime=runtime, config=cfg, clock=lambda: NOW,
    )


def test_disabled_and_one_account_one_book_fail_closed():
    with pytest.raises(Exception):
        _config(books=("BASELINE", "ENHANCED"))


def test_success_partial_final_fill_and_replay_are_book_scoped():
    conn = connect(":memory:")
    intent = _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    result = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="approved paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["status"] == STATE_SUBMITTED
    assert runtime.submit_calls == 1
    runtime.add_fill(1)
    first = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert first["status"] == "MATCHED"
    runtime.add_fill(1)
    second = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert second["status"] == "MATCHED"
    assert repo.load_latest_external_order_event(conn, "BASELINE", preview["client_order_id"])["new_state"] == STATE_FILLED
    assert len(repo.list_fills_for_intent(conn, "BASELINE", "intent-1")) == 2
    assert repo.list_positions(conn, "ENHANCED") == []
    replay = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="replay", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert runtime.submit_calls == 1
    assert replay["duplicate_submit"] is False


def test_ambiguous_submission_is_repaired_by_lookup_without_resubmit():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    runtime.raise_submit = True
    runtime.create_before_raise = True
    preview = _preview(conn, runtime, cfg)
    result = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["status"] == STATE_UNKNOWN
    with pytest.raises(ExternalPaperError, match="lookup"):
        submit_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
            operator="alice", reason="must fail", runtime=runtime, config=cfg, clock=lambda: NOW,
        )
    repaired = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert repaired["status"] == "MATCHED"
    assert repo.load_latest_external_order_event(conn, "BASELINE", preview["client_order_id"])["new_state"] == STATE_SUBMITTED
    assert runtime.submit_calls == 1


def test_authoritative_not_found_allows_one_explicit_retry():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    runtime.raise_submit = True
    preview = _preview(conn, runtime, cfg)
    result = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["status"] == STATE_UNKNOWN
    missing = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert missing["status"] == "ORDER_MISSING_AT_BROKER"
    runtime.raise_submit = False
    retried = retry_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        reason="authoritative not-found retry", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert retried["status"] == STATE_SUBMITTED
    assert runtime.submit_calls == 2
    with pytest.raises(ExternalPaperError):
        retry_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            reason="second retry blocked", runtime=runtime, config=cfg, clock=lambda: NOW,
        )


def test_identity_is_deterministic_and_book_scoped():
    conn = connect(":memory:")
    intent = _seed(conn)
    first = derive_external_order_identity(intent)
    second = derive_external_order_identity(intent)
    assert first == second
    assert first[0].startswith("epb-baseline-")


def test_explicit_cancel_remains_available_after_submission_disable_and_mismatch():
    conn = connect(":memory:")
    _seed(conn)
    runtime = FakeRuntime()
    preview = _preview(conn, runtime, _config())
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=_config(), clock=lambda: NOW,
    )
    runtime.cash = Decimal("99999")
    mismatch = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=_config(),
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert mismatch["status"] == "CASH_MISMATCH"

    cancelled = cancel_external_paper_order(
        conn, book_id="BASELINE", client_order_id=preview["client_order_id"], operator="alice",
        reason="risk-reducing cancellation", runtime=runtime, config=_config(submission=False),
        clock=lambda: NOW,
    )
    assert cancelled["status"] == "CANCELLED"
    assert runtime.cancel_calls == 1
    assert cash_ledger.reserved_cash(conn, "BASELINE") == 0


def test_cumulative_fill_fallback_applies_only_delta_and_preserves_notional():
    conn = connect(":memory:")
    _seed(conn)
    runtime = FakeRuntime()
    preview = _preview(conn, runtime, _config())
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=_config(), clock=lambda: NOW,
    )
    runtime.order.update(status="PARTIALLY_FILLED", filled_quantity=1, average_fill_price="40")
    runtime.fills = [{
        "fill_id": "alpaca-cumulative-broker-1-1", "broker_order_id": "broker-1",
        "client_order_id": preview["client_order_id"], "book_id": "BASELINE", "symbol": "AAPL",
        "side": "BUY", "quantity": "1", "price": "40", "filled_at": NOW.isoformat(),
        "account_fingerprint": FINGERPRINT,
    }]
    runtime.cash, runtime.position = Decimal("99960"), Decimal("1")
    assert reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=_config(),
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )["status"] == "MATCHED"

    runtime.order.update(status="FILLED", filled_quantity=2, average_fill_price="41")
    runtime.fills = [{
        "fill_id": "alpaca-cumulative-broker-1-2", "broker_order_id": "broker-1",
        "client_order_id": preview["client_order_id"], "book_id": "BASELINE", "symbol": "AAPL",
        "side": "BUY", "quantity": "2", "price": "41", "filled_at": NOW.isoformat(),
        "account_fingerprint": FINGERPRINT,
    }]
    runtime.cash, runtime.position = Decimal("99918"), Decimal("2")
    assert reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=_config(),
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )["status"] == "MATCHED"
    fills = repo.list_fills_for_intent(conn, "BASELINE", "intent-1")
    assert [(fill["fill_quantity"], fill["fill_price"]) for fill in fills] == [("1", "40"), ("1", "42")]


def test_account_fingerprint_cannot_be_reused_for_another_book():
    conn = connect(":memory:")
    _seed(conn)
    conn.execute(
        "INSERT INTO paper_external_order_events "
        "(external_order_event_id, external_order_scope_id, book_id, paper_order_intent_id, "
        "client_order_id, account_fingerprint, previous_state, new_state, payload_hash, quantity, "
        "limit_price, operator, reason, created_at, policy_version, config_hash, attempt_number) "
        "VALUES ('event-1', 'scope-1', 'BASELINE', 'intent-1', 'epb-baseline-one', ?, "
        "'NOT_SUBMITTED', 'PREVIEWED', 'hash', '1', '1', 'alice', 'test', ?, 'v1', 'cfg', 0)",
        (FINGERPRINT, NOW.isoformat()),
    )
    conn.commit()
    with pytest.raises(ExternalPaperError, match="already mapped"):
        _verify_fingerprint_history(conn, "ENHANCED", FINGERPRINT)


def test_critical_reconciliation_stays_active_per_order_scope():
    conn = connect(":memory:")
    _seed(conn)
    base = {
        "book_id": "BASELINE", "paper_order_intent_id": "intent-1",
        "account_fingerprint": FINGERPRINT, "statuses": ("CASH_MISMATCH",), "details": {},
        "critical": 1, "policy_version": "v1", "config_hash": "cfg",
    }
    repo.save_external_reconciliation(conn, {
        **base, "reconciliation_id": "r1", "client_order_id": "order-a", "status": "CASH_MISMATCH",
        "created_at": NOW.isoformat(),
    })
    repo.save_external_reconciliation(conn, {
        **base, "reconciliation_id": "r2", "client_order_id": "order-b", "status": "MATCHED",
        "statuses": ("MATCHED",), "critical": 0, "created_at": NOW.isoformat(),
    })
    with pytest.raises(ExternalPaperError, match="latest external reconciliation is critical"):
        _safety_checks(conn, "BASELINE")


def test_external_evidence_permanently_blocks_local_simulated_fill():
    conn = connect(":memory:")
    _seed(conn)
    _preview(conn, FakeRuntime(), _config())
    intent = PaperBookOrderIntent(
        paper_order_intent_id="intent-1", book_id="BASELINE", experiment_arm="BASELINE",
        cycle_id="cycle-1", recommendation_id="rec-1", symbol="AAPL", side="BUY",
        order_type="LIMIT", quantity=Decimal("2"), limit_price=Decimal("40"),
        notional_usd=Decimal("80"), time_in_force="DAY", as_of=NOW,
        risk_decision_id="risk-1", portfolio_snapshot_id="snap-1", config_hash="cfg-m11",
        created_at=NOW,
    )
    with pytest.raises(execution.FillSimulationError, match="externally scoped"):
        execution.submit_and_simulate(
            conn, intent, execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW,
        )
    assert repo.list_fills_for_intent(conn, "BASELINE", "intent-1") == []
