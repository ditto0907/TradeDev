"""Signal detector — finds 2-consecutive-bar signals in 5min RTH bars.

See doc/strategy_research_v1.md §2 for the strict spec.

Pure functions only — no DB / API / IO. Inputs are bar dicts, outputs are
SignalRecord dataclasses serializable to dict via asdict().
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

STRONG_THRESHOLD = 0.69       # COH/L >= 0.69 → strong bull; <= 0.31 → strong bear

# Tick size for gap detection. MES = 0.25, MNQ = 0.25; use a small fixed eps.
GAP_EPS = 0.01  # if open differs from prev_close by more than this, it's a gap


@dataclass
class SignalRecord:
    date: str               # "20260301"
    bar_cnt: str            # "B4"
    bar_ts: int             # Unix ts (signal bar's open ts)
    direction: str          # "Bull" | "Bear"
    gap: str                # "None" | "GapUp" | "GapDown"
    signal_strength: str    # "Strong" | "Weak"
    coh_l: float            # 0..1, computed on signal bar
    overlapping: str        # "small" | "medium" | "large"
    overlapping_pct: float  # 0..1
    pb_bars: int            # number of opposite bars right before this 2-bar pair
    pb_strength: str        # "weak" | "strong"
    ft: str                 # "Y" | "2nd" | "N"

    def to_dict(self) -> dict:
        return asdict(self)


def _is_bull(b: dict) -> bool:
    return b["close"] > b["open"]


def _is_bear(b: dict) -> bool:
    return b["close"] < b["open"]


def _bar_range(b: dict) -> float:
    return abs(b["high"] - b["low"])


def _coh_l(b: dict, direction: str) -> float:
    """Close-Off-High/Low ratio.

    Bull bar: (close - low) / range  → 1.0 when close == high
    Bear bar: (high - close) / range → 1.0 when close == low
    """
    rng = _bar_range(b)
    if rng <= 0:
        return 0.5
    if direction == "Bull":
        return (b["close"] - b["low"]) / rng
    else:  # Bear
        return (b["high"] - b["close"]) / rng


def _is_strong(b: dict, direction: str) -> bool:
    """Strong = body closes in the top/bottom 31% of the bar's range."""
    return _coh_l(b, direction) >= STRONG_THRESHOLD


def _overlap_pct(b1: dict, b2: dict) -> float:
    """Overlap as a fraction of the union range of two bars."""
    overlap = max(0.0, min(b1["high"], b2["high"]) - max(b1["low"], b2["low"]))
    union = max(b1["high"], b2["high"]) - min(b1["low"], b2["low"])
    if union <= 0:
        return 0.0
    return overlap / union


def _classify_overlap(pct: float) -> str:
    if pct < 0.33:
        return "small"
    if pct < 0.66:
        return "medium"
    return "large"


def _bar_cnt_for_ts(ts: int) -> str:
    """Return 'B<n>' where n is the 5min bar number after 09:30 ET.

    9:30 → B1, 9:35 → B2, 9:38 (intra-bar) → B2, 10:00 → B7.
    """
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET)
    open_dt = dt.replace(hour=9, minute=30, second=0, microsecond=0)
    if dt < open_dt:
        # Pre-market — return B0 to flag it (caller filters RTH usually)
        return "B0"
    delta_min = (dt - open_dt).total_seconds() / 60.0
    n = int(delta_min // 5) + 1
    return f"B{n}"


def _date_str_et(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET)
    return dt.strftime("%Y%m%d")


def _classify_gap(open_price: float, prev_close: Optional[float]) -> str:
    if prev_close is None:
        return "None"
    diff = open_price - prev_close
    if diff > GAP_EPS:
        return "GapUp"
    if diff < -GAP_EPS:
        return "GapDown"
    return "None"


def _is_doji(b: dict) -> bool:
    """Doji = open == close (no body)."""
    return b["close"] == b["open"]


def _signal_direction(b_prev: dict, b_curr: dict) -> Optional[str]:
    """Return 'Bull', 'Bear', or None if not a valid 2-consecutive signal."""
    if _is_bull(b_prev) and _is_bull(b_curr):
        return "Bull"
    if _is_bear(b_prev) and _is_bear(b_curr):
        return "Bear"
    return None


def _ft_classify(signal_dir: str, after_bars: List[dict]) -> str:
    """Determine Follow-Through after the signal bar.

    Y    : 1st bar after signal is same-direction (allows doji+min body)
    2nd  : 1st is opposite/doji, 2nd is same-direction
    N    : neither of the next 2 bars is same-direction
    """
    if not after_bars:
        return "N"
    b1 = after_bars[0]
    if signal_dir == "Bull":
        if _is_bull(b1):
            return "Y"
    else:
        if _is_bear(b1):
            return "Y"
    # Check 2nd bar
    if len(after_bars) >= 2:
        b2 = after_bars[1]
        if signal_dir == "Bull" and _is_bull(b2):
            return "2nd"
        if signal_dir == "Bear" and _is_bear(b2):
            return "2nd"
    return "N"


def _pullback_count(signal_dir: str, before_bars: List[dict]) -> tuple[int, str]:
    """Count consecutive opposite-direction bars right before the 2-bar pair.

    Returns (count, strength).  Strength = "strong" if any bar in the
    pullback leg has strong COH/L in the opposite (PB) direction.
    """
    if not before_bars:
        return 0, "weak"
    pb_dir = "Bear" if signal_dir == "Bull" else "Bull"
    cnt = 0
    strong = False
    for b in reversed(before_bars):
        if pb_dir == "Bull" and _is_bull(b):
            cnt += 1
            if _is_strong(b, "Bull"):
                strong = True
        elif pb_dir == "Bear" and _is_bear(b):
            cnt += 1
            if _is_strong(b, "Bear"):
                strong = True
        else:
            break
    return cnt, ("strong" if strong else "weak")


def detect_signals(
    bars_5min: List[dict],
    prev_day_close: Optional[float] = None,
) -> List[SignalRecord]:
    """Scan a list of 5min RTH bars and return all signal K records.

    Args:
        bars_5min: chronological list of dicts with keys
                   {time, open, high, low, close, volume}.
        prev_day_close: previous trading day's RTH close, used for Gap detection.
                        If None, all signals get gap="None".

    Returns:
        List of SignalRecord objects, one per detected signal bar.
        The signal bar is the *2nd* bar of the 2-consecutive-bar pair.
    """
    out: List[SignalRecord] = []
    if len(bars_5min) < 2:
        return out

    # Determine session open price (first bar of the day) for gap detection.
    # Caller may pass mixed-day bars, so per-day open lookup:
    day_open: dict[str, float] = {}
    for b in bars_5min:
        d = _date_str_et(b["time"])
        if d not in day_open:
            day_open[d] = b["open"]

    for i in range(1, len(bars_5min)):
        b_prev = bars_5min[i - 1]
        b_curr = bars_5min[i]
        # Same trading day check — never form a signal across day boundary
        if _date_str_et(b_prev["time"]) != _date_str_et(b_curr["time"]):
            continue
        direction = _signal_direction(b_prev, b_curr)
        if direction is None:
            continue

        # Strength: both bars must be strong on their direction
        sig_strong = _is_strong(b_prev, direction) and _is_strong(b_curr, direction)
        coh = _coh_l(b_curr, direction)
        ov_pct = _overlap_pct(b_prev, b_curr)

        # Pullback leg = consecutive opposite bars before b_prev
        pb_cnt, pb_strength = _pullback_count(direction, bars_5min[:i - 1])

        # FT = look ahead 2 bars after signal
        ft = _ft_classify(direction, bars_5min[i + 1: i + 3])

        date_str = _date_str_et(b_curr["time"])
        # Gap = day_open[d] vs prev_day_close (only for first signal of session
        # — but we want it tagged on every signal that day for context)
        gap = _classify_gap(day_open.get(date_str, b_curr["open"]), prev_day_close)

        rec = SignalRecord(
            date=date_str,
            bar_cnt=_bar_cnt_for_ts(b_curr["time"]),
            bar_ts=int(b_curr["time"]),
            direction=direction,
            gap=gap,
            signal_strength="Strong" if sig_strong else "Weak",
            coh_l=round(coh, 3),
            overlapping=_classify_overlap(ov_pct),
            overlapping_pct=round(ov_pct, 3),
            pb_bars=pb_cnt,
            pb_strength=pb_strength,
            ft=ft,
        )
        out.append(rec)

    return out


__all__ = ["SignalRecord", "detect_signals"]
