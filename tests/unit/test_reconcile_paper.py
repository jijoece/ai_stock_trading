from datetime import datetime, timezone

import pytest

from trading_research.paper.ledger import PaperLedger
from trading_research.runtime.client.process_client import RuntimeClient
from trading_research.services.reconcile_paper import reconcile_paper_account_and_positions
from trading_research.storage import execution_repositories as exec_repo
from trading_research.storage.database import connect

from tests.support.runtime_client_fixtures import FakeTransport, fake_transport_factory, start_ready_client

NOW = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "reconcile.sqlite3")
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


def test_matched_account_and_position(conn, ledger):
    ledger.apply_external_fill(symbol="AAPL", side="buy", qty=10, price=150.0, idempotency_key="fill-1", now=NOW)
    client, fake = _client_with_fake()

    fake.queue_success({"cash": "98500", "equity": "98500", "buying_power": "98500", "currency": "USD", "as_of": NOW.isoformat()})
    fake.queue_success(
        {"positions": [
            {"symbol": "AAPL", "quantity": "10", "average_entry_price": "150", "market_value": "1500", "as_of": NOW.isoformat()},
        ]}
    )

    report = reconcile_paper_account_and_positions(conn=conn, ledger=ledger, client=client, clock=lambda: NOW)

    assert report.account.status == "MATCHED"
    assert len(report.positions) == 1
    assert report.positions[0].status == "MATCHED"

    persisted = exec_repo.get_latest_account_reconciliation(conn)
    assert persisted.status == "MATCHED"


def test_cash_mismatch_is_persisted_not_repaired(conn, ledger):
    client, fake = _client_with_fake()
    fake.queue_success({"cash": "50000", "equity": "50000", "buying_power": "50000", "currency": "USD", "as_of": NOW.isoformat()})
    fake.queue_success({"positions": []})

    report = reconcile_paper_account_and_positions(conn=conn, ledger=ledger, client=client, clock=lambda: NOW)

    assert report.account.status == "CASH_MISMATCH"
    assert ledger.total_cash() == 100_000.0  # never silently repaired

    persisted = exec_repo.get_latest_account_reconciliation(conn)
    assert persisted.status == "CASH_MISMATCH"
    assert persisted.reasons


def test_position_mismatch_persists_per_symbol_results(conn, ledger):
    ledger.apply_external_fill(symbol="AAPL", side="buy", qty=10, price=150.0, idempotency_key="fill-2", now=NOW)
    client, fake = _client_with_fake()

    fake.queue_success({"cash": "98500", "equity": "98500", "buying_power": "98500", "currency": "USD", "as_of": NOW.isoformat()})
    fake.queue_success(
        {"positions": [
            {"symbol": "AAPL", "quantity": "6", "average_entry_price": "150", "market_value": "900", "as_of": NOW.isoformat()},
        ]}
    )

    report = reconcile_paper_account_and_positions(conn=conn, ledger=ledger, client=client, clock=lambda: NOW)
    assert report.positions[0].status == "POSITION_MISMATCH"

    persisted = exec_repo.list_latest_position_reconciliations(conn)
    assert len(persisted) == 1
    assert persisted[0].symbol == "AAPL"
    assert persisted[0].status == "POSITION_MISMATCH"
