from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.execution.config import load_execution_config
from trading_research.execution.eligibility import PaperExecutionEligibilityPolicy
from trading_research.execution.intent_builder import build_paper_order_intent
from trading_research.paper.ledger import PaperLedger
from trading_research.runtime.client.process_client import RuntimeClient
from trading_research.services.submit_credentialed_paper_order import submit_credentialed_paper_order
from trading_research.services.sync_paper_orders import (
    OUTCOME_RUNTIME_UNAVAILABLE,
    OUTCOME_STILL_UNKNOWN,
    OUTCOME_UPDATED,
    sync_paper_orders,
)
from trading_research.storage import execution_repositories as exec_repo
from trading_research.storage.database import connect
from trading_research.universe.tickers import default_universe

from tests.support.execution_fixtures import buy_candidate_payload, insert_recommendation_row
from tests.support.runtime_client_fixtures import FakeTransport, fake_transport_factory, start_ready_client

NOW = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)
CONFIG = load_execution_config()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "sync.sqlite3")
    yield c
    c.close()


@pytest.fixture
def ledger(conn):
    return PaperLedger(conn, starting_cash=100_000.0)


def _client_with_fake():
    fake = FakeTransport()
    client = RuntimeClient(
        command=["python3", "-m", "trading_paper_runtime"],
        transport_factory=fake_transport_factory(fake),
        startup_timeout_seconds=1.0, request_timeout_seconds=1.0,
    )
    start_ready_client(client, fake)
    return client, fake


def _submit_and_accept(conn, client, fake, rec_id, symbol="SOFI"):
    payload = buy_candidate_payload(rec_id=rec_id, symbol=symbol, now=NOW)
    insert_recommendation_row(conn, payload)
    intent = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    policy = PaperExecutionEligibilityPolicy(universe=default_universe(), config=CONFIG)

    fake.queue_failure("UNKNOWN_ORDER", "not found")
    fake.queue_success(_order_snapshot(intent.intent_id, status="ACCEPTED"))
    submit_credentialed_paper_order(
        rec_id, conn=conn, execution_config=CONFIG, eligibility_policy=policy, client=client,
        git_sha="abc1234", clock=lambda: NOW,
    )
    return intent


def _order_snapshot(intent_id, *, status, filled_quantity=0, average_fill_price=None, broker_order_id="alp-1"):
    return {
        "intent_id": intent_id, "client_order_id": intent_id, "broker_order_id": broker_order_id,
        "status": status, "raw_broker_status": status.lower(), "quantity": 70,
        "filled_quantity": filled_quantity, "average_fill_price": average_fill_price,
        "submitted_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    }


def test_full_fill_applies_to_ledger_and_finalizes_result(conn, ledger):
    client, fake = _client_with_fake()
    intent = _submit_and_accept(conn, client, fake, "rec-sync-1")

    fake.queue_success(_order_snapshot(intent.intent_id, status="FILLED", filled_quantity=70, average_fill_price="14.92"))
    outcomes = sync_paper_orders(conn=conn, ledger=ledger, client=client, clock=lambda: NOW + timedelta(minutes=1))

    assert len(outcomes) == 1
    assert outcomes[0].outcome == OUTCOME_UPDATED
    assert outcomes[0].submission_status == "FILLED"

    positions = ledger.positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "SOFI"
    assert positions[0]["qty"] == 70

    result = exec_repo.get_result(conn, intent.intent_id)
    assert result is not None
    assert result.final_status == "FILLED"
    assert result.filled_quantity == 70

    # No longer unresolved.
    assert exec_repo.list_unresolved_submissions(conn) == []


def test_two_partial_fills_apply_incremental_deltas_only(conn, ledger):
    client, fake = _client_with_fake()
    intent = _submit_and_accept(conn, client, fake, "rec-sync-2")

    fake.queue_success(
        _order_snapshot(intent.intent_id, status="PARTIALLY_FILLED", filled_quantity=30, average_fill_price="15.00")
    )
    sync_paper_orders(conn=conn, ledger=ledger, client=client, clock=lambda: NOW + timedelta(minutes=1))
    assert ledger.positions()[0]["qty"] == 30

    fake.queue_success(
        _order_snapshot(intent.intent_id, status="FILLED", filled_quantity=70, average_fill_price="15.10")
    )
    sync_paper_orders(conn=conn, ledger=ledger, client=client, clock=lambda: NOW + timedelta(minutes=2))
    assert ledger.positions()[0]["qty"] == 70  # 30 + 40 delta, never double-applied

    result = exec_repo.get_result(conn, intent.intent_id)
    assert result.final_status == "FILLED"
    assert result.filled_quantity == 70


def test_repeated_poll_with_unchanged_status_does_not_reapply(conn, ledger):
    client, fake = _client_with_fake()
    intent = _submit_and_accept(conn, client, fake, "rec-sync-3")

    fake.queue_success(_order_snapshot(intent.intent_id, status="ACCEPTED", filled_quantity=0))
    outcomes = sync_paper_orders(conn=conn, ledger=ledger, client=client, clock=lambda: NOW + timedelta(minutes=1))
    assert outcomes[0].new_events == 0  # no fill, non-terminal -> no event created
    assert ledger.positions() == []

    # Still unresolved (ACCEPTED is not terminal).
    assert len(exec_repo.list_unresolved_submissions(conn)) == 1


def test_cancellation_after_partial_fill_finalizes_with_partial_quantity(conn, ledger):
    client, fake = _client_with_fake()
    intent = _submit_and_accept(conn, client, fake, "rec-sync-4")

    fake.queue_success(
        _order_snapshot(intent.intent_id, status="PARTIALLY_FILLED", filled_quantity=20, average_fill_price="15.00")
    )
    sync_paper_orders(conn=conn, ledger=ledger, client=client, clock=lambda: NOW + timedelta(minutes=1))

    fake.queue_success(
        _order_snapshot(intent.intent_id, status="CANCELLED", filled_quantity=20, average_fill_price="15.00")
    )
    sync_paper_orders(conn=conn, ledger=ledger, client=client, clock=lambda: NOW + timedelta(minutes=2))

    result = exec_repo.get_result(conn, intent.intent_id)
    assert result.final_status == "CANCELLED"
    assert result.filled_quantity == 20
    assert ledger.positions()[0]["qty"] == 20


def test_still_unknown_order_is_reported_not_finalized(conn, ledger):
    client, fake = _client_with_fake()
    intent = _submit_and_accept(conn, client, fake, "rec-sync-5")

    fake.queue_failure("UNKNOWN_ORDER", "vanished")
    outcomes = sync_paper_orders(conn=conn, ledger=ledger, client=client, clock=lambda: NOW + timedelta(minutes=1))
    assert outcomes[0].outcome == OUTCOME_STILL_UNKNOWN
    assert exec_repo.get_result(conn, intent.intent_id) is None


def test_runtime_unavailable_during_poll_is_reported(conn, ledger):
    client, fake = _client_with_fake()
    _submit_and_accept(conn, client, fake, "rec-sync-6")

    fake.queue_eof()
    outcomes = sync_paper_orders(conn=conn, ledger=ledger, client=client, clock=lambda: NOW + timedelta(minutes=1))
    assert outcomes[0].outcome == OUTCOME_RUNTIME_UNAVAILABLE


def test_no_unresolved_submissions_is_a_no_op(conn, ledger):
    client, fake = _client_with_fake()
    outcomes = sync_paper_orders(conn=conn, ledger=ledger, client=client, clock=lambda: NOW)
    assert outcomes == []
