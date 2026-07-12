"""Pure reconciliation logic: compares an adapter's broker-side view of an
intent against the internal paper ledger's view (Milestone 3, Step 5/10).

Framework-neutral and side-effect-free — never touches LumiBot, the ledger,
or a database. `services/execute_paper_recommendation.py` is the only
caller; it supplies both sides.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .adapter_protocol import BrokerExecutionSnapshot
from .models import PaperOrderIntent, ReconciliationResult


def reconcile_intent(
    intent: PaperOrderIntent,
    broker: BrokerExecutionSnapshot,
    *,
    ledger_quantity: int,
    ledger_notional: Decimal,
    now: datetime,
) -> ReconciliationResult:
    reasons: list[str] = []

    if broker.broker_quantity == ledger_quantity and broker.broker_notional == ledger_notional:
        status = "MATCHED"
    elif broker.broker_quantity == 0 and ledger_quantity == 0:
        status = "PENDING"
    elif ledger_quantity == 0 and broker.broker_quantity > 0:
        status = "MISSING_INTERNAL_EVENT"
        reasons.append(
            f"broker reports {broker.broker_quantity} filled shares but the ledger has applied none"
        )
    elif broker.broker_quantity == 0 and ledger_quantity > 0:
        status = "MISSING_BROKER_EVENT"
        reasons.append(
            f"ledger has applied {ledger_quantity} filled shares but the broker snapshot reports none"
        )
    else:
        status = "MISMATCH"
        if broker.broker_quantity != ledger_quantity:
            reasons.append(f"broker_quantity={broker.broker_quantity} != ledger_quantity={ledger_quantity}")
        if broker.broker_notional != ledger_notional:
            reasons.append(f"broker_notional={broker.broker_notional} != ledger_notional={ledger_notional}")

    return ReconciliationResult(
        intent_id=intent.intent_id,
        status=status,
        broker_quantity=broker.broker_quantity,
        ledger_quantity=ledger_quantity,
        broker_notional=broker.broker_notional,
        ledger_notional=ledger_notional,
        reasons=tuple(reasons),
        reconciled_at=now,
    )
