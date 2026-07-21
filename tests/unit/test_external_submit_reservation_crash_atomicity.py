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

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.paper_books import cash_ledger
from trading_research.paper_books.config import (
    ExecutionSection, ExternalBrokerSection, PaperBookDefinition,
    PaperBooksConfiguration, RiskSection, ScheduledIntegrationSection, ValuationSection,
)
from trading_research.paper_books.external_broker import (
    QUEUE_STATUS_AWAITING_SUBMISSION, STATE_SUBMISSION_REQUESTED, STATE_UNKNOWN,
    ExternalPaperError,
    _reserve_daily_notional,
    activate_external_reconciliation_baseline, derive_external_order_identity, derive_external_queue_status,
    preview_external_paper_order, reconcile_external_paper_order, retry_external_paper_order,
    submit_external_paper_order,
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
    reservation = repo.load_active_attempt_reservation(
        restarted, preview["client_order_id"], 0, FINGERPRINT, "BASELINE",
    )
    assert reservation["state"] == "RESERVED"
    reused = _reserve_daily_notional(
        restarted, cfg, book_id="BASELINE", fingerprint=FINGERPRINT,
        client_order_id=preview["client_order_id"], attempt_number=0,
        intent=repo.load_order_intent(restarted, "BASELINE", "intent-1"),
        now=NOW + timedelta(days=1),
    )
    assert reused["reservation_id"] == reservation["reservation_id"]
    assert len(repo.list_attempt_reservations_for_attempt(
        restarted, preview["client_order_id"], 0, FINGERPRINT, "BASELINE",
    )) == 1
    restarted.close()


# --- Milestone 27 B1/B4: retry-preparation transaction rollback -------------


class _RetryFaultRuntime:
    """Controllable runtime for retry-preparation fault injection: the
    original submit is made ambiguous (`raise_submit`), the follow-up
    reconciliation lookup is an authoritative NOT_FOUND (no broker order
    exists), and `submit_limit_order` for any later retry attempt is counted
    so tests can prove the broker was never reached when preparation itself
    fails."""

    def __init__(self):
        self.submit_calls = 0
        self.raise_submit = False

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
        if self.raise_submit:
            raise RuntimeError("submission outcome is ambiguous")
        return {
            "provider": "alpaca_paper", "environment": "paper", "account_fingerprint": FINGERPRINT,
            "book_id": payload["book_id"], "client_order_id": payload["client_order_id"],
            "broker_order_id": "bo-1", "symbol": payload["symbol"], "side": payload["side"],
            "quantity": str(payload["quantity"]), "limit_price": payload["limit_price"],
            "time_in_force": "DAY", "status": "ACCEPTED", "submitted_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(), "filled_quantity": "0", "average_fill_price": None,
            "rejection_code": None,
        }

    def get_order_by_client_order_id(self, book_id, client_order_id):
        return None  # authoritative NOT_FOUND: no broker order was ever created

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


def _prepare_confirmed_not_found_retry(db_path, cfg):
    """Attempt 0: ambiguous submit, then authoritative NOT_FOUND
    reconciliation -- the exact preconditions `retry_external_paper_order`
    requires. Returns the still-open connection, the client_order_id, and
    the retry runtime (fresh, `raise_submit=False`) for the caller's own
    fault-injected retry attempt."""
    conn = connect(db_path)
    _seed(conn)
    runtime = _RetryFaultRuntime()
    activate_external_reconciliation_baseline(
        conn, book_id="BASELINE", operator="alice", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    preview = preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    runtime.raise_submit = True
    result = submit_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", preview_id=preview["preview_id"],
        operator="alice", reason="ambiguous", runtime=runtime, config=cfg, clock=lambda: NOW,
    )
    assert result["status"] == STATE_UNKNOWN
    lookup_result = reconcile_external_paper_order(
        conn, book_id="BASELINE", runtime=runtime, config=cfg,
        client_order_id=preview["client_order_id"], clock=lambda: NOW,
    )
    assert lookup_result["status"] == "ORDER_MISSING_AT_BROKER"
    # A fresh runtime instance for the retry itself, so its own
    # `submit_calls` counter starts at zero and isolates whether *this*
    # retry attempt ever reached the broker from the original ambiguous
    # attempt's own (already-counted) call.
    retry_runtime = _RetryFaultRuntime()
    return conn, preview["client_order_id"], retry_runtime


def _assert_retry_preparation_fully_rolled_back(conn, client_order_id, retry_runtime):
    """Shared assertions for every fault-injection scenario below
    (docs/milestones/27.md B4 "Transaction rollback"): the prior
    reservation is unchanged, no new reservation or event exists, the
    lookup remains unconsumed, and the broker was never called."""
    prior_reservation = repo.load_active_attempt_reservation(conn, client_order_id, 0, FINGERPRINT, "BASELINE")
    assert prior_reservation is not None and prior_reservation["state"] == "RECONCILIATION_REQUIRED"
    new_reservation = repo.load_active_attempt_reservation(conn, client_order_id, 1, FINGERPRINT, "BASELINE")
    assert new_reservation is None
    current = repo.load_latest_external_order_event(conn, "BASELINE", client_order_id)
    assert current["new_state"] == STATE_UNKNOWN
    assert current["attempt_number"] == 0
    lookup = repo.load_latest_external_lookup(conn, "BASELINE", client_order_id)
    assert lookup["consumed_by_retry_event_id"] is None
    assert retry_runtime.submit_calls == 0


def test_retry_rollback_on_reservation_supersede_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "retry_rollback_supersede.sqlite3"
    conn, client_order_id, retry_runtime = _prepare_confirmed_not_found_retry(db_path, _config())
    conn.close()

    import trading_research.storage.paper_books_repositories as repo_module

    def _fail_transition(*args, **kwargs):
        raise RuntimeError("simulated failure during reservation supersede")

    monkeypatch.setattr(repo_module, "transition_attempt_reservation_state", _fail_transition)
    conn = connect(db_path)
    with pytest.raises(RuntimeError):
        retry_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            reason="retry", runtime=retry_runtime, config=_config(), clock=lambda: NOW,
        )
    monkeypatch.undo()
    _assert_retry_preparation_fully_rolled_back(conn, client_order_id, retry_runtime)
    conn.close()


def test_retry_rollback_on_new_reservation_creation_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "retry_rollback_create.sqlite3"
    conn, client_order_id, retry_runtime = _prepare_confirmed_not_found_retry(db_path, _config())
    conn.close()

    import trading_research.storage.paper_books_repositories as repo_module

    def _fail_save(*args, **kwargs):
        raise RuntimeError("simulated failure during new reservation creation")

    monkeypatch.setattr(repo_module, "save_attempt_reservation", _fail_save)
    conn = connect(db_path)
    with pytest.raises(RuntimeError):
        retry_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            reason="retry", runtime=retry_runtime, config=_config(), clock=lambda: NOW,
        )
    monkeypatch.undo()
    _assert_retry_preparation_fully_rolled_back(conn, client_order_id, retry_runtime)
    conn.close()


def test_retry_rollback_on_event_append_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "retry_rollback_event.sqlite3"
    conn, client_order_id, retry_runtime = _prepare_confirmed_not_found_retry(db_path, _config())
    conn.close()

    import trading_research.storage.paper_books_repositories as repo_module

    def _fail_save_event(*args, **kwargs):
        raise RuntimeError("simulated failure during SUBMISSION_REQUESTED event append")

    monkeypatch.setattr(repo_module, "save_external_order_event", _fail_save_event)
    conn = connect(db_path)
    with pytest.raises(RuntimeError):
        retry_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            reason="retry", runtime=retry_runtime, config=_config(), clock=lambda: NOW,
        )
    monkeypatch.undo()
    _assert_retry_preparation_fully_rolled_back(conn, client_order_id, retry_runtime)
    conn.close()


def test_retry_rollback_on_lookup_consumption_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "retry_rollback_lookup.sqlite3"
    conn, client_order_id, retry_runtime = _prepare_confirmed_not_found_retry(db_path, _config())
    conn.close()

    import trading_research.storage.paper_books_repositories as repo_module

    def _fail_consume(*args, **kwargs):
        raise RuntimeError("simulated failure during lookup consumption")

    monkeypatch.setattr(repo_module, "consume_external_lookup", _fail_consume)
    conn = connect(db_path)
    with pytest.raises(RuntimeError):
        retry_external_paper_order(
            conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
            reason="retry", runtime=retry_runtime, config=_config(), clock=lambda: NOW,
        )
    monkeypatch.undo()
    _assert_retry_preparation_fully_rolled_back(conn, client_order_id, retry_runtime)
    conn.close()


def test_retry_preparation_commits_atomically_with_reservation_rollover(tmp_path):
    """docs/milestones/27.md B1: reservation rollover, the next-attempt
    checkpoint event, and lookup consumption commit together, strictly
    before the broker is ever called."""
    db_path = tmp_path / "retry_prepare_atomic.sqlite3"
    conn, client_order_id, retry_runtime = _prepare_confirmed_not_found_retry(db_path, _config())
    result = retry_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="intent-1", operator="alice",
        reason="retry", runtime=retry_runtime, config=_config(), clock=lambda: NOW,
    )
    assert retry_runtime.submit_calls == 1
    prior_reservation = repo.load_latest_attempt_reservation(conn, client_order_id, 0, FINGERPRINT, "BASELINE")
    assert prior_reservation["state"] == "SUPERSEDED_BY_RETRY"
    new_reservation = repo.load_active_attempt_reservation(conn, client_order_id, 1, FINGERPRINT, "BASELINE")
    assert new_reservation is not None
    assert result["status"] in ("SUBMITTED", STATE_UNKNOWN)
    conn.close()
