import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from trading_research.paper.ledger import (
    DuplicateOrderError,
    FillModel,
    LedgerError,
    PaperLedger,
)

NOW = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def ledger():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return PaperLedger(conn, starting_cash=10_000.0, fill_model=FillModel(slippage_bps=10))


def test_buy_fill_includes_spread_and_slippage(ledger):
    result = ledger.submit_and_fill("SOFI", "buy", 100, bid=14.90, ask=14.94,
                                    idempotency_key="k1", now=NOW)
    mid = 14.92
    expected = mid + 0.02 + mid * 0.001  # mid + half-spread + 10bps
    assert result["fill_price"] == pytest.approx(expected, abs=1e-4)
    assert ledger.positions()[0]["qty"] == 100


def test_buy_reduces_settled_cash(ledger):
    ledger.submit_and_fill("SOFI", "buy", 100, 14.90, 14.94, "k1", now=NOW)
    assert ledger.settled_cash(NOW) < 10_000.0 - 100 * 14.92


def test_insufficient_settled_cash_rejected(ledger):
    with pytest.raises(LedgerError, match="insufficient settled cash"):
        ledger.submit_and_fill("AAPL", "buy", 1000, 232.10, 232.16, "k1", now=NOW)


def test_duplicate_idempotency_key_rejected(ledger):
    ledger.submit_and_fill("SOFI", "buy", 10, 14.90, 14.94, "same-key", now=NOW)
    with pytest.raises(DuplicateOrderError):
        ledger.submit_and_fill("SOFI", "buy", 10, 14.90, 14.94, "same-key", now=NOW)


def test_sell_proceeds_settle_t_plus_1(ledger):
    ledger.submit_and_fill("SOFI", "buy", 100, 14.90, 14.94, "k1", now=NOW)
    cash_after_buy = ledger.settled_cash(NOW)
    ledger.submit_and_fill("SOFI", "sell", 100, 14.90, 14.94, "k2", now=NOW)
    # Same day: proceeds pending, settled cash unchanged.
    assert ledger.settled_cash(NOW) == pytest.approx(cash_after_buy)
    assert ledger.total_cash() > cash_after_buy
    # Next day: settled.
    tomorrow = NOW + timedelta(days=1)
    assert ledger.settled_cash(tomorrow) == pytest.approx(ledger.total_cash())


def test_cannot_sell_more_than_held(ledger):
    with pytest.raises(LedgerError, match="cannot sell"):
        ledger.submit_and_fill("SOFI", "sell", 5, 14.90, 14.94, "k1", now=NOW)


def test_sell_worse_than_mid(ledger):
    ledger.submit_and_fill("SOFI", "buy", 10, 14.90, 14.94, "k1", now=NOW)
    result = ledger.submit_and_fill("SOFI", "sell", 10, 14.90, 14.94, "k2", now=NOW)
    assert result["fill_price"] < 14.92


def test_position_averaging(ledger):
    ledger.submit_and_fill("SOFI", "buy", 10, 10.00, 10.02, "k1", now=NOW)
    ledger.submit_and_fill("SOFI", "buy", 10, 12.00, 12.02, "k2", now=NOW)
    (pos,) = ledger.positions()
    assert pos["qty"] == 20
    assert 10.0 < pos["avg_cost"] < 12.1


def test_invalid_quote_rejected(ledger):
    with pytest.raises(LedgerError, match="invalid quote"):
        ledger.submit_and_fill("SOFI", "buy", 10, bid=14.94, ask=14.90, idempotency_key="k1", now=NOW)


def test_snapshot_fails_closed_without_marks(ledger):
    ledger.submit_and_fill("SOFI", "buy", 10, 14.90, 14.94, "k1", now=NOW)
    with pytest.raises(LedgerError, match="fail closed"):
        ledger.snapshot(marks={}, now=NOW)


def test_snapshot_equity(ledger):
    ledger.submit_and_fill("SOFI", "buy", 100, 14.90, 14.94, "k1", now=NOW)
    snap = ledger.snapshot(marks={"SOFI": 15.50}, now=NOW)
    assert snap["equity"] == pytest.approx(
        ledger.total_cash() + 100 * 15.50, abs=0.01
    )
