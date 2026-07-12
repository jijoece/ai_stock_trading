from decimal import Decimal

import pytest

from trading_research.execution.config import load_execution_config
from trading_research.execution.intent_builder import IntentBuildError, build_paper_order_intent
from trading_research.execution.models import derive_intent_id

from tests.support.execution_fixtures import buy_candidate_payload

CONFIG = load_execution_config()


def test_deterministic_intent_creation():
    payload = buy_candidate_payload()
    intent = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    assert intent.intent_id == derive_intent_id(payload["rec_id"], CONFIG.execution_version)
    assert intent.recommendation_id == payload["rec_id"]
    assert intent.symbol == "SOFI"
    assert intent.order_type == "MARKET"
    assert intent.limit_price is None


def test_same_inputs_produce_identical_intent():
    payload = buy_candidate_payload()
    a = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    b = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    assert a == b


def test_whole_share_quantity_no_round_up():
    payload = buy_candidate_payload(shares=70)
    intent = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    assert intent.quantity == 70
    assert isinstance(intent.quantity, int)


def test_zero_shares_rejected():
    payload = buy_candidate_payload(shares=70)
    payload["risk_plan"]["shares"] = 0
    with pytest.raises(IntentBuildError, match="positive int"):
        build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")


def test_negative_shares_rejected():
    payload = buy_candidate_payload()
    payload["risk_plan"]["shares"] = -5
    with pytest.raises(IntentBuildError, match="positive int"):
        build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")


def test_missing_risk_plan_fails_closed():
    payload = buy_candidate_payload()
    payload["risk_plan"] = None
    with pytest.raises(IntentBuildError, match="risk_plan"):
        build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")


def test_missing_price_fails_closed():
    payload = buy_candidate_payload()
    payload["price_at_rec"] = None
    with pytest.raises(IntentBuildError, match="price_at_rec"):
        build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")


def test_exact_expected_notional_reconstruction():
    payload = buy_candidate_payload(shares=70, entry_price=14.25)
    intent = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    assert intent.expected_notional == Decimal("14.25") * 70
    assert intent.reference_price == Decimal("14.25")


def test_stable_idempotency_key_across_calls():
    payload = buy_candidate_payload()
    first = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    second = build_paper_order_intent(payload, config=CONFIG, git_sha="differentsha")
    # intent_id is derived from (recommendation_id, execution_version) only —
    # not from git_sha — so a rebuild with a different git_sha still yields
    # the same idempotency key.
    assert first.intent_id == second.intent_id


def test_provenance_fields_preserved_from_recommendation():
    payload = buy_candidate_payload()
    intent = build_paper_order_intent(payload, config=CONFIG, git_sha="deadbee")
    assert intent.config_hash == payload["config_hash"]
    assert intent.git_sha == "deadbee"
    assert intent.policy_version == CONFIG.policy_version
    assert intent.execution_version == CONFIG.execution_version


def test_expires_at_derived_from_ttl():
    from datetime import timedelta

    payload = buy_candidate_payload()
    intent = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    assert intent.expires_at == intent.recommendation_frozen_at + timedelta(
        minutes=CONFIG.recommendation_ttl_minutes
    )
