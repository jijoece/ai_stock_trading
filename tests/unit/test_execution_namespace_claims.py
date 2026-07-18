"""Milestone 11.3.1 Item 6: durable per-intent execution-namespace claims
(`paper_order_execution_claims`) and crash-atomic local BUY intent +
reservation creation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tests.unit.test_external_paper_broker import _config, _preview, FakeRuntime
from trading_research.paper_books import cash_ledger, execution
from trading_research.paper_books.external_broker import ExternalPaperError
from trading_research.paper_books.models import PaperBookOrderIntent, PaperRiskDecision, RISK_APPROVED
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect
from trading_research.storage.paper_books_repositories import (
    EXECUTION_NAMESPACE_EXTERNAL, EXECUTION_NAMESPACE_LOCAL, ExecutionNamespaceConflictError,
)
from trading_research.storage.schema_version import apply_pending_schema_migrations

NOW = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)


def _seed(conn):
    """Open the BASELINE/ENHANCED books and the shared risk decision only --
    deliberately does NOT pre-insert an order intent (unlike
    `test_external_paper_broker.py::_seed`), since these tests need control
    over whether/when the intent row and its execution-namespace claim are
    created."""
    cash_ledger.open_book(
        conn, book_id="BASELINE", starting_cash_usd=Decimal("100000"), config_hash="cfg-m11", clock=lambda: NOW,
    )
    cash_ledger.open_book(
        conn, book_id="ENHANCED", starting_cash_usd=Decimal("100000"), config_hash="cfg-m11", clock=lambda: NOW,
    )
    decision = PaperRiskDecision(RISK_APPROVED, Decimal("80"), Decimal("80"), Decimal("2"), (), "risk-v1")
    repo.save_risk_decision(conn, "risk-1", "BASELINE", "cycle-1", "rec-1", "AAPL", decision, "snap-1", NOW)


def _intent(**overrides):
    fields = dict(
        paper_order_intent_id="intent-1", book_id="BASELINE", experiment_arm="BASELINE",
        cycle_id="cycle-1", recommendation_id="rec-1", symbol="AAPL", side="BUY",
        order_type="LIMIT", quantity=Decimal("2"), limit_price=Decimal("40"),
        notional_usd=Decimal("80"), time_in_force="DAY", as_of=NOW,
        risk_decision_id="risk-1", portfolio_snapshot_id="snap-1", config_hash="cfg-m11",
        created_at=NOW,
    )
    fields.update(overrides)
    return PaperBookOrderIntent(**fields)


# --- 1. local path wins and external preview/submission is blocked ----------


def test_local_claim_blocks_external_preview():
    conn = connect(":memory:")
    _seed(conn)
    execution.submit_and_simulate(
        conn, _intent(), execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW,
    )
    claim = repo.load_execution_namespace_claim(conn, "BASELINE", "intent-1")
    assert claim["execution_namespace"] == EXECUTION_NAMESPACE_LOCAL

    with pytest.raises(ExternalPaperError) as excinfo:
        _preview(conn, FakeRuntime(), _config())
    assert excinfo.value.code == "INTENT_NOT_ELIGIBLE_FOR_EXTERNAL"


# --- 2. external path wins and local simulation is blocked -------------------


def test_external_claim_blocks_local_simulation():
    conn = connect(":memory:")
    _seed(conn)
    repo.save_order_intent(conn, _intent())
    _preview(conn, FakeRuntime(), _config())
    claim = repo.load_execution_namespace_claim(conn, "BASELINE", "intent-1")
    assert claim["execution_namespace"] == EXECUTION_NAMESPACE_EXTERNAL

    with pytest.raises(execution.FillSimulationError, match="externally scoped"):
        execution.submit_and_simulate(
            conn, _intent(), execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW,
        )


# --- 3. exactly one namespace claim is persisted -----------------------------


def test_exactly_one_claim_row_persisted_for_local_path():
    conn = connect(":memory:")
    _seed(conn)
    execution.submit_and_simulate(
        conn, _intent(), execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW,
    )
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM paper_order_execution_claims WHERE book_id = ? AND paper_order_intent_id = ?",
        ("BASELINE", "intent-1"),
    ).fetchone()
    assert rows["c"] == 1


# --- 4. no double reservation occurs -----------------------------------------


def test_repeated_local_submission_does_not_double_reserve():
    conn = connect(":memory:")
    _seed(conn)
    intent = _intent()
    execution.submit_and_simulate(conn, intent, execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW)
    reserved_after_first = cash_ledger.reserved_cash(conn, "BASELINE")
    # Idempotent replay of the same submission: intent already exists, so
    # this must not reserve a second time.
    execution.submit_and_simulate(conn, intent, execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW)
    assert cash_ledger.reserved_cash(conn, "BASELINE") == reserved_after_first


# --- 5. no local fill and broker submission can both occur -------------------


def test_no_local_fill_and_broker_submission_can_both_occur():
    conn = connect(":memory:")
    _seed(conn)
    repo.save_order_intent(conn, _intent())
    _preview(conn, FakeRuntime(), _config())
    with pytest.raises(execution.FillSimulationError):
        execution.submit_and_simulate(
            conn, _intent(), execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW,
        )
    assert repo.list_fills_for_intent(conn, "BASELINE", "intent-1") == []


# --- 6. crash after namespace claim but before intent/reservation -----------


def test_crash_after_claim_but_before_intent_is_recoverable(monkeypatch):
    conn = connect(":memory:")
    _seed(conn)
    intent = _intent()

    import trading_research.paper_books.execution as execution_module
    real_save_order_intent = repo.save_order_intent

    def _crash(*args, **kwargs):
        raise RuntimeError("simulated crash between claim and intent insert")

    monkeypatch.setattr(execution_module.repo, "save_order_intent", _crash)
    with pytest.raises(RuntimeError):
        execution.submit_and_simulate(conn, intent, execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW)
    monkeypatch.setattr(execution_module.repo, "save_order_intent", real_save_order_intent)

    # The whole transaction (claim + intent + reservation) rolled back --
    # no orphaned claim, no orphaned reservation.
    assert repo.load_execution_namespace_claim(conn, "BASELINE", "intent-1") is None
    assert repo.load_order_intent(conn, "BASELINE", "intent-1") is None
    assert cash_ledger.reserved_cash(conn, "BASELINE") == Decimal("0")

    # A fresh retry succeeds cleanly.
    execution.submit_and_simulate(conn, intent, execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW)
    claim = repo.load_execution_namespace_claim(conn, "BASELINE", "intent-1")
    assert claim["execution_namespace"] == EXECUTION_NAMESPACE_LOCAL


# --- 7. crash after intent but before reservation leaves no partial txn ------


def test_crash_after_intent_but_before_reservation_leaves_no_partial_transaction(monkeypatch):
    conn = connect(":memory:")
    _seed(conn)
    intent = _intent()

    import trading_research.paper_books.execution as execution_module
    real_reserve = cash_ledger.reserve_for_order

    def _crash(*args, **kwargs):
        raise RuntimeError("simulated crash between intent insert and reservation")

    monkeypatch.setattr(execution_module.cash_ledger, "reserve_for_order", _crash)
    with pytest.raises(RuntimeError):
        execution.submit_and_simulate(conn, intent, execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW)
    monkeypatch.setattr(execution_module.cash_ledger, "reserve_for_order", real_reserve)

    # Whole transaction rolled back: no claim, no intent, no reservation.
    assert repo.load_execution_namespace_claim(conn, "BASELINE", "intent-1") is None
    assert repo.load_order_intent(conn, "BASELINE", "intent-1") is None
    assert cash_ledger.reserved_cash(conn, "BASELINE") == Decimal("0")


# --- 8. replay verifies reservation integrity --------------------------------


def test_replay_fails_closed_when_reservation_missing_for_existing_pending_intent():
    conn = connect(":memory:")
    _seed(conn)
    # Simulate a legacy/foreign intent inserted directly with no reservation
    # and no claim -- indistinguishable from a genuine crash-before-
    # reservation scenario once a claim already exists for another namespace.
    repo.save_order_intent(conn, _intent())
    repo.claim_execution_namespace(
        conn, "BASELINE", "intent-1", EXECUTION_NAMESPACE_LOCAL, NOW, "local_simulator",
    )
    # No reservation was ever created for this BUY intent -- this must fail
    # closed rather than silently proceed to fill without one.
    with pytest.raises(execution.FillSimulationError, match="reservation"):
        execution.submit_and_simulate(
            conn, _intent(),
            execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW,
        )


# --- 9. cancel and expire release only remaining reserved cash --------------


def test_cancel_releases_only_remaining_reserved_cash():
    conn = connect(":memory:")
    _seed(conn)
    intent = _intent(limit_price=Decimal("10"), notional_usd=Decimal("20"))  # will not cross -> stays pending
    result = execution.submit_and_simulate(conn, intent, execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW)
    assert result["status"] == "PENDING_SUBMISSION"
    assert cash_ledger.reserved_cash(conn, "BASELINE") == Decimal("20")
    execution.cancel_pending_intent(conn, intent, NOW)
    assert cash_ledger.reserved_cash(conn, "BASELINE") == Decimal("0")


# --- 10. namespace claims survive restart ------------------------------------


def test_namespace_claim_survives_restart(tmp_path):
    db_path = tmp_path / "claims_restart.sqlite3"
    conn = connect(db_path)
    _seed(conn)
    execution.submit_and_simulate(
        conn, _intent(), execution.MarketSimulationInput(Decimal("39"), Decimal("39")), NOW,
    )
    conn.close()

    restarted = connect(db_path)
    claim = repo.load_execution_namespace_claim(restarted, "BASELINE", "intent-1")
    assert claim["execution_namespace"] == EXECUTION_NAMESPACE_LOCAL
    restarted.close()


# --- 11 & 12. legacy rows are migrated safely, and migration is idempotent --


def test_legacy_rows_are_migrated_and_migration_is_idempotent(tmp_path):
    """Simulates a database written before Item 6 introduced
    `paper_order_execution_claims`: intents (one local-shaped, one with
    external evidence) exist with no claim row and the schema_version ledger
    has not yet recorded migration 2. `apply_pending_schema_migrations` must
    backfill both correctly, and running it again must be a safe no-op."""
    from trading_research.paper_books.external_broker import preview_external_paper_order

    db_path = tmp_path / "legacy_migration.sqlite3"
    conn = connect(db_path)  # first connect() already runs migration 2 once
    _seed(conn)
    repo.save_order_intent(conn, _intent(paper_order_intent_id="legacy-local"))
    repo.save_order_intent(conn, _intent(paper_order_intent_id="legacy-external"))
    preview_external_paper_order(
        conn, book_id="BASELINE", paper_order_intent_id="legacy-external", operator="alice",
        runtime=FakeRuntime(), config=_config(), clock=lambda: NOW,
    )
    # Roll the database back to "pre-Item-6": no claim rows, and the
    # schema_version ledger has not yet recorded migration 2 -- exactly the
    # state a real database upgrading from before Item 6 would be in.
    conn.execute("DELETE FROM paper_order_execution_claims")
    conn.execute("DELETE FROM schema_version WHERE version = 2")
    conn.commit()

    apply_pending_schema_migrations(conn)
    legacy_local_claim = repo.load_execution_namespace_claim(conn, "BASELINE", "legacy-local")
    legacy_external_claim = repo.load_execution_namespace_claim(conn, "BASELINE", "legacy-external")
    assert legacy_local_claim["execution_namespace"] == EXECUTION_NAMESPACE_LOCAL
    assert legacy_external_claim["execution_namespace"] == EXECUTION_NAMESPACE_EXTERNAL

    # Idempotent: migration 2 is already recorded, so a second call must be
    # a no-op that does not alter or error on the now-existing claim rows.
    apply_pending_schema_migrations(conn)
    legacy_local_claim_again = repo.load_execution_namespace_claim(conn, "BASELINE", "legacy-local")
    legacy_external_claim_again = repo.load_execution_namespace_claim(conn, "BASELINE", "legacy-external")
    assert legacy_local_claim_again == legacy_local_claim
    assert legacy_external_claim_again == legacy_external_claim
    conn.close()


# --- claim conflict raises a clear domain error ------------------------------


def test_claim_execution_namespace_raises_on_conflicting_namespace():
    conn = connect(":memory:")
    _seed(conn)
    repo.claim_execution_namespace(conn, "BASELINE", "intent-1", EXECUTION_NAMESPACE_LOCAL, NOW, "local_simulator")
    with pytest.raises(ExecutionNamespaceConflictError):
        repo.claim_execution_namespace(
            conn, "BASELINE", "intent-1", EXECUTION_NAMESPACE_EXTERNAL, NOW, "external_operator",
        )


def test_claim_execution_namespace_same_namespace_is_idempotent_noop():
    conn = connect(":memory:")
    _seed(conn)
    first = repo.claim_execution_namespace(conn, "BASELINE", "intent-1", EXECUTION_NAMESPACE_LOCAL, NOW, "op")
    second = repo.claim_execution_namespace(conn, "BASELINE", "intent-1", EXECUTION_NAMESPACE_LOCAL, NOW, "op")
    assert first is True
    assert second is False
