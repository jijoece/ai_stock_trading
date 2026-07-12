from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_research.execution.config import load_execution_config
from trading_research.execution.intent_builder import build_paper_order_intent
from trading_research.execution.ledger_events import apply_all_new_events, apply_paper_execution_event
from trading_research.execution.models import PaperExecutionEvent
from trading_research.paper.ledger import FillModel, PaperLedger
from trading_research.storage import execution_repositories as exec_repo
from trading_research.storage.database import connect

from tests.support.execution_fixtures import buy_candidate_payload, insert_recommendation_row

NOW = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)
EXEC_CONFIG = load_execution_config()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "ledger_events.sqlite3")
    yield c
    c.close()


@pytest.fixture
def ledger(conn):
    return PaperLedger(conn, starting_cash=10_000.0, fill_model=FillModel(slippage_bps=10))


@pytest.fixture
def intent(conn):
    """A real, persisted intent — satisfies the FK chain
    paper_execution_events.intent_id -> paper_execution_intents.intent_id ->
    recommendations.rec_id that a bare string like "intent-1" would violate."""
    payload = buy_candidate_payload(rec_id="rec-1", symbol="SOFI", shares=100, entry_price=14.25)
    insert_recommendation_row(conn, payload)
    built = build_paper_order_intent(payload, config=EXEC_CONFIG, git_sha="abc1234")
    exec_repo.save_intent(conn, built, now=NOW)
    return built


def _event(intent, event_id, event_type, filled_qty, fill_price):
    return PaperExecutionEvent(
        event_id=event_id, intent_id=intent.intent_id, recommendation_id=intent.recommendation_id,
        symbol=intent.symbol, event_type=event_type, broker_order_id="broker-1",
        quantity=100, filled_quantity=filled_qty, fill_price=fill_price, occurred_at=NOW,
        raw_status=event_type.lower(),
    )


def test_full_fill_updates_ledger_once(conn, ledger, intent):
    event = _event(intent, "evt-1", "FILLED", 100, Decimal("14.30"))
    applied = apply_paper_execution_event(conn, ledger, event, now=NOW)
    assert applied is True
    assert ledger.positions()[0]["qty"] == 100
    assert ledger.positions()[0]["avg_cost"] == pytest.approx(14.30)


def test_partial_fills_update_incrementally(conn, ledger, intent):
    e1 = _event(intent, "evt-1", "PARTIALLY_FILLED", 40, Decimal("14.28"))
    e2 = _event(intent, "evt-2", "FILLED", 30, Decimal("14.32"))
    apply_paper_execution_event(conn, ledger, e1, now=NOW)
    apply_paper_execution_event(conn, ledger, e2, now=NOW)
    pos = ledger.positions()[0]
    assert pos["qty"] == 70
    assert 14.28 < pos["avg_cost"] < 14.32


def test_duplicate_event_ignored_not_applied_twice(conn, ledger, intent):
    event = _event(intent, "evt-1", "FILLED", 100, Decimal("14.30"))
    first = apply_paper_execution_event(conn, ledger, event, now=NOW)
    second = apply_paper_execution_event(conn, ledger, event, now=NOW)
    assert first is True
    assert second is False
    assert ledger.positions()[0]["qty"] == 100  # not 200


def test_cancelled_order_does_not_update_position(conn, ledger, intent):
    event = _event(intent, "evt-1", "CANCELLED", 0, None)
    applied = apply_paper_execution_event(conn, ledger, event, now=NOW)
    assert applied is False
    assert ledger.positions() == []


def test_rejected_order_does_not_update_position(conn, ledger, intent):
    event = _event(intent, "evt-1", "REJECTED", 0, None)
    applied = apply_paper_execution_event(conn, ledger, event, now=NOW)
    assert applied is False
    assert ledger.positions() == []


def test_zero_fill_does_not_update_cash_or_holdings(conn, ledger, intent):
    cash_before = ledger.total_cash()
    event = _event(intent, "evt-1", "PARTIALLY_FILLED", 0, None)
    applied = apply_paper_execution_event(conn, ledger, event, now=NOW)
    assert applied is False
    assert ledger.positions() == []
    assert ledger.total_cash() == cash_before


def test_submitted_and_accepted_events_are_no_ops(conn, ledger, intent):
    for event_type in ("SUBMITTED", "ACCEPTED"):
        event = _event(intent, f"evt-{event_type}", event_type, 0, None)
        applied = apply_paper_execution_event(conn, ledger, event, now=NOW)
        assert applied is False
    assert ledger.positions() == []


def test_error_event_does_not_update_position(conn, ledger, intent):
    event = _event(intent, "evt-1", "ERROR", 0, None)
    applied = apply_paper_execution_event(conn, ledger, event, now=NOW)
    assert applied is False
    assert ledger.positions() == []


def test_replay_is_deterministic(conn, ledger, intent):
    events = (
        _event(intent, "evt-1", "PARTIALLY_FILLED", 40, Decimal("14.28")),
        _event(intent, "evt-2", "FILLED", 30, Decimal("14.32")),
    )
    apply_all_new_events(conn, ledger, events, now=NOW)
    pos_after_first_pass = dict(ledger.positions()[0])

    # Replaying the exact same event stream must be a full no-op.
    applied_count = apply_all_new_events(conn, ledger, events, now=NOW)
    assert applied_count == 0
    assert dict(ledger.positions()[0]) == pos_after_first_pass


def test_out_of_order_callback_still_converges(conn, ledger, intent):
    e1 = _event(intent, "evt-1", "PARTIALLY_FILLED", 40, Decimal("14.28"))
    e2 = _event(intent, "evt-2", "PARTIALLY_FILLED", 30, Decimal("14.32"))
    # Apply in reverse arrival order.
    apply_paper_execution_event(conn, ledger, e2, now=NOW)
    apply_paper_execution_event(conn, ledger, e1, now=NOW)
    assert ledger.positions()[0]["qty"] == 70


def test_fees_accounted_for_via_spread_and_slippage(conn, ledger, intent):
    event = _event(intent, "evt-1", "FILLED", 100, Decimal("14.30"))
    apply_paper_execution_event(conn, ledger, event, now=NOW)
    fill_row = conn.execute("SELECT price, qty, spread_cost, slippage_cost FROM simulated_fills").fetchone()
    assert fill_row["price"] == 14.30
    # External fills carry zero adapter-attributed spread/slippage cost —
    # any such cost is already embedded in the supplied fill_price.
    assert fill_row["spread_cost"] == 0
    assert fill_row["slippage_cost"] == 0


def test_recommendation_linkage_preserved_on_fill(conn, ledger, intent):
    event = _event(intent, "evt-1", "FILLED", 100, Decimal("14.30"))
    apply_paper_execution_event(conn, ledger, event, now=NOW)
    order = conn.execute("SELECT rec_id FROM simulated_orders").fetchone()
    assert order["rec_id"] == intent.recommendation_id
