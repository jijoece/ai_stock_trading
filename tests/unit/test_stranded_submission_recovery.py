"""Milestone 11.3.1 Item 1: an explicit, safe recovery path for an external
order whose local event chain is stranded at SUBMISSION_REQUESTED after a
hard crash between the reservation+checkpoint commit and any broker
response. See `external_broker.py::recover_stranded_submission`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_research.paper_books import cash_ledger
from trading_research.paper_books.config import (
    ExecutionSection, ExternalBrokerSection, PaperBookDefinition,
    PaperBooksConfiguration, RiskSection, ScheduledIntegrationSection, ValuationSection,
)
from trading_research.paper_books.external_broker import (
    STATE_FILLED, STATE_SUBMISSION_REQUESTED, STATE_UNKNOWN,
    ExternalPaperError, derive_external_order_identity, preview_external_paper_order,
    recover_stranded_submission, retry_external_paper_order, submit_external_paper_order,
)
from trading_research.paper_books.models import PaperBookOrderIntent, PaperRiskDecision, RISK_APPROVED
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
FINGERPRINT = "acct_0123456789abcdef0123456789abcdef"


class _CrashBeforeBrokerCallRuntime:
    def __init__(self):
        self.submit_calls = 0

    def account_check(self, book_id):
        return {
            "provider": "alpaca_paper", "environment": "paper", "book_id": book_id,
            "account_fingerprint": FINGERPRINT, "paper_endpoint_verified": True,
        }

    def preview_limit_order(self, payload):
        return {
            "provider": "alpaca_paper", "environment": "paper", "book_id": payload["book_id"],
            "client_order_id": payload["client_order_id"], "account_fingerprint": FINGERPRINT,
            "result": "APPROVED", "reasons": [],
        }

    def submit_limit_order(self, payload):
        self.submit_calls += 1
        raise BaseException("process died before the broker call could complete")

    def get_order_by_client_order_id(self, book_id, client_order_id):
        raise NotImplementedError

    def cancel_external_order(self, book_id, client_order_id, account_fingerprint):
        raise NotImplementedError

    def list_order_fills(self, book_id, client_order_id):
        return []

    def get_external_positions(self, book_id):
        return {"book_id": book_id, "account_fingerprint": FINGERPRINT, "positions": []}

    def get_external_account_snapshot(self, book_id):
        return {
            "provider": "alpaca_paper", "environment": "paper", "book_id": book_id,
            "account_fingerprint": FINGERPRINT, "cash": "100000", "equity": "100000",
            "buying_power": "100000", "currency": "USD", "as_of": NOW.isoformat(),
        }

    def list_recent_external_orders(self, book_id, *, limit=50):
        return []


class _RecoveryRuntime(_CrashBeforeBrokerCallRuntime):
    """Reused for recovery: `submit_limit_order`/`preview_limit_order` must
    never be called again during recovery -- only the lookup methods."""

    def __init__(self, *, lookup_mode: str, order_overrides: dict | None = None):
        super().__init__()
        self.lookup_mode = lookup_mode
        self.lookup_calls = 0
        self.order_overrides = order_overrides or {}

    def get_order_by_client_order_id(self, book_id, client_order_id):
        self.lookup_calls += 1
        if self.lookup_mode == "not_found":
            return None
        if self.lookup_mode == "timeout":
            raise TimeoutError("broker lookup timed out")
        if self.lookup_mode == "malformed":
            return {"status": "FILLED"}  # missing required fields
        order = {
            "provider": "alpaca_paper", "environment": "paper", "account_fingerprint": FINGERPRINT,
            "book_id": book_id, "client_order_id": client_order_id, "broker_order_id": "bo-1",
            "symbol": "AAPL", "side": "BUY", "quantity": "2", "limit_price": "40",
            "time_in_force": "DAY", "status": "FILLED", "submitted_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(), "filled_quantity": "2", "average_fill_price": "40",
            "rejection_code": None,
        }
        order.update(self.order_overrides)
        return order

    def list_order_fills(self, book_id, client_order_id):
        if self.lookup_mode != "found":
            return []
        return [{
            "fill_id": "f-1", "broker_order_id": "bo-1", "client_order_id": client_order_id,
            "book_id": book_id, "symbol": "AAPL", "side": "BUY", "quantity": "2", "price": "40",
            "filled_at": NOW.isoformat(), "account_fingerprint": FINGERPRINT,
        }]

    def get_external_positions(self, book_id):
        positions = [{
            "symbol": "AAPL", "quantity": "2", "average_entry_price": "40",
            "market_value": "80", "as_of": NOW.isoformat(),
        }] if self.lookup_mode == "found" else []
        return {"book_id": book_id, "account_fingerprint": FINGERPRINT, "positions": positions}

    def get_external_account_snapshot(self, book_id):
        cash = "99920" if self.lookup_mode == "found" else "100000"
        return {
            "provider": "alpaca_paper", "environment": "paper", "book_id": book_id,
            "account_fingerprint": FINGERPRINT, "cash": cash, "equity": "100000",
            "buying_power": cash, "currency": "USD", "as_of": NOW.isoformat(),
        }

    def cancel_external_order(self, book_id, client_order_id, account_fingerprint):
        raise NotImplementedError

    def list_recent_external_orders(self, book_id, *, limit=50):
        return []


def _config():
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
            True, "alpaca_paper", True, ("BASELINE",), True, 300,
            Decimal("100"), Decimal("300"), ("limit",), ("day",), 3,
        ),
    )


def _seed(conn):
    cash_ledger.open_book(
        conn, book_id="BASELINE", starting_cash_usd=Decimal("100000"),
        config_hash="cfg-m11", clock=lambda: NOW,
    )
    decision = PaperRiskDecision(RISK_APPROVED, Decimal("80"), Decimal("80"), Decimal("2"), (), "risk-v1")
    repo.save_risk_decision(conn, "risk-1", "BASELINE", "cycle-1", "rec-1", "AAPL", decision, "snap-1", NOW)
    intent = PaperBookOrderIntent(
        paper_order_intent_id="intent-1", book_id="BASELINE", experiment_arm="BASELINE",
        cycle_id="cycle-1", recommendation_id="rec-1", symbol="AAPL", side="BUY",
        order_type="LIMIT", quantity=Decimal("2"), limit_price=Decimal("40"),
        notional_usd=Decimal("80"), time_in_force="DAY", as_of=NOW,
        risk_decision_id="risk-1", portfolio_snapshot_id="snap-1", config_hash="cfg-m11",
        created_at=NOW,
    )
    repo.save_order_intent(conn, intent)


def _strand_at_submission_requested(db_path, cfg) -> None:
    """Reproduce the crash-after-checkpoint scenario end to end, then close
    the connection (simulated process death)."""
    conn = connect(db_path)
    _seed(conn)
    preview = preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        runtime=_CrashBeforeBrokerCallRuntime(), config=cfg, clock=lambda: NOW,
    )
    with pytest.raises(BaseException):
        submit_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
            operator="alice", reason="submit", runtime=_CrashBeforeBrokerCallRuntime(), config=cfg,
            clock=lambda: NOW,
        )
    conn.close()


# --- 1. crash after checkpoint, broker order found on restart ---------------


def test_recovery_finds_broker_order_and_applies_fills(tmp_path):
    db_path = tmp_path / "recover_found.sqlite3"
    cfg = _config()
    _strand_at_submission_requested(db_path, cfg)

    restarted = connect(db_path)
    runtime = _RecoveryRuntime(lookup_mode="found")
    result = recover_stranded_submission(
        restarted, book_id="BASELINE", paper_order_intent_id="intent-1",
        runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert runtime.submit_calls == 0
    event = repo.load_latest_external_order_event_for_intent(restarted, "BASELINE", "intent-1")
    assert event["new_state"] == STATE_FILLED
    fills = repo.list_fills_for_intent(restarted, "BASELINE", "intent-1")
    assert len(fills) == 1
    # Terminal releases only the remaining reservation.
    assert cash_ledger.reserved_cash(restarted, "BASELINE") == Decimal("0")
    restarted.close()


# --- 2. crash after checkpoint, authoritative broker NOT_FOUND ---------------


def test_recovery_with_authoritative_not_found_transitions_to_unknown(tmp_path):
    db_path = tmp_path / "recover_not_found.sqlite3"
    cfg = _config()
    _strand_at_submission_requested(db_path, cfg)

    restarted = connect(db_path)
    runtime = _RecoveryRuntime(lookup_mode="not_found")
    recover_stranded_submission(
        restarted, book_id="BASELINE", paper_order_intent_id="intent-1",
        runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert runtime.submit_calls == 0
    event = repo.load_latest_external_order_event_for_intent(restarted, "BASELINE", "intent-1")
    assert event["new_state"] == STATE_UNKNOWN
    # Reservation is retained -- ambiguity, not proof of non-submission.
    assert cash_ledger.reserved_cash(restarted, "BASELINE") == Decimal("80")

    client_order_id, _ = derive_external_order_identity(repo.load_order_intent(restarted, "BASELINE", "intent-1"))
    lookup = repo.load_latest_external_lookup(restarted, "BASELINE", client_order_id)
    assert lookup["result"] == "NOT_FOUND"
    assert lookup["authoritative"] == 1
    restarted.close()


# --- 3. crash after checkpoint, lookup timeout -------------------------------


def test_recovery_with_lookup_timeout_is_ambiguous_not_authoritative(tmp_path):
    db_path = tmp_path / "recover_timeout.sqlite3"
    cfg = _config()
    _strand_at_submission_requested(db_path, cfg)

    restarted = connect(db_path)
    runtime = _RecoveryRuntime(lookup_mode="timeout")
    recover_stranded_submission(
        restarted, book_id="BASELINE", paper_order_intent_id="intent-1",
        runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert runtime.submit_calls == 0
    event = repo.load_latest_external_order_event_for_intent(restarted, "BASELINE", "intent-1")
    assert event["new_state"] == STATE_UNKNOWN
    assert cash_ledger.reserved_cash(restarted, "BASELINE") == Decimal("80")

    client_order_id, _ = derive_external_order_identity(repo.load_order_intent(restarted, "BASELINE", "intent-1"))
    lookup = repo.load_latest_external_lookup(restarted, "BASELINE", client_order_id)
    # A timeout/exception is never authoritative NOT_FOUND evidence.
    assert lookup["authoritative"] == 0
    restarted.close()


# --- 4. crash after checkpoint, malformed broker response --------------------


def test_recovery_with_malformed_broker_response_stays_blocked(tmp_path):
    db_path = tmp_path / "recover_malformed.sqlite3"
    cfg = _config()
    _strand_at_submission_requested(db_path, cfg)

    restarted = connect(db_path)
    runtime = _RecoveryRuntime(lookup_mode="malformed")
    recover_stranded_submission(
        restarted, book_id="BASELINE", paper_order_intent_id="intent-1",
        runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert runtime.submit_calls == 0
    event = repo.load_latest_external_order_event_for_intent(restarted, "BASELINE", "intent-1")
    assert event["new_state"] == STATE_UNKNOWN
    assert cash_ledger.reserved_cash(restarted, "BASELINE") == Decimal("80")
    restarted.close()


# --- 5. repeated recovery invocation is idempotent ---------------------------


def test_repeated_recovery_invocation_is_idempotent(tmp_path):
    db_path = tmp_path / "recover_idempotent.sqlite3"
    cfg = _config()
    _strand_at_submission_requested(db_path, cfg)

    restarted = connect(db_path)
    runtime1 = _RecoveryRuntime(lookup_mode="not_found")
    recover_stranded_submission(
        restarted, book_id="BASELINE", paper_order_intent_id="intent-1",
        runtime=runtime1, config=cfg, clock=lambda: NOW,
    )
    event_after_first = repo.load_latest_external_order_event_for_intent(restarted, "BASELINE", "intent-1")
    reserved_after_first = cash_ledger.reserved_cash(restarted, "BASELINE")

    # Once genuinely recovered off SUBMISSION_REQUESTED, the function's own
    # precondition makes a second call a clear, deterministic no-op error
    # rather than a silent success -- same input, same outcome, and (the
    # actual idempotency property this test protects) zero additional
    # broker calls or state changes.
    runtime2 = _RecoveryRuntime(lookup_mode="not_found")
    with pytest.raises(ExternalPaperError) as excinfo:
        recover_stranded_submission(
            restarted, book_id="BASELINE", paper_order_intent_id="intent-1",
            runtime=runtime2, config=cfg, clock=lambda: NOW,
        )
    assert excinfo.value.code == "RECOVERY_NOT_APPLICABLE"
    assert runtime2.submit_calls == 0
    assert runtime2.lookup_calls == 0

    event_after_second = repo.load_latest_external_order_event_for_intent(restarted, "BASELINE", "intent-1")
    assert event_after_second["new_state"] == STATE_UNKNOWN == event_after_first["new_state"]
    assert event_after_second["external_order_event_id"] == event_after_first["external_order_event_id"]
    assert cash_ledger.reserved_cash(restarted, "BASELINE") == reserved_after_first == Decimal("80")
    restarted.close()


# --- 6. recovery never calls broker submission -------------------------------


def test_recovery_never_calls_broker_submission(tmp_path):
    db_path = tmp_path / "recover_no_submit.sqlite3"
    cfg = _config()
    _strand_at_submission_requested(db_path, cfg)

    restarted = connect(db_path)
    for mode in ("found", "not_found", "timeout", "malformed"):
        runtime = _RecoveryRuntime(lookup_mode=mode)
        try:
            recover_stranded_submission(
                restarted, book_id="BASELINE", paper_order_intent_id="intent-1",
                runtime=runtime, config=cfg, clock=lambda: NOW,
            )
        except ExternalPaperError:
            pass
        assert runtime.submit_calls == 0
    restarted.close()


# --- 7. retry remains blocked without attempt-bound NOT_FOUND ----------------


def test_retry_still_blocked_until_recovery_produces_fresh_not_found(tmp_path):
    db_path = tmp_path / "recover_retry_gate.sqlite3"
    cfg = _config()
    _strand_at_submission_requested(db_path, cfg)

    restarted = connect(db_path)
    with pytest.raises(ExternalPaperError) as excinfo:
        retry_external_paper_order(
            restarted, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            reason="retry", runtime=_RecoveryRuntime(lookup_mode="not_found"), config=cfg, clock=lambda: NOW,
        )
    assert excinfo.value.code == "RETRY_NOT_ALLOWED"

    recover_stranded_submission(
        restarted, book_id="BASELINE", paper_order_intent_id="intent-1",
        runtime=_RecoveryRuntime(lookup_mode="not_found"), config=cfg, clock=lambda: NOW,
    )
    # Now a fresh preview-backed retry is admissible -- the point of this
    # test is the *gate* (RETRY_NOT_ALLOWED before recovery, admitted after),
    # not the retried submission's own outcome, so the retry's own broker
    # call is made ambiguous again (an ordinary Exception, not a simulated
    # crash) purely so the assertion below has a deterministic status.
    class _AmbiguousRetryRuntime(_RecoveryRuntime):
        def submit_limit_order(self, payload):
            self.submit_calls += 1
            raise RuntimeError("retry submission outcome is ambiguous")

    retry_runtime = _AmbiguousRetryRuntime(lookup_mode="not_found")
    result = retry_external_paper_order(
        restarted, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        reason="retry", runtime=retry_runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["status"] == STATE_UNKNOWN
    restarted.close()


# --- 8. reservation remains held while outcome is ambiguous ------------------


def test_reservation_remains_held_while_ambiguous(tmp_path):
    db_path = tmp_path / "recover_hold.sqlite3"
    cfg = _config()
    _strand_at_submission_requested(db_path, cfg)

    restarted = connect(db_path)
    before = cash_ledger.reserved_cash(restarted, "BASELINE")
    recover_stranded_submission(
        restarted, book_id="BASELINE", paper_order_intent_id="intent-1",
        runtime=_RecoveryRuntime(lookup_mode="not_found"), config=cfg, clock=lambda: NOW,
    )
    after = cash_ledger.reserved_cash(restarted, "BASELINE")
    assert before == after == Decimal("80")
    restarted.close()


# --- 9. terminal broker state releases only the remaining reservation -------


def test_terminal_found_state_releases_only_remaining_reservation(tmp_path):
    db_path = tmp_path / "recover_release.sqlite3"
    cfg = _config()
    _strand_at_submission_requested(db_path, cfg)

    restarted = connect(db_path)
    recover_stranded_submission(
        restarted, book_id="BASELINE", paper_order_intent_id="intent-1",
        runtime=_RecoveryRuntime(lookup_mode="found"), config=cfg, clock=lambda: NOW,
    )
    assert cash_ledger.reserved_cash(restarted, "BASELINE") == Decimal("0")
    assert cash_ledger.available_cash(restarted, "BASELINE") == Decimal("99920")
    restarted.close()


# --- 10. recovery after restart uses a fresh database connection ------------


def test_recovery_operates_on_a_freshly_opened_connection(tmp_path):
    db_path = tmp_path / "recover_fresh_conn.sqlite3"
    cfg = _config()
    _strand_at_submission_requested(db_path, cfg)

    # A brand-new connect() call -- no shared in-memory state, no in-flight
    # transaction, no cached Python objects from the crashed process.
    restarted = connect(db_path)
    recover_stranded_submission(
        restarted, book_id="BASELINE", paper_order_intent_id="intent-1",
        runtime=_RecoveryRuntime(lookup_mode="found"), config=cfg, clock=lambda: NOW,
    )
    event = repo.load_latest_external_order_event_for_intent(restarted, "BASELINE", "intent-1")
    assert event["new_state"] == STATE_FILLED
    restarted.close()


# --- recovery is not applicable outside SUBMISSION_REQUESTED -----------------


def test_recovery_not_applicable_when_not_stranded(tmp_path):
    db_path = tmp_path / "recover_not_applicable.sqlite3"
    cfg = _config()
    conn = connect(db_path)
    _seed(conn)
    with pytest.raises(ExternalPaperError) as excinfo:
        recover_stranded_submission(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1",
            runtime=_RecoveryRuntime(lookup_mode="found"), config=cfg, clock=lambda: NOW,
        )
    assert excinfo.value.code == "RECOVERY_NOT_APPLICABLE"
    conn.close()
