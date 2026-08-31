"""Gap detector — structural gap detection and lifecycle tracking.

Pure functions only (no DB/network/file IO).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

try:
    from .signal_detector import ET, _bar_cnt_for_ts, _date_str_et
except ImportError:  # pragma: no cover - fallback when imported as top-level package
    from strategy.signal_detector import ET, _bar_cnt_for_ts, _date_str_et

TICK_SIZE = {"MES": 0.25, "MNQ": 0.25, "MGC": 0.1, "NK225MC": 5}
G3_MIN_TICKS = 2
G3_ATR_PCT = 0.15
G5_STRONG_STREAK = 20
BAR_SECONDS = 300


@dataclass
class Gap:
    gap_id: int
    gap_type: str
    direction: str
    create_ts: int
    create_bar_cnt: str
    low: float
    high: float
    size_ticks: float
    origin_price: float
    far_price: float
    key_level_source: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GapEvent:
    gap_id: int
    ts: int
    bar_cnt: str
    from_state: str
    to_state: str
    depth_pct: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class G5Heartbeat:
    direction: str
    current_streak: int
    max_streak_today: int
    ema_touch_events: list[int]

    def to_dict(self) -> dict:
        return asdict(self)


def compute_ema20(bars: list[dict]) -> list[float]:
    """Compute standard EMA(20) over close prices."""
    if not bars:
        return []
    period = 20
    alpha = 2.0 / (period + 1.0)
    ema: list[float] = []
    prev = float(bars[0]["close"])
    ema.append(prev)
    for b in bars[1:]:
        close = float(b["close"])
        prev = alpha * close + (1.0 - alpha) * prev
        ema.append(prev)
    return ema


def compute_atr(bars: list[dict], n: int = 20) -> list[float]:
    """Compute ATR as rolling mean of True Range."""
    if not bars:
        return []
    n = max(1, int(n))
    tr_values: list[float] = []
    atr: list[float] = []
    prev_close: Optional[float] = None
    for b in bars:
        high = float(b["high"])
        low = float(b["low"])
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)
        start = max(0, len(tr_values) - n)
        window = tr_values[start:]
        atr.append(sum(window) / len(window))
        prev_close = float(b["close"])
    return atr


def _depth_pct_for_gap(gap: Gap, bar: dict) -> float:
    width = max(1e-12, float(gap.high) - float(gap.low))
    if gap.direction == "Bull":
        if float(bar["low"]) >= float(gap.high):
            return 0.0
        entered = float(gap.high) - max(float(bar["low"]), float(gap.low))
    else:
        if float(bar["high"]) <= float(gap.low):
            return 0.0
        entered = min(float(bar["high"]), float(gap.high)) - float(gap.low)
    return max(0.0, min(1.0, entered / width))


def detect_g3_micro_gaps(bars: list[dict], tick: float, atr20: list[float]) -> list[Gap]:
    """Detect G3 micro-gaps around breakout bar i using i-1 / i+1 relation."""
    out: list[Gap] = []
    if len(bars) < 3:
        return out
    gid = 1
    tick = max(float(tick), 1e-12)

    for i in range(1, len(bars) - 1):
        b_prev = bars[i - 1]
        b_next = bars[i + 1]

        if int(bars[i]["time"]) - int(b_prev["time"]) != BAR_SECONDS:
            continue

        atr_th = 0.0
        if i < len(atr20):
            atr_th = G3_ATR_PCT * max(0.0, float(atr20[i]))

        # Bull: low(i+1) > high(i-1)
        gap_low_bull = float(b_prev["high"])
        gap_high_bull = float(b_next["low"])
        if gap_high_bull > gap_low_bull:
            size = gap_high_bull - gap_low_bull
            if size >= G3_MIN_TICKS * tick or size >= atr_th:
                out.append(
                    Gap(
                        gap_id=gid,
                        gap_type="G3",
                        direction="Bull",
                        create_ts=int(bars[i]["time"]),
                        create_bar_cnt=_bar_cnt_for_ts(int(bars[i]["time"])),
                        low=gap_low_bull,
                        high=gap_high_bull,
                        size_ticks=round(size / tick, 3),
                        origin_price=gap_high_bull,
                        far_price=gap_low_bull,
                        key_level_source="",
                    )
                )
                gid += 1

        # Bear: high(i+1) < low(i-1)
        gap_low_bear = float(b_next["high"])
        gap_high_bear = float(b_prev["low"])
        if gap_high_bear > gap_low_bear:
            size = gap_high_bear - gap_low_bear
            if size >= G3_MIN_TICKS * tick or size >= atr_th:
                out.append(
                    Gap(
                        gap_id=gid,
                        gap_type="G3",
                        direction="Bear",
                        create_ts=int(bars[i]["time"]),
                        create_bar_cnt=_bar_cnt_for_ts(int(bars[i]["time"])),
                        low=gap_low_bear,
                        high=gap_high_bear,
                        size_ticks=round(size / tick, 3),
                        origin_price=gap_low_bear,
                        far_price=gap_high_bear,
                        key_level_source="",
                    )
                )
                gid += 1
    return out


def detect_g4_breakout_gaps(bars: list[dict], key_levels: dict) -> list[Gap]:
    """Detect G4 breakout gaps from key levels that never get touched later."""
    out: list[Gap] = []
    if not bars or not key_levels:
        return out

    gid = 1
    tick = TICK_SIZE["MES"]

    for key, val in key_levels.items():
        if val is None:
            continue
        try:
            level = float(val)
        except (TypeError, ValueError):
            continue

        # Bull breakout: breakout bar itself clears level with a gap above it.
        for i, b in enumerate(bars):
            b_low = float(b["low"])
            b_high = float(b["high"])
            b_close = float(b["close"])
            if b_low > level and b_close > level:
                touched_later = any(float(x["low"]) <= level for x in bars[i + 1:])
                if touched_later:
                    continue
                size = b_low - level
                out.append(
                    Gap(
                        gap_id=gid,
                        gap_type="G4",
                        direction="Bull",
                        create_ts=int(b["time"]),
                        create_bar_cnt=_bar_cnt_for_ts(int(b["time"])),
                        low=level,
                        high=b_low,
                        size_ticks=round(size / tick, 3),
                        origin_price=b_low,
                        far_price=level,
                        key_level_source=str(key),
                    )
                )
                gid += 1
                break

            # Bear breakout: mirror logic.
            if b_high < level and b_close < level:
                touched_later = any(float(x["high"]) >= level for x in bars[i + 1:])
                if touched_later:
                    continue
                size = level - b_high
                out.append(
                    Gap(
                        gap_id=gid,
                        gap_type="G4",
                        direction="Bear",
                        create_ts=int(b["time"]),
                        create_bar_cnt=_bar_cnt_for_ts(int(b["time"])),
                        low=b_high,
                        high=level,
                        size_ticks=round(size / tick, 3),
                        origin_price=b_high,
                        far_price=level,
                        key_level_source=str(key),
                    )
                )
                gid += 1
                break

    return out


def detect_g5_ema_gap_bars(bars: list[dict], ema20: list[float]) -> tuple[list[Gap], G5Heartbeat]:
    """Detect G5 streaks of bars staying above/below EMA20."""
    out: list[Gap] = []
    if not bars:
        return out, G5Heartbeat(direction="", current_streak=0, max_streak_today=0, ema_touch_events=[])

    gid = 1
    tick = TICK_SIZE["MES"]
    current_dir = ""
    streak = 0
    max_streak = 0
    touches: list[int] = []
    prev_touch = False

    for i, b in enumerate(bars):
        ema = float(ema20[i]) if i < len(ema20) else float(b["close"])
        low = float(b["low"])
        high = float(b["high"])

        bull = low > ema
        bear = high < ema
        direction = "Bull" if bull else ("Bear" if bear else "")

        if direction and direction == current_dir:
            streak += 1
        elif direction:
            current_dir = direction
            streak = 1
        else:
            current_dir = ""
            streak = 0

        max_streak = max(max_streak, streak)

        touching = low <= ema <= high
        if touching and not prev_touch:
            touches.append(int(b["time"]))
        prev_touch = touching

        if direction and streak == G5_STRONG_STREAK:
            if direction == "Bull":
                zone_low, zone_high = ema, low
                origin_price, far_price = low, ema
            else:
                zone_low, zone_high = high, ema
                origin_price, far_price = high, ema
            out.append(
                Gap(
                    gap_id=gid,
                    gap_type="G5",
                    direction=direction,
                    create_ts=int(b["time"]),
                    create_bar_cnt=_bar_cnt_for_ts(int(b["time"])),
                    low=min(zone_low, zone_high),
                    high=max(zone_low, zone_high),
                    size_ticks=round(abs(zone_high - zone_low) / tick, 3),
                    origin_price=origin_price,
                    far_price=far_price,
                    key_level_source="EMA20",
                )
            )
            gid += 1

    hb = G5Heartbeat(
        direction=current_dir,
        current_streak=streak,
        max_streak_today=max_streak,
        ema_touch_events=touches,
    )
    return out, hb


def track_gap_lifecycle(
    gap: Gap,
    bars_after: list[dict],
    checkpoints: tuple[int, ...] = (6, 12, 18),
) -> list[GapEvent]:
    """Track lifecycle state transitions for one gap after creation."""
    events: list[GapEvent] = []
    state = "CREATED"
    tested_once = False

    def _push(new_state: str, bar: dict, depth: float) -> None:
        nonlocal state
        if state == new_state:
            return
        events.append(
            GapEvent(
                gap_id=gap.gap_id,
                ts=int(bar["time"]),
                bar_cnt=_bar_cnt_for_ts(int(bar["time"])),
                from_state=state,
                to_state=new_state,
                depth_pct=round(depth, 3),
            )
        )
        state = new_state

    for idx, b in enumerate(bars_after, start=1):
        depth = _depth_pct_for_gap(gap, b)
        if depth > 0:
            tested_once = True
            _push("TESTED", b, depth)
            if depth > 0.5:
                _push("PARTIAL", b, depth)

        if gap.direction == "Bull":
            closed = float(b["low"]) <= float(gap.far_price)
            reversed_ = float(b["close"]) < float(gap.origin_price)
            lower_wick = min(float(b["open"]), float(b["close"])) - float(b["low"])
            upper_wick = float(b["high"]) - max(float(b["open"]), float(b["close"]))
            body = abs(float(b["close"]) - float(b["open"]))
            defended = depth > 0 and float(b["close"]) > float(gap.high) and lower_wick > max(body, upper_wick)
        else:
            closed = float(b["high"]) >= float(gap.far_price)
            reversed_ = float(b["close"]) > float(gap.origin_price)
            lower_wick = min(float(b["open"]), float(b["close"])) - float(b["low"])
            upper_wick = float(b["high"]) - max(float(b["open"]), float(b["close"]))
            body = abs(float(b["close"]) - float(b["open"]))
            defended = depth > 0 and float(b["close"]) < float(gap.low) and upper_wick > max(body, lower_wick)

        if defended:
            _push("DEFENDED", b, depth)
        if closed:
            _push("CLOSED", b, depth)
        if reversed_:
            _push("REVERSED", b, depth)

        if idx in checkpoints and not tested_once:
            _push("HELD", b, depth)

    return events


def analyze_day(
    bars_5m: list[dict],
    key_levels: dict,
    symbol: str = "MES",
    tick: Optional[float] = None,
    cutoff_ts: Optional[int] = None,
) -> dict:
    """Run all gap detectors + lifecycle tracking on one session."""
    bars = sorted(bars_5m, key=lambda b: int(b["time"]))
    if cutoff_ts is not None:
        bars = [b for b in bars if int(b["time"]) <= int(cutoff_ts)]

    if not bars:
        empty_hb = G5Heartbeat(direction="", current_streak=0, max_streak_today=0, ema_touch_events=[])
        return {"gaps": [], "events": [], "g5_heartbeat": empty_hb.to_dict()}

    if tick is None:
        tick = TICK_SIZE.get(symbol, TICK_SIZE["MES"])

    ema20 = compute_ema20(bars)
    atr20 = compute_atr(bars, n=20)

    g3 = detect_g3_micro_gaps(bars, float(tick), atr20)
    g4 = detect_g4_breakout_gaps(bars, key_levels)
    g5, hb = detect_g5_ema_gap_bars(bars, ema20)

    all_gaps = sorted(g3 + g4 + g5, key=lambda g: (g.create_ts, g.gap_type, g.direction))
    for i, g in enumerate(all_gaps, start=1):
        g.gap_id = i

    events: list[GapEvent] = []
    for g in all_gaps:
        bars_after = [b for b in bars if int(b["time"]) > int(g.create_ts)]
        events.extend(track_gap_lifecycle(g, bars_after))

    return {
        "gaps": [g.to_dict() for g in all_gaps],
        "events": [e.to_dict() for e in events],
        "g5_heartbeat": hb.to_dict(),
    }


__all__ = [
    "ET",
    "BAR_SECONDS",
    "G3_ATR_PCT",
    "G3_MIN_TICKS",
    "G5_STRONG_STREAK",
    "TICK_SIZE",
    "Gap",
    "GapEvent",
    "G5Heartbeat",
    "compute_ema20",
    "compute_atr",
    "detect_g3_micro_gaps",
    "detect_g4_breakout_gaps",
    "detect_g5_ema_gap_bars",
    "track_gap_lifecycle",
    "analyze_day",
    "_bar_cnt_for_ts",
    "_date_str_et",
]
