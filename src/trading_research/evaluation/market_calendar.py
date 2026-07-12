"""Deterministic US equity market calendar (docs/milestone-4.md Step 13).

No network, no third-party calendar dependency (`pandas_market_calendars`
is not installed in this repository — confirmed at implementation time).
This is a fixed-rule NYSE/Nasdaq federal-holiday calendar with standard
weekend-observance shifting (a holiday landing on Saturday is observed the
preceding Friday; on Sunday, the following Monday). It intentionally does
**not** model early-close half-days (e.g. the day after Thanksgiving, Dec
24 in some years) — see docs/milestone4-isolated-paper-broker.md "Known
limitations": this affects only intraday `is_market_open` precision near
early closes, never full-day trading/holiday classification, and never
evaluation horizon math (which only needs whole trading *days*, not
intraday hours).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MARKET_TIMEZONE_NAME = "America/New_York"
MARKET_OPEN_TIME = time(9, 30)
MARKET_CLOSE_TIME = time(16, 0)


class MarketCalendarError(RuntimeError):
    """The market-session policy cannot be determined — fail closed rather
    than guess (docs/milestone-4.md Step 13: "Do not submit an order when
    the market-session policy cannot be determined")."""


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """`weekday`: Monday=0 ... Sunday=6. `n` is 1-indexed (1st, 2nd, 3rd, ...)."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    d += timedelta(days=offset)
    d += timedelta(weeks=n - 1)
    return d


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    d = next_month - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher) — deterministic,
    no external dependency."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(holiday: date) -> date:
    if holiday.weekday() == 5:  # Saturday -> observed Friday before
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:  # Sunday -> observed Monday after
        return holiday + timedelta(days=1)
    return holiday


def _federal_holidays(year: int) -> set[date]:
    holidays = {
        _observed(date(year, 1, 1)),          # New Year's Day
        _nth_weekday_of_month(year, 1, 0, 3),  # MLK Day: 3rd Monday of January
        _nth_weekday_of_month(year, 2, 0, 3),  # Washington's Birthday: 3rd Monday of February
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday_of_month(year, 5, 0),     # Memorial Day: last Monday of May
        _observed(date(year, 6, 19)),          # Juneteenth
        _observed(date(year, 7, 4)),           # Independence Day
        _nth_weekday_of_month(year, 9, 0, 1),   # Labor Day: 1st Monday of September
        _nth_weekday_of_month(year, 11, 3, 4),  # Thanksgiving: 4th Thursday of November
        _observed(date(year, 12, 25)),         # Christmas Day
    }
    return holidays


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def is_market_holiday(day: date) -> bool:
    return day in _federal_holidays(day.year)


def is_trading_day(day: date) -> bool:
    return not is_weekend(day) and not is_market_holiday(day)


def next_trading_session(day: date, *, inclusive: bool = False) -> date:
    """The next valid trading session on or after `day` (`inclusive=True`)
    or strictly after `day` (`inclusive=False`) — the deterministic rule for
    "a horizon lands on a holiday or weekend" (docs/milestone-4.md Step 13)."""
    current = day if inclusive else day + timedelta(days=1)
    while not is_trading_day(current):
        current += timedelta(days=1)
    return current


def add_trading_days(start: date, n: int) -> date:
    """`start` is treated as day zero regardless of whether it is itself a
    trading session; returns the date reached after stepping forward `n`
    trading sessions (docs/milestone-4.md Step 11 horizons: 1/5/10/20/60
    trading days)."""
    if n < 0:
        raise ValueError("n must not be negative")
    current = start
    counted = 0
    while counted < n:
        current += timedelta(days=1)
        if is_trading_day(current):
            counted += 1
    return current


def is_market_open(moment: datetime) -> bool:
    """Regular U.S. equities hours only (docs/milestone-4.md Step 13 default
    policy: "regular-hours U.S. equities only", "no extended-hours
    orders"). Does not account for early-close half-days — see this
    module's docstring."""
    if moment.tzinfo is None:
        raise MarketCalendarError("is_market_open requires a timezone-aware datetime")
    try:
        local = moment.astimezone(ZoneInfo(MARKET_TIMEZONE_NAME))
    except ZoneInfoNotFoundError as exc:
        raise MarketCalendarError(
            "the America/New_York timezone database is not available in this environment — "
            "cannot determine the market session, refusing to guess"
        ) from exc
    if not is_trading_day(local.date()):
        return False
    return MARKET_OPEN_TIME <= local.time() < MARKET_CLOSE_TIME
