"""Account and position reconciliation orchestrator (docs/milestone-4.md
Step 10). Pulls a broker snapshot through the runtime client, pulls the
current `PaperLedger` state, compares them with
`execution/account_reconciliation.py`'s pure functions, and persists every
result — never silently repairs a mismatch.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from ..execution.account_reconciliation import reconcile_account, reconcile_all_positions
from ..execution.broker_snapshots import AccountReconciliationResult, BrokerAccountSnapshot, BrokerPositionSnapshot, PositionReconciliationResult
from ..paper.ledger import PaperLedger
from ..runtime.client.process_client import RuntimeClient
from ..storage import execution_repositories as exec_repo

DEFAULT_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class PaperReconciliationReport:
    account: AccountReconciliationResult
    positions: tuple[PositionReconciliationResult, ...]


def reconcile_paper_account_and_positions(
    *, conn: sqlite3.Connection, ledger: PaperLedger, client: RuntimeClient, clock: Callable[[], datetime],
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> PaperReconciliationReport:
    now = clock()

    account_payload = client.get_account()
    broker_account = BrokerAccountSnapshot(
        cash=Decimal(account_payload["cash"]), equity=Decimal(account_payload["equity"]),
        currency=account_payload["currency"], as_of=datetime.fromisoformat(account_payload["as_of"]),
    )
    ledger_cash = Decimal(str(ledger.total_cash()))
    account_result = reconcile_account(broker_account, ledger_cash=ledger_cash, tolerance=tolerance, now=now)
    exec_repo.save_account_reconciliation(conn, account_result)

    position_payloads = client.list_positions()
    broker_positions = [
        BrokerPositionSnapshot(
            symbol=p["symbol"], quantity=Decimal(p["quantity"]),
            average_entry_price=Decimal(p["average_entry_price"]),
            market_value=Decimal(p["market_value"]) if p.get("market_value") is not None else None,
            as_of=datetime.fromisoformat(p["as_of"]),
        )
        for p in position_payloads
    ]
    ledger_positions = {p["symbol"]: Decimal(str(p["qty"])) for p in ledger.positions()}
    position_results = reconcile_all_positions(
        broker_positions, ledger_positions, tolerance=tolerance, broker_as_of=broker_account.as_of, now=now,
    )
    for result in position_results:
        exec_repo.save_position_reconciliation(conn, result)

    return PaperReconciliationReport(account=account_result, positions=tuple(position_results))
