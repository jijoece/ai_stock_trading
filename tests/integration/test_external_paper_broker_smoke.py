"""Opt-in real Alpaca paper smoke for the Milestone 11 boundary.

This test operates on one already-approved, bounded paper-book LIMIT intent.
It is inert unless the dedicated Milestone 11 flag is true; credentials alone
never select it. The operator must also name the exact book and intent.
"""
from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.external_paper_broker

_ENABLED = os.environ.get("RUN_EXTERNAL_PAPER_BROKER_TESTS", "").strip().lower() == "true"
_SKIP_REASON = (
    "opt-in Milestone 11 Alpaca paper smoke; set RUN_EXTERNAL_PAPER_BROKER_TESTS=true "
    "and provide an explicitly approved bounded paper intent"
)


@pytest.mark.skipif(not _ENABLED, reason=_SKIP_REASON)
def test_explicit_external_paper_preview_submit_reconcile_and_cancel():
    from trading_research.cli import (
        external_paper_account_check_cli,
        external_paper_cancel_cli,
        external_paper_order_show_cli,
        external_paper_preview_cli,
        external_paper_reconcile_cli,
        external_paper_submit_cli,
    )
    from trading_research.config import load_config

    book_id = os.environ.get("EXTERNAL_PAPER_SMOKE_BOOK_ID", "").strip().upper()
    intent_id = os.environ.get("EXTERNAL_PAPER_SMOKE_INTENT_ID", "").strip()
    operator = os.environ.get("EXTERNAL_PAPER_SMOKE_OPERATOR", "").strip()
    assert os.environ.get("ALPACA_IS_PAPER", "").strip().lower() == "true"
    assert book_id in ("BASELINE", "ENHANCED")
    assert intent_id, "EXTERNAL_PAPER_SMOKE_INTENT_ID must name an approved frozen intent"
    assert operator, "EXTERNAL_PAPER_SMOKE_OPERATOR must identify the authorizing operator"

    db_path = load_config().research_database_path
    account = external_paper_account_check_cli(db_path, book_id=book_id)
    assert "error" not in account

    preview = external_paper_preview_cli(
        db_path, book_id=book_id, intent_id=intent_id, operator=operator,
    )
    assert "error" not in preview
    submitted = external_paper_submit_cli(
        db_path, book_id=book_id, intent_id=intent_id, preview_id=preview["preview_id"],
        operator=operator, reason="explicit Milestone 11 real-paper smoke",
    )
    assert "error" not in submitted

    client_order_id = preview["client_order_id"]
    reconciled = external_paper_reconcile_cli(
        db_path, book_id=book_id, client_order_id=client_order_id,
    )
    assert "error" not in reconciled

    evidence = external_paper_order_show_cli(
        db_path, book_id=book_id, client_order_id=client_order_id,
    )
    assert "error" not in evidence
    if evidence["current"]["new_state"] in {"SUBMITTED", "PARTIALLY_FILLED"}:
        cancelled = external_paper_cancel_cli(
            db_path, book_id=book_id, client_order_id=client_order_id, operator=operator,
            reason="explicit cleanup after Milestone 11 real-paper smoke",
        )
        assert "error" not in cancelled
        final_reconciliation = external_paper_reconcile_cli(
            db_path, book_id=book_id, client_order_id=client_order_id,
        )
        assert "error" not in final_reconciliation
