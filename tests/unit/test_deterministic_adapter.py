from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_research.execution.adapter_protocol import BrokerExecutionSnapshot
from trading_research.execution.models import PaperExecutionEvent, PaperExecutionResult
from trading_research.runtime.deterministic_adapter import DeterministicAdapterError, DeterministicPaperAdapter

from tests.support.execution_fixtures import buy_candidate_payload
from trading_research.execution.config import load_execution_config
from trading_research.execution.intent_builder import build_paper_order_intent

NOW = datetime(2026, 7, 11, 14, 0, 30, tzinfo=timezone.utc)
CONFIG = load_execution_config()


@pytest.fixture
def intent():
    payload = buy_candidate_payload()
    return build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")


def _event(intent, event_type, filled_qty, fill_price, event_id_suffix="1", occurred_at=NOW):
    return PaperExecutionEvent(
        event_id=f"{intent.intent_id}-{event_id_suffix}", intent_id=intent.intent_id,
        recommendation_id=intent.recommendation_id, symbol=intent.symbol, event_type=event_type,
        broker_order_id="broker-1", quantity=intent.quantity, filled_quantity=filled_qty,
        fill_price=fill_price, occurred_at=occurred_at, raw_status=event_type.lower(),
    )


def test_submit_without_registration_raises(intent):
    adapter = DeterministicPaperAdapter()
    with pytest.raises(DeterministicAdapterError):
        adapter.submit(intent)


def test_full_fill_scenario(intent):
    adapter = DeterministicPaperAdapter()
    event = _event(intent, "FILLED", intent.quantity, Decimal("14.30"))
    result = PaperExecutionResult(
        intent_id=intent.intent_id, recommendation_id=intent.recommendation_id, final_status="FILLED",
        requested_quantity=intent.quantity, filled_quantity=intent.quantity,
        average_fill_price=Decimal("14.30"), fees=Decimal("0"), event_ids=(event.event_id,), completed_at=NOW,
    )
    adapter.register(intent.intent_id, (event,), result)
    events, got_result = adapter.submit(intent)
    assert events == (event,)
    assert got_result.final_status == "FILLED"
    assert adapter.submit_calls == [intent.intent_id]


def test_partial_then_full_fill_scenario(intent):
    adapter = DeterministicPaperAdapter()
    partial = _event(intent, "PARTIALLY_FILLED", 40, Decimal("14.28"), "1")
    full = _event(intent, "FILLED", 30, Decimal("14.32"), "2")
    result = PaperExecutionResult(
        intent_id=intent.intent_id, recommendation_id=intent.recommendation_id, final_status="FILLED",
        requested_quantity=intent.quantity, filled_quantity=70, average_fill_price=Decimal("14.2971"),
        fees=Decimal("0"), event_ids=(partial.event_id, full.event_id), completed_at=NOW,
    )
    adapter.register(intent.intent_id, (partial, full), result)
    events, got_result = adapter.submit(intent)
    assert len(events) == 2
    assert events[0].event_type == "PARTIALLY_FILLED"
    assert events[1].event_type == "FILLED"


def test_rejection_scenario(intent):
    adapter = DeterministicPaperAdapter()
    event = _event(intent, "REJECTED", 0, None)
    result = PaperExecutionResult(
        intent_id=intent.intent_id, recommendation_id=intent.recommendation_id, final_status="REJECTED",
        requested_quantity=intent.quantity, filled_quantity=0, average_fill_price=None, fees=Decimal("0"),
        event_ids=(event.event_id,), completed_at=NOW,
    )
    adapter.register(intent.intent_id, (event,), result)
    events, got_result = adapter.submit(intent)
    assert got_result.final_status == "REJECTED"
    assert got_result.filled_quantity == 0


def test_cancellation_scenario(intent):
    adapter = DeterministicPaperAdapter()
    event = _event(intent, "CANCELLED", 0, None)
    result = PaperExecutionResult(
        intent_id=intent.intent_id, recommendation_id=intent.recommendation_id, final_status="CANCELLED",
        requested_quantity=intent.quantity, filled_quantity=0, average_fill_price=None, fees=Decimal("0"),
        event_ids=(event.event_id,), completed_at=NOW,
    )
    adapter.register(intent.intent_id, (event,), result)
    _, got_result = adapter.submit(intent)
    assert got_result.final_status == "CANCELLED"


def test_adapter_error_scenario(intent):
    adapter = DeterministicPaperAdapter()
    event = _event(intent, "ERROR", 0, None)
    result = PaperExecutionResult(
        intent_id=intent.intent_id, recommendation_id=intent.recommendation_id, final_status="ERROR",
        requested_quantity=intent.quantity, filled_quantity=0, average_fill_price=None, fees=Decimal("0"),
        event_ids=(event.event_id,), completed_at=NOW,
    )
    adapter.register(intent.intent_id, (event,), result)
    _, got_result = adapter.submit(intent)
    assert got_result.final_status == "ERROR"


def test_reconcile_without_registration_raises(intent):
    adapter = DeterministicPaperAdapter()
    with pytest.raises(DeterministicAdapterError):
        adapter.reconcile(intent.intent_id)


def test_reconciliation_mismatch_scenario(intent):
    adapter = DeterministicPaperAdapter()
    snapshot = BrokerExecutionSnapshot(
        intent_id=intent.intent_id, broker_quantity=70, broker_notional=Decimal("1001.00"),
        broker_status="fill", as_of=NOW,
    )
    adapter.register_reconciliation(intent.intent_id, snapshot)
    got = adapter.reconcile(intent.intent_id)
    assert got.broker_quantity == 70
    assert adapter.reconcile_calls == [intent.intent_id]
