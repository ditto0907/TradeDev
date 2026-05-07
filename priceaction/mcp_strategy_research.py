"""MCP Server — Strategy Research

Exposes signal detection and Google Sheets read/write so an LLM can:
  1. Scan a date range for 2-consecutive-bar signals
  2. Persist the quantified signal characteristics into a Google Sheet
  3. After analyzing each signal's context, write back Pattern/Context/SR
  4. List existing signals for review

Requires:
  - TradeDev backend running at TRADEDEV_URL (default http://localhost:8000)
  - priceaction/credentials/service_account.json
  - Sheet shared with the service account email

Usage (stdio):
    python mcp_strategy_research.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP

# Make sibling modules importable when run from any cwd
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from strategy.signal_detector import detect_signals  # noqa: E402
from strategy.sheet_writer import StrategySheet  # noqa: E402

logger = logging.getLogger("mcp_strategy_research")

BASE_URL = os.environ.get("TRADEDEV_URL", "http://localhost:8000")
TIMEOUT = 30.0
ET = ZoneInfo("America/New_York")

mcp = FastMCP(
    "strategy-research",
    instructions="""You are a trading-strategy research assistant.

WORKFLOW:
1. User asks to research signals in a date range, e.g. "MES 2026-04-01 to 2026-04-15"
2. Call `detect_signals_in_range` to find all 2-consecutive-bar signals (Bull or Bear)
3. Call `write_signals_to_sheet` to persist them (data columns B–I auto-filled)
4. For EACH signal, fetch context bars via `get_context_bars` (signal + lookback)
5. Following Al Brooks PA methodology, identify:
   - Pattern (Wedge / DB / DT / BO / Climax / etc.)
   - Minor or Major reversal
   - Leg count (1st / 2nd / 3rd …)
   - Context (current market cycle phase, e.g. "空头通道底部")
   - SR (Y/N — is there a near S/R level?) and SR Detail (specific level)
6. Call `update_signal_analysis` to write J–O back per row
7. Stop and let the human review Sheet columns P (背景支持) and Q (支持理由)

RULES:
- Never sell at TR bottom unless consecutive large bear bars
- Always identify magnets: prior day H/L, MM targets, round numbers
- Cite bar characteristics: body size, tails, overlap, gaps
- Use PA abbreviations: TR, TTR, BO, FT, BC, SC, MTR, MM, OR, HH, HL, LH, LL, DT, DB, ii, oo
""",
)


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)


def _ts_for_date_rth(date_str: str) -> tuple[int, int]:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    end = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)
    return int(start.timestamp()), int(end.timestamp())


def _fetch_bars(
    symbol: str,
    resolution: str,
    from_dt: str,
    to_dt: str,
    session: str = "RTH",
) -> list[dict]:
    """Fetch bars from backend; returns chronological list of dicts."""
    params = {
        "symbol": symbol,
        "resolution": resolution,
        "session": session,
        "from_dt": from_dt,
        "to_dt": to_dt,
    }
    with _client() as c:
        resp = c.get("/api/skill/bars", params=params)
        resp.raise_for_status()
        data = resp.json()
    return data.get("bars", [])


def _prev_day_close(symbol: str, date_str: str) -> Optional[float]:
    """Get the prior trading day's RTH close (best-effort)."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    # Look back up to 5 calendar days to skip weekends/holidays
    for days in range(1, 6):
        prev = d - timedelta(days=days)
        ds = prev.strftime("%Y-%m-%d")
        try:
            bars = _fetch_bars(symbol, "1D", ds, ds, session="RTH")
            if bars:
                return float(bars[-1]["close"])
        except Exception as e:
            logger.debug("prev_day_close fetch error %s: %s", ds, e)
    return None


# ─── Tool 1: detect_signals_in_range ───────────────────────────────────────
@mcp.tool()
def detect_signals_in_range(
    symbol: str = "MES",
    from_date: str = "",
    to_date: str = "",
    before_time: str = "",
) -> str:
    """Scan a date range for 2-consecutive-bar signals on 5min RTH bars.

    Args:
        symbol: Symbol token, e.g. "MES", "MES@CONT_FRONT", or "MES@202506".
        from_date: "YYYY-MM-DD" (inclusive).
        to_date:   "YYYY-MM-DD" (inclusive).
        before_time: Optional ET time cutoff "HH:MM". When set, only signals
                     whose bar_ts falls strictly before that time of day are
                     kept.  e.g. "12:00" keeps morning-session signals only.

    Returns:
        JSON: {"count": N, "signals": [SignalRecord, ...]}
        SignalRecord fields: date, bar_cnt, bar_ts, direction, gap,
        signal_strength, coh_l, overlapping, overlapping_pct,
        pb_bars, pb_strength, ft.
    """
    if not from_date or not to_date:
        return json.dumps({"error": "from_date and to_date are required"})

    bars = _fetch_bars(
        symbol,
        resolution="5",
        from_dt=from_date,
        to_dt=to_date,
        session="RTH",
    )
    # Use prev day close from BEFORE from_date for gap detection.
    prev_close = _prev_day_close(symbol, from_date)
    sigs = detect_signals(bars, prev_day_close=prev_close)

    # Optional time-of-day cutoff filter
    if before_time:
        try:
            cutoff_h, cutoff_m = map(int, before_time.split(":"))
            filtered = []
            for s in sigs:
                bar_dt = datetime.fromtimestamp(s.bar_ts, tz=ET)
                if (bar_dt.hour, bar_dt.minute) < (cutoff_h, cutoff_m):
                    filtered.append(s)
            sigs = filtered
        except ValueError:
            return json.dumps({"error": f"Invalid before_time '{before_time}', use 'HH:MM'"})

    return json.dumps(
        {"count": len(sigs), "signals": [s.to_dict() for s in sigs]},
        ensure_ascii=False,
    )


# ─── Tool 2: write_signals_to_sheet ────────────────────────────────────────
@mcp.tool()
def write_signals_to_sheet(signals_json: str) -> str:
    """Append signal records to the Google Sheet (Orders tab).

    Args:
        signals_json: JSON array of SignalRecord dicts (output of
                      detect_signals_in_range under "signals" key).

    Returns:
        JSON: {"written": N, "rows": [3, 4, 5, ...]}
              The "rows" list maps each signal (in order) to its absolute
              sheet row number — pass these to update_signal_analysis later.
    """
    sigs = json.loads(signals_json)
    if isinstance(sigs, dict) and "signals" in sigs:
        sigs = sigs["signals"]
    if not isinstance(sigs, list):
        return json.dumps({"error": "signals_json must be a list or {signals:[]}"})
    sheet = StrategySheet()
    rows = sheet.append_signals(sigs)
    return json.dumps({"written": len(rows), "rows": rows})


# ─── Tool 3: get_context_bars ──────────────────────────────────────────────
@mcp.tool()
def get_context_bars(
    symbol: str,
    signal_ts: int,
    lookback_bars: int = 80,
    include_d1: bool = True,
) -> str:
    """Fetch bars surrounding a signal for LLM context analysis.

    Returns 5min RTH bars from `lookback_bars` * 5min before the signal up
    through the signal bar itself, optionally plus the previous day's 1D bar.

    Args:
        symbol: Symbol token, same form as detect_signals_in_range.
        signal_ts: Unix ts (seconds) of the signal bar.
        lookback_bars: How many 5min bars before the signal to include.
        include_d1: If True, also include the prior trading day's 1D bar.

    Returns:
        JSON: {"5min": [...], "1D": [...]}.
    """
    sig_dt = datetime.fromtimestamp(signal_ts, tz=timezone.utc).astimezone(ET)
    lookback_min = lookback_bars * 5
    from_dt = (sig_dt - timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M")
    to_dt = sig_dt.strftime("%Y-%m-%d %H:%M")
    bars_5m = _fetch_bars(symbol, "5", from_dt, to_dt, session="RTH")
    out: dict = {"5min": bars_5m}
    if include_d1:
        # Get prior 5 days of 1D bars for context
        end_d = sig_dt.date()
        from_d = (end_d - timedelta(days=10)).strftime("%Y-%m-%d")
        to_d = end_d.strftime("%Y-%m-%d")
        try:
            out["1D"] = _fetch_bars(symbol, "1D", from_d, to_d, session="RTH")
        except Exception as e:
            logger.debug("1D fetch failed: %s", e)
            out["1D"] = []
    return json.dumps(out, ensure_ascii=False)


# ─── Tool 4: update_signal_analysis ────────────────────────────────────────
@mcp.tool()
def update_signal_analysis(
    row: int,
    pattern: Optional[str] = None,
    minor_major: Optional[str] = None,
    leg_cnt: Optional[str] = None,
    context: Optional[str] = None,
    sr: Optional[str] = None,
    sr_detail: Optional[str] = None,
) -> str:
    """Write LLM analysis back to columns J–O of a single sheet row.

    Args:
        row: The absolute sheet row number returned by write_signals_to_sheet.
        pattern: e.g. "Bull BO", "DB", "Wedge"
        minor_major: "Minor" | "Major"
        leg_cnt: "1st" | "2nd" | "3rd"
        context: Free-text description of market cycle phase.
        sr: "Y" | "N"
        sr_detail: e.g. "PDH @ 5210, OR low @ 5180"

    Returns:
        JSON: {"ok": true, "row": <row>}
    """
    sheet = StrategySheet()
    sheet.update_analysis(
        row=row,
        pattern=pattern,
        minor_major=minor_major,
        leg_cnt=leg_cnt,
        context=context,
        sr=sr,
        sr_detail=sr_detail,
    )
    return json.dumps({"ok": True, "row": row})


# ─── Tool 5: list_signals_from_sheet ───────────────────────────────────────
@mcp.tool()
def list_signals_from_sheet(only_unanalyzed: bool = False) -> str:
    """Read all signal rows currently in the Sheet.

    Args:
        only_unanalyzed: If True, only return rows where Pattern column is empty.

    Returns:
        JSON array of row dicts. Each entry includes "_row" (absolute row #)
        plus all sub-header columns.
    """
    sheet = StrategySheet()
    rows = sheet.read_all_signals()
    if only_unanalyzed:
        rows = [r for r in rows if not r.get("Pattern")]
    return json.dumps(rows, ensure_ascii=False)


# ─── Tool 6: calculate_outcomes ────────────────────────────────────────────
@mcp.tool()
def calculate_outcomes(
    signals_json: str,
    rows_json: str,
    symbol: str = "MES",
    from_date: str = "",
    to_date: str = "",
) -> str:
    """Backtest 1:1 and 2:1 RR outcomes for a list of signals.

    Stop loss = first bar's low (Bull) / high (Bear).
    Entry      = close of the signal bar (2nd bar of the 2-bar pattern).
    Outcome    = Y if target hit before stop within the same trading day,
                 N otherwise.  Results are written to Sheet columns R (1:1)
                 and S (2:1) in a single API call.

    Args:
        signals_json: JSON array of SignalRecord dicts (same format as
                      detect_signals_in_range output["signals"]).
        rows_json:    JSON array of absolute sheet row numbers (same order
                      as signals_json), e.g. [4,5,6,...].
        symbol:       Symbol to fetch 5min context bars for.
        from_date:    Start of bar range to load, "YYYY-MM-DD".
        to_date:      End of bar range to load, "YYYY-MM-DD".

    Returns:
        JSON: {"total": N, "wins_1r": W1, "wins_2r": W2,
               "win_pct_1r": 0.xx, "win_pct_2r": 0.xx,
               "outcomes": [{"row":R, "r1":"Y"|"N", "r2":"Y"|"N"}, ...]}
    """
    sigs = json.loads(signals_json)
    if isinstance(sigs, dict) and "signals" in sigs:
        sigs = sigs["signals"]
    rows = json.loads(rows_json)
    if len(sigs) != len(rows):
        return json.dumps({"error": "signals and rows must have the same length"})
    if not from_date or not to_date:
        return json.dumps({"error": "from_date and to_date are required"})

    # Load a wider bar window (10 days before start for b_prev lookup safety)
    d_start = datetime.strptime(from_date, "%Y-%m-%d") - timedelta(days=1)
    wider_start = d_start.strftime("%Y-%m-%d")
    all_5m = _fetch_bars(symbol, "5", wider_start, to_date, session="RTH")

    # Index by ts → bar dict, and ts → bars-for-the-same-day list
    ts_to_bar: dict[int, dict] = {int(b["time"]): b for b in all_5m}
    # Group bars by ET date string
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
    from collections import defaultdict as _dd
    day_bars: dict[str, list[dict]] = _dd(list)
    for b in all_5m:
        dt = datetime.fromtimestamp(b["time"], tz=_ET)
        day_bars[dt.strftime("%Y%m%d")].append(b)

    def _outcome(sig: dict, ratio: float) -> str:
        sig_ts = int(sig["bar_ts"])
        prev_ts = sig_ts - 300  # 5 min before = first bar of pair
        b_curr = ts_to_bar.get(sig_ts)
        b_prev = ts_to_bar.get(prev_ts)
        if not b_curr or not b_prev:
            return "N"
        entry = b_curr["close"]
        direction = sig["direction"]
        if direction == "Bull":
            sl = b_prev["low"]
            risk = entry - sl
            if risk <= 0:
                return "N"
            tp = entry + risk * ratio
        else:
            sl = b_prev["high"]
            risk = sl - entry
            if risk <= 0:
                return "N"
            tp = entry - risk * ratio

        # Scan bars strictly after signal, same day only
        date_str = sig["date"]
        after = [b for b in day_bars[date_str] if int(b["time"]) > sig_ts]
        for b in after:
            if direction == "Bull":
                if b["high"] >= tp:
                    return "Y"
                if b["low"] <= sl:
                    return "N"
            else:
                if b["low"] <= tp:
                    return "Y"
                if b["high"] >= sl:
                    return "N"
        return "N"  # end of day, not triggered

    outcomes = []
    wins_1r = wins_2r = 0
    for sig, row in zip(sigs, rows):
        r1 = _outcome(sig, 1.0)
        r2 = _outcome(sig, 2.0)
        outcomes.append({"row": row, "r1": r1, "r2": r2})
        if r1 == "Y":
            wins_1r += 1
        if r2 == "Y":
            wins_2r += 1

    total = len(outcomes)
    # Write to sheet
    sheet = StrategySheet()
    sheet.write_outcome_headers()
    sheet.bulk_update_outcomes(outcomes)

    return json.dumps({
        "total": total,
        "wins_1r": wins_1r,
        "wins_2r": wins_2r,
        "win_pct_1r": round(wins_1r / total, 3) if total else 0,
        "win_pct_2r": round(wins_2r / total, 3) if total else 0,
        "outcomes": outcomes,
    }, ensure_ascii=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run()
