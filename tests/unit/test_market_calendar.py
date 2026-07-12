from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from trading_research.evaluation.market_calendar import (
    add_trading_days,
    is_market_holiday,
    is_market_open,
    is_trading_day,
    is_weekend,
    next_trading_session,
)


@pytest.mark.parametrize(
    "day",
    [
        date(2026, 1, 1),   # New Year's Day (Thursday)
        date(2026, 1, 19),  # MLK Day
        date(2026, 2, 16),  # Presidents Day
        date(2026, 4, 3),   # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),   # Independence Day observed (July 4 is a Saturday)
        date(2026, 9, 7),   # Labor Day
        date(2026, 11, 26), # Thanksgiving
        date(2026, 12, 25), # Christmas
    ],
)
def test_known_2026_holidays(day):
    assert is_market_holiday(day) is True
    assert is_trading_day(day) is False


def test_july_4_saturday_shifts_observance_to_friday_not_saturday_itself():
    # July 4, 2026 falls on a Saturday, which is already a non-trading day
    # for weekend reasons — the *observed* holiday shift lands on Friday.
    assert is_market_holiday(date(2026, 7, 4)) is False  # not itself in the holiday set
    assert is_market_holiday(date(2026, 7, 3)) is True
    assert is_trading_day(date(2026, 7, 4)) is False  # still a non-trading day (weekend)


def test_ordinary_weekday_is_a_trading_day():
    assert is_trading_day(date(2026, 7, 13)) is True  # a Monday, no nearby holiday


def test_weekend_is_not_a_trading_day():
    assert is_weekend(date(2026, 7, 11)) is True  # Saturday
    assert is_trading_day(date(2026, 7, 11)) is False
    assert is_trading_day(date(2026, 7, 12)) is False  # Sunday


def test_next_trading_session_skips_weekend():
    friday = date(2026, 7, 10)
    assert next_trading_session(friday) == date(2026, 7, 13)  # following Monday


def test_next_trading_session_skips_holiday_and_weekend_together():
    # Independence Day (observed Friday July 3, 2026) directly precedes the weekend.
    thursday_before = date(2026, 7, 2)
    assert next_trading_session(thursday_before) == date(2026, 7, 6)  # Monday


def test_next_trading_session_inclusive_returns_same_day_if_already_trading():
    monday = date(2026, 7, 13)
    assert next_trading_session(monday, inclusive=True) == monday


def test_add_trading_days_one_day_horizon():
    monday = date(2026, 7, 13)
    assert add_trading_days(monday, 1) == date(2026, 7, 14)


def test_add_trading_days_crosses_weekend():
    friday = date(2026, 7, 10)
    assert add_trading_days(friday, 1) == date(2026, 7, 13)  # skips the weekend


def test_add_trading_days_five_day_horizon_is_one_calendar_week_later():
    monday = date(2026, 7, 13)
    assert add_trading_days(monday, 5) == date(2026, 7, 20)


def test_add_trading_days_zero_returns_start():
    day = date(2026, 7, 13)
    assert add_trading_days(day, 0) == day


def test_add_trading_days_negative_rejected():
    with pytest.raises(ValueError):
        add_trading_days(date(2026, 7, 13), -1)


def test_is_market_open_during_regular_hours():
    moment = datetime(2026, 7, 13, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert is_market_open(moment) is True


def test_is_market_open_false_before_open():
    moment = datetime(2026, 7, 13, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    assert is_market_open(moment) is False


def test_is_market_open_false_after_close():
    moment = datetime(2026, 7, 13, 17, 0, tzinfo=ZoneInfo("America/New_York"))
    assert is_market_open(moment) is False


def test_is_market_open_false_on_weekend():
    moment = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert is_market_open(moment) is False


def test_is_market_open_false_on_holiday():
    moment = datetime(2026, 12, 25, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert is_market_open(moment) is False


def test_is_market_open_requires_timezone_aware_datetime():
    from trading_research.evaluation.market_calendar import MarketCalendarError

    with pytest.raises(MarketCalendarError):
        is_market_open(datetime(2026, 7, 13, 10, 0))


def test_is_market_open_converts_from_utc():
    # 14:00 UTC on a July weekday is 10:00 America/New_York (EDT, UTC-4).
    moment = datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc)
    assert is_market_open(moment) is True
