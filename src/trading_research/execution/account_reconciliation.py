"""Account and position level reconciliation (docs/milestone-4.md Step 10).

Pure, framework-neutral comparison functions extending Milestone 3's
per-intent `reconciliation.py::reconcile_intent` (untouched) to the account
and position level. Never repairs a mismatch — only reports it, with the
compared values, the difference, and the configured tolerance, so a human
or a future automated process decides what to do.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .broker_snapshots import (
    AccountReconciliationResult,
    BrokerAccountSnapshot,
    BrokerPositionSnapshot,
    PositionReconciliationResult,
)


def reconcile_account(
    broker: BrokerAccountSnapshot, *, ledger_cash: Decimal, tolerance: Decimal, now: datetime,
) -> AccountReconciliationResult:
    difference = abs(broker.cash - ledger_cash)
    if difference <= tolerance:
        status, reasons = "MATCHED", ()
    else:
        status = "CASH_MISMATCH"
        reasons = (
            f"broker_cash={broker.cash} ledger_cash={ledger_cash} "
            f"difference={difference} exceeds tolerance={tolerance}",
        )
    return AccountReconciliationResult(
        status=status, broker_cash=broker.cash, ledger_cash=ledger_cash, difference=difference,
        tolerance=tolerance, reasons=reasons, broker_as_of=broker.as_of, reconciled_at=now,
    )


def reconcile_position(
    symbol: str, broker: BrokerPositionSnapshot | None, *, ledger_quantity: Decimal, tolerance: Decimal,
    broker_as_of: datetime, now: datetime,
) -> PositionReconciliationResult:
    broker_quantity = broker.quantity if broker is not None else Decimal("0")

    if broker is None and ledger_quantity != 0:
        status = "MISSING_BROKER_POSITION"
        reasons = (f"ledger holds {ledger_quantity} {symbol} but the broker reports no position",)
    elif broker is not None and ledger_quantity == 0 and broker.quantity != 0:
        status = "MISSING_INTERNAL_POSITION"
        reasons = (f"broker holds {broker.quantity} {symbol} but the ledger has no position",)
    else:
        difference = abs(broker_quantity - ledger_quantity)
        if difference <= tolerance:
            status, reasons = "MATCHED", ()
        else:
            status = "POSITION_MISMATCH"
            reasons = (
                f"broker_quantity={broker_quantity} ledger_quantity={ledger_quantity} "
                f"difference={difference} exceeds tolerance={tolerance}",
            )

    return PositionReconciliationResult(
        symbol=symbol, status=status, broker_quantity=broker_quantity, ledger_quantity=ledger_quantity,
        tolerance=tolerance, reasons=reasons, broker_as_of=broker_as_of, reconciled_at=now,
    )


def reconcile_all_positions(
    broker_positions: list[BrokerPositionSnapshot], ledger_positions: dict[str, Decimal], *,
    tolerance: Decimal, broker_as_of: datetime, now: datetime,
) -> list[PositionReconciliationResult]:
    """Covers the union of symbols known to either side — a symbol the
    ledger holds that the broker never mentions is exactly as reportable as
    the reverse (docs/milestone-4.md Step 10: "open broker orders versus
    unresolved internal intents" / "broker position quantity versus
    paper-ledger position")."""
    broker_by_symbol = {p.symbol: p for p in broker_positions}
    symbols = sorted(set(broker_by_symbol) | set(ledger_positions))
    results = []
    for symbol in symbols:
        results.append(
            reconcile_position(
                symbol, broker_by_symbol.get(symbol), ledger_quantity=ledger_positions.get(symbol, Decimal("0")),
                tolerance=tolerance, broker_as_of=broker_as_of, now=now,
            )
        )
    return results
