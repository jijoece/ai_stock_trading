from datetime import datetime, timezone
from decimal import Decimal

from trading_research.execution.account_reconciliation import (
    reconcile_account,
    reconcile_all_positions,
    reconcile_position,
)
from trading_research.execution.broker_snapshots import BrokerAccountSnapshot, BrokerPositionSnapshot

NOW = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc)


def _account(cash: str) -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(cash=Decimal(cash), equity=Decimal(cash), currency="USD", as_of=NOW)


def test_exact_cash_match():
    result = reconcile_account(_account("1000.00"), ledger_cash=Decimal("1000.00"), tolerance=Decimal("0.01"), now=NOW)
    assert result.status == "MATCHED"
    assert result.reasons == ()


def test_cash_mismatch_beyond_tolerance():
    result = reconcile_account(_account("1000.00"), ledger_cash=Decimal("990.00"), tolerance=Decimal("0.01"), now=NOW)
    assert result.status == "CASH_MISMATCH"
    assert result.reasons


def test_cash_within_configured_rounding_tolerance():
    result = reconcile_account(_account("1000.00"), ledger_cash=Decimal("999.995"), tolerance=Decimal("0.01"), now=NOW)
    assert result.status == "MATCHED"


def test_position_exact_match():
    broker = BrokerPositionSnapshot(symbol="AAPL", quantity=Decimal("10"), average_entry_price=Decimal("150"), market_value=Decimal("1500"), as_of=NOW)
    result = reconcile_position("AAPL", broker, ledger_quantity=Decimal("10"), tolerance=Decimal("0"), broker_as_of=NOW, now=NOW)
    assert result.status == "MATCHED"


def test_position_quantity_mismatch():
    broker = BrokerPositionSnapshot(symbol="AAPL", quantity=Decimal("10"), average_entry_price=Decimal("150"), market_value=Decimal("1500"), as_of=NOW)
    result = reconcile_position("AAPL", broker, ledger_quantity=Decimal("8"), tolerance=Decimal("0"), broker_as_of=NOW, now=NOW)
    assert result.status == "POSITION_MISMATCH"
    assert result.reasons


def test_missing_broker_position():
    result = reconcile_position("AAPL", None, ledger_quantity=Decimal("10"), tolerance=Decimal("0"), broker_as_of=NOW, now=NOW)
    assert result.status == "MISSING_BROKER_POSITION"


def test_missing_internal_position():
    broker = BrokerPositionSnapshot(symbol="AAPL", quantity=Decimal("10"), average_entry_price=Decimal("150"), market_value=Decimal("1500"), as_of=NOW)
    result = reconcile_position("AAPL", broker, ledger_quantity=Decimal("0"), tolerance=Decimal("0"), broker_as_of=NOW, now=NOW)
    assert result.status == "MISSING_INTERNAL_POSITION"


def test_zero_zero_is_matched_not_missing():
    result = reconcile_position("AAPL", None, ledger_quantity=Decimal("0"), tolerance=Decimal("0"), broker_as_of=NOW, now=NOW)
    assert result.status == "MATCHED"


def test_reconcile_all_positions_covers_union_of_symbols():
    broker_positions = [
        BrokerPositionSnapshot(symbol="AAPL", quantity=Decimal("10"), average_entry_price=Decimal("150"), market_value=Decimal("1500"), as_of=NOW),
        BrokerPositionSnapshot(symbol="MSFT", quantity=Decimal("5"), average_entry_price=Decimal("300"), market_value=Decimal("1500"), as_of=NOW),
    ]
    ledger_positions = {"AAPL": Decimal("10"), "SOFI": Decimal("70")}

    results = reconcile_all_positions(broker_positions, ledger_positions, tolerance=Decimal("0"), broker_as_of=NOW, now=NOW)
    by_symbol = {r.symbol: r for r in results}

    assert by_symbol["AAPL"].status == "MATCHED"
    assert by_symbol["MSFT"].status == "MISSING_INTERNAL_POSITION"
    assert by_symbol["SOFI"].status == "MISSING_BROKER_POSITION"
    assert set(by_symbol) == {"AAPL", "MSFT", "SOFI"}
