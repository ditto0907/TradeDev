"""Gap Continuation Research

Strategy: On a Gap day (today's RTH open != prior RTH close), if the first 2
bars of the RTH session both close in the gap direction (Bull pair on GapUp,
Bear pair on GapDown), enter at the 2nd bar's close. SL = pair low/high,
TP = entry +/- risk (1:1 RR). Walk subsequent same-day RTH 5min bars to
determine outcome.

Reuses strategy.signal_detector for the 2-bar pair logic and writes results
to the Orders sheet via strategy.sheet_writer.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

_HERE = os.path.dirname(os.path.abspath(__file__))
_PA = os.path.dirname(_HERE)
sys.path.insert(0, _PA)

from strategy.signal_detector import detect_signals, SignalRecord  # noqa: E402
from strategy.sheet_writer import StrategySheet  # noqa: E402

ET = ZoneInfo("America/New_York")
BASE_URL = os.environ.get("TRADEDEV_URL", "http://localhost:8000")


def fetch_rth_5min(symbol: str, day: date) -> list[dict]:
    """Fetch one trading day's RTH 5min bars."""
    from_dt = f"{day.isoformat()} 09:30"
    to_dt = f"{day.isoformat()} 16:00"
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as c:
        r = c.get(
            "/api/skill/bars",
            params={
                "symbol": symbol,
                "resolution": "5",
                "session": "RTH",
                "from_dt": from_dt,
                "to_dt": to_dt,
            },
        )
        r.raise_for_status()
        return r.json().get("bars", [])


def prior_rth_close(symbol: str, target_day: date, max_lookback: int = 7) -> Optional[float]:
    """Find the most recent prior RTH session's last bar close."""
    d = target_day - timedelta(days=1)
    for _ in range(max_lookback):
        bars = fetch_rth_5min(symbol, d)
        if bars:
            return float(bars[-1]["close"])
        d -= timedelta(days=1)
    return None


@dataclass
class GapSignal:
    date: str
    bar_ts: int
    direction: str          # Bull / Bear
    gap: str                # GapUp / GapDown
    open_price: float
    prev_close: float
    gap_pts: float
    entry: float            # close of 2nd bar (B2)
    sl: float               # pair low/high
    tp: float               # 1:1
    risk_pts: float
    outcome_1r: str         # Y / N / Open (no resolution within day)
    bars_to_resolve: int
    signal_rec: dict


def simulate_1r(direction: str, entry: float, sl: float, tp: float,
                future_bars: list[dict]) -> tuple[str, int]:
    """Return ('Y'|'N'|'Open', bars_to_resolve). Conservative: if both levels
    hit in same bar, count as SL (loss)."""
    for i, b in enumerate(future_bars, start=1):
        hi, lo = b["high"], b["low"]
        if direction == "Bull":
            hit_sl = lo <= sl
            hit_tp = hi >= tp
        else:
            hit_sl = hi >= sl
            hit_tp = lo <= tp
        if hit_sl and hit_tp:
            return "N", i  # conservative
        if hit_sl:
            return "N", i
        if hit_tp:
            return "Y", i
    return "Open", len(future_bars)


def detect_gap_continuation(symbol: str, day: date) -> list[GapSignal]:
    bars = fetch_rth_5min(symbol, day)
    if len(bars) < 2:
        return []
    prev_close = prior_rth_close(symbol, day)
    if prev_close is None:
        return []
    sigs = detect_signals(bars, prev_day_close=prev_close)
    out: list[GapSignal] = []
    for s in sigs:
        if s.bar_cnt != "B2":
            continue
        # gap direction must match continuation direction
        if not ((s.direction == "Bull" and s.gap == "GapUp") or
                (s.direction == "Bear" and s.gap == "GapDown")):
            continue
        # locate the pair (bar index of the signal bar = 2nd bar)
        idx = next((i for i, b in enumerate(bars) if int(b["time"]) == s.bar_ts), None)
        if idx is None or idx < 1:
            continue
        b1 = bars[idx - 1]
        b2 = bars[idx]
        entry = float(b2["close"])
        if s.direction == "Bull":
            sl = float(min(b1["low"], b2["low"]))
            risk = entry - sl
            tp = entry + risk
        else:
            sl = float(max(b1["high"], b2["high"]))
            risk = sl - entry
            tp = entry - risk
        if risk <= 0:
            continue
        future = bars[idx + 1:]
        outcome, n = simulate_1r(s.direction, entry, sl, tp, future)
        out.append(GapSignal(
            date=s.date,
            bar_ts=s.bar_ts,
            direction=s.direction,
            gap=s.gap,
            open_price=float(bars[0]["open"]),
            prev_close=prev_close,
            gap_pts=round(float(bars[0]["open"]) - prev_close, 2),
            entry=entry,
            sl=sl,
            tp=tp,
            risk_pts=round(risk, 2),
            outcome_1r=outcome,
            bars_to_resolve=n,
            signal_rec=asdict(s),
        ))
    return out


def trading_days(start: date, end: date) -> list[date]:
    """Return weekdays in [start, end] inclusive (holidays will return 0 bars
    and be naturally skipped downstream)."""
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def main() -> None:
    symbol = "MES"
    start = date(2026, 3, 1)
    end = date(2026, 4, 30)
    print(f"Scanning {symbol} {start} .. {end}")
    all_sigs: list[GapSignal] = []
    days = trading_days(start, end)
    for d in days:
        try:
            sigs = detect_gap_continuation(symbol, d)
        except httpx.HTTPError as e:
            print(f"  {d}: HTTP error {e}")
            continue
        if sigs:
            for s in sigs:
                print(f"  {d} {s.direction:4s} gap={s.gap_pts:+.2f}pts  "
                      f"entry={s.entry:.2f} SL={s.sl:.2f} TP={s.tp:.2f} "
                      f"risk={s.risk_pts:.2f}  → {s.outcome_1r} "
                      f"({s.bars_to_resolve} bars)")
            all_sigs.extend(sigs)

    n = len(all_sigs)
    wins = sum(1 for s in all_sigs if s.outcome_1r == "Y")
    losses = sum(1 for s in all_sigs if s.outcome_1r == "N")
    opens = sum(1 for s in all_sigs if s.outcome_1r == "Open")
    closed = wins + losses
    wr = (wins / closed * 100.0) if closed else 0.0
    print(f"\nTotal signals: {n}  | Wins: {wins}  Losses: {losses}  Open(EOD): {opens}")
    print(f"1:1 RR Win Rate (closed only): {wr:.1f}%")

    # Write to sheet
    print("\nWriting to Orders sheet ...")
    sheet = StrategySheet()
    sheet.authenticate()
    sheet.write_outcome_headers()

    records = [s.signal_rec for s in all_sigs]
    rows = sheet.append_signals(records)
    # Outcomes -> column R
    items = []
    for row, s in zip(rows, all_sigs):
        r1 = "Y" if s.outcome_1r == "Y" else ("N" if s.outcome_1r == "N" else "Open")
        items.append({"row": row, "r1": r1})
    sheet.bulk_update_outcomes(items)
    print(f"Wrote {len(rows)} rows to sheet.  Win rate = {wr:.1f}% ({wins}/{closed})")


if __name__ == "__main__":
    main()
