"""
Trading Calendar — per-instrument session schedule with holiday awareness.

Provides the authoritative definition of "when is this instrument's market
open?" so that gap detection, data validation, and bar-continuity checks
all use the same logic instead of ad-hoc heuristics scattered across modules.

Key concepts:
  - **Session window**: A (start, end) time-of-day pair in the instrument's
    local timezone that defines one contiguous trading period.
  - **Session day**: The set of session windows active on a given weekday
    (0=Mon … 6=Sun).  Most futures have Sun-evening → Fri-afternoon sessions.
  - **Holiday calendar**: Exchange-specific closure dates that override
    the normal schedule.

Usage:
    from trading_calendar import TradingCalendar

    cal = TradingCalendar("MES")
    cal.is_trading_time(unix_ts)            # → bool
    cal.expected_bar_count(from_ts, to_ts, 300)  # → int
    cal.classify_gap(from_ts, to_ts)        # → "weekend" | "holiday" | ...
    cal.next_session_start(unix_ts)         # → int (unix)
"""
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import config
from market_holidays import us_market_holidays

logger = logging.getLogger(__name__)

# ─── Session Definitions ─────────────────────────────────────────────────────
# Each instrument's trading schedule is defined as a list of session windows
# per weekday.  Times are in the instrument's local timezone.
#
# CME E-mini/Micro futures (MES, MNQ):
#   Sun 18:00 – Fri 17:00 ET with a daily 17:00–18:00 maintenance break
#
# COMEX Micro Gold (MGC):
#   Sun 18:00 – Fri 17:00 ET with a daily 17:00–18:00 maintenance break
#
# OSE Nikkei (NK225MC):
#   Mon-Fri 08:45–15:45 + 17:00–06:00 JST (night session)

# Type alias: a session window is (start_time, end_time) in local TZ.
# If end < start, the window wraps past midnight (e.g. 18:00→17:00 next day).
SessionWindow = Tuple[time, time]


def _build_cme_sessions() -> Dict[int, List[SessionWindow]]:
    """CME Globex: Sun 18:00 → Mon 17:00, Mon 18:00 → Tue 17:00, … Fri close 17:00.
    Represented as per-weekday windows where trading STARTS on that day.
    
    The daily maintenance break is 17:00-18:00 ET.
    Sunday opening at 18:00, Friday close at 17:00.
    """
    # Trading windows (all in ET):
    # Sun: 18:00 → Mon 17:00 (overnight into Monday)
    # Mon: 18:00 → Tue 17:00
    # Tue: 18:00 → Wed 17:00
    # Wed: 18:00 → Thu 17:00
    # Thu: 18:00 → Fri 17:00
    # Fri: closed
    # Sat: closed
    
    # For intraday bar classification, we represent this as continuous windows:
    # Each weekday has TWO segments:
    #   1. 00:00-17:00 (continuation from previous evening)
    #   2. 18:00-23:59:59 (start of new session)
    # Sunday only has segment 2 (18:00-23:59:59)
    # Friday only has segment 1 (00:00-17:00)
    #
    # Note: 23:59:59 is used instead of 00:00 to avoid midnight-wrapping
    # complexity. Bars starting at 23:55 are included in the current day's
    # session — their 5-minute window ends at 00:00 which falls into the
    # next calendar day's 00:00-17:00 segment (continuous coverage).
    
    return {
        0: [(time(0, 0), time(17, 0)), (time(18, 0), time(23, 59, 59))],  # Monday
        1: [(time(0, 0), time(17, 0)), (time(18, 0), time(23, 59, 59))],  # Tuesday
        2: [(time(0, 0), time(17, 0)), (time(18, 0), time(23, 59, 59))],  # Wednesday
        3: [(time(0, 0), time(17, 0)), (time(18, 0), time(23, 59, 59))],  # Thursday
        4: [(time(0, 0), time(17, 0))],                                    # Friday (close at 17:00)
        5: [],                                                              # Saturday (closed)
        6: [(time(18, 0), time(23, 59, 59))],                              # Sunday (open at 18:00)
    }


def _build_ose_sessions() -> Dict[int, List[SessionWindow]]:
    """OSE (Osaka Exchange) for Nikkei futures.
    Day session: 08:45-15:45 JST (Mon-Fri)
    Night session: 17:00-06:00 JST (Mon-Fri, wraps to next day)
    """
    return {
        0: [(time(0, 0), time(6, 0)), (time(8, 45), time(15, 45)), (time(17, 0), time(23, 59, 59))],
        1: [(time(0, 0), time(6, 0)), (time(8, 45), time(15, 45)), (time(17, 0), time(23, 59, 59))],
        2: [(time(0, 0), time(6, 0)), (time(8, 45), time(15, 45)), (time(17, 0), time(23, 59, 59))],
        3: [(time(0, 0), time(6, 0)), (time(8, 45), time(15, 45)), (time(17, 0), time(23, 59, 59))],
        4: [(time(0, 0), time(6, 0)), (time(8, 45), time(15, 45))],  # Friday: no night session
        5: [],
        6: [],
    }


# Registry: exchange → session builder
_SESSION_BUILDERS = {
    "CME":     _build_cme_sessions,
    "COMEX":   _build_cme_sessions,   # COMEX follows CME Globex schedule
    "OSE.JPN": _build_ose_sessions,
}


# ─── Holiday Calendar ─────────────────────────────────────────────────────────
# Maps exchange → function that returns a set of holiday dates for a year.
# US exchanges (CME, COMEX) use US market holidays.
# Japanese exchanges use a simplified set (expand as needed).

def _jpn_holidays(year: int) -> set:
    """Simplified Japanese market holidays.  Add more as needed."""
    holidays = set()
    # New Year's (Dec 31 - Jan 3)
    holidays.update([date(year, 1, d) for d in range(1, 4)])
    if year > 1:
        holidays.add(date(year - 1, 12, 31))
    # Coming of Age Day (2nd Monday in Jan)
    d = date(year, 1, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    holidays.add(d + timedelta(weeks=1))
    # National Foundation Day
    holidays.add(date(year, 2, 11))
    # Vernal Equinox (approx Mar 20)
    holidays.add(date(year, 3, 20))
    # Showa Day
    holidays.add(date(year, 4, 29))
    # Constitution Memorial Day, Greenery Day, Children's Day
    holidays.update([date(year, 5, d) for d in (3, 4, 5)])
    # Marine Day (3rd Monday in Jul)
    d = date(year, 7, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    holidays.add(d + timedelta(weeks=2))
    # Mountain Day
    holidays.add(date(year, 8, 11))
    # Respect for the Aged Day (3rd Monday in Sep)
    d = date(year, 9, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    holidays.add(d + timedelta(weeks=2))
    # Autumnal Equinox (approx Sep 23)
    holidays.add(date(year, 9, 23))
    # Sports Day (2nd Monday in Oct)
    d = date(year, 10, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    holidays.add(d + timedelta(weeks=1))
    # Culture Day
    holidays.add(date(year, 11, 3))
    # Labor Thanksgiving Day
    holidays.add(date(year, 11, 23))
    return holidays


_HOLIDAY_FUNCS = {
    "CME":     us_market_holidays,
    "COMEX":   us_market_holidays,
    "OSE.JPN": _jpn_holidays,
}


# ─── TradingCalendar ─────────────────────────────────────────────────────────

class TradingCalendar:
    """Session-aware calendar for a specific instrument.

    All public methods accept/return Unix timestamps (UTC seconds).
    Internal calculations use the instrument's local timezone.
    """

    def __init__(self, symbol: str):
        inst = config.INSTRUMENTS.get(symbol)
        if not inst:
            raise ValueError(f"Unknown instrument: {symbol}")

        self.symbol = symbol
        self.exchange = inst["exchange"]
        self.tz = ZoneInfo(inst["timezone"])
        self.rth_start = time(*inst["rth_start"])
        self.rth_end = time(*inst["rth_end"])

        builder = _SESSION_BUILDERS.get(self.exchange, _build_cme_sessions)
        self.sessions: Dict[int, List[SessionWindow]] = builder()

        holiday_func = _HOLIDAY_FUNCS.get(self.exchange, us_market_holidays)
        self._holiday_func = holiday_func
        self._holiday_cache: Dict[int, set] = {}

    def _holidays_for_year(self, year: int) -> set:
        if year not in self._holiday_cache:
            self._holiday_cache[year] = self._holiday_func(year)
        return self._holiday_cache[year]

    def is_holiday(self, d: date) -> bool:
        """Check if a date is a market holiday."""
        return d in self._holidays_for_year(d.year)

    def _to_local(self, ts: int) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(self.tz)

    def _to_utc_ts(self, dt_local: datetime) -> int:
        return int(dt_local.astimezone(timezone.utc).timestamp())

    # ─── Core: is this timestamp during a trading session? ───────────────

    def is_trading_time(self, ts: int) -> bool:
        """Return True if the given UTC timestamp falls within a trading session."""
        dt = self._to_local(ts)
        d = dt.date()

        # Check holiday
        if self.is_holiday(d):
            return False

        # Check session windows for this weekday
        t = dt.time()
        windows = self.sessions.get(dt.weekday(), [])
        for win_start, win_end in windows:
            if win_start <= t <= win_end:
                return True
        return False

    def is_rth(self, ts: int) -> bool:
        """Return True if timestamp is within Regular Trading Hours."""
        dt = self._to_local(ts)
        t = dt.time()
        if self.rth_start <= self.rth_end:
            return self.rth_start <= t < self.rth_end
        else:
            return t >= self.rth_start or t < self.rth_end

    # ─── Gap classification ──────────────────────────────────────────────

    def classify_gap(self, gap_start_ts: int, gap_end_ts: int) -> str:
        """Classify a time gap between two consecutive bars.

        Returns one of:
          - "normal"       — gap is within expected session schedule (maintenance, weekend)
          - "weekend"      — gap spans a weekend closure
          - "holiday"      — gap includes a market holiday
          - "maintenance"  — daily maintenance break (e.g. 17:00-18:00 ET)
          - "data_gap"     — unexpected gap during trading hours (data issue)
        """
        dt_start = self._to_local(gap_start_ts)
        dt_end = self._to_local(gap_end_ts)
        gap_seconds = gap_end_ts - gap_start_ts

        # Check if gap spans a weekend
        start_wd = dt_start.weekday()
        end_wd = dt_end.weekday()

        # Weekend check: gap starts Friday afternoon and ends Sunday/Monday
        if start_wd == 4 and end_wd in (0, 6) and gap_seconds < 259200:  # < 72h
            return "weekend"

        # Holiday check: any day in the gap range is a holiday
        d = dt_start.date()
        while d <= dt_end.date():
            if self.is_holiday(d):
                if gap_seconds < 345600:  # < 96h (holiday + weekend combo)
                    return "holiday"
            d += timedelta(days=1)

        # Maintenance check: gap within the same day's maintenance window
        # For CME: 17:00-18:00 ET
        if dt_start.hour >= 16 and dt_end.hour <= 19 and gap_seconds < 7200:  # < 2h
            return "maintenance"

        # Check if both endpoints are outside trading hours
        if not self.is_trading_time(gap_start_ts) and not self.is_trading_time(gap_end_ts):
            if gap_seconds < 259200:  # < 72h
                return "normal"

        # Check if the entire gap falls outside trading hours
        # (e.g. Saturday to Sunday — both non-trading)
        all_non_trading = True
        check_ts = gap_start_ts
        while check_ts < gap_end_ts:
            if self.is_trading_time(check_ts):
                all_non_trading = False
                break
            check_ts += 300  # check every 5 minutes

        if all_non_trading:
            return "normal"

        return "data_gap"

    # ─── Expected bar enumeration ────────────────────────────────────────

    def expected_bars(
        self,
        from_ts: int,
        to_ts: int,
        interval: int,
        rth_only: bool = False,
    ) -> List[int]:
        """Return a sorted list of expected bar timestamps in [from_ts, to_ts].

        Each timestamp represents the start of a bar that should exist
        during active trading hours. Used for gap detection and data validation.
        """
        result = []
        # Align to interval grid
        ts = (from_ts // interval) * interval
        while ts <= to_ts:
            if self.is_trading_time(ts):
                if not rth_only or self.is_rth(ts):
                    result.append(ts)
            ts += interval
        return result

    def expected_bar_count(
        self,
        from_ts: int,
        to_ts: int,
        interval: int,
        rth_only: bool = False,
    ) -> int:
        """Count expected bars in range (fast for small ranges)."""
        return len(self.expected_bars(from_ts, to_ts, interval, rth_only))

    # ─── Session navigation ──────────────────────────────────────────────

    def next_session_start(self, ts: int) -> Optional[int]:
        """Find the start of the next trading session after ts."""
        dt = self._to_local(ts)
        # Look up to 7 days ahead
        for day_offset in range(8):
            check_date = dt.date() + timedelta(days=day_offset)
            if self.is_holiday(check_date):
                continue
            wd = check_date.weekday()
            windows = self.sessions.get(wd, [])
            for win_start, _win_end in windows:
                candidate = datetime.combine(check_date, win_start, tzinfo=self.tz)
                candidate_ts = self._to_utc_ts(candidate)
                if candidate_ts > ts:
                    return candidate_ts
        return None

    def prev_session_end(self, ts: int) -> Optional[int]:
        """Find the end of the previous trading session before ts."""
        dt = self._to_local(ts)
        for day_offset in range(8):
            check_date = dt.date() - timedelta(days=day_offset)
            if self.is_holiday(check_date):
                continue
            wd = check_date.weekday()
            windows = self.sessions.get(wd, [])
            for _win_start, win_end in reversed(windows):
                candidate = datetime.combine(check_date, win_end, tzinfo=self.tz)
                candidate_ts = self._to_utc_ts(candidate)
                if candidate_ts < ts:
                    return candidate_ts
        return None

    # ─── Data integrity ──────────────────────────────────────────────────

    def find_missing_bars(
        self,
        actual_timestamps: List[int],
        from_ts: int,
        to_ts: int,
        interval: int,
    ) -> List[int]:
        """Compare actual bar timestamps against expected schedule.

        Returns sorted list of timestamps where bars are expected but missing.
        """
        expected = set(self.expected_bars(from_ts, to_ts, interval))
        actual = set(actual_timestamps)
        missing = sorted(expected - actual)
        return missing

    def find_gaps(
        self,
        bars: List[dict],
        interval: int,
    ) -> List[dict]:
        """Detect and classify gaps in a list of bars.

        Returns a list of gap records with classification.
        More reliable than simple timestamp-difference checks because
        it uses the trading session calendar.
        """
        if len(bars) < 2:
            return []

        gaps = []
        for i in range(1, len(bars)):
            t1 = bars[i - 1]["time"]
            t2 = bars[i]["time"]
            gap_sec = t2 - t1

            if gap_sec <= interval:
                continue

            gap_type = self.classify_gap(t1, t2)
            if gap_type == "data_gap" or (gap_type == "normal" and gap_sec > interval * 2):
                gaps.append({
                    "gap_start": t1,
                    "gap_end": t2,
                    "gap_seconds": gap_sec,
                    "gap_type": gap_type,
                    "expected_bars": max(0, gap_sec // interval - 1),
                })

        return gaps

    def validate_bar(self, bar: dict) -> List[str]:
        """Validate a single bar's OHLCV integrity.

        Returns a list of violation descriptions (empty = valid).
        """
        violations = []

        o, h, l, c = bar.get("open", 0), bar.get("high", 0), bar.get("low", 0), bar.get("close", 0)
        v = bar.get("volume", 0)

        if h < l:
            violations.append(f"high ({h}) < low ({l})")
        if o > h or o < l:
            violations.append(f"open ({o}) outside [low={l}, high={h}]")
        if c > h or c < l:
            violations.append(f"close ({c}) outside [low={l}, high={h}]")
        if any(p <= 0 for p in (o, h, l, c)):
            violations.append(f"non-positive price: O={o} H={h} L={l} C={c}")
        if v < 0:
            violations.append(f"negative volume: {v}")

        return violations


# ─── Module-level convenience ─────────────────────────────────────────────────

_calendar_cache: Dict[str, TradingCalendar] = {}


def get_calendar(symbol: str) -> TradingCalendar:
    """Get (or create) a TradingCalendar for the given symbol."""
    if symbol not in _calendar_cache:
        _calendar_cache[symbol] = TradingCalendar(symbol)
    return _calendar_cache[symbol]
