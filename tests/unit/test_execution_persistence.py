import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_research.execution.adapter_protocol import BrokerExecutionSnapshot
from trading_research.execution.config import load_execution_config
from trading_research.execution.intent_builder import build_paper_order_intent
from trading_research.execution.models import PaperExecutionEvent, PaperExecutionResult
from trading_research.execution.reconciliation import reconcile_intent
from trading_research.storage import execution_repositories as exec_repo
from trading_research.storage.database import connect

from tests.support.execution_fixtures import buy_candidate_payload, insert_recommendation_row

NOW = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)
CONFIG = load_execution_config()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "persist.sqlite3")
    yield c
    c.close()


@pytest.fixture
def intent(conn):
    payload = buy_candidate_payload(rec_id="rec-1", symbol="SOFI")
    insert_recommendation_row(conn, payload)
    return build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")


def test_repeated_intent_save_is_idempotent_noop(conn, intent):
    first = exec_repo.save_intent(conn, intent, now=NOW)
    second = exec_repo.save_intent(conn, intent, now=NOW)
    assert first is True
    assert second is False
    count = conn.execute("SELECT COUNT(*) AS n FROM paper_execution_intents").fetchone()["n"]
    assert count == 1


def test_second_different_intent_for_same_recommendation_raises_integrity_error(conn, intent):
    exec_repo.save_intent(conn, intent, now=NOW)
    from dataclasses import replace

    other = replace(intent, intent_id="intent-a-totally-different-id")
    with pytest.raises(sqlite3.IntegrityError):
        exec_repo.save_intent(conn, other, now=NOW)


def test_intent_links_to_recommendation(conn, intent):
    exec_repo.save_intent(conn, intent, now=NOW)
    fetched = exec_repo.get_intent_by_recommendation(conn, intent.recommendation_id, intent.execution_version)
    assert fetched.intent_id == intent.intent_id
    assert fetched.recommendation_id == intent.recommendation_id


def test_duplicate_event_id_is_idempotent_noop(conn, intent):
    exec_repo.save_intent(conn, intent, now=NOW)
    event = PaperExecutionEvent(
        event_id="evt-1", intent_id=intent.intent_id, recommendation_id=intent.recommendation_id,
        symbol=intent.symbol, event_type="FILLED", broker_order_id="b-1", quantity=intent.quantity,
        filled_quantity=intent.quantity, fill_price=Decimal("14.30"), occurred_at=NOW, raw_status="fill",
    )
    first = exec_repo.save_event(conn, event, now=NOW)
    second = exec_repo.save_event(conn, event, now=NOW)
    assert first is True
    assert second is False
    count = conn.execute("SELECT COUNT(*) AS n FROM paper_execution_events").fetchone()["n"]
    assert count == 1


def test_event_ids_are_unique_across_intents(conn, intent):
    exec_repo.save_intent(conn, intent, now=NOW)
    event = PaperExecutionEvent(
        event_id="evt-shared", intent_id=intent.intent_id, recommendation_id=intent.recommendation_id,
        symbol=intent.symbol, event_type="SUBMITTED", broker_order_id=None, quantity=intent.quantity,
        filled_quantity=0, fill_price=None, occurred_at=NOW, raw_status="submitted",
    )
    exec_repo.save_event(conn, event, now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO paper_execution_events (event_id, intent_id, recommendation_id, symbol, event_type, "
            "broker_order_id, quantity, filled_quantity, fill_price, occurred_at, raw_status, source, "
            "ledger_applied, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            ("evt-shared", intent.intent_id, intent.recommendation_id, intent.symbol, "SUBMITTED",
             None, intent.quantity, 0, None, NOW.isoformat(), "submitted", "LUMIBOT_PAPER", NOW.isoformat()),
        )


def test_result_links_to_events_and_is_idempotent_upsert(conn, intent):
    exec_repo.save_intent(conn, intent, now=NOW)
    result = PaperExecutionResult(
        intent_id=intent.intent_id, recommendation_id=intent.recommendation_id, final_status="FILLED",
        requested_quantity=intent.quantity, filled_quantity=intent.quantity,
        average_fill_price=Decimal("14.30"), fees=Decimal("0"), event_ids=("evt-1", "evt-2"),
        completed_at=NOW,
    )
    exec_repo.save_result(conn, result)
    exec_repo.save_result(conn, result)  # idempotent re-derivation must not fail
    fetched = exec_repo.get_result(conn, intent.intent_id)
    assert fetched.event_ids == ("evt-1", "evt-2")
    assert fetched.filled_quantity == intent.quantity


def test_reconciliation_is_persisted(conn, intent):
    exec_repo.save_intent(conn, intent, now=NOW)
    snapshot = BrokerExecutionSnapshot(
        intent_id=intent.intent_id, broker_quantity=intent.quantity,
        broker_notional=intent.expected_notional, broker_status="fill", as_of=NOW,
    )
    recon = reconcile_intent(
        intent, snapshot, ledger_quantity=intent.quantity, ledger_notional=intent.expected_notional, now=NOW,
    )
    exec_repo.save_reconciliation(conn, recon)
    fetched = exec_repo.get_reconciliation(conn, intent.intent_id)
    assert fetched.status == "MATCHED"


def test_failure_is_auditable(conn, intent):
    exec_repo.record_failure(
        conn, recommendation_id=intent.recommendation_id, intent_id=intent.intent_id,
        stage="adapter_submit", reason="simulated broker outage", now=NOW,
    )
    row = conn.execute("SELECT * FROM paper_execution_failures").fetchone()
    assert row["stage"] == "adapter_submit"
    assert row["reason"] == "simulated broker outage"


def test_real_orders_remains_write_blocked_by_execution_layer():
    import re

    import trading_research.storage.execution_repositories as repo_module
    import trading_research.execution.ledger_events as ledger_events_module
    import trading_research.services.execute_paper_recommendation as service_module

    sql_write_pattern = re.compile(r"(INSERT|UPDATE|DELETE)\s+(INTO\s+|FROM\s+)?real_orders", re.IGNORECASE)
    for module in (repo_module, ledger_events_module, service_module):
        source = open(module.__file__).read()
        assert not sql_write_pattern.search(source), f"{module.__name__} contains a write to real_orders"
