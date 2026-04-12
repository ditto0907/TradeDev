"""
Price Action Analyzer — detects support/resistance levels and market cycles
from OHLCV bar data using swing high/low analysis.

Market Cycle definitions (Wyckoff-based):
  - markup      : HH + HL sequence (uptrend)
  - markdown    : LH + LL sequence (downtrend)
  - accumulation: tight range after markdown (potential reversal up)
  - distribution: tight range after markup (potential reversal down)

Usage:
    analyzer = PriceActionAnalyzer()
    result = analyzer.get_analysis(bars_5min)
    # result = {
    #   "support_levels": [...],
    #   "resistance_levels": [...],
    #   "market_cycle": "markup",
    #   "cycle_ranges": [...],
    # }
"""
from typing import Dict, List, Tuple

import config


# ─── Swing Point Detection ────────────────────────────────────────────────────

def find_swing_points(bars: List[dict], lookback: int = None) -> Tuple[List[dict], List[dict]]:
    """
    Identify swing highs and swing lows using an N-bar lookback on each side.

    A swing high at index i: bar[i].high > bar[j].high for all j in [i-N, i+N]
    A swing low  at index i: bar[i].low  < bar[j].low  for all j in [i-N, i+N]

    Returns:
        (swing_highs, swing_lows) — each is a list of bar dicts with extra keys:
          "index": position in bars list
          "price": the swing high (bar.high) or swing low (bar.low)
    """
    if lookback is None:
        lookback = config.SR_LOOKBACK
    if len(bars) < 2 * lookback + 1:
        return [], []

    swing_highs = []
    swing_lows = []

    for i in range(lookback, len(bars) - lookback):
        window = bars[i - lookback: i + lookback + 1]
        hi = bars[i]["high"]
        lo = bars[i]["low"]

        if hi == max(b["high"] for b in window):
            swing_highs.append({**bars[i], "index": i, "price": hi})

        if lo == min(b["low"] for b in window):
            swing_lows.append({**bars[i], "index": i, "price": lo})

    return swing_highs, swing_lows


# ─── S/R Level Clustering ─────────────────────────────────────────────────────

def cluster_levels(points: List[dict], merge_pct: float = None) -> List[dict]:
    """
    Group nearby swing points into a single S/R level by merging prices that
    are within merge_pct % of each other.

    Returns a list of level dicts sorted by descending strength:
        {"price": float, "touches": int, "strength": int, "last_time": int}
    """
    if merge_pct is None:
        merge_pct = config.SR_MERGE_PCT
    if not points:
        return []

    prices = sorted(p["price"] for p in points)

    # Simple greedy merge: group prices within threshold
    groups: List[List[float]] = []
    current_group = [prices[0]]
    for price in prices[1:]:
        reference = current_group[0]
        if abs(price - reference) / reference * 100 <= merge_pct:
            current_group.append(price)
        else:
            groups.append(current_group)
            current_group = [price]
    groups.append(current_group)

    # Build level dict for each group
    levels = []
    for group in groups:
        avg_price = sum(group) / len(group)
        touches = len(group)
        # Find the most recent touch time
        last_time = max(
            (p["time"] for p in points if any(abs(p["price"] - g) / g * 100 <= merge_pct for g in group)),
            default=0,
        )
        strength = min(touches, 5)  # cap visual strength at 5
        levels.append({"price": round(avg_price, 2), "touches": touches, "strength": strength, "last_time": last_time})

    levels.sort(key=lambda x: x["touches"], reverse=True)
    return [l for l in levels if l["touches"] >= config.SR_MIN_TOUCHES]


def select_primary_levels(
    levels: List[dict],
    current_price: float,
    latest_time: int,
    is_support: bool,
) -> List[dict]:
    """
    Keep only major S/R levels by combining strength, recency, and proximity.

    Steps:
    1) Keep levels on the correct side of current price.
    2) Drop levels too far from current price.
    3) Score by touches (major), recency (secondary), proximity (secondary).
    4) Return a small ordered set nearest to current price directionally.
    """
    if not levels or current_price <= 0:
        return []

    max_distance_pct = getattr(config, "SR_MAX_DISTANCE_PCT", 1.2)
    max_levels = getattr(config, "SR_MAX_LEVELS_PER_SIDE", 4)
    if max_levels <= 0:
        return []

    # Keep levels on the expected side and inside the max distance window.
    side_levels = []
    for l in levels:
        price = l["price"]
        if is_support and price > current_price:
            continue
        if (not is_support) and price < current_price:
            continue
        dist_pct = abs(price - current_price) / current_price * 100
        if dist_pct <= max_distance_pct:
            side_levels.append(l)

    # Fallback: if the distance filter is too strict, keep side-only levels.
    if not side_levels:
        side_levels = [
            l for l in levels
            if (l["price"] <= current_price if is_support else l["price"] >= current_price)
        ]

    # Score and keep only the strongest few.
    scored = []
    for l in side_levels:
        touches = float(l.get("touches", 0))
        last_time = int(l.get("last_time", 0) or 0)
        dist_pct = abs(l["price"] - current_price) / current_price * 100

        recency = 0.0
        if latest_time > 0 and last_time > 0:
            age_ratio = max(0.0, min(1.0, (latest_time - last_time) / 86400.0))
            recency = 1.0 - age_ratio

        proximity = max(0.0, 1.0 - dist_pct / max(max_distance_pct, 1e-6))
        score = touches * 3.0 + recency + proximity
        scored.append((score, l))

    scored.sort(key=lambda x: x[0], reverse=True)
    selected = [l for _, l in scored[:max_levels]]

    # Render order: nearest first (support from high to low, resistance low to high)
    selected.sort(key=lambda x: x["price"], reverse=is_support)
    return selected


# ─── Market Structure & Cycle Detection ──────────────────────────────────────

def _range_pct(bars: List[dict]) -> float:
    """Price range as percentage of midpoint for a list of bars."""
    if not bars:
        return 0.0
    hi = max(b["high"] for b in bars)
    lo = min(b["low"] for b in bars)
    mid = (hi + lo) / 2
    return (hi - lo) / mid * 100 if mid else 0.0


def detect_market_structure(bars: List[dict]) -> Tuple[str, List[dict]]:
    """
    Determine the current market cycle and produce a list of annotated ranges.

    Algorithm:
    1. Find swing highs (SH) and swing lows (SL) on the full bar set.
    2. Walk through swing points in time order to detect HH/HL/LH/LL sequences.
    3. Label each segment:
       - 2+ HH + HL  →  markup
       - 2+ LH + LL  →  markdown
       - Tight range after markup  →  distribution
       - Tight range after markdown →  accumulation

    Returns:
        (current_cycle, cycle_ranges)
        current_cycle: one of "markup", "markdown", "accumulation", "distribution", "unknown"
        cycle_ranges: list of {"start_time", "end_time", "type"}
    """
    if len(bars) < 20:
        return "unknown", []

    swing_highs, swing_lows = find_swing_points(bars)
    if not swing_highs or not swing_lows:
        return "unknown", []

    # Merge SH and SL into a single time-ordered sequence
    all_swings = (
        [{"kind": "SH", **p} for p in swing_highs] +
        [{"kind": "SL", **p} for p in swing_lows]
    )
    all_swings.sort(key=lambda x: x["index"])

    # Walk swings to label segments
    cycle_ranges = []
    current_cycle = "unknown"

    # Track last SH and SL for comparison
    prev_sh = None
    prev_sl = None
    segment_start = bars[0]["time"] if bars else 0
    segment_type = "unknown"

    consecutive_hh_hl = 0
    consecutive_lh_ll = 0

    for swing in all_swings:
        if swing["kind"] == "SH":
            if prev_sh is not None:
                if swing["price"] > prev_sh["price"]:      # Higher High
                    consecutive_hh_hl += 1
                    consecutive_lh_ll = 0
                elif swing["price"] < prev_sh["price"]:    # Lower High
                    consecutive_lh_ll += 1
                    consecutive_hh_hl = 0
            prev_sh = swing

        elif swing["kind"] == "SL":
            if prev_sl is not None:
                if swing["price"] > prev_sl["price"]:      # Higher Low
                    consecutive_hh_hl += 1
                    consecutive_lh_ll = 0
                elif swing["price"] < prev_sl["price"]:    # Lower Low
                    consecutive_lh_ll += 1
                    consecutive_hh_hl = 0
            prev_sl = swing

        # Classify current segment when we have enough evidence
        new_type = segment_type
        if consecutive_hh_hl >= 2:
            new_type = "markup"
        elif consecutive_lh_ll >= 2:
            new_type = "markdown"

        if new_type != segment_type:
            if segment_type != "unknown":
                cycle_ranges.append({
                    "start_time": segment_start,
                    "end_time": swing["time"],
                    "type": segment_type,
                })
            segment_start = swing["time"]
            segment_type = new_type

    # Check for range/consolidation phases at the end of the bar set
    recent_bars = bars[-40:] if len(bars) >= 40 else bars
    range_size = _range_pct(recent_bars)
    RANGE_THRESHOLD = 0.5   # price range < 0.5% of midpoint → considered a range

    if range_size < RANGE_THRESHOLD:
        # Determine if we're ranging after markup (distribution) or markdown (accumulation)
        if segment_type == "markup":
            segment_type = "distribution"
        elif segment_type == "markdown":
            segment_type = "accumulation"
        else:
            segment_type = "accumulation"

    # Close the last open segment to the end of the data
    if bars:
        cycle_ranges.append({
            "start_time": segment_start,
            "end_time": bars[-1]["time"],
            "type": segment_type,
        })

    current_cycle = segment_type if segment_type != "unknown" else "unknown"
    return current_cycle, cycle_ranges


# ─── Main Analysis Entry Point ────────────────────────────────────────────────

class PriceActionAnalyzer:
    """
    Runs full price action analysis on a list of OHLCV bar dicts.
    """

    def get_analysis(self, bars: List[dict]) -> Dict:
        """
        Returns:
        {
            "support_levels": [{"price": float, "touches": int, "strength": int}, ...],
            "resistance_levels": [...],
            "market_cycle": str,
            "cycle_ranges": [{"start_time": int, "end_time": int, "type": str}, ...],
        }
        """
        if len(bars) < 2 * config.SR_LOOKBACK + 1:
            return {"support_levels": [], "resistance_levels": [], "market_cycle": "unknown", "cycle_ranges": []}

        swing_highs, swing_lows = find_swing_points(bars)
        support_levels = cluster_levels(swing_lows)
        resistance_levels = cluster_levels(swing_highs)

        # Keep only primary levels around current price.
        current_price = bars[-1]["close"] if bars else 0
        latest_time = bars[-1]["time"] if bars else 0
        support_levels = select_primary_levels(
            support_levels, current_price, latest_time, is_support=True
        )
        resistance_levels = select_primary_levels(
            resistance_levels, current_price, latest_time, is_support=False
        )

        market_cycle, cycle_ranges = detect_market_structure(bars)

        return {
            "support_levels": support_levels,
            "resistance_levels": resistance_levels,
            "market_cycle": market_cycle,
            "cycle_ranges": cycle_ranges,
        }


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import csv, sys

    # Quick test with synthetic data
    import math, random
    random.seed(42)
    bars = []
    price = 5800.0
    t = 1700000000
    for i in range(200):
        change = random.gauss(0, 5)
        o = price
        c = price + change
        h = max(o, c) + abs(random.gauss(0, 2))
        l = min(o, c) - abs(random.gauss(0, 2))
        bars.append({"time": t + i * 300, "open": round(o, 2), "high": round(h, 2),
                     "low": round(l, 2), "close": round(c, 2), "volume": 100})
        price = c

    analyzer = PriceActionAnalyzer()
    result = analyzer.get_analysis(bars)
    print("Market cycle:", result["market_cycle"])
    print("Support levels:", result["support_levels"][:3])
    print("Resistance levels:", result["resistance_levels"][:3])
    print("Cycle ranges:", result["cycle_ranges"][-3:])
