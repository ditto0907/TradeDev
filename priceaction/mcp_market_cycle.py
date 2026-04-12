"""
MCP Server — Market Cycle Analysis (Al Brooks Price Action)

Exposes TradeDev trading terminal's K-line data and analysis writeback
as MCP tools so that any MCP-compatible LLM agent can:
  1. Read OHLCV bars (RTH/ETH, multi-timeframe)
  2. Write structured analysis + chart annotations
  3. Query existing analyses
  4. Manage analysis visibility

Requires the TradeDev backend running at TRADEDEV_URL (default http://localhost:8000).

Usage (stdio):
    python mcp_market_cycle.py
"""

import os
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = os.environ.get("TRADEDEV_URL", "http://localhost:8000")
TIMEOUT = 15.0

mcp = FastMCP(
    "market-cycle-analysis",
    instructions="""You are an Al Brooks Price Action analysis expert.
Use the provided tools to read K-line data from the trading terminal,
perform market cycle analysis following Al Brooks methodology strictly,
and write annotated results back to the chart.

## Core Rules (Al Brooks Methodology)
- Never sell at TR bottom unless consecutive large bear bars appear
- Strong BO: enter on close or small pullback (1-2 bars)
- Trade channels in trend direction, unless MTR on 2nd attempt
- Always identify magnets: prior day H/L, MM targets, round numbers
- Cite bar characteristics: body size, tails, overlap, gaps
- Use PA abbreviations: TR, TTR, BO, FT, BC, SC, MTR, MM, OR, HH, HL, LH, LL, DT, DB, ii, oo, ioi

## Output Format
Summary must use concise bullet points:
• Phase: [TR / BO / BC / Bear Channel / MTR]
• Context: [D1 context if available]
• OR: [H/L of opening range]
• Key levels: [S/R with price]
• Magnets: [prior H/L, MM targets]
• Bias: [Bull/Bear/Neutral] — [reasoning citing bar characteristics]

## Annotation Types
- range: Rectangle on chart (Opening Range, TR, legs, channels). Needs start_time, end_time, price_high, price_low.
- hline: Horizontal line (S/R, MM targets). Needs price, start_time.
- label: Text at specific bar/price (BO point, reversal). Needs start_time, price.

## Color Palette (auto-applied by label name)
Opening Range → Blue | Bear Leg/Breakout → Red | Bull Leg/Breakout → Green
Reversal/Double Bottom/Top → Orange | Trading Range/TTR → Gray
Channel → Purple | Measured Move → Cyan | Climax → Dark Red

## Analysis Procedure
1. Fetch bars (usually 5min RTH for intraday)
2. Optionally fetch 1D for multi-TF context
3. Identify: OR, legs, TR, BO, MM, reversals
4. Classify current phase
5. Write summary + annotations via save_analysis tool
""",
)


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)


def _ts_for_date_rth(date_str: str) -> tuple[int, int]:
    """Convert 'YYYY-MM-DD' to RTH start/end Unix timestamps (ET)."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, 9, 30, tzinfo=et)
    end = datetime(d.year, d.month, d.day, 16, 0, tzinfo=et)
    return int(start.timestamp()), int(end.timestamp())


# ---------------------------------------------------------------------------
# Tool 1: Read K-line Bars
# ---------------------------------------------------------------------------
@mcp.tool()
def get_bars(
    symbol: str = "MES",
    resolution: str = "5",
    session: str = "RTH",
    date: Optional[str] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    limit: int = 200,
) -> str:
    """Read OHLCV K-line bars from the trading terminal.

    Args:
        symbol: Futures contract symbol. MES (default), MNQ, NK225MC, MGC.
        resolution: Bar timeframe. "5" (5min, default), "15", "30", "60", "1D".
        session: "RTH" (09:30-16:00 ET, default) or "ETH" (all hours).
        date: Shortcut for a single RTH day, e.g. "2026-04-09". Overrides from_ts/to_ts.
        from_ts: Start Unix timestamp (seconds). Optional.
        to_ts: End Unix timestamp (seconds). Optional.
        limit: Max bars to return (default 200).

    Returns:
        JSON with symbol, resolution, session, count, and bars array
        [{time, open, high, low, close, volume}, ...].
        Time is Unix seconds. Bars are chronological (oldest first).
    """
    params: dict = {
        "symbol": symbol,
        "resolution": resolution,
        "session": session,
    }

    if date:
        ts_from, ts_to = _ts_for_date_rth(date)
        params["from"] = ts_from
        params["to"] = ts_to
    else:
        if from_ts is not None:
            params["from"] = from_ts
        if to_ts is not None:
            params["to"] = to_ts

    with _client() as c:
        resp = c.get("/api/skill/bars", params=params)
        resp.raise_for_status()
        data = resp.json()

    bars = data.get("bars", [])
    if limit and len(bars) > limit:
        bars = bars[-limit:]
        data["bars"] = bars
        data["count"] = len(bars)
        data["truncated"] = True

    return json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 2: Save Analysis + Annotations
# ---------------------------------------------------------------------------
@mcp.tool()
def save_analysis(
    symbol: str,
    timeframe: str,
    session: str,
    bar_from: int,
    bar_to: int,
    summary: str,
    annotations: str,
) -> str:
    """Save a market cycle analysis with chart annotations.

    The analysis is persisted to the database and instantly rendered on
    the chart via WebSocket broadcast.

    Args:
        symbol: e.g. "MES"
        timeframe: e.g. "5" (5min), "60", "1D"
        session: "RTH" or "ETH"
        bar_from: Unix timestamp (seconds) of the first bar in scope
        bar_to: Unix timestamp (seconds) of the last bar in scope
        summary: Concise analysis summary using Al Brooks PA terminology.
                 Use bullet points with abbreviations (TR, BO, MM, etc.).
        annotations: JSON string — array of annotation objects.
                     Each object has: label (str), type ("range"|"hline"|"label"),
                     start_time (int), and type-specific fields:
                     - range: end_time, price_high, price_low
                     - hline: price, style ("solid"|"dashed"|"dotted")
                     - label: price
                     Optional: color (CSS rgba string).

    Returns:
        JSON with success status and the assigned analysis ID.
    """
    ann_list = json.loads(annotations)

    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "session": session,
        "bar_from": bar_from,
        "bar_to": bar_to,
        "summary": summary,
        "annotations": ann_list,
    }

    with _client() as c:
        resp = c.post("/api/skill/analysis", json=payload)
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 3: List Existing Analyses
# ---------------------------------------------------------------------------
@mcp.tool()
def list_analyses(
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    active_only: bool = False,
) -> str:
    """Query existing market cycle analyses from the database.

    Args:
        symbol: Filter by symbol (e.g. "MES"). None = all.
        timeframe: Filter by timeframe (e.g. "5"). None = all.
        active_only: If True, return only analyses visible on chart.

    Returns:
        JSON array of analysis records, each containing id, symbol,
        timeframe, session, created_at, summary, annotations, active.
    """
    params: dict = {"active_only": str(active_only).lower()}
    if symbol:
        params["symbol"] = symbol
    if timeframe:
        params["timeframe"] = timeframe

    with _client() as c:
        resp = c.get("/api/skill/analyses", params=params)
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 4: Toggle Analysis Visibility
# ---------------------------------------------------------------------------
@mcp.tool()
def toggle_analysis(analysis_id: int, active: bool) -> str:
    """Toggle an analysis's visibility on the chart.

    Args:
        analysis_id: The ID of the analysis to toggle.
        active: True to show on chart, False to hide.

    Returns:
        JSON with success status.
    """
    with _client() as c:
        resp = c.put(
            f"/api/skill/analyses/{analysis_id}/active",
            params={"active": str(active).lower()},
        )
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 5: Delete Analysis
# ---------------------------------------------------------------------------
@mcp.tool()
def delete_analysis(analysis_id: int) -> str:
    """Permanently delete an analysis record and remove its annotations from the chart.

    Args:
        analysis_id: The ID of the analysis to delete.

    Returns:
        JSON with success status.
    """
    with _client() as c:
        resp = c.delete(f"/api/skill/analyses/{analysis_id}")
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()
