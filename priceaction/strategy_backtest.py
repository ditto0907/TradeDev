"""
IBS 2-Bar Strategy Backtest Engine.

Signal Definition:
  IBS = (Close − Low) / (High − Low)

  Long  — Bar 1 bullish (Close ≥ Open)  AND Bar 2 IBS ≥ threshold
  Short — Bar 1 bearish (Close ≤ Open)  AND Bar 2 IBS ≤ (1 − threshold)

Entry  : Bar 2 close (market-on-close simulation)
Stop   : Entry ± 2-bar range  (max(H1,H2) − min(L1,L2))
Target : Entry ± stop_distance  (1:1 R:R)

Market Context Filter (prevents look-ahead bias via rolling window):
  - Price near support  (within SR_PROXIMITY_PCT) → block short
  - Price near resistance (within SR_PROXIMITY_PCT) → block long
  - Mid-channel / breakout → allow
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import config
import db
from price_action_analyzer import (
    find_swing_points,
    cluster_levels,
    select_primary_levels,
)

logger = logging.getLogger(__name__)


# ─── IBS helpers ─────────────────────────────────────────────────────────────

def compute_ibs(bar: dict) -> float:
    """IBS = (Close − Low) / (High − Low).  Returns 0.5 if range is zero."""
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return 0.5
    return (bar["close"] - bar["low"]) / rng


def _is_bullish(bar: dict) -> bool:
    return bar["close"] >= bar["open"]


def _is_bearish(bar: dict) -> bool:
    return bar["close"] <= bar["open"]


# ─── Context filter ───────────────────────────────────────────────────────────

def _context_filter(bars_window: List[dict], entry_price: float, direction: str,
                    proximity_pct: float) -> tuple[bool, str]:
    """
    Returns (allowed: bool, reason: str).
    Uses the provided rolling window of bars to compute S/R levels without
    look-ahead bias.
    """
    if len(bars_window) < 2 * config.SR_LOOKBACK + 1:
        return True, ""

    swing_highs, swing_lows = find_swing_points(bars_window)
    support_levels = cluster_levels(swing_lows)
    resistance_levels = cluster_levels(swing_highs)

    current_price = bars_window[-1]["close"]
    latest_time = bars_window[-1]["time"]
    support_levels = select_primary_levels(
        support_levels, current_price, latest_time, is_support=True
    )
    resistance_levels = select_primary_levels(
        resistance_levels, current_price, latest_time, is_support=False
    )

    def near_level(price: float, levels: list, pct: float) -> Optional[float]:
        for lvl in levels:
            if abs(price - lvl["price"]) / max(price, 1e-9) * 100 <= pct:
                return lvl["price"]
        return None

    if direction == "short":
        sup = near_level(entry_price, support_levels, proximity_pct)
        if sup is not None:
            return False, f"near support {sup:.2f}"
    elif direction == "long":
        res = near_level(entry_price, resistance_levels, proximity_pct)
        if res is not None:
            return False, f"near resistance {res:.2f}"

    return True, ""


# ─── Main backtest engine ─────────────────────────────────────────────────────

def run_backtest(
    symbol: str = "MES",
    timeframe: str = "5min",
    from_ts: int = 0,
    to_ts: int = 9_999_999_999,
    ibs_threshold: float = None,
    rr_ratio: float = 1.0,
    use_context_filter: bool = True,
) -> dict:
    """
    Run the IBS 2-bar strategy backtest over stored bars.

    Returns a dict:
    {
        "backtest_id": str,
        "summary": { ... },
        "trades": [ ... ],
        "params": { ... },
    }
    """
    if ibs_threshold is None:
        ibs_threshold = config.IBS_THRESHOLD

    proximity_pct = config.IBS_SR_PROXIMITY_PCT
    context_lookback = config.IBS_CONTEXT_LOOKBACK
    tick_value = config.MES_TICK_VALUE

    # ── Load bars from DB ──────────────────────────────────────────────────────
    all_bars = db.get_bars(symbol, timeframe, from_ts=from_ts, to_ts=to_ts)
    if len(all_bars) < 2:
        logger.warning("Not enough bars to run backtest (%d bars)", len(all_bars))
        return {
            "backtest_id": None,
            "summary": _empty_summary(0, "db"),
            "trades": [],
            "params": _build_params(symbol, timeframe, from_ts, to_ts,
                                    ibs_threshold, rr_ratio, use_context_filter),
        }

    actual_from = all_bars[0]["time"]
    actual_to   = all_bars[-1]["time"]

    trades: List[dict] = []
    open_trade: Optional[dict] = None
    backtest_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    for i in range(1, len(all_bars)):
        bar1 = all_bars[i - 1]
        bar2 = all_bars[i]

        # ── If a trade is open, check for exit ────────────────────────────────
        if open_trade is not None:
            direction    = open_trade["direction"]
            stop_price   = open_trade["stop_price"]
            target_price = open_trade["target_price"]
            entry_price  = open_trade["entry_price"]

            # Check high/low of current bar for target/stop hit
            hit_target = False
            hit_stop   = False
            if direction == "long":
                if bar2["high"] >= target_price:
                    hit_target = True
                elif bar2["low"] <= stop_price:
                    hit_stop = True
            else:  # short
                if bar2["low"] <= target_price:
                    hit_target = True
                elif bar2["high"] >= stop_price:
                    hit_stop = True

            if hit_target or hit_stop:
                exit_price = target_price if hit_target else stop_price
                if direction == "long":
                    pnl = (exit_price - entry_price) * tick_value
                else:
                    pnl = (entry_price - exit_price) * tick_value

                open_trade["exit_time"]  = bar2["time"]
                open_trade["exit_price"] = exit_price
                open_trade["pnl"]        = round(pnl, 2)
                open_trade["outcome"]    = "win" if hit_target else "loss"
                open_trade["bars_held"]  = i - open_trade["_entry_idx"]
                trades.append(open_trade)
                open_trade = None

            # Skip signal check while trade is open
            continue

        # ── Check IBS signal on (bar1, bar2) ─────────────────────────────────
        ibs2 = compute_ibs(bar2)
        direction = None

        if _is_bullish(bar1) and ibs2 >= ibs_threshold:
            direction = "long"
        elif _is_bearish(bar1) and ibs2 <= (1.0 - ibs_threshold):
            direction = "short"

        if direction is None:
            continue

        entry_price   = bar2["close"]
        stop_distance = (max(bar1["high"], bar2["high"]) -
                         min(bar1["low"],  bar2["low"]))
        if stop_distance <= 0:
            continue

        if direction == "long":
            stop_price   = entry_price - stop_distance
            target_price = entry_price + stop_distance * rr_ratio
        else:
            stop_price   = entry_price + stop_distance
            target_price = entry_price - stop_distance * rr_ratio

        # ── Context filter ────────────────────────────────────────────────────
        context_pass   = 1
        context_reason = ""
        if use_context_filter:
            window_start = max(0, i + 1 - context_lookback)
            window = all_bars[window_start: i + 1]
            allowed, reason = _context_filter(
                window, entry_price, direction, proximity_pct
            )
            if not allowed:
                context_pass   = 0
                context_reason = reason

        trade_record = {
            "backtest_id":    backtest_id,
            "symbol":         symbol,
            "timeframe":      timeframe,
            "direction":      direction,
            "entry_time":     bar2["time"],
            "entry_price":    entry_price,
            "exit_time":      None,
            "exit_price":     None,
            "stop_price":     round(stop_price, 4),
            "target_price":   round(target_price, 4),
            "pnl":            None,
            "outcome":        "open",
            "bars_held":      0,
            "signal_ibs":     round(ibs2, 4),
            "context_pass":   context_pass,
            "context_reason": context_reason,
            "created_at":     created_at,
            "_entry_idx":     i,   # internal — stripped before DB insert
        }

        if context_pass == 0:
            # Filtered-out signal — record but do not open position
            trades.append(trade_record)
        else:
            open_trade = trade_record

    # ── Close any remaining open trade at last bar ────────────────────────────
    if open_trade is not None:
        last_bar = all_bars[-1]
        open_trade["exit_time"]  = last_bar["time"]
        open_trade["exit_price"] = last_bar["close"]
        if open_trade["direction"] == "long":
            pnl = (last_bar["close"] - open_trade["entry_price"]) * tick_value
        else:
            pnl = (open_trade["entry_price"] - last_bar["close"]) * tick_value
        open_trade["pnl"]    = round(pnl, 2)
        open_trade["outcome"] = "open"
        open_trade["bars_held"] = len(all_bars) - 1 - open_trade["_entry_idx"]
        trades.append(open_trade)

    # ── Strip internal field before DB storage ────────────────────────────────
    for t in trades:
        t.pop("_entry_idx", None)

    # ── Compute summary ───────────────────────────────────────────────────────
    summary = _compute_summary(trades, len(all_bars), "db")

    # ── Persist to DB ─────────────────────────────────────────────────────────
    params = _build_params(symbol, timeframe, actual_from, actual_to,
                           ibs_threshold, rr_ratio, use_context_filter)
    db.save_backtest(
        backtest_id=backtest_id,
        symbol=symbol,
        timeframe=timeframe,
        from_ts=actual_from,
        to_ts=actual_to,
        created_at=created_at,
        params_json=json.dumps(params),
        summary_json=json.dumps(summary),
        trade_count=len([t for t in trades if t["context_pass"] == 1]),
    )
    db.save_strategy_trades(trades)

    logger.info(
        "Backtest %s: %d bars, %d trades (filtered: %d), win_rate=%.1f%%, pnl=$%.2f",
        backtest_id, len(all_bars), summary["total"], summary["filtered_count"],
        summary["win_rate"] * 100, summary["total_pnl"],
    )

    return {
        "backtest_id": backtest_id,
        "summary": summary,
        "trades": trades,
        "params": params,
    }


# ─── Summary helpers ──────────────────────────────────────────────────────────

def _compute_summary(trades: List[dict], bars_used: int, data_source: str) -> dict:
    executed = [t for t in trades if t["context_pass"] == 1]
    filtered = [t for t in trades if t["context_pass"] == 0]

    closed = [t for t in executed if t["outcome"] in ("win", "loss")]
    wins   = [t for t in closed if t["outcome"] == "win"]
    losses = [t for t in closed if t["outcome"] == "loss"]

    total_pnl  = sum(t["pnl"] for t in closed if t["pnl"] is not None)
    gross_win  = sum(t["pnl"] for t in wins   if t["pnl"] is not None)
    gross_loss = abs(sum(t["pnl"] for t in losses if t["pnl"] is not None))

    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    # Max drawdown
    running = 0.0
    peak    = 0.0
    max_dd  = 0.0
    for t in closed:
        if t["pnl"] is not None:
            running += t["pnl"]
            if running > peak:
                peak = running
            dd = peak - running
            if dd > max_dd:
                max_dd = dd

    return {
        "total":           len(executed),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        round(len(wins) / len(closed), 4) if closed else 0.0,
        "total_pnl":       round(total_pnl, 2),
        "avg_win":         round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss":        round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor":   round(profit_factor, 4) if profit_factor != float("inf") else 999.0,
        "max_drawdown":    round(-max_dd, 2),
        "filtered_count":  len(filtered),
        "bars_used":       bars_used,
        "data_source":     data_source,
    }


def _empty_summary(bars_used: int, data_source: str) -> dict:
    return {
        "total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
        "total_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "profit_factor": 0.0, "max_drawdown": 0.0,
        "filtered_count": 0, "bars_used": bars_used, "data_source": data_source,
    }


def _build_params(symbol, timeframe, from_ts, to_ts,
                  ibs_threshold, rr_ratio, use_context_filter) -> dict:
    return {
        "symbol":             symbol,
        "timeframe":          timeframe,
        "from_ts":            from_ts,
        "to_ts":              to_ts,
        "ibs_threshold":      ibs_threshold,
        "rr_ratio":           rr_ratio,
        "use_context_filter": use_context_filter,
    }
