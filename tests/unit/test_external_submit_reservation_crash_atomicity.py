"""Milestone 11.3 Part 36/37: the one offline end-to-end scenario Milestone
11.2's report explicitly flagged as unresolved —

    reservation + SUBMISSION_REQUESTED commit
    -> simulated process crash before broker call
    -> restart
    -> no blind broker mutation
    -> operator sees unresolved pre-submission checkpoint

`external_broker.py::_submit_once` previously composed the reservation
(`reserve_for_order`/`reserve_shares_for_sell`, each `commit=True` by
default) and the `SUBMISSION_REQUESTED` event append
(`_append_event`/`save_external_order_event`, also `commit=True`) as two
independently committed writes. Milestone 11.3 made them one atomic
transaction (see `_submit_once`'s `begin_immediate`/`commit()` block). This
test proves the composition is now safe on both sides of that atomic
boundary: a crash *before* the broker is ever called leaves a durable,
self-consistent, human-visible checkpoint — never a half-written
reservation with no explaining event, and never a blind assumption that
submission succeeded."""
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
    QUEUE_STATUS_AWAITING_SUBMISSION, STATE_SUBMISSION_REQUESTED,
    activate_external_reconciliation_baseline, derive_external_queue_status,
    preview_external_paper_order, submit_external_paper_order,
)
from trading_research.paper_books.models import PaperBookOrderIntent, PaperRiskDecision, RISK_APPROVED
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
FINGERPRINT = "acct_0123456789abcdef0123456789abcdef"


class _SimulatedProcessCrash(BaseException):
    """Deliberately a `BaseException`, not `Exception` — `_submit_once`'s
    `except Exception:` handler around the broker call must NOT catch this,
    exactly like a real process crash (SIGKILL, hard power loss) is not a
    Python exception any handler could ever observe."""


class _CrashBeforeBrokerCallRuntime:
    """A runtime whose `submit_limit_order` never returns — the broker was
    never actually reached (the process died first), so no order accepted/
    rejected acknowledgement of any kind exists anywhere, local or remote."""

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
        raise _SimulatedProcessCrash("process died before the broker call could complete")

    def get_external_positions(self, book_id):
        return {"book_id": book_id, "account_fingerprint": FINGERPRINT, "positions": []}

    def get_external_account_snapshot(self, book_id):
        return {
            "provider": "alpaca_paper", "environment": "paper", "book_id": book_id,
            "account_fingerprint": FINGERPRINT, "cash": "100000", "equity": "100000",
            "buying_power": "100000", "currency": "USD", "as_of": NOW.isoformat(),
        }


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
            Decimal("100"), Decimal("300"), ("limit",), ("day",), 1,
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
    return repo.load_order_intent(conn, "BASELINE", "intent-1")


def test_reservation_and_submission_requested_survive_crash_before_broker_call(tmp_path):
    db_path = tmp_path / "crash.sqlite3"
    cfg = _config()

    conn = connect(db_path)
    _seed(conn)
    activate_external_reconciliation_baseline(
        conn, book_id="BASELINE", operator="alice", runtime=_CrashBeforeBrokerCallRuntime(), config=cfg,
        clock=lambda: NOW,
    )
    preview_runtime = _CrashBeforeBrokerCallRuntime()
    preview = preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        runtime=preview_runtime, config=cfg, clock=lambda: NOW,
    )
    assert preview["result"] == "APPROVED"

    crash_runtime = _CrashBeforeBrokerCallRuntime()
    with pytest.raises(_SimulatedProcessCrash):
        submit_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
            operator="alice", reason="submit", runtime=crash_runtime, config=cfg, clock=lambda: NOW,
        )
    assert crash_runtime.submit_calls == 1  # the broker call was attempted and "crashed" — never acknowledged
    conn.close()  # simulates process death: no further code in this process runs

    # --- restart: fresh connection against the same on-disk database ---
    restarted = connect(db_path)

    # The atomic reservation + SUBMISSION_REQUESTED commit is durable.
    assert cash_ledger.reserved_cash(restarted, "BASELINE") == Decimal("80")
    assert cash_ledger.available_cash(restarted, "BASELINE") == Decimal("99920")

    event = repo.load_latest_external_order_event_for_intent(restarted, "BASELINE", "intent-1")
    assert event is not None
    assert event["new_state"] == STATE_SUBMISSION_REQUESTED

    # No fills, no broker mutation of any kind was ever recorded locally —
    # the crash happened before any broker acknowledgement existed.
    fills = repo.list_fills_for_intent(restarted, "BASELINE", "intent-1")
    assert fills == []

    # The order's own status column was never blindly advanced past
    # PENDING_SUBMISSION — _submit_once only calls update_order_status
    # after a real broker response, which never arrived.
    order_row = repo.load_order_intent(restarted, "BASELINE", "intent-1")
    assert order_row["status"] == "PENDING_SUBMISSION"

    # The operator's own status view surfaces the unresolved checkpoint —
    # not silence, not a fabricated success, not AWAITING_SUBMISSION (which
    # would suggest nothing had happened yet).
    status = derive_external_queue_status(restarted, book_id="BASELINE", paper_order_intent_id="intent-1")
    assert status["status"] == STATE_SUBMISSION_REQUESTED
    assert status["status"] != QUEUE_STATUS_AWAITING_SUBMISSION
    restarted.close()


def test_crash_before_reservation_commit_leaves_zero_effects(tmp_path, monkeypatch):
    """The other half of the same atomic boundary: if the crash happens
    *inside* the reservation+event transaction (before its own commit), the
    whole transaction must roll back — no partial reservation, no partial
    event, exactly as if submission was never attempted."""
    db_path = tmp_path / "crash2.sqlite3"
    cfg = _config()

    conn = connect(db_path)
    _seed(conn)
    activate_external_reconciliation_baseline(
        conn, book_id="BASELINE", operator="alice", runtime=_CrashBeforeBrokerCallRuntime(), config=cfg,
        clock=lambda: NOW,
    )
    preview_runtime = _CrashBeforeBrokerCallRuntime()
    preview = preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        runtime=preview_runtime, config=cfg, clock=lambda: NOW,
    )

    import trading_research.paper_books.external_broker as external_broker_module
    real_append_event = external_broker_module._append_event

    def _crash_after_reservation(*args, **kwargs):
        raise _SimulatedProcessCrash("crash between reservation insert and event append")

    monkeypatch.setattr(external_broker_module, "_append_event", _crash_after_reservation)
    crash_runtime = _CrashBeforeBrokerCallRuntime()
    with pytest.raises(_SimulatedProcessCrash):
        submit_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
            operator="alice", reason="submit", runtime=crash_runtime, config=cfg, clock=lambda: NOW,
        )
    monkeypatch.setattr(external_broker_module, "_append_event", real_append_event)
    conn.close()

    restarted = connect(db_path)
    # Rollback undid the reservation too — zero effects, not a stranded one.
    assert cash_ledger.reserved_cash(restarted, "BASELINE") == 0
    assert cash_ledger.available_cash(restarted, "BASELINE") == Decimal("100000")
    event = repo.load_latest_external_order_event_for_intent(restarted, "BASELINE", "intent-1")
    assert event is None or event["new_state"] != STATE_SUBMISSION_REQUESTED
    assert crash_runtime.submit_calls == 0  # broker was never reached at all
    restarted.close()
