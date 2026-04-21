"""
Contract Calendar — official futures rollover rules.

Replaces the previous ad-hoc "every month, day <= 10 = previous contract"
heuristic used in ``ib_data_fetcher.py`` with the actual, published
rollover conventions of each exchange.

Supported rollover rule types (set per-instrument via
``config.INSTRUMENTS[symbol]["rollover_rule"]``):

    1. ``{"type": "n_bdays_before_ltd", "n": 1}``
       Roll N business days before the Last Trading Day (LTD).
       Used for COMEX metals (MGC) where LTD is the third-to-last business
       day of the contract month.  Setting n=1 means "roll one business
       day before LTD", which is the generally accepted convention.

    2. ``{"type": "nth_business_day", "n": 8}``
       Roll on the N-th business day of the contract month itself.
       This is the CME convention for equity index futures (MES, MNQ):
       the 8th business day of the contract month (the "Quarterly Roll
       Date" published on the CME website), which immediately precedes
       the 3rd-Friday Last Trading Day.

    3. ``{"type": "second_friday", "offset_bdays": -1}``
       Roll ``offset_bdays`` business days before the 2nd Friday of the
       contract month.  Used for OSE Nikkei (NK225MC) whose SQ is the
       2nd Friday; rolling on the business day before SQ matches the
       common practice.

All rules are *deterministic functions of the calendar* — they never
consult the current date to decide whether to "switch to the next
contract yet", unlike the old day-10 heuristic.

Holiday-adjustment:
    For US-listed products (CME, COMEX), the business-day counter uses
    ``market_holidays.is_us_market_holiday`` so e.g. Good Friday or
    Thanksgiving-adjacent closures correctly shift the rollover date.
    For OSE products we use a simplified weekends-only calendar (the
    full JP holiday table lives in ``trading_calendar.py`` for session
    classification and is not needed here because SQ days are already
    published as non-holiday Fridays).

Public API
----------
    active_contract(ts_utc: int, symbol: str) -> str          # "YYYYMM"
    neighbor_contracts(symbol, month, include_prev=True,
                       include_next=True) -> List[str]
    rollover_date(symbol: str, year: int, month: int) -> date
    is_rollover_day(d: date, symbol: str) -> bool

The output ``YYYYMM`` strings are the *contract month* (not the
rollover month).  ``active_contract`` returns the contract that is
currently the front-month at ``ts_utc``.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import config
from market_holidays import is_us_market_holiday

logger = logging.getLogger(__name__)


# ─── Business-day helpers ─────────────────────────────────────────────────────

def _is_business_day_us(d: date) -> bool:
    """Weekday and not a US market holiday."""
    return d.weekday() < 5 and not is_us_market_holiday(d)


def _is_business_day_basic(d: date) -> bool:
    """Weekday only (no holiday table)."""
    return d.weekday() < 5


def _is_business_day(d: date, symbol: str) -> bool:
    inst = config.INSTRUMENTS.get(symbol, {})
    tz = inst.get("timezone", "America/New_York")
    # US-listed exchanges use the US holiday table; others only skip weekends.
    if tz == "America/New_York":
        return _is_business_day_us(d)
    return _is_business_day_basic(d)


def _nth_business_day(year: int, month: int, n: int, symbol: str) -> date:
    """Return the date of the *n*-th business day of the given month."""
    d = date(year, month, 1)
    count = 0
    while True:
        if _is_business_day(d, symbol):
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)
        if d.month != month:
            # Month had fewer than n business days — exceptional; fall back
            # to last business day of month.
            d -= timedelta(days=1)
            while not _is_business_day(d, symbol):
                d -= timedelta(days=1)
            return d


def _last_business_day(year: int, month: int, symbol: str) -> date:
    """Last business day of the given (year, month)."""
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while not _is_business_day(d, symbol):
        d -= timedelta(days=1)
    return d


def _bday_offset(d: date, offset: int, symbol: str) -> date:
    """Return the date *offset* business days away from *d* (signed)."""
    if offset == 0:
        return d
    step = 1 if offset > 0 else -1
    remaining = abs(offset)
    cur = d
    while remaining:
        cur += timedelta(days=step)
        if _is_business_day(cur, symbol):
            remaining -= 1
    return cur


def _second_friday(year: int, month: int) -> date:
    """Return the 2nd Friday of the given month."""
    d = date(year, month, 1)
    fridays = 0
    while True:
        if d.weekday() == 4:  # Friday
            fridays += 1
            if fridays == 2:
                return d
        d += timedelta(days=1)


# ─── Rollover-date resolution ─────────────────────────────────────────────────

def _resolve_rollover_rule(symbol: str) -> dict:
    inst = config.INSTRUMENTS.get(symbol)
    if not inst:
        # Default for unknown symbols: CME quarterly equity-index convention.
        return {"type": "nth_business_day", "n": 8}
    rule = inst.get("rollover_rule")
    if rule:
        return rule
    # Back-compat default by contract_type.
    ctype = inst.get("contract_type", "quarterly")
    if ctype == "quarterly":
        return {"type": "nth_business_day", "n": 8}
    if ctype == "bi-monthly":
        return {"type": "n_bdays_before_ltd", "n": 1}
    if ctype == "monthly":
        return {"type": "second_friday", "offset_bdays": -1}
    return {"type": "nth_business_day", "n": 8}


@lru_cache(maxsize=2048)
def rollover_date(symbol: str, year: int, month: int) -> date:
    """Return the date on which *symbol*'s (year,month) contract becomes
    inactive, i.e. the day at which the *next* contract assumes front-month
    duties.

    On dates strictly before the returned date, the (year,month) contract
    is the front month.  On dates >= the returned date, the next listed
    contract month is the front month.
    """
    rule = _resolve_rollover_rule(symbol)
    rtype = rule.get("type")

    if rtype == "nth_business_day":
        n = int(rule.get("n", 8))
        return _nth_business_day(year, month, n, symbol)

    if rtype == "n_bdays_before_ltd":
        n = int(rule.get("n", 1))
        ltd = _last_business_day(year, month, symbol)
        return _bday_offset(ltd, -n, symbol)

    if rtype == "second_friday":
        offset = int(rule.get("offset_bdays", -1))
        sq = _second_friday(year, month)
        return _bday_offset(sq, offset, symbol)

    logger.warning(
        "Unknown rollover rule %r for %s — falling back to 8th business day",
        rule, symbol,
    )
    return _nth_business_day(year, month, 8, symbol)


def is_rollover_day(d: date, symbol: str) -> bool:
    """True if *d* is the rollover day for some (symbol, year, month)."""
    inst = config.INSTRUMENTS.get(symbol, {})
    months = inst.get("contract_months", [3, 6, 9, 12])
    for m in months:
        if rollover_date(symbol, d.year, m) == d:
            return True
    return False


# ─── Active-contract lookup ───────────────────────────────────────────────────

def _contract_months_for_symbol(symbol: str) -> List[int]:
    inst = config.INSTRUMENTS.get(symbol)
    if not inst:
        return [3, 6, 9, 12]
    return list(inst.get("contract_months", [3, 6, 9, 12]))


def active_contract(ts_utc: int, symbol: str = "MES") -> str:
    """Return the front-month contract ``YYYYMM`` for *symbol* at *ts_utc*.

    "Front month" here means: the earliest listed contract whose rollover
    date is strictly after ``ts_utc`` (interpreted in the instrument's
    local timezone, since rollover is a date concept).

    This is a pure, deterministic function of the calendar — it does NOT
    consult the current system clock.  It works for past, present, and
    future timestamps.
    """
    inst = config.INSTRUMENTS.get(symbol, {})
    tz_name = inst.get("timezone", "America/New_York")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/New_York")

    local_date = datetime.fromtimestamp(int(ts_utc), tz=timezone.utc).astimezone(tz).date()
    months = _contract_months_for_symbol(symbol)

    # Walk up to 24 months forward until we find a contract whose rollover
    # date is strictly after local_date.
    y, m = local_date.year, local_date.month
    for _ in range(36):
        # Find the first listed month >= m in the cycle for year y.
        candidates = [q for q in months if q >= m]
        if not candidates:
            y += 1
            m = months[0]
            continue
        qm = candidates[0]
        roll = rollover_date(symbol, y, qm)
        if local_date < roll:
            return f"{y}{qm:02d}"
        # Past this contract's rollover — move to the next listed month.
        idx = months.index(qm)
        if idx + 1 < len(months):
            m = months[idx + 1]
        else:
            y += 1
            m = months[0]

    # Fallback (should never happen): return a best-effort month.
    logger.warning("active_contract: no contract found within 36 steps for %s @ %s",
                   symbol, local_date)
    return f"{local_date.year}{months[0]:02d}"


def neighbor_contracts(
    symbol: str,
    month: str,
    include_prev: bool = True,
    include_next: bool = True,
) -> List[str]:
    """Return neighbour contract months for an ``YYYYMM``.

    Useful when a historical range spans a rollover and two contracts'
    data need to be combined.  The caller decides what to do with the
    list (fetch, merge, dedupe).
    """
    inst = config.INSTRUMENTS.get(symbol, {})
    months = inst.get("contract_months", [3, 6, 9, 12])

    y, m = int(month[:4]), int(month[4:])

    result: List[str] = []
    if include_prev:
        if m in months:
            idx = months.index(m)
            if idx > 0:
                result.append(f"{y}{months[idx - 1]:02d}")
            else:
                result.append(f"{y - 1}{months[-1]:02d}")
        else:
            prev = [q for q in months if q < m]
            if prev:
                result.append(f"{y}{prev[-1]:02d}")
            else:
                result.append(f"{y - 1}{months[-1]:02d}")

    result.append(month)

    if include_next:
        if m in months:
            idx = months.index(m)
            if idx + 1 < len(months):
                result.append(f"{y}{months[idx + 1]:02d}")
            else:
                result.append(f"{y + 1}{months[0]:02d}")
        else:
            nxt = [q for q in months if q > m]
            if nxt:
                result.append(f"{y}{nxt[0]:02d}")
            else:
                result.append(f"{y + 1}{months[0]:02d}")

    # Deduplicate while preserving order.
    seen = set()
    out: List[str] = []
    for x in result:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
