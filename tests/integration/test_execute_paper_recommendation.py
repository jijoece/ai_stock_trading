"""Offline vertical-slice integration tests for Milestone 3:

    fixture candidate -> analyze_candidate (existing service)
    -> frozen buy_candidate recommendation
    -> paper-execution eligibility
    -> deterministic PaperOrderIntent
    -> simulated LumiBot paper fill (deterministic test adapter)
    -> internal PaperLedger update
    -> reconciliation MATCHED

Also covers: an ANALYSIS_INCOMPLETE recommendation never reaches the
adapter/ledger, and executing the same recommendation twice only ever
submits one paper order.

No network, no LumiBot import, no Robinhood/Reddit/Claude — everything runs
on fixtures against a temporary SQLite database, matching
tests/integration/test_analyze_candidate.py's existing conventions.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.analysis.scorer import load_scoring_config
from trading_research.analysis.screener import load_screening_config
from trading_research.execution.adapter_protocol import BrokerExecutionSnapshot
from trading_research.execution.config import load_execution_config
from trading_research.execution.eligibility import PaperExecutionEligibilityPolicy
from trading_research.execution.models import PaperExecutionEvent, PaperExecutionResult
from trading_research.paper.ledger import PaperLedger
from trading_research.runtime.deterministic_adapter import DeterministicPaperAdapter
from trading_research.services.analyze_candidate import analyze_candidate
from trading_research.services.execute_paper_recommendation import (
    STATUS_EXECUTED,
    STATUS_REJECTED_INELIGIBLE,
    STATUS_RESUMED,
    execute_paper_recommendation,
)
from trading_research.storage.database import connect
from trading_research.universe.tickers import default_universe

from tests.integration.test_analyze_candidate import good_candidate

NOW = datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc)
SUBMIT_NOW = NOW + timedelta(seconds=30)
EXEC_CONFIG = load_execution_config()


def _build_recommendation(conn, **overrides):
    universe = default_universe()
    screening_config = load_screening_config()
    scoring_config = load_scoring_config()
    candidate = good_candidate(**overrides)
    return analyze_candidate(candidate, universe, screening_config, scoring_config, conn, NOW)


def _register_full_fill(adapter, intent, price=Decimal("14.92")):
    event = PaperExecutionEvent(
        event_id=f"{intent.intent_id}-evt-1", intent_id=intent.intent_id,
        recommendation_id=intent.recommendation_id, symbol=intent.symbol, event_type="FILLED",
        broker_order_id="sim-broker-1", quantity=intent.quantity, filled_quantity=intent.quantity,
        fill_price=price, occurred_at=SUBMIT_NOW, raw_status="fill",
    )
    result = PaperExecutionResult(
        intent_id=intent.intent_id, recommendation_id=intent.recommendation_id, final_status="FILLED",
        requested_quantity=intent.quantity, filled_quantity=intent.quantity, average_fill_price=price,
        fees=Decimal("0"), event_ids=(event.event_id,), completed_at=SUBMIT_NOW,
    )
    adapter.register(intent.intent_id, (event,), result)
    adapter.register_reconciliation(
        intent.intent_id,
        BrokerExecutionSnapshot(
            intent_id=intent.intent_id, broker_quantity=intent.quantity,
            broker_notional=price * intent.quantity, broker_status="fill", as_of=SUBMIT_NOW,
        ),
    )


def _preview_intent(recommendation_payload):
    """Build (without persisting) the intent the vertical slice is expected
    to produce, so the test can pre-register a matching scripted fill on the
    deterministic adapter before the orchestration service builds the real
    one (same deterministic construction, so intent_ids match)."""
    from trading_research.execution.intent_builder import build_paper_order_intent

    return build_paper_order_intent(recommendation_payload, config=EXEC_CONFIG, git_sha="abc1234")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "execute_paper_integration.sqlite3")
    yield c
    c.close()


def test_vertical_slice_happy_path_reconciles_matched(conn):
    result = _build_recommendation(conn, idempotency_key="paper-vslice-happy")
    rec = result.recommendation
    assert rec.side == "buy_candidate"
    assert rec.status == "active"

    from trading_research.storage.trading_repositories import load_recommendation

    payload = load_recommendation(conn, rec.rec_id)
    intent_preview = _preview_intent(payload)

    adapter = DeterministicPaperAdapter()
    _register_full_fill(adapter, intent_preview)

    ledger = PaperLedger(conn, starting_cash=100_000.0)
    policy = PaperExecutionEligibilityPolicy(universe=default_universe(), config=EXEC_CONFIG)

    outcome = execute_paper_recommendation(
        rec.rec_id, conn=conn, execution_config=EXEC_CONFIG, ledger=ledger, adapter=adapter,
        eligibility_policy=policy, git_sha="abc1234", clock=lambda: SUBMIT_NOW,
    )

    assert outcome.status == STATUS_EXECUTED
    assert outcome.eligibility.eligible is True
    assert outcome.result.final_status == "FILLED"
    assert outcome.result.filled_quantity == intent_preview.quantity
    assert outcome.reconciliation.status == "MATCHED"

    positions = ledger.positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "SOFI"
    assert positions[0]["qty"] == intent_preview.quantity

    # The source recommendation remains immutable throughout.
    row = conn.execute("SELECT frozen FROM recommendations WHERE rec_id = ?", (rec.rec_id,)).fetchone()
    assert row["frozen"] == 1


def test_analysis_incomplete_recommendation_never_reaches_adapter_or_ledger(conn):
    result = _build_recommendation(conn, symbol="ZZZZZ", idempotency_key="paper-vslice-incomplete")
    rec = result.recommendation
    assert rec.status == "analysis_incomplete"

    adapter = DeterministicPaperAdapter()
    ledger = PaperLedger(conn, starting_cash=100_000.0)
    policy = PaperExecutionEligibilityPolicy(universe=default_universe(), config=EXEC_CONFIG)

    outcome = execute_paper_recommendation(
        rec.rec_id, conn=conn, execution_config=EXEC_CONFIG, ledger=ledger, adapter=adapter,
        eligibility_policy=policy, git_sha="abc1234", clock=lambda: SUBMIT_NOW,
    )

    assert outcome.status == STATUS_REJECTED_INELIGIBLE
    assert outcome.intent is None
    assert adapter.submit_calls == []
    assert ledger.positions() == []
    orders = conn.execute("SELECT COUNT(*) AS n FROM simulated_orders").fetchone()["n"]
    assert orders == 0
    intents = conn.execute("SELECT COUNT(*) AS n FROM paper_execution_intents").fetchone()["n"]
    assert intents == 0


def test_same_recommendation_executed_twice_submits_once(conn):
    result = _build_recommendation(conn, idempotency_key="paper-vslice-duplicate")
    rec = result.recommendation

    from trading_research.storage.trading_repositories import load_recommendation

    payload = load_recommendation(conn, rec.rec_id)
    intent_preview = _preview_intent(payload)

    adapter = DeterministicPaperAdapter()
    _register_full_fill(adapter, intent_preview)

    ledger = PaperLedger(conn, starting_cash=100_000.0)
    policy = PaperExecutionEligibilityPolicy(universe=default_universe(), config=EXEC_CONFIG)

    first = execute_paper_recommendation(
        rec.rec_id, conn=conn, execution_config=EXEC_CONFIG, ledger=ledger, adapter=adapter,
        eligibility_policy=policy, git_sha="abc1234", clock=lambda: SUBMIT_NOW,
    )
    second = execute_paper_recommendation(
        rec.rec_id, conn=conn, execution_config=EXEC_CONFIG, ledger=ledger, adapter=adapter,
        eligibility_policy=policy, git_sha="abc1234", clock=lambda: SUBMIT_NOW + timedelta(seconds=5),
    )

    assert first.status == STATUS_EXECUTED
    assert second.status == STATUS_RESUMED
    assert adapter.submit_calls == [intent_preview.intent_id]  # only ever submitted once

    orders = conn.execute("SELECT COUNT(*) AS n FROM simulated_orders").fetchone()["n"]
    fills = conn.execute("SELECT COUNT(*) AS n FROM simulated_fills").fetchone()["n"]
    assert orders == 1
    assert fills == 1
    assert ledger.positions()[0]["qty"] == intent_preview.quantity  # not doubled

    intents = conn.execute("SELECT COUNT(*) AS n FROM paper_execution_intents").fetchone()["n"]
    assert intents == 1


def test_unknown_recommendation_raises_not_found(conn):
    from trading_research.services.execute_paper_recommendation import RecommendationNotFoundError

    adapter = DeterministicPaperAdapter()
    ledger = PaperLedger(conn, starting_cash=100_000.0)
    policy = PaperExecutionEligibilityPolicy(universe=default_universe(), config=EXEC_CONFIG)

    with pytest.raises(RecommendationNotFoundError):
        execute_paper_recommendation(
            "rec-does-not-exist", conn=conn, execution_config=EXEC_CONFIG, ledger=ledger, adapter=adapter,
            eligibility_policy=policy, git_sha="abc1234", clock=lambda: SUBMIT_NOW,
        )


def test_kill_switch_blocks_execution_before_intent_creation(conn):
    from dataclasses import replace

    result = _build_recommendation(conn, idempotency_key="paper-vslice-killswitch")
    rec = result.recommendation

    killed_config = replace(EXEC_CONFIG, kill_switch_enabled=True)
    adapter = DeterministicPaperAdapter()
    ledger = PaperLedger(conn, starting_cash=100_000.0)
    policy = PaperExecutionEligibilityPolicy(universe=default_universe(), config=killed_config)

    outcome = execute_paper_recommendation(
        rec.rec_id, conn=conn, execution_config=killed_config, ledger=ledger, adapter=adapter,
        eligibility_policy=policy, git_sha="abc1234", clock=lambda: SUBMIT_NOW,
    )
    assert outcome.status == STATUS_REJECTED_INELIGIBLE
    assert any("kill switch" in r for r in outcome.eligibility.reasons)
    assert adapter.submit_calls == []


def test_rejected_broker_outcome_creates_no_position(conn):
    result = _build_recommendation(conn, idempotency_key="paper-vslice-rejected")
    rec = result.recommendation

    from trading_research.storage.trading_repositories import load_recommendation

    payload = load_recommendation(conn, rec.rec_id)
    intent_preview = _preview_intent(payload)

    adapter = DeterministicPaperAdapter()
    reject_event = PaperExecutionEvent(
        event_id=f"{intent_preview.intent_id}-evt-1", intent_id=intent_preview.intent_id,
        recommendation_id=intent_preview.recommendation_id, symbol=intent_preview.symbol,
        event_type="REJECTED", broker_order_id="sim-broker-2", quantity=intent_preview.quantity,
        filled_quantity=0, fill_price=None, occurred_at=SUBMIT_NOW, raw_status="rejected",
    )
    reject_result = PaperExecutionResult(
        intent_id=intent_preview.intent_id, recommendation_id=intent_preview.recommendation_id,
        final_status="REJECTED", requested_quantity=intent_preview.quantity, filled_quantity=0,
        average_fill_price=None, fees=Decimal("0"), event_ids=(reject_event.event_id,),
        completed_at=SUBMIT_NOW,
    )
    adapter.register(intent_preview.intent_id, (reject_event,), reject_result)
    adapter.register_reconciliation(
        intent_preview.intent_id,
        BrokerExecutionSnapshot(
            intent_id=intent_preview.intent_id, broker_quantity=0, broker_notional=Decimal("0"),
            broker_status="rejected", as_of=SUBMIT_NOW,
        ),
    )

    ledger = PaperLedger(conn, starting_cash=100_000.0)
    policy = PaperExecutionEligibilityPolicy(universe=default_universe(), config=EXEC_CONFIG)

    outcome = execute_paper_recommendation(
        rec.rec_id, conn=conn, execution_config=EXEC_CONFIG, ledger=ledger, adapter=adapter,
        eligibility_policy=policy, git_sha="abc1234", clock=lambda: SUBMIT_NOW,
    )
    assert outcome.status == STATUS_EXECUTED
    assert outcome.result.final_status == "REJECTED"
    assert ledger.positions() == []
