from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.paper_books import cash_ledger, execution, positions
from trading_research.paper_books.config import (
    ExecutionSection, ExternalBrokerSection, PaperBookDefinition,
    PaperBooksConfiguration, RiskSection, ScheduledIntegrationSection, ValuationSection,
)
from trading_research.paper_books.external_broker import (
    ExternalPaperError, STATE_FILLED, STATE_REJECTED, STATE_SUBMITTED, STATE_UNKNOWN,
    _reserve_daily_notional, _safety_checks, _verify_fingerprint_history,
    activate_external_reconciliation_baseline, cancel_external_paper_order,
    derive_external_order_identity, preview_external_paper_order,
    reconcile_external_paper_order, retry_external_paper_order, submit_external_paper_order,
)
from trading_research.paper_books.models import PaperBookOrderIntent, PaperRiskDecision, RISK_APPROVED
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect


NOW = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
FINGERPRINT = "acct_0123456789abcdef0123456789abcdef"


def _config(*, submission: bool = True, external_enabled: bool = True, books=("BASELINE",), maximum_retry_attempts=1):
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
            Decimal("100"), Decimal("300"), ("limit",), ("day",), maximum_retry_attempts,
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


def _seed_sell(conn, quantity=Decimal("10"), *, sell_quantity=Decimal("4"), limit_price=Decimal("20")):
    """Seed a confirmed long AAPL position plus one approved external SELL intent."""
    _seed(conn)
    positions.apply_buy_fill(conn, "BASELINE", "AAPL", "seed-fill", quantity, Decimal("30"), NOW)
    intent = PaperBookOrderIntent(
        paper_order_intent_id="intent-sell", book_id="BASELINE", experiment_arm="BASELINE",
        cycle_id="cycle-1", recommendation_id="rec-1", symbol="AAPL", side="SELL",
        order_type="LIMIT", quantity=sell_quantity, limit_price=limit_price,
        notional_usd=sell_quantity * limit_price, time_in_force="DAY", as_of=NOW,
        risk_decision_id="risk-1", portfolio_snapshot_id="snap-1", config_hash="cfg-m11",
        created_at=NOW,
    )
    repo.save_order_intent(conn, intent)
    return repo.load_order_intent(conn, "BASELINE", "intent-sell")


def _activate_baseline(conn, runtime, cfg, book_id="BASELINE"):
    """Milestone 24 Part A3: submission now fails closed without an
    explicitly activated reconciliation baseline — every test that submits
    must activate one first (idempotent, so calling it more than once is
    harmless)."""
    return activate_external_reconciliation_baseline(
        conn, book_id=book_id, operator="alice", runtime=runtime, config=cfg, clock=lambda: NOW,
    )


def _preview_sell(conn, runtime, cfg):
    _activate_baseline(conn, runtime, cfg)
    return preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-sell", operator="alice",
        runtime=runtime, config=cfg, clock=lambda: NOW,
    )


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
        self.recent_orders = []

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
        side = self.order["side"]
        price = str(self.order["limit_price"])
        self.fills.append({
            "fill_id": f"fill-{index}", "broker_order_id": "broker-1",
            "client_order_id": self.order["client_order_id"], "book_id": "BASELINE",
            "symbol": "AAPL", "side": side, "quantity": str(quantity), "price": price,
            "filled_at": NOW.isoformat(), "account_fingerprint": FINGERPRINT,
        })
        delta_cash = quantity * Decimal(price)
        self.cash += delta_cash if side == "SELL" else -delta_cash
        self.position += quantity if side == "BUY" else -quantity
        cumulative = sum(Decimal(fill["quantity"]) for fill in self.fills)
        total_quantity = Decimal(str(self.order["quantity"]))
        self.order["filled_quantity"] = int(cumulative)
        self.order["average_fill_price"] = price
        self.order["status"] = "FILLED" if cumulative == total_quantity else "PARTIALLY_FILLED"

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

    def list_recent_external_orders(self, book_id, *, limit=50):
        return [dict(o) for o in self.recent_orders][:limit]


def _preview(conn, runtime, cfg):
    _activate_baseline(conn, runtime, cfg)
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


def test_local_simulated_position_does_not_contaminate_external_reconciliation():
    """Milestone 23 Part A3/A5: a book that also carries unrelated
    local-simulated activity (e.g. an earlier fixture-mode fill never
    mirrored to the broker) must not surface as a false
    POSITION_MISMATCH/CASH_MISMATCH once external reconciliation starts."""
    conn = connect(":memory:")
    intent = _seed(conn)
    # Pre-existing local-simulated MSFT fill, unrelated to the external
    # AAPL order below and never sent to (or known by) the fake broker.
    positions.apply_buy_fill(conn, "BASELINE", "MSFT", "local-fill-1", Decimal("5"), Decimal("300"), NOW)
    cash_ledger.settle_buy(conn, "BASELINE", "local-fill-1", Decimal("1500"), Decimal("0"), Decimal("0"), NOW)

    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    result = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="approved paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["status"] == STATE_SUBMITTED
    runtime.add_fill(2)
    outcome = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert outcome["status"] == "MATCHED"


def test_reconciliation_baseline_initializes_once_and_is_idempotent():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    first = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert first["status"] == "MATCHED"
    baseline_after_first = repo.load_external_reconciliation_baseline(conn, "BASELINE")
    assert baseline_after_first is not None

    runtime.add_fill(2)
    second = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert second["status"] == "MATCHED"
    baseline_after_second = repo.load_external_reconciliation_baseline(conn, "BASELINE")
    assert baseline_after_second == baseline_after_first


def test_daily_notional_cap_blocks_second_submission_over_limit():
    from dataclasses import replace

    conn = connect(":memory:")
    intent = _seed(conn)
    cfg = _config()
    cfg = replace(cfg, external_broker=replace(cfg.external_broker, maximum_daily_notional_usd=Decimal("150")))
    runtime = FakeRuntime()
    preview1 = _preview(conn, runtime, cfg)
    result1 = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview1["preview_id"],
        operator="alice", reason="first order", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result1["status"] == STATE_SUBMITTED

    decision = PaperRiskDecision(RISK_APPROVED, Decimal("80"), Decimal("80"), Decimal("2"), (), "risk-v1")
    repo.save_risk_decision(conn, "risk-2", "BASELINE", "cycle-1", "rec-2", "AAPL", decision, "snap-1", NOW)
    intent2 = PaperBookOrderIntent(
        paper_order_intent_id="intent-2", book_id="BASELINE", experiment_arm="BASELINE",
        cycle_id="cycle-1", recommendation_id="rec-2", symbol="AAPL", side="BUY",
        order_type="LIMIT", quantity=Decimal("2"), limit_price=Decimal("40"),
        notional_usd=Decimal("80"), time_in_force="DAY", as_of=NOW,
        risk_decision_id="risk-2", portfolio_snapshot_id="snap-1", config_hash="cfg-m11",
        created_at=NOW,
    )
    repo.save_order_intent(conn, intent2)
    preview2 = preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-2", operator="alice",
        runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    with pytest.raises(ExternalPaperError) as excinfo:
        submit_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-2", preview_id=preview2["preview_id"],
            operator="alice", reason="second order over daily cap", runtime=runtime, config=cfg, clock=lambda: NOW,
        )
    assert excinfo.value.code == "EXTERNAL_DAILY_NOTIONAL_LIMIT"
    assert runtime.submit_calls == 1


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


def test_refresh_retry_preview_unblocks_retry_after_original_preview_expires():
    """Milestone 11.2 Part 17/37: UNKNOWN -> authoritative NOT_FOUND ->
    original preview expires -> explicit refresh-retry-preview -> explicit
    retry -> lookup consumed exactly once."""
    from trading_research.paper_books.external_broker import refresh_retry_preview

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
    lookup = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert lookup["status"] == "ORDER_MISSING_AT_BROKER"

    # Original preview (require_recent_preview_seconds=300) has now expired.
    much_later = NOW + timedelta(seconds=600)
    runtime.raise_submit = False
    with pytest.raises(ExternalPaperError, match="expired"):
        retry_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            reason="retry with stale preview", runtime=runtime, config=cfg, clock=lambda: much_later,
        )

    refreshed = refresh_retry_preview(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        reason="original preview expired while broker lookup was pending", config=cfg, clock=lambda: much_later,
    )
    assert refreshed["result"] == "APPROVED"
    assert refreshed["preview_id"] != preview["preview_id"]  # new preview ID and expiry
    assert refreshed["expires_at"] > much_later.isoformat()

    # The lookup is still unconsumed by the refresh itself.
    lookup_row = repo.load_latest_external_lookup(conn, "BASELINE", preview["client_order_id"])
    assert lookup_row["consumed_by_retry_event_id"] is None
    original_lookup_id = lookup_row["lookup_id"]

    retried = retry_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        reason="retry after refresh", runtime=runtime, config=cfg, clock=lambda: much_later,
    )
    assert retried["status"] == STATE_SUBMITTED
    assert runtime.submit_calls == 2

    # The original lookup that authorized this retry is now consumed exactly
    # once (a later, unrelated reconciliation lookup for the new SUBMITTED
    # state may also exist — that one is untouched by consumption).
    consumed = conn.execute(
        "SELECT consumed_by_retry_event_id FROM paper_external_order_lookups WHERE lookup_id = ?",
        (original_lookup_id,),
    ).fetchone()
    assert consumed["consumed_by_retry_event_id"] is not None


def test_refresh_retry_preview_rejected_once_broker_order_is_found():
    """Refresh cannot occur after the broker order is actually found —
    once reconciliation resolves UNKNOWN to a real state, there is nothing
    left to refresh."""
    from trading_research.paper_books.external_broker import refresh_retry_preview

    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    with pytest.raises(ExternalPaperError, match="UNKNOWN_REQUIRES_RECONCILIATION") as excinfo:
        refresh_retry_preview(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            reason="should not be allowed", config=cfg, clock=lambda: NOW,
        )
    assert excinfo.value.code == "REFRESH_NOT_ALLOWED"


def test_refresh_retry_preview_makes_no_broker_call():
    from trading_research.paper_books.external_broker import refresh_retry_preview

    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    runtime.raise_submit = True
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    calls_before = runtime.submit_calls
    refresh_retry_preview(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        reason="refresh only", config=cfg, clock=lambda: NOW + timedelta(seconds=600),
    )
    assert runtime.submit_calls == calls_before  # no broker mutation


def test_consumed_lookup_cannot_authorize_a_second_retry_until_fresh_evidence():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(maximum_retry_attempts=2), FakeRuntime()
    runtime.raise_submit = True
    preview = _preview(conn, runtime, cfg)
    result = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["status"] == STATE_UNKNOWN  # attempt 0 UNKNOWN

    lookup0 = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert lookup0["status"] == "ORDER_MISSING_AT_BROKER"  # lookup 0 authoritative NOT_FOUND

    retry1 = retry_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        reason="retry 1", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert retry1["status"] == STATE_UNKNOWN  # retry 1 also ambiguous; lookup 0 now consumed

    with pytest.raises(ExternalPaperError, match="fresh, unconsumed"):
        retry_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            reason="retry 2 without fresh lookup", runtime=runtime, config=cfg, clock=lambda: NOW,
        )

    lookup1 = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert lookup1["status"] == "ORDER_MISSING_AT_BROKER"  # fresh lookup 1 for attempt 1

    runtime.raise_submit = False
    retry2 = retry_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        reason="retry 2 with fresh lookup", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert retry2["status"] == STATE_SUBMITTED
    assert runtime.submit_calls == 3


def test_stale_and_mismatched_lookup_evidence_rejected():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    runtime.raise_submit = True
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    # a non-authoritative NOT_FOUND (from an uncertain runtime request) never authorizes a retry.
    repo.save_external_lookup(conn, {
        "lookup_id": "lookup-nonauthoritative", "book_id": "BASELINE",
        "paper_order_intent_id": "intent-1", "client_order_id": preview["client_order_id"],
        "account_fingerprint": FINGERPRINT, "result": "NOT_FOUND", "authoritative": 0,
        "runtime_request_id": "req-1", "created_at": NOW.isoformat(),
    })
    with pytest.raises(ExternalPaperError, match="fresh, unconsumed"):
        retry_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            reason="retry blocked by nonauthoritative lookup", runtime=runtime, config=cfg, clock=lambda: NOW,
        )

    # a lookup tied to a different (stale) ambiguous event never authorizes this attempt.
    current = repo.load_latest_external_order_event(conn, "BASELINE", preview["client_order_id"])
    repo.save_external_lookup(conn, {
        "lookup_id": "lookup-stale-event", "book_id": "BASELINE",
        "paper_order_intent_id": "intent-1", "client_order_id": preview["client_order_id"],
        "account_fingerprint": FINGERPRINT, "result": "NOT_FOUND", "authoritative": 1,
        "runtime_request_id": "req-2", "created_at": NOW.isoformat(),
        "attempt_number": current["attempt_number"], "ambiguous_event_id": "some-other-event-id",
        "payload_hash": current["payload_hash"], "lookup_started_at": NOW.isoformat(),
        "lookup_completed_at": NOW.isoformat(),
    })
    with pytest.raises(ExternalPaperError, match="fresh, unconsumed"):
        retry_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            reason="retry blocked by stale-event lookup", runtime=runtime, config=cfg, clock=lambda: NOW,
        )


def test_concurrent_submit_blocked_by_order_lease_performs_no_runtime_mutation():
    conn = connect(":memory:")
    intent = _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    client_order_id = preview["client_order_id"]
    lease_key = f"BASELINE:{client_order_id}"
    held = repo.acquire_external_order_lease(
        conn, lease_key=lease_key, book_id="BASELINE", client_order_id=client_order_id,
        owner_id="other-caller", operation="SUBMIT", now=NOW.isoformat(),
        expires_at=(NOW + timedelta(seconds=30)).isoformat(),
    )
    assert isinstance(held, int) and held >= 1  # acquired generation (Part 10)
    with pytest.raises(ExternalPaperError, match="lease"):
        submit_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
            operator="alice", reason="blocked by concurrent lease", runtime=runtime, config=cfg, clock=lambda: NOW,
        )
    assert runtime.submit_calls == 0


def test_stale_order_lease_recovers_for_new_owner():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    client_order_id = preview["client_order_id"]
    lease_key = f"BASELINE:{client_order_id}"
    expired_at = (NOW - timedelta(seconds=1)).isoformat()
    repo.acquire_external_order_lease(
        conn, lease_key=lease_key, book_id="BASELINE", client_order_id=client_order_id,
        owner_id="dead-owner", operation="SUBMIT", now=(NOW - timedelta(seconds=60)).isoformat(), expires_at=expired_at,
    )
    result = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="recovers stale lease", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["status"] == STATE_SUBMITTED
    assert runtime.submit_calls == 1


def test_wrong_owner_cannot_release_order_lease():
    conn = connect(":memory:")
    _seed(conn)
    repo.acquire_external_order_lease(
        conn, lease_key="BASELINE:cid", book_id="BASELINE", client_order_id="cid",
        owner_id="real-owner", operation="SUBMIT", now=NOW.isoformat(),
        expires_at=(NOW + timedelta(seconds=30)).isoformat(),
    )
    released = repo.release_external_order_lease(conn, lease_key="BASELINE:cid", owner_id="wrong-owner", now=NOW.isoformat())
    assert released is False
    released = repo.release_external_order_lease(conn, lease_key="BASELINE:cid", owner_id="real-owner", now=NOW.isoformat())
    assert released is True


def test_event_chain_cannot_fork_on_duplicate_scope_sequence():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    current = repo.load_latest_external_order_event(conn, "BASELINE", preview["client_order_id"])
    assert current["scope_sequence"] == 0
    forked = dict(current)
    forked["external_order_event_id"] = "forked-event-id"
    forked["new_state"] = "SUBMISSION_REQUESTED"
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO paper_external_order_events "
            "(external_order_event_id, external_order_scope_id, book_id, paper_order_intent_id, "
            "client_order_id, account_fingerprint, previous_state, new_state, payload_hash, quantity, "
            "limit_price, operator, reason, created_at, policy_version, config_hash, attempt_number, "
            "scope_sequence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                forked["external_order_event_id"], forked["external_order_scope_id"], forked["book_id"],
                forked["paper_order_intent_id"], forked["client_order_id"], forked["account_fingerprint"],
                forked["previous_state"], forked["new_state"], forked["payload_hash"], forked["quantity"],
                forked["limit_price"], forked["operator"], forked["reason"], forked["created_at"],
                forked["policy_version"], forked["config_hash"], forked["attempt_number"],
                forked["scope_sequence"],
            ),
        )
        conn.commit()


def test_malformed_broker_fill_persists_critical_reconciliation():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    # malformed fill: negative quantity is rejected by apply_external_fills as FILL_QUANTITY_INVALID.
    runtime.fills = [{
        "fill_id": "fill-bad", "broker_order_id": "broker-1", "client_order_id": preview["client_order_id"],
        "book_id": "BASELINE", "symbol": "AAPL", "side": "BUY", "quantity": "-1", "price": "40",
        "filled_at": NOW.isoformat(), "account_fingerprint": FINGERPRINT,
    }]
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "MALFORMED_BROKER_FILL"
    assert result["critical"] == 1
    # blocked from further external submission by the persisted critical evidence.
    with pytest.raises(ExternalPaperError, match="critical"):
        submit_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
            operator="alice", reason="blocked by critical reconciliation", runtime=runtime, config=cfg,
            clock=lambda: NOW,
        )


def test_malformed_broker_order_persists_critical_reconciliation():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.order["quantity"] = 999  # no longer matches the frozen approved intent
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "MALFORMED_BROKER_ORDER"
    assert result["critical"] == 1


def test_fill_application_internal_failure_persists_critical_reconciliation(monkeypatch):
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.add_fill(1)
    import trading_research.paper_books.external_broker as external_broker_module

    def _boom(*args, **kwargs):
        raise RuntimeError("unexpected failure while applying a fill")

    monkeypatch.setattr(external_broker_module.positions, "apply_buy_fill", _boom)
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "FILL_APPLICATION_FAILED"
    assert result["critical"] == 1


class _InstantFillRuntime(FakeRuntime):
    """Milestone 11.2 Part 12/13: a broker that fills the order synchronously
    within the submit/cancel response itself (no separate reconciliation
    round-trip needed to observe the fill) — used to exercise the fill sweep
    that runs directly inside `_submit_once`/`cancel_external_paper_order`,
    as opposed to the separately-tested reconciliation-triggered sweep."""

    def submit_limit_order(self, payload):
        order = super().submit_limit_order(payload)
        self.add_fill(payload["quantity"])
        return dict(self.order)

    def cancel_external_order(self, book_id, client_order_id, account_fingerprint):
        if not self.fills:
            self.add_fill(Decimal(str(self.order["quantity"])) / 2)
        self.cancel_calls += 1
        self.order["status"] = "CANCELLED"
        return dict(self.order)


class _CancelFillsRuntime(FakeRuntime):
    """Fills half the order only when cancellation is requested (submission
    itself leaves the order open) — models a fill that raced ahead of a
    cancel request and is only discovered in the cancel response."""

    def cancel_external_order(self, book_id, client_order_id, account_fingerprint):
        if not self.fills:
            self.add_fill(Decimal(str(self.order["quantity"])) / 2)
        self.cancel_calls += 1
        self.order["status"] = "CANCELLED"
        return dict(self.order)


def test_post_submit_fill_sweep_failure_persists_critical_reconciliation_before_raising(monkeypatch):
    """Milestone 11.2 Part 12: a fill-application failure that happens
    *inside* `_submit_once`'s own post-submit fill sweep (not a later
    reconciliation call) must still persist critical evidence before the
    exception propagates — previously this sweep was completely
    unprotected."""
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), _InstantFillRuntime()
    preview = _preview(conn, runtime, cfg)
    import trading_research.paper_books.external_broker as external_broker_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated crash applying the instant fill")

    monkeypatch.setattr(external_broker_module.positions, "apply_buy_fill", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        submit_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
            operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
        )
    records = repo.list_external_reconciliations(conn, "BASELINE", preview["client_order_id"])
    assert len(records) == 1
    assert records[0]["critical"] == 1
    assert records[0]["status"] == "FILL_APPLICATION_FAILED"
    assert records[0]["details"]["stage"] == "post_submit_fill_sweep"


def test_post_cancel_fill_sweep_failure_persists_critical_and_withholds_reservation_release(monkeypatch):
    """Milestone 11.2 Part 13: a fill discovered inside a cancellation
    response's own fill sweep that fails to apply must persist critical
    evidence, and must NOT release the reservation or mark the order
    terminal — the exposure stays visibly unresolved."""
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), _CancelFillsRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    import trading_research.paper_books.external_broker as external_broker_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated crash applying the pre-cancel fill")

    monkeypatch.setattr(external_broker_module.positions, "apply_buy_fill", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        cancel_external_paper_order(
            conn, book_id="BASELINE", client_order_id=preview["client_order_id"],
            operator="alice", reason="risk reduction", runtime=runtime, config=cfg, clock=lambda: NOW,
        )
    records = repo.list_external_reconciliations(conn, "BASELINE", preview["client_order_id"])
    assert records[-1]["critical"] == 1
    assert records[-1]["details"]["stage"] == "post_cancel_fill_sweep"

    # Order status was never advanced to a terminal cancelled state.
    order = repo.load_order_intent(conn, "BASELINE", "intent-1")
    assert order["status"] != "CANCELLED"
    # Cash reservation remains — it was never released.
    remaining = cash_ledger.remaining_buy_reservation(conn, "BASELINE", "intent-1")
    assert remaining > 0


def test_reconciliation_internal_error_from_numeric_conversion_is_critical(monkeypatch):
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.get_external_account_snapshot = lambda book_id: {
        "provider": "alpaca_paper", "environment": "paper", "book_id": book_id,
        "account_fingerprint": FINGERPRINT, "cash": "not-a-number", "equity": "100000",
        "buying_power": None, "currency": "USD", "as_of": NOW.isoformat(),
    }
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "RECONCILIATION_INTERNAL_ERROR"
    assert result["critical"] == 1


def test_one_matching_broker_order_is_not_flagged_duplicate():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.recent_orders = [dict(runtime.order)]
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "MATCHED"
    assert result["critical"] == 0


def test_same_client_order_id_mapped_to_two_broker_orders_is_duplicate():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    duplicate = dict(runtime.order)
    duplicate["broker_order_id"] = "broker-DUPLICATE"
    runtime.recent_orders = [dict(runtime.order), duplicate]
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "BROKER_ORDER_DUPLICATE"
    assert result["critical"] == 1
    assert "broker-DUPLICATE" in result["details"]["duplicate_broker_order_ids"]

    # later external submissions in this book are blocked by the critical evidence.
    with pytest.raises(ExternalPaperError, match="critical"):
        preview_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            runtime=runtime, config=cfg, clock=lambda: NOW,
        )


def test_materially_identical_order_under_another_client_id_is_duplicate():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    shadow = dict(runtime.order)
    shadow["client_order_id"] = "epb-baseline-shadowdupe0000000000000000"
    shadow["broker_order_id"] = "broker-shadow"
    runtime.recent_orders = [dict(runtime.order), shadow]
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "BROKER_ORDER_DUPLICATE"
    assert result["critical"] == 1
    assert result["details"]["duplicate_client_order_id"] == shadow["client_order_id"]


def test_manually_created_order_without_project_prefix_is_detected_as_duplicate():
    """Milestone 11.2 Part 15: a manually-created (or another application's)
    Alpaca order never carries this project's `epb-{book_id}-` client_order_id
    prefix — it must still be detected as a duplicate by shape/timing, not
    silently skipped merely because the prefix is absent."""
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    manual = dict(runtime.order)
    manual["client_order_id"] = "manual-web-ui-order-0001"  # no epb-baseline- prefix at all
    manual["broker_order_id"] = "broker-manual"
    runtime.recent_orders = [dict(runtime.order), manual]
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "BROKER_ORDER_DUPLICATE"
    assert result["critical"] == 1
    assert result["details"]["duplicate_client_order_id"] == "manual-web-ui-order-0001"


def test_malformed_recent_orders_response_fails_closed():
    """Malformed/oversized recent-order results must raise (fail closed),
    not silently report 'no duplicate found'."""
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.recent_orders = ["not-a-dict"]
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "RECONCILIATION_INTERNAL_ERROR"
    assert result["critical"] == 1


def test_unrelated_recent_order_is_not_flagged_duplicate():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    unrelated = dict(runtime.order)
    unrelated["client_order_id"] = "epb-baseline-unrelated00000000000000000"
    unrelated["broker_order_id"] = "broker-unrelated"
    unrelated["symbol"] = "MSFT"
    unrelated["quantity"] = 5
    runtime.recent_orders = [dict(runtime.order), unrelated]
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "MATCHED"
    assert result["critical"] == 0


def test_future_intent_as_of_is_rejected():
    conn = connect(":memory:")
    _seed(conn)
    future_as_of = NOW + timedelta(hours=1)
    conn.execute(
        "UPDATE paper_book_orders SET as_of = ? WHERE paper_order_intent_id = 'intent-1'",
        (future_as_of.isoformat(),),
    )
    conn.commit()
    cfg, runtime = _config(), FakeRuntime()
    with pytest.raises(ExternalPaperError, match="future"):
        _preview(conn, runtime, cfg)


def test_broker_order_submitted_after_updated_is_rejected():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.order["updated_at"] = (NOW - timedelta(seconds=5)).isoformat()
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "MALFORMED_BROKER_ORDER"
    assert result["critical"] == 1


def test_broker_order_equivalent_offset_timestamps_are_accepted():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)

    def _make_order_offset(payload):
        order = FakeRuntime._make_order(runtime, payload)
        # same instant as NOW, expressed with a non-UTC numeric offset.
        offset_time = NOW.astimezone(timezone(timedelta(hours=-5)))
        order["submitted_at"] = offset_time.isoformat()
        order["updated_at"] = offset_time.isoformat()
        return order

    runtime._make_order = _make_order_offset
    result = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["status"] == STATE_SUBMITTED


def test_broker_order_naive_timestamp_is_rejected():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.order["submitted_at"] = NOW.replace(tzinfo=None).isoformat()
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "MALFORMED_BROKER_ORDER"
    assert result["critical"] == 1


def test_future_fill_timestamp_persists_critical_reconciliation():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.fills = [{
        "fill_id": "fill-future", "broker_order_id": "broker-1", "client_order_id": preview["client_order_id"],
        "book_id": "BASELINE", "symbol": "AAPL", "side": "BUY", "quantity": "2", "price": "40",
        "filled_at": (NOW + timedelta(hours=1)).isoformat(), "account_fingerprint": FINGERPRINT,
    }]
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "MALFORMED_BROKER_FILL"
    assert result["critical"] == 1


@pytest.mark.parametrize("bad_quantity", ["1.5", "NaN", "Infinity", "-Infinity"])
def test_fractional_or_nonfinite_broker_fill_quantity_rejected(bad_quantity):
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.fills = [{
        "fill_id": "fill-bad-qty", "broker_order_id": "broker-1", "client_order_id": preview["client_order_id"],
        "book_id": "BASELINE", "symbol": "AAPL", "side": "BUY", "quantity": bad_quantity, "price": "40",
        "filled_at": NOW.isoformat(), "account_fingerprint": FINGERPRINT,
    }]
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "MALFORMED_BROKER_FILL"
    assert result["critical"] == 1


@pytest.mark.parametrize("bad_quantity", ["1.5", "NaN", "Infinity"])
def test_fractional_or_nonfinite_broker_order_quantity_rejected(bad_quantity):
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.order["quantity"] = bad_quantity
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "MALFORMED_BROKER_ORDER"
    assert result["critical"] == 1


def test_corrupted_low_notional_with_high_quantity_times_price_fails_closed():
    conn = connect(":memory:")
    _seed(conn)
    # A hand-crafted row simulating a corrupted/tampered intent: notional_usd
    # stays low (80, matching the approved risk decision) but quantity *
    # limit_price would actually be 400 if the stored notional were trusted
    # naively instead of recomputed.
    conn.execute(
        "INSERT INTO paper_book_orders (book_id, paper_order_intent_id, experiment_arm, cycle_id, "
        "recommendation_id, symbol, side, order_type, quantity, limit_price, notional_usd, "
        "time_in_force, as_of, risk_decision_id, portfolio_snapshot_id, config_hash, created_at, status) "
        "VALUES ('BASELINE', 'intent-corrupted', 'BASELINE', 'cycle-1', 'rec-1', 'AAPL', 'BUY', 'LIMIT', "
        "'10', '40', '80', 'DAY', ?, 'risk-1', 'snap-1', 'cfg-m11', ?, 'PENDING_SUBMISSION')",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    cfg, runtime = _config(), FakeRuntime()
    with pytest.raises(ExternalPaperError, match="recomputed notional"):
        preview_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-corrupted", operator="alice",
            runtime=runtime, config=cfg, clock=lambda: NOW,
        )


class _TransactionAssertingRuntime(FakeRuntime):
    """Wraps every runtime call to assert the connection has no open write
    transaction at call time (Part 15: the runtime call must never happen
    inside a held DB transaction)."""

    def __init__(self, conn):
        super().__init__()
        self._conn = conn

    def _assert_no_open_transaction(self, label):
        assert self._conn.in_transaction is False, f"{label} was called with an open transaction"

    def account_check(self, book_id):
        self._assert_no_open_transaction("account_check")
        return super().account_check(book_id)

    def preview_limit_order(self, payload):
        self._assert_no_open_transaction("preview_limit_order")
        return super().preview_limit_order(payload)

    def submit_limit_order(self, payload):
        self._assert_no_open_transaction("submit_limit_order")
        return super().submit_limit_order(payload)

    def get_order_by_client_order_id(self, book_id, client_order_id):
        self._assert_no_open_transaction("get_order_by_client_order_id")
        return super().get_order_by_client_order_id(book_id, client_order_id)

    def list_order_fills(self, book_id, client_order_id):
        self._assert_no_open_transaction("list_order_fills")
        return super().list_order_fills(book_id, client_order_id)

    def cancel_external_order(self, book_id, client_order_id, account_fingerprint):
        self._assert_no_open_transaction("cancel_external_order")
        return super().cancel_external_order(book_id, client_order_id, account_fingerprint)

    def get_external_positions(self, book_id):
        self._assert_no_open_transaction("get_external_positions")
        return super().get_external_positions(book_id)

    def get_external_account_snapshot(self, book_id):
        self._assert_no_open_transaction("get_external_account_snapshot")
        return super().get_external_account_snapshot(book_id)


def test_runtime_calls_never_occur_inside_an_open_db_transaction():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), _TransactionAssertingRuntime(conn)
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.add_fill(1)
    reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert conn.in_transaction is False


def test_cancel_runtime_call_never_occurs_inside_an_open_db_transaction():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), _TransactionAssertingRuntime(conn)
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    cancel_external_paper_order(
        conn, book_id="BASELINE", client_order_id=preview["client_order_id"], operator="alice",
        reason="risk-reducing cancellation", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert conn.in_transaction is False


def test_queue_status_derived_from_events_never_stuck_awaiting_after_submission():
    from trading_research.paper_books.external_broker import derive_external_queue_status

    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    repo.enqueue_external_submission(
        conn, queue_id="q1", book_id="BASELINE", paper_order_intent_id="intent-1",
        source="RECURRING_LOCAL_PAPER", created_at=NOW.isoformat(),
    )
    before = derive_external_queue_status(conn, book_id="BASELINE", paper_order_intent_id="intent-1")
    assert before["status"] == "AWAITING_OPERATOR_EXTERNAL_SUBMISSION"

    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    after = derive_external_queue_status(conn, book_id="BASELINE", paper_order_intent_id="intent-1")
    assert after["status"] == STATE_SUBMITTED
    assert after["client_order_id"] == preview["client_order_id"]


def test_queue_status_reflects_blocked_by_reconciliation_for_nonterminal_order():
    from trading_research.paper_books.external_broker import derive_external_queue_status

    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.order["quantity"] = 999  # forces a critical MALFORMED_BROKER_ORDER reconciliation
    reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    status = derive_external_queue_status(conn, book_id="BASELINE", paper_order_intent_id="intent-1")
    assert status["status"] == "BLOCKED_BY_RECONCILIATION"


def test_queue_status_stays_terminal_after_full_fill():
    from trading_research.paper_books.external_broker import derive_external_queue_status

    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.add_fill(2)
    reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    status = derive_external_queue_status(conn, book_id="BASELINE", paper_order_intent_id="intent-1")
    assert status["status"] == STATE_FILLED


def test_integration_scenario_sell_isolation_partial_fill_cancel_and_reopen():
    """Part 19 integration scenario: long 10 -> external SELL 10 submitted ->
    shares reserved -> a second exit cycle creates no second SELL -> broker
    partially fills 4 -> reservation remaining 6 -> order cancelled ->
    remaining 6 released -> position now 6."""
    from trading_research.paper_books.lifecycle import _has_unresolved_pending_sell

    conn = connect(":memory:")
    _seed_sell(conn, quantity=Decimal("10"), sell_quantity=Decimal("10"), limit_price=Decimal("9"))
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview_sell(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-sell", preview_id=preview["preview_id"],
        operator="alice", reason="closing sell", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert positions.remaining_share_reservation(conn, "BASELINE", "intent-sell") == Decimal("10")

    # a second exit-evaluation cycle must not create a second SELL for this symbol.
    assert _has_unresolved_pending_sell(conn, "BASELINE", "AAPL") is True

    runtime.add_fill(4)
    reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert positions.remaining_share_reservation(conn, "BASELINE", "intent-sell") == Decimal("6")
    position = repo.load_position(conn, "BASELINE", "AAPL")
    assert Decimal(position["quantity"]) == Decimal("6")

    cancelled = cancel_external_paper_order(
        conn, book_id="BASELINE", client_order_id=preview["client_order_id"], operator="alice",
        reason="risk-reducing cancellation", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert cancelled["status"] == "CANCELLED"
    assert positions.remaining_share_reservation(conn, "BASELINE", "intent-sell") == 0
    position = repo.load_position(conn, "BASELINE", "AAPL")
    assert Decimal(position["quantity"]) == Decimal("6")
    assert Decimal(position["available_quantity"]) == Decimal("6")

    # the exit is now fully resolved — a later exit cycle could create a new SELL.
    assert _has_unresolved_pending_sell(conn, "BASELINE", "AAPL") is False


def test_ambiguous_sell_submission_keeps_shares_reserved():
    conn = connect(":memory:")
    _seed_sell(conn, quantity=Decimal("10"), sell_quantity=Decimal("4"))
    cfg, runtime = _config(), FakeRuntime()
    runtime.raise_submit = True
    preview = _preview_sell(conn, runtime, cfg)
    result = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-sell", preview_id=preview["preview_id"],
        operator="alice", reason="closing sell", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["status"] == STATE_UNKNOWN
    assert positions.remaining_share_reservation(conn, "BASELINE", "intent-sell") == Decimal("4")


def test_retry_rejects_lookup_with_mismatched_payload_hash():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    runtime.raise_submit = True
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    current = repo.load_latest_external_order_event(conn, "BASELINE", preview["client_order_id"])
    repo.save_external_lookup(conn, {
        "lookup_id": "lookup-bad-payload", "book_id": "BASELINE",
        "paper_order_intent_id": "intent-1", "client_order_id": preview["client_order_id"],
        "account_fingerprint": FINGERPRINT, "result": "NOT_FOUND", "authoritative": 1,
        "runtime_request_id": "req-3", "created_at": NOW.isoformat(),
        "attempt_number": current["attempt_number"], "ambiguous_event_id": current["external_order_event_id"],
        "payload_hash": "wrong-payload-hash", "lookup_started_at": NOW.isoformat(),
        "lookup_completed_at": NOW.isoformat(),
    })
    with pytest.raises(ExternalPaperError, match="fresh, unconsumed"):
        retry_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            reason="retry blocked by payload mismatch", runtime=runtime, config=cfg, clock=lambda: NOW,
        )


def test_fill_before_submitted_at_is_rejected():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    stale_fill_time = NOW - timedelta(days=1)
    runtime.fills = [{
        "fill_id": "fill-before-submit", "broker_order_id": "broker-1",
        "client_order_id": preview["client_order_id"], "book_id": "BASELINE", "symbol": "AAPL",
        "side": "BUY", "quantity": "2", "price": "40", "filled_at": stale_fill_time.isoformat(),
        "account_fingerprint": FINGERPRINT,
    }]
    runtime.order["submitted_at"] = NOW.isoformat()
    result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert result["status"] == "MALFORMED_BROKER_FILL"
    assert result["critical"] == 1


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


class ImmediateFilledRuntime(FakeRuntime):
    """Broker reports FILLED on submit while /fills lags empty (Part 2 gap)."""

    def _make_order(self, payload):
        order = super()._make_order(payload)
        order.update(status="FILLED", filled_quantity=2, average_fill_price="40")
        return order


def test_broker_filled_with_no_fills_retains_reservation_and_is_critical():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), ImmediateFilledRuntime()
    preview = _preview(conn, runtime, cfg)
    result = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["status"] == STATE_FILLED
    assert repo.list_fills_for_intent(conn, "BASELINE", "intent-1") == []
    assert cash_ledger.reserved_cash(conn, "BASELINE") == Decimal("80")
    assert result["reconciliation"]["critical"] == 1
    assert "FILL_QUANTITY_MISMATCH" in result["reconciliation"]["statuses"]

    # delayed fills arrive later; reconciliation settles and releases in full.
    runtime.fills = [{
        "fill_id": "fill-1", "broker_order_id": "broker-1", "client_order_id": preview["client_order_id"],
        "book_id": "BASELINE", "symbol": "AAPL", "side": "BUY", "quantity": "2", "price": "40",
        "filled_at": NOW.isoformat(), "account_fingerprint": FINGERPRINT,
    }]
    runtime.cash, runtime.position = Decimal("99920"), Decimal("2")
    settled = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert settled["status"] == "MATCHED"
    assert cash_ledger.reserved_cash(conn, "BASELINE") == 0
    assert cash_ledger.available_cash(conn, "BASELINE") == Decimal("99920")


def test_partial_fill_preserves_remaining_reservation_then_cancel_releases_only_remainder():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert cash_ledger.reserved_cash(conn, "BASELINE") == Decimal("80")
    runtime.add_fill(1)
    reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert cash_ledger.remaining_buy_reservation(conn, "BASELINE", "intent-1") == Decimal("40")
    assert cash_ledger.reserved_cash(conn, "BASELINE") == Decimal("40")

    cancelled = cancel_external_paper_order(
        conn, book_id="BASELINE", client_order_id=preview["client_order_id"], operator="alice",
        reason="risk-reducing cancellation", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert cancelled["status"] == "CANCELLED"
    assert cash_ledger.reserved_cash(conn, "BASELINE") == 0
    assert cash_ledger.available_cash(conn, "BASELINE") == Decimal("99960")


def test_repeated_reconciliation_does_not_over_release_reservation():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.add_fill(2)
    for _ in range(3):
        reconcile_external_paper_order(
            conn, book_id="BASELINE", runtime=runtime, config=cfg,
            client_order_id=preview["client_order_id"], clock=lambda: NOW,
        )
    assert cash_ledger.reserved_cash(conn, "BASELINE") == 0
    assert cash_ledger.available_cash(conn, "BASELINE") == Decimal("99920")


def test_cancelled_external_order_reconciles():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    cancelled = cancel_external_paper_order(
        conn, book_id="BASELINE", client_order_id=preview["client_order_id"], operator="alice",
        reason="risk-reducing cancellation", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert cancelled["status"] == "CANCELLED"
    outcome = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert outcome["status"] == "MATCHED"
    assert repo.list_positions(conn, "BASELINE") == []
    assert cash_ledger.reserved_cash(conn, "BASELINE") == 0


def test_expired_external_order_reconciles():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.order["status"] = "EXPIRED"
    outcome = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert outcome["status"] == "MATCHED"
    assert repo.load_latest_external_order_event(conn, "BASELINE", preview["client_order_id"])["new_state"] == "EXPIRED"
    assert repo.list_positions(conn, "BASELINE") == []
    assert cash_ledger.reserved_cash(conn, "BASELINE") == 0


def test_quantity_mismatch_blocks_further_submission():
    conn = connect(":memory:")
    _seed(conn)
    cfg, runtime = _config(), ImmediateFilledRuntime()
    preview = _preview(conn, runtime, cfg)
    result = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="paper test", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["reconciliation"]["critical"] == 1
    assert "FILL_QUANTITY_MISMATCH" in result["reconciliation"]["statuses"]

    decision = PaperRiskDecision(RISK_APPROVED, Decimal("80"), Decimal("80"), Decimal("2"), (), "risk-v1")
    repo.save_risk_decision(conn, "risk-2", "BASELINE", "cycle-1", "rec-2", "AAPL", decision, "snap-1", NOW)
    intent2 = PaperBookOrderIntent(
        paper_order_intent_id="intent-2", book_id="BASELINE", experiment_arm="BASELINE",
        cycle_id="cycle-1", recommendation_id="rec-2", symbol="AAPL", side="BUY",
        order_type="LIMIT", quantity=Decimal("2"), limit_price=Decimal("40"),
        notional_usd=Decimal("80"), time_in_force="DAY", as_of=NOW,
        risk_decision_id="risk-2", portfolio_snapshot_id="snap-1", config_hash="cfg-m11",
        created_at=NOW,
    )
    repo.save_order_intent(conn, intent2)
    with pytest.raises(ExternalPaperError, match="latest external reconciliation is critical"):
        preview_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-2", operator="alice",
            runtime=runtime, config=cfg, clock=lambda: NOW,
        )


def test_external_sell_reserves_shares_and_blocks_second_sell():
    conn = connect(":memory:")
    _seed_sell(conn, sell_quantity=Decimal("4"))
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview_sell(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-sell", preview_id=preview["preview_id"],
        operator="alice", reason="closing sell", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    position = repo.load_position(conn, "BASELINE", "AAPL")
    assert Decimal(position["reserved_quantity"]) == Decimal("4")
    assert Decimal(position["available_quantity"]) == Decimal("6")
    assert positions.remaining_share_reservation(conn, "BASELINE", "intent-sell") == Decimal("4")

    # a second SELL for more than the unreserved remainder fails closed.
    with pytest.raises(positions.InsufficientPositionError):
        positions.reserve_shares_for_sell(
            conn, "BASELINE", "AAPL", "intent-sell-2", "some-client-id", Decimal("7"), NOW,
        )


def test_partial_sell_fill_preserves_remainder_then_cancel_releases_rest():
    conn = connect(":memory:")
    _seed_sell(conn, sell_quantity=Decimal("4"))
    cfg, runtime = _config(), FakeRuntime()
    preview = _preview_sell(conn, runtime, cfg)
    submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-sell", preview_id=preview["preview_id"],
        operator="alice", reason="closing sell", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.add_fill(1)
    reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert positions.remaining_share_reservation(conn, "BASELINE", "intent-sell") == Decimal("3")
    position = repo.load_position(conn, "BASELINE", "AAPL")
    assert Decimal(position["quantity"]) == Decimal("9")
    assert Decimal(position["reserved_quantity"]) == Decimal("3")
    assert Decimal(position["available_quantity"]) == Decimal("6")

    cancelled = cancel_external_paper_order(
        conn, book_id="BASELINE", client_order_id=preview["client_order_id"], operator="alice",
        reason="risk-reducing cancellation", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert cancelled["status"] == "CANCELLED"
    position = repo.load_position(conn, "BASELINE", "AAPL")
    assert Decimal(position["reserved_quantity"]) == 0
    assert Decimal(position["available_quantity"]) == Decimal("9")
    assert positions.remaining_share_reservation(conn, "BASELINE", "intent-sell") == 0


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


def test_local_fill_blocks_subsequent_external_preview():
    """Milestone 11.2 Part 9 (reverse invariant): once the local simulator
    has filled an intent, external preview/submit/retry must refuse it —
    no intent may be filled in both namespaces."""
    conn = connect(":memory:")
    _seed(conn)
    order_intent = PaperBookOrderIntent(
        paper_order_intent_id="intent-1", book_id="BASELINE", experiment_arm="BASELINE",
        cycle_id="cycle-1", recommendation_id="rec-1", symbol="AAPL", side="BUY",
        order_type="LIMIT", quantity=Decimal("2"), limit_price=Decimal("40"),
        notional_usd=Decimal("80"), time_in_force="DAY", as_of=NOW,
        risk_decision_id="risk-1", portfolio_snapshot_id="snap-1", config_hash="cfg-m11",
        created_at=NOW,
    )
    # `_seed` inserts intent-1 directly (mirroring an external-style flow
    # that owns its own reservation timing), but a real local BUY always
    # reserves cash atomically with the intent (Milestone 11.3.1 Item 6
    # Part A) -- reserve here so this pre-seeded intent matches that
    # invariant before driving a local fill through it.
    cash_ledger.reserve_for_order(conn, "BASELINE", "intent-1", Decimal("80"), NOW)
    result = execution.submit_and_simulate(
        conn, order_intent, execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW,
    )
    assert result["status"] == "FILLED"

    with pytest.raises(ExternalPaperError, match="terminal local status") as excinfo:
        _preview(conn, FakeRuntime(), _config())
    assert excinfo.value.code == "INTENT_NOT_ELIGIBLE_FOR_EXTERNAL"


def test_local_cancel_blocks_subsequent_external_submit():
    conn = connect(":memory:")
    _seed(conn)
    order_intent = PaperBookOrderIntent(
        paper_order_intent_id="intent-1", book_id="BASELINE", experiment_arm="BASELINE",
        cycle_id="cycle-1", recommendation_id="rec-1", symbol="AAPL", side="BUY",
        order_type="LIMIT", quantity=Decimal("2"), limit_price=Decimal("40"),
        notional_usd=Decimal("80"), time_in_force="DAY", as_of=NOW,
        risk_decision_id="risk-1", portfolio_snapshot_id="snap-1", config_hash="cfg-m11",
        created_at=NOW,
    )
    execution.cancel_pending_intent(conn, order_intent, NOW)

    with pytest.raises(ExternalPaperError, match="terminal local status") as excinfo:
        submit_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id="does-not-matter",
            operator="alice", reason="retry", runtime=FakeRuntime(), config=_config(), clock=lambda: NOW,
        )
    assert excinfo.value.code == "INTENT_NOT_ELIGIBLE_FOR_EXTERNAL"


def test_pending_intent_still_previews_normally():
    """Sanity: the eligibility check does not regress the ordinary path —
    an untouched PENDING_SUBMISSION intent still previews successfully."""
    conn = connect(":memory:")
    _seed(conn)
    record = _preview(conn, FakeRuntime(), _config())
    assert record["result"] == "APPROVED"


# --- Milestone 24 Part A1/A2/A3: account-wide daily cap, atomic reservation,
# and explicit reconciliation baseline activation --------------------------


def _fresh_intent(conn, *, book_id, intent_id, quantity, limit_price):
    decision = PaperRiskDecision(
        RISK_APPROVED, quantity * limit_price, quantity * limit_price, quantity, (), "risk-v1",
    )
    risk_id = f"risk-{intent_id}"
    repo.save_risk_decision(conn, risk_id, book_id, "cycle-1", f"rec-{intent_id}", "AAPL", decision, "snap-1", NOW)
    intent = PaperBookOrderIntent(
        paper_order_intent_id=intent_id, book_id=book_id, experiment_arm=book_id,
        cycle_id="cycle-1", recommendation_id=f"rec-{intent_id}", symbol="AAPL", side="BUY",
        order_type="LIMIT", quantity=quantity, limit_price=limit_price,
        notional_usd=quantity * limit_price, time_in_force="DAY", as_of=NOW,
        risk_decision_id=risk_id, portfolio_snapshot_id="snap-1", config_hash="cfg-m11", created_at=NOW,
    )
    repo.save_order_intent(conn, intent)
    return repo.load_order_intent(conn, book_id, intent_id)


class _AltFingerprintRuntime(FakeRuntime):
    """A distinct, unrelated Alpaca paper account — every fingerprint-
    bearing response is overridden to prove its daily cap is tracked
    independently of `FakeRuntime`'s `FINGERPRINT`."""

    ALT_FINGERPRINT = "acct_ffffffffffffffffffffffffffffffff"

    def account_check(self, book_id):
        return {**super().account_check(book_id), "account_fingerprint": self.ALT_FINGERPRINT}

    def preview_limit_order(self, payload):
        return {**super().preview_limit_order(payload), "account_fingerprint": self.ALT_FINGERPRINT}

    def _make_order(self, payload):
        order = super()._make_order(payload)
        order["account_fingerprint"] = self.ALT_FINGERPRINT
        return order

    def get_external_positions(self, book_id):
        return {**super().get_external_positions(book_id), "account_fingerprint": self.ALT_FINGERPRINT}

    def get_external_account_snapshot(self, book_id):
        return {**super().get_external_account_snapshot(book_id), "account_fingerprint": self.ALT_FINGERPRINT}


class _RejectingRuntime(FakeRuntime):
    """The broker responds definitively (not ambiguously) with REJECTED —
    no notional was ever actually sent, so the daily reservation must
    release rather than keep holding the account's budget."""

    def submit_limit_order(self, payload):
        self.submit_calls += 1
        order = self._make_order(payload)
        order["status"] = "REJECTED"
        order["rejection_code"] = "INSUFFICIENT_BUYING_POWER"
        self.order = order
        return dict(order)


def test_daily_notional_cap_is_account_wide_across_books():
    """A1: the cap is scoped by (account_fingerprint, UTC date), aggregating
    every book that shares the fingerprint — not by book_id. (A separate,
    pre-existing invariant, `_verify_fingerprint_history`'s
    `ACCOUNT_ALREADY_MAPPED` check, already forbids two books from actually
    sharing one live Alpaca paper account end to end; this test exercises
    the reservation layer directly, which is where A1's aggregation itself
    lives and where that unrelated invariant does not apply.)"""
    from dataclasses import replace

    conn = connect(":memory:")
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000"), config_hash="cfg-m11", clock=lambda: NOW)
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=Decimal("100000"), config_hash="cfg-m11", clock=lambda: NOW)
    intent_b = _fresh_intent(conn, book_id="BASELINE", intent_id="intent-b", quantity=Decimal("1"), limit_price=Decimal("100"))
    intent_e = _fresh_intent(conn, book_id="ENHANCED", intent_id="intent-e", quantity=Decimal("1"), limit_price=Decimal("51"))

    cfg = _config()
    cfg = replace(cfg, external_broker=replace(cfg.external_broker, maximum_daily_notional_usd=Decimal("150")))

    reserved = _reserve_daily_notional(
        conn, cfg, book_id="BASELINE", fingerprint=FINGERPRINT, client_order_id="epb-baseline-100",
        intent=intent_b, now=NOW,
    )
    assert reserved["state"] == "RESERVED"

    # Same Alpaca paper account (fingerprint), a different local book: $51
    # more would push the account past its $150 daily cap.
    with pytest.raises(ExternalPaperError) as excinfo:
        _reserve_daily_notional(
            conn, cfg, book_id="ENHANCED", fingerprint=FINGERPRINT, client_order_id="epb-enhanced-51",
            intent=intent_e, now=NOW,
        )
    assert excinfo.value.code == "EXTERNAL_DAILY_NOTIONAL_LIMIT"


def test_different_account_fingerprints_have_independent_daily_caps():
    from dataclasses import replace

    conn = connect(":memory:")
    cash_ledger.open_book(conn, book_id="BASELINE", starting_cash_usd=Decimal("100000"), config_hash="cfg-m11", clock=lambda: NOW)
    cash_ledger.open_book(conn, book_id="ENHANCED", starting_cash_usd=Decimal("100000"), config_hash="cfg-m11", clock=lambda: NOW)
    _fresh_intent(conn, book_id="BASELINE", intent_id="intent-b", quantity=Decimal("1"), limit_price=Decimal("100"))
    _fresh_intent(conn, book_id="ENHANCED", intent_id="intent-e", quantity=Decimal("1"), limit_price=Decimal("100"))

    runtime_a, runtime_b = FakeRuntime(), _AltFingerprintRuntime()
    cfg_baseline = _config(books=("BASELINE",))
    cfg_baseline = replace(cfg_baseline, external_broker=replace(cfg_baseline.external_broker, maximum_daily_notional_usd=Decimal("100")))
    cfg_enhanced = _config(books=("ENHANCED",))
    cfg_enhanced = replace(cfg_enhanced, external_broker=replace(cfg_enhanced.external_broker, maximum_daily_notional_usd=Decimal("100")))

    activate_external_reconciliation_baseline(
        conn, book_id="BASELINE", operator="alice", runtime=runtime_a, config=cfg_baseline, clock=lambda: NOW,
    )
    activate_external_reconciliation_baseline(
        conn, book_id="ENHANCED", operator="alice", runtime=runtime_b, config=cfg_enhanced, clock=lambda: NOW,
    )

    preview_b = preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-b", operator="alice",
        runtime=runtime_a, config=cfg_baseline, clock=lambda: NOW,
    )
    result_b = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-b", preview_id=preview_b["preview_id"],
        operator="alice", reason="first account, exhausts its own cap", runtime=runtime_a, config=cfg_baseline,
        clock=lambda: NOW,
    )
    assert result_b["status"] == STATE_SUBMITTED

    # A different, unrelated Alpaca paper account at the identical notional
    # cap must not be blocked by the first account's exhausted budget.
    preview_e = preview_external_paper_order(
        conn, book_id="ENHANCED", paper_order_intent_id="intent-e", operator="alice",
        runtime=runtime_b, config=cfg_enhanced, clock=lambda: NOW,
    )
    result_e = submit_external_paper_order(
        conn, book_id="ENHANCED", paper_order_intent_id="intent-e", preview_id=preview_e["preview_id"],
        operator="alice", reason="second, independent account", runtime=runtime_b, config=cfg_enhanced,
        clock=lambda: NOW,
    )
    assert result_e["status"] == STATE_SUBMITTED


def test_concurrent_reservations_cannot_exceed_account_cap():
    from dataclasses import replace

    conn = connect(":memory:")
    intent = _seed(conn)  # intent-1: quantity 2 * limit 40 = $80 notional
    cfg = _config()
    cfg = replace(cfg, external_broker=replace(cfg.external_broker, maximum_daily_notional_usd=Decimal("100")))
    reservation_date = NOW.astimezone(timezone.utc).date().isoformat()
    # A reservation committed by a concurrent process/book for the same
    # account/day, just before this one's atomic check-and-reserve runs.
    repo.save_external_daily_reservation(conn, {
        "client_order_id": "epb-competing-concurrent-reservation", "account_fingerprint": FINGERPRINT,
        "reservation_date": reservation_date, "book_id": "ENHANCED",
        "reserved_notional_usd": Decimal("60"), "state": "RESERVED",
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    })
    with pytest.raises(ExternalPaperError) as excinfo:
        _reserve_daily_notional(
            conn, cfg, book_id="BASELINE", fingerprint=FINGERPRINT, client_order_id="epb-new-order-over-cap",
            intent=intent, now=NOW,
        )
    assert excinfo.value.code == "EXTERNAL_DAILY_NOTIONAL_LIMIT"
    # The would-be-blocked reservation was never created.
    assert repo.load_external_daily_reservation(conn, "epb-new-order-over-cap") is None


def test_duplicate_client_order_id_reuses_existing_reservation():
    conn = connect(":memory:")
    intent = _seed(conn)
    cfg = _config()
    client_order_id, _ = derive_external_order_identity({**intent, "_external_config_hash": cfg.config_hash})
    first = _reserve_daily_notional(
        conn, cfg, book_id="BASELINE", fingerprint=FINGERPRINT, client_order_id=client_order_id,
        intent=intent, now=NOW,
    )
    second = _reserve_daily_notional(
        conn, cfg, book_id="BASELINE", fingerprint=FINGERPRINT, client_order_id=client_order_id,
        intent=intent, now=NOW,
    )
    assert first["client_order_id"] == second["client_order_id"]
    assert Decimal(str(first["reserved_notional_usd"])) == Decimal(str(second["reserved_notional_usd"])) == Decimal("80")
    reservation_date = NOW.astimezone(timezone.utc).date().isoformat()
    assert repo.sum_external_daily_reserved_notional(conn, FINGERPRINT, reservation_date) == Decimal("80")


def test_broker_rejection_releases_daily_reservation():
    conn = connect(":memory:")
    intent = _seed(conn)
    cfg, runtime = _config(), _RejectingRuntime()
    preview = _preview(conn, runtime, cfg)
    result = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="rejected order", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["status"] == STATE_REJECTED
    reservation = repo.load_external_daily_reservation(conn, preview["client_order_id"])
    assert reservation["state"] == "RELEASED"


def test_ambiguous_submission_marks_reservation_reconciliation_required():
    """Complements `test_ambiguous_submission_is_repaired_by_lookup_without_
    resubmit` and `test_authoritative_not_found_allows_one_explicit_retry`,
    which already prove a blind resubmit is blocked while ambiguous — this
    proves the daily reservation itself also stays held (not released) and
    keeps counting against the account's cap until reconciliation resolves
    it."""
    conn = connect(":memory:")
    intent = _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    runtime.raise_submit = True
    preview = _preview(conn, runtime, cfg)
    result = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="ambiguous", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["status"] == STATE_UNKNOWN
    reservation = repo.load_external_daily_reservation(conn, preview["client_order_id"])
    assert reservation["state"] == "RECONCILIATION_REQUIRED"
    reservation_date = NOW.astimezone(timezone.utc).date().isoformat()
    assert repo.sum_external_daily_reserved_notional(conn, FINGERPRINT, reservation_date) == Decimal("80")


def test_submission_without_baseline_fails_closed():
    conn = connect(":memory:")
    intent = _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        runtime=runtime, config=cfg, clock=lambda: NOW,
    )  # deliberately no activate_external_reconciliation_baseline call
    with pytest.raises(ExternalPaperError) as excinfo:
        submit_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
            operator="alice", reason="no baseline activated", runtime=runtime, config=cfg, clock=lambda: NOW,
        )
    assert excinfo.value.code == "RECONCILIATION_BASELINE_MISSING"
    assert runtime.submit_calls == 0


def test_reconciliation_does_not_auto_create_missing_baseline():
    conn = connect(":memory:")
    intent = _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview = preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    outcome = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert outcome["critical"] == 1
    assert "RECONCILIATION_BASELINE_MISSING" in outcome["statuses"]
    assert repo.load_external_reconciliation_baseline(conn, "BASELINE") is None


def test_baseline_requires_operator_review_after_prior_external_activity():
    conn = connect(":memory:")
    intent = _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    with pytest.raises(ExternalPaperError) as excinfo:
        activate_external_reconciliation_baseline(
            conn, book_id="BASELINE", operator="alice", runtime=runtime, config=cfg, clock=lambda: NOW,
        )
    assert excinfo.value.code == "BASELINE_REQUIRES_OPERATOR_REVIEW"
    assert repo.load_external_reconciliation_baseline(conn, "BASELINE") is None


def test_baseline_account_fingerprint_mismatch_fails_closed():
    conn = connect(":memory:")
    intent = _seed(conn)
    cfg, runtime = _config(), FakeRuntime()
    repo.save_external_reconciliation_baseline(conn, {
        "book_id": "BASELINE", "snapshot_timestamp": NOW.isoformat(),
        "account_fingerprint": "acct_deadbeefdeadbeefdeadbeefdeadbeef",
        "local_settled_cash_usd": "100000", "local_positions_json": "{}",
        "broker_cash_usd": "100000", "broker_positions_json": "{}",
        "source_environment": "paper", "config_hash": cfg.config_hash, "created_at": NOW.isoformat(),
    })
    preview = preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    with pytest.raises(ExternalPaperError) as excinfo:
        submit_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
            operator="alice", reason="baseline fingerprint mismatch", runtime=runtime, config=cfg,
            clock=lambda: NOW,
        )
    assert excinfo.value.code == "ACCOUNT_FINGERPRINT_MISMATCH"
    assert runtime.submit_calls == 0
