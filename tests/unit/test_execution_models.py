from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.execution.models import (
    IntentValidationError,
    PaperExecutionEligibility,
    PaperExecutionEvent,
    PaperExecutionResult,
    PaperOrderIntent,
    ReconciliationResult,
    derive_intent_id,
)

NOW = datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)


def make_intent(**overrides) -> PaperOrderIntent:
    defaults = dict(
        intent_id=derive_intent_id("rec-1", "v1"),
        recommendation_id="rec-1",
        symbol="SOFI",
        side="BUY",
        quantity=70,
        order_type="MARKET",
        limit_price=None,
        reference_price=Decimal("14.25"),
        expected_notional=Decimal("14.25") * 70,
        recommendation_created_at=NOW,
        recommendation_frozen_at=NOW,
        expires_at=LATER,
        config_hash="a" * 64,
        git_sha="abc1234",
        policy_version="p1",
        execution_version="v1",
    )
    defaults.update(overrides)
    return PaperOrderIntent(**defaults)


def test_valid_market_intent():
    intent = make_intent()
    assert intent.quantity == 70
    assert not intent.is_expired(NOW)
    assert intent.is_expired(LATER)


def test_rec_id_deterministic():
    assert derive_intent_id("rec-1", "v1") == derive_intent_id("rec-1", "v1")
    assert derive_intent_id("rec-1", "v1") != derive_intent_id("rec-1", "v2")
    assert derive_intent_id("rec-1", "v1") != derive_intent_id("rec-2", "v1")


def test_sell_side_rejected():
    with pytest.raises(IntentValidationError, match="long-only"):
        make_intent(side="SELL")


@pytest.mark.parametrize("qty", [0, -5])
def test_non_positive_quantity_rejected(qty):
    with pytest.raises(IntentValidationError, match="positive whole number"):
        make_intent(quantity=qty, expected_notional=Decimal("14.25") * qty)


def test_fractional_quantity_rejected():
    with pytest.raises(IntentValidationError, match="int"):
        make_intent(quantity=70.5)


def test_limit_order_requires_positive_limit_price():
    with pytest.raises(IntentValidationError, match="LIMIT orders require"):
        make_intent(order_type="LIMIT", limit_price=None)
    with pytest.raises(IntentValidationError, match="LIMIT orders require"):
        make_intent(order_type="LIMIT", limit_price=Decimal("0"))


def test_market_order_forbids_limit_price():
    with pytest.raises(IntentValidationError, match="MARKET orders must have"):
        make_intent(order_type="MARKET", limit_price=Decimal("14.00"))


def test_valid_limit_order():
    intent = make_intent(order_type="LIMIT", limit_price=Decimal("14.50"))
    assert intent.limit_price == Decimal("14.50")


def test_missing_reference_price_fails_closed():
    with pytest.raises(IntentValidationError, match="reference_price"):
        make_intent(reference_price=None)


def test_expected_notional_must_reconstruct_exactly():
    with pytest.raises(IntentValidationError, match="expected_notional"):
        make_intent(expected_notional=Decimal("9999.99"))


def test_expected_notional_exact_reconstruction():
    intent = make_intent(quantity=70, reference_price=Decimal("14.25"), expected_notional=Decimal("997.50"))
    assert intent.expected_notional == Decimal("997.50")


def test_expired_recommendation_cannot_produce_intent():
    with pytest.raises(IntentValidationError, match="expires_at must be after"):
        make_intent(expires_at=NOW - timedelta(seconds=1))


def test_naive_datetime_rejected():
    with pytest.raises(IntentValidationError, match="timezone-aware"):
        make_intent(recommendation_frozen_at=datetime(2026, 7, 11, 14, 0, 0))


@pytest.mark.parametrize("field", ["config_hash", "git_sha", "policy_version", "execution_version", "symbol"])
def test_missing_required_provenance_fields_fail_closed(field):
    with pytest.raises(IntentValidationError):
        make_intent(**{field: ""})


def test_provenance_fields_preserved():
    intent = make_intent(config_hash="b" * 64, git_sha="deadbee", policy_version="pv2", execution_version="ev2")
    assert intent.config_hash == "b" * 64
    assert intent.git_sha == "deadbee"
    assert intent.policy_version == "pv2"
    assert intent.execution_version == "ev2"


# -- PaperExecutionEvent -----------------------------------------------------


def make_event(**overrides) -> PaperExecutionEvent:
    defaults = dict(
        event_id="evt-1", intent_id="intent-1", recommendation_id="rec-1", symbol="SOFI",
        event_type="FILLED", broker_order_id="broker-1", quantity=70, filled_quantity=70,
        fill_price=Decimal("14.30"), occurred_at=NOW, raw_status="fill", source="LUMIBOT_PAPER",
    )
    defaults.update(overrides)
    return PaperExecutionEvent(**defaults)


def test_valid_fill_event():
    event = make_event()
    assert event.filled_quantity == 70


def test_unknown_event_type_fails_closed():
    with pytest.raises(IntentValidationError, match="unknown event_type"):
        make_event(event_type="WEIRD")


def test_positive_fill_requires_price():
    with pytest.raises(IntentValidationError, match="positive fill_price"):
        make_event(fill_price=None)


def test_zero_fill_does_not_require_price():
    event = make_event(event_type="PARTIALLY_FILLED", filled_quantity=0, fill_price=None)
    assert event.filled_quantity == 0


def test_filled_quantity_cannot_exceed_quantity():
    with pytest.raises(IntentValidationError, match="out of range"):
        make_event(quantity=10, filled_quantity=20)


# -- PaperExecutionResult -----------------------------------------------------


def make_result(**overrides) -> PaperExecutionResult:
    defaults = dict(
        intent_id="intent-1", recommendation_id="rec-1", final_status="FILLED",
        requested_quantity=70, filled_quantity=70, average_fill_price=Decimal("14.30"),
        fees=Decimal("0"), event_ids=("evt-1",), completed_at=NOW,
    )
    defaults.update(overrides)
    return PaperExecutionResult(**defaults)


def test_filled_result_requires_full_quantity():
    with pytest.raises(IntentValidationError, match="FILLED result"):
        make_result(filled_quantity=50)


def test_negative_fees_rejected():
    with pytest.raises(IntentValidationError, match="fees"):
        make_result(fees=Decimal("-1"))


def test_unknown_final_status_fails_closed():
    with pytest.raises(IntentValidationError, match="unknown final_status"):
        make_result(final_status="WEIRD")


# -- ReconciliationResult -----------------------------------------------------


def test_mismatch_requires_reasons():
    with pytest.raises(IntentValidationError, match="MISMATCH"):
        ReconciliationResult(
            intent_id="intent-1", status="MISMATCH", broker_quantity=70, ledger_quantity=50,
            broker_notional=Decimal("1000"), ledger_notional=Decimal("700"), reasons=(), reconciled_at=NOW,
        )


def test_matched_reconciliation():
    recon = ReconciliationResult(
        intent_id="intent-1", status="MATCHED", broker_quantity=70, ledger_quantity=70,
        broker_notional=Decimal("1001"), ledger_notional=Decimal("1001"), reasons=(), reconciled_at=NOW,
    )
    assert recon.status == "MATCHED"


# -- PaperExecutionEligibility -------------------------------------------------


def test_eligible_result_cannot_carry_reasons():
    with pytest.raises(IntentValidationError, match="must not carry"):
        PaperExecutionEligibility(
            recommendation_id="rec-1", eligible=True, reasons=("oops",), evaluated_at=NOW, policy_version="p1",
        )


def test_ineligible_result_requires_reasons():
    with pytest.raises(IntentValidationError, match="at least one reason"):
        PaperExecutionEligibility(
            recommendation_id="rec-1", eligible=False, reasons=(), evaluated_at=NOW, policy_version="p1",
        )
