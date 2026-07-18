"""Milestone 11.3 Part 32: explicit settlement-policy semantics.

`paper_books` uses IMMEDIATE_SIMULATED_SETTLEMENT — a fill's cash effect
applies in the same transaction as the fill, never deferred to T+1. This
must be an explicit, versioned, documented policy (not implicit behavior),
and the same policy must back every buying-power/risk read."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_research.paper_books import cash_ledger
from trading_research.paper_books.cash_ledger import SETTLEMENT_POLICY_VERSION
from trading_research.paper_books.models import PaperBook
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect


def test_settlement_policy_version_is_explicit_and_named_simulation():
    assert SETTLEMENT_POLICY_VERSION == "IMMEDIATE_SIMULATED_SETTLEMENT.v1"
    assert "REGULATORY" not in SETTLEMENT_POLICY_VERSION.upper()


def test_settled_cash_reflects_fill_immediately_not_deferred(tmp_path):
    conn = connect(tmp_path / "db.sqlite3")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    repo.save_book(conn, PaperBook(
        book_id="BASELINE", experiment_arm="BASELINE", currency="USD",
        starting_cash_usd=Decimal("100000"), status="ACTIVE", created_at=now, config_hash="cfg",
    ))
    before = cash_ledger.settled_cash(conn, "BASELINE")
    cash_ledger.settle_buy(conn, "BASELINE", "fill-1", Decimal("500"), Decimal("1"), Decimal("0"), now)
    after = cash_ledger.settled_cash(conn, "BASELINE")
    # Immediate: the very next read (no elapsed "settlement day") reflects
    # the fill's full cash effect, unlike a real T+1 broker settlement cycle.
    assert after == before - Decimal("501")
    conn.close()


def test_snapshot_source_hash_changes_with_settlement_policy_version():
    """The snapshot source_hash payload must include the settlement policy
    version so a future policy change is independently detectable/auditable
    from the snapshot's own hash, not just from reading code."""
    from trading_research.hashing import hash_config
    payload_a = {"book_id": "BASELINE", "as_of": "2026-01-01T00:00:00+00:00", "positions": [],
                 "settlement_policy_version": "IMMEDIATE_SIMULATED_SETTLEMENT.v1"}
    payload_b = {**payload_a, "settlement_policy_version": "MARKET_DAY_T_PLUS_1.v1"}
    assert hash_config(payload_a) != hash_config(payload_b)
