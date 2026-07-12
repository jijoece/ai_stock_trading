from datetime import datetime, timedelta, timezone

import pytest

from trading_research.execution.config import load_execution_config
from trading_research.execution.eligibility import PaperExecutionEligibilityPolicy
from trading_research.execution.models import derive_intent_id
from trading_research.storage.database import connect
from trading_research.storage import execution_repositories as exec_repo
from trading_research.universe.tickers import default_universe

from tests.support.execution_fixtures import buy_candidate_payload, insert_recommendation_row

NOW = datetime(2026, 7, 11, 14, 0, 30, tzinfo=timezone.utc)
CONFIG = load_execution_config()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "elig.sqlite3")
    yield c
    c.close()


@pytest.fixture
def policy():
    return PaperExecutionEligibilityPolicy(universe=default_universe(), config=CONFIG)


def test_frozen_valid_buy_candidate_is_eligible(policy, conn):
    payload = buy_candidate_payload(now=NOW - timedelta(seconds=30))
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is True
    assert result.reasons == ()


def test_unfrozen_recommendation_rejected(policy, conn):
    payload = buy_candidate_payload(frozen=False)
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("not frozen" in r for r in result.reasons)


@pytest.mark.parametrize("side", ["screened_out", "watch", "no_action", "analysis_incomplete"])
def test_ineligible_sides_rejected(policy, conn, side):
    payload = buy_candidate_payload(side=side, status="active" if side != "analysis_incomplete" else "analysis_incomplete")
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("not eligible" in r for r in result.reasons)


def test_missing_risk_plan_rejected(policy, conn):
    payload = buy_candidate_payload()
    payload["risk_plan"] = None
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("risk_plan" in r for r in result.reasons)


def test_expired_recommendation_rejected(policy, conn):
    old = NOW - timedelta(minutes=CONFIG.recommendation_ttl_minutes + 5)
    payload = buy_candidate_payload(now=old, market_data_age_seconds=1)
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("expired" in r for r in result.reasons)


def test_stale_price_rejected(policy, conn):
    payload = buy_candidate_payload(market_data_age_seconds=CONFIG.max_price_staleness_seconds + 60)
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("stale" in r for r in result.reasons)


def test_missing_market_timestamp_fails_closed(policy, conn):
    payload = buy_candidate_payload()
    payload["data_timestamps"] = {}
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("data_timestamps.market" in r for r in result.reasons)


def test_unknown_symbol_rejected(policy, conn):
    payload = buy_candidate_payload(symbol="ZZZZZ")
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("TickerUniverse" in r for r in result.reasons)


def test_otc_symbol_rejected(policy, conn):
    payload = buy_candidate_payload(symbol="SHELCO")
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False


def test_global_kill_switch_rejects(conn):
    from dataclasses import replace

    kill_switch_config = replace(CONFIG, kill_switch_enabled=True)
    policy = PaperExecutionEligibilityPolicy(universe=default_universe(), config=kill_switch_config)
    payload = buy_candidate_payload()
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("kill switch" in r for r in result.reasons)


def test_duplicate_execution_rejected(policy, conn):
    payload = buy_candidate_payload()
    insert_recommendation_row(conn, payload)
    intent_id = derive_intent_id(payload["rec_id"], CONFIG.execution_version)
    # Simulate an already-persisted intent for this recommendation.
    from trading_research.execution.intent_builder import build_paper_order_intent

    intent = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    assert intent.intent_id == intent_id
    exec_repo.save_intent(conn, intent, now=NOW)

    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("duplicate execution" in r for r in result.reasons)


def test_missing_price_rejected(policy, conn):
    payload = buy_candidate_payload()
    payload["price_at_rec"] = None
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("price_at_rec" in r for r in result.reasons)


def test_incomplete_provenance_rejected(policy, conn):
    payload = buy_candidate_payload()
    payload["config_hash"] = ""
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("provenance" in r for r in result.reasons)


def test_recommendation_not_found_rejected(policy, conn):
    result = policy.evaluate(None, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("not found" in r for r in result.reasons)


def test_zero_share_risk_plan_rejected(policy, conn):
    payload = buy_candidate_payload()
    payload["risk_plan"]["shares"] = 0
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("positive quantity" in r for r in result.reasons)


def test_portfolio_guardrail_injection_can_reject(conn):
    def always_reject(recommendation: dict) -> list[str]:
        return ["sector concentration breach (injected test guardrail)"]

    policy = PaperExecutionEligibilityPolicy(
        universe=default_universe(), config=CONFIG, portfolio_guardrail=always_reject,
    )
    payload = buy_candidate_payload()
    result = policy.evaluate(payload, conn=conn, now=NOW)
    assert result.eligible is False
    assert any("sector concentration" in r for r in result.reasons)


def test_evaluation_is_deterministic(policy, conn):
    payload = buy_candidate_payload()
    first = policy.evaluate(payload, conn=conn, now=NOW)
    second = policy.evaluate(payload, conn=conn, now=NOW)
    assert first.eligible == second.eligible
    assert first.reasons == second.reasons
