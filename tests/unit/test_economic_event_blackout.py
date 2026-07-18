from datetime import datetime, timedelta, timezone

from trading_research.evidence_providers.economic_calendar import (
    BLACKOUT_BLOCKED_EVENT, BLACKOUT_BLOCKED_UNKNOWN,
    EconomicEvent, EconomicEventBlackoutConfiguration,
    evaluate_economic_event_blackout,
)


NOW = datetime(2026, 7, 18, 14, 30, tzinfo=timezone.utc)
CFG = EconomicEventBlackoutConfiguration(
    enabled=True, before_minutes=30, after_minutes=30, minimum_importance="HIGH",
    markets=("US",), blocked_categories=("CPI",), maximum_data_age_minutes=60,
)


def _event(**overrides):
    values = dict(
        event_id="cpi", title="CPI", category="CPI", market="US", scheduled_at=NOW,
        originally_published_at=NOW - timedelta(days=1), last_updated_at=NOW - timedelta(minutes=5),
        importance="HIGH", status="SCHEDULED", actual_value=None, forecast_value="2.5%",
        previous_value="2.4%", source_provider="fixture", source_locator="fixture:cpi",
        retrieved_at=NOW - timedelta(minutes=5), available_at=NOW - timedelta(days=1),
        point_in_time_safe=True, content_hash="hash",
    )
    values.update(overrides)
    return EconomicEvent(**values)


def test_exact_blackout_boundaries_block_and_sell_bypass_is_caller_owned():
    before = evaluate_economic_event_blackout(
        as_of=NOW - timedelta(minutes=30),
        events=(_event(
            retrieved_at=NOW - timedelta(minutes=35),
            last_updated_at=NOW - timedelta(minutes=35),
        ),), configuration=CFG,
    )
    after = evaluate_economic_event_blackout(
        as_of=NOW + timedelta(minutes=30),
        events=(_event(retrieved_at=NOW + timedelta(minutes=25)),), configuration=CFG,
    )
    assert before.allowed is False and BLACKOUT_BLOCKED_EVENT in before.reason_codes
    assert after.allowed is False


def test_low_impact_outside_window_and_disabled_are_allowed():
    assert evaluate_economic_event_blackout(
        as_of=NOW, events=(_event(importance="LOW"),), configuration=CFG,
    ).allowed is True
    disabled = EconomicEventBlackoutConfiguration(
        enabled=False, before_minutes=30, after_minutes=30, minimum_importance="HIGH",
        markets=("US",), blocked_categories=("CPI",),
    )
    assert evaluate_economic_event_blackout(as_of=NOW, events=None, configuration=disabled).allowed is True


def test_unavailable_future_or_unsafe_data_fails_closed():
    assert BLACKOUT_BLOCKED_UNKNOWN in evaluate_economic_event_blackout(
        as_of=NOW, events=None, configuration=CFG,
    ).reason_codes
    assert evaluate_economic_event_blackout(
        as_of=NOW, events=(_event(available_at=NOW + timedelta(minutes=1)),), configuration=CFG,
    ).allowed is False
