"""
US CME equity-futures holiday calendar.

Covers the major *full-close* holidays for CME equity index micro futures
(MES, MNQ, etc.).  Early-close / abbreviated sessions are NOT treated as
full holidays here — they still produce bars, just fewer of them.

The calendar is computed algorithmically so no static year-tables are needed.
Good Friday uses the Anonymous Gregorian Easter algorithm.

Public API
----------
    is_us_market_holiday(d: date) -> bool
    us_market_holidays(year: int) -> set[date]
    spans_us_holiday(start: date, end: date) -> bool
"""

from datetime import date, timedelta
from functools import lru_cache
from typing import Set


# ── Easter (Anonymous Gregorian algorithm) ────────────────────────────────────

def _easter(year: int) -> date:
    """Return the date of Easter Sunday for *year*."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    leap_corr = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * leap_corr) // 451
    month, day = divmod(h + leap_corr - 7 * m + 114, 31)
    return date(year, month, day + 1)


# ── Observed-date helpers ─────────────────────────────────────────────────────

def _observed(d: date) -> date:
    """If *d* falls on a weekend, return the weekday the holiday is observed."""
    if d.weekday() == 5:          # Saturday → Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:          # Sunday → Monday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the *n*-th occurrence of *weekday* (0=Mon) in *month*."""
    first = date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return first + timedelta(days=delta + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of *weekday* in *month*."""
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    delta = (last.weekday() - weekday) % 7
    return last - timedelta(days=delta)


# ── Holiday list builder ─────────────────────────────────────────────────────

@lru_cache(maxsize=32)
def us_market_holidays(year: int) -> Set[date]:
    """
    Return the set of US-market full-close holidays for *year*.

    CME equity index futures observe the following holidays:

    1. New Year's Day — Jan 1 (observed)
    2. Martin Luther King Jr. Day — 3rd Monday in January
    3. Presidents' Day — 3rd Monday in February
    4. Good Friday — Friday before Easter Sunday
    5. Memorial Day — last Monday in May
    6. Juneteenth — Jun 19 (observed, since 2022)
    7. Independence Day — Jul 4 (observed)
    8. Labor Day — 1st Monday in September
    9. Thanksgiving Day — 4th Thursday in November
    10. Christmas Day — Dec 25 (observed)
    """
    holidays: Set[date] = set()

    # 1. New Year's Day
    holidays.add(_observed(date(year, 1, 1)))

    # 2. MLK Day (3rd Monday in January)
    holidays.add(_nth_weekday(year, 1, 0, 3))

    # 3. Presidents' Day (3rd Monday in February)
    holidays.add(_nth_weekday(year, 2, 0, 3))

    # 4. Good Friday (2 days before Easter Sunday)
    holidays.add(_easter(year) - timedelta(days=2))

    # 5. Memorial Day (last Monday in May)
    holidays.add(_last_weekday(year, 5, 0))

    # 6. Juneteenth (since 2022)
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))

    # 7. Independence Day
    holidays.add(_observed(date(year, 7, 4)))

    # 8. Labor Day (1st Monday in September)
    holidays.add(_nth_weekday(year, 9, 0, 1))

    # 9. Thanksgiving Day (4th Thursday in November)
    holidays.add(_nth_weekday(year, 11, 3, 4))

    # 10. Christmas Day
    holidays.add(_observed(date(year, 12, 25)))

    return holidays


def is_us_market_holiday(d: date) -> bool:
    """Return True if *d* is a US-market full-close holiday."""
    return d in us_market_holidays(d.year)


def spans_us_holiday(start: date, end: date) -> bool:
    """Return True if at least one US-market holiday falls in [start, end]."""
    current = start
    while current <= end:
        if is_us_market_holiday(current):
            return True
        current += timedelta(days=1)
    return False
