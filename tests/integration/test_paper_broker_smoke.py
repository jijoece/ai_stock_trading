"""Genuine credentialed paper-broker validation (docs/milestone-4.md Step
17). Opt-in only — never runs as part of the default suite, and never runs
automatically just because Alpaca credentials happen to be present in the
environment. Requires both:

    RUN_PAPER_BROKER_TESTS=true
    ALPACA_API_KEY / ALPACA_API_SECRET / ALPACA_IS_PAPER=true

Sequence (matches docs/milestone-4.md Step 17 exactly):

1. Health check.
2. Verify paper endpoint.
3. Verify real-money capability is false.
4. Retrieve paper account snapshot.
5. Submit one small, non-marketable limit order for a highly liquid
   allowed equity (AAPL, $1.00 limit — far below any plausible market
   price, so it cannot fill).
6. Confirm broker acknowledgement and broker order id.
7. Retrieve order state.
8. Cancel the paper order.
9. Confirm cancellation.
10. Reconcile no fill and no position change.
11. Persist the test outcome (via the real `reconcile_paper_account_and_
    positions` service, against a temporary database) without secrets.

At implementation time, this repository's `.env` has no ALPACA_API_KEY /
ALPACA_API_SECRET / ALPACA_IS_PAPER — this test was written and is
committed, but has NOT been executed against a real Alpaca paper account.
Running it requires an operator to supply real paper-trading credentials
and explicitly opt in.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

RUN_PAPER_BROKER_TESTS = os.environ.get("RUN_PAPER_BROKER_TESTS", "").strip().lower() == "true"

pytestmark = pytest.mark.paper_broker

_SKIP_REASON = (
    "opt-in credentialed paper-broker smoke test — set RUN_PAPER_BROKER_TESTS=true "
    "and real ALPACA_API_KEY/ALPACA_API_SECRET/ALPACA_IS_PAPER=true to run"
)


@pytest.mark.skipif(not RUN_PAPER_BROKER_TESTS, reason=_SKIP_REASON)
def test_credentialed_alpaca_paper_round_trip(tmp_path):
    from trading_research.paper.ledger import PaperLedger
    from trading_research.runtime.client.models import intent_to_submit_payload
    from trading_research.runtime.client.process_client import RuntimeClient
    from trading_research.cli import _paper_runtime_command_env
    from trading_research.config import REPO_ROOT
    from trading_research.runtime.paper_runtime_config import load_paper_runtime_config
    from trading_research.services.reconcile_paper import reconcile_paper_account_and_positions
    from trading_research.storage.database import connect

    runtime_config = load_paper_runtime_config()
    client = RuntimeClient(
        command=list(runtime_config.command), startup_timeout_seconds=runtime_config.startup_timeout_seconds,
        request_timeout_seconds=runtime_config.request_timeout_seconds, cwd=str(REPO_ROOT),
        env=_paper_runtime_command_env(),
    )

    # 1-3: health, paper-endpoint, real-money-false — RuntimeClient.start()
    # itself enforces all three and raises RuntimeCapabilityError if any
    # check fails, so a passing start() is the proof.
    client.start()
    assert client.last_health["paper_endpoint_verified"] is True
    assert client.last_capabilities["real_money"] is False

    try:
        # 4: paper account snapshot.
        account = client.get_account()
        assert Decimal(account["cash"]) >= 0

        # 5: one small, non-marketable limit order on a highly liquid symbol.
        now = datetime.now(timezone.utc)
        client_order_id = f"smoke-test-{int(now.timestamp())}"
        payload = {
            "intent_id": client_order_id, "recommendation_id": "smoke-test", "symbol": "AAPL",
            "side": "BUY", "quantity": 1, "order_type": "LIMIT", "limit_price": "1.00",
            "reference_price": "1.00", "expires_at": (now + timedelta(hours=1)).isoformat(),
            "idempotency_key": client_order_id,
        }
        submitted = client.submit_order(payload)

        # 6: broker acknowledgement + broker order id.
        assert submitted["broker_order_id"]
        assert submitted["status"] in ("ACCEPTED", "SUBMITTED")

        # 7: retrieve order state.
        fetched = client.get_order(client_order_id)
        assert fetched is not None
        assert fetched["filled_quantity"] == 0  # non-marketable — must not fill

        # 8-9: cancel and confirm.
        cancelled = client.cancel_paper_order(client_order_id)
        assert cancelled["status"] == "CANCELLED"

        # 10-11: reconcile no fill / no position change; persist the outcome.
        conn = connect(tmp_path / "paper_broker_smoke.sqlite3")
        try:
            ledger = PaperLedger(conn)
            report = reconcile_paper_account_and_positions(
                conn=conn, ledger=ledger, client=client, clock=lambda: datetime.now(timezone.utc),
            )
            aapl_position = next((p for p in report.positions if p.symbol == "AAPL"), None)
            assert aapl_position is None or aapl_position.ledger_quantity == 0
        finally:
            conn.close()
    finally:
        client.shutdown()
