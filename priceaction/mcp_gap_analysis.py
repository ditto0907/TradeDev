"""MCP Server — Gap Analysis

Provides structural gap analysis (G3/G4/G5) based on intraday bars,
with lifecycle tracking and analysis writeback.

Usage (stdio):
    python mcp_gap_analysis.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP

# Make sibling modules importable when run from any cwd
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from strategy.gap_detector import analyze_day, TICK_SIZE  # noqa: E402

BASE_URL = os.environ.get("TRADEDEV_URL", "http://localhost:8000")
TIMEOUT = 15.0
ET = ZoneInfo("America/New_York")
OR_BAR_COUNT = 6  # first 30 minutes on 5min bars

mcp = FastMCP(
    "gap-analysis",
    instructions="""You are an Al Brooks Gap Analysis specialist.

Use the tools to detect structural gaps (G3/G4/G5), then provide the qualitative
second layer:
1) Leg-count + EMA stretch to classify each key gap as Breakout / Measuring / Exhaustion.
2) For Measuring gaps, project measured-move (MM) target from the gap context.
3) Build bull/bear scoreboard:
   - Trend-side evidence vs counter-trend evidence
   - Conclude AIL / AIS / Neutral
   - Propose day-type hypothesis and trapped-side stop clusters.
4) Output structure:
   - Key levels + overnight range
   - Gap list + lifecycle events
   - G5 heartbeat (current / max / EMA-touch events)
   - Top-3 actionable narrative gaps
   - Scoreboard conclusion and trade implications

Annotation palette:
- G3 range: green/red semi-transparent rectangle
- G4 level: dashed hline
- G5 threshold: label
- REVERSED event: label (exception: keep this even when gap is no longer alive)
- Chart should show only alive gaps by default.
""",
)


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)


def _err(msg: str) -> str:
    return json.dumps({"error": str(msg)}, ensure_ascii=False)


def _request(method: str, path: str, **kwargs) -> tuple[Optional[dict], Optional[str]]:
    try:
        with _client() as c:
            resp = c.request(method, path, **kwargs)
            resp.raise_for_status()
            return resp.json(), None
    except httpx.ConnectError:
        return None, _err(f"Cannot reach TradeDev backend at {BASE_URL}. Is the server running?")
    except httpx.TimeoutException:
        return None, _err(f"Request to {path} timed out after {TIMEOUT}s.")
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text[:300]
        except Exception:
            pass
        return None, _err(f"Backend returned HTTP {e.response.status_code} for {path}: {body}")
    except Exception as e:
        return None, _err(f"Request to {path} failed: {e}")


def _fetch_bars(
    symbol: str,
    resolution: str,
    from_dt: str,
    to_dt: str,
    session: str = "RTH",
) -> tuple[list[dict], Optional[str]]:
    params = {
        "symbol": symbol,
        "resolution": resolution,
        "session": session,
        "from_dt": from_dt,
        "to_dt": to_dt,
    }
    data, err = _request("GET", "/api/skill/bars", params=params)
    if err:
        return [], err
    return data.get("bars", []), None


def _prev_day_levels(symbol: str, date_str: str) -> tuple[dict, Optional[str]]:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    from_d = (d - timedelta(days=10)).strftime("%Y-%m-%d")
    to_d = d.strftime("%Y-%m-%d")
    bars, err = _fetch_bars(symbol, "1D", from_d, to_d, session="RTH")
    if err:
        return {}, err
    prior = [b for b in bars if b.get("trade_date", "") < date_str]
    if not prior:
        return {"PDC": None, "PDH": None, "PDL": None}, None
    prev = prior[-1]
    return {
        "PDC": float(prev.get("close", 0.0)),
        "PDH": float(prev.get("high", 0.0)),
        "PDL": float(prev.get("low", 0.0)),
    }, None


def _cutoff_ts(date_str: str, cutoff: str) -> Optional[int]:
    if not cutoff:
        return None
    h, m = map(int, cutoff.split(":"))
    y, mo, d = map(int, date_str.split("-"))
    return int(datetime(y, mo, d, h, m, tzinfo=ET).timestamp())


def _prev_unfilled_gap_hint(symbol: str, date_str: str) -> str:
    """Best-effort single-line hint about previous daily unfilled gap."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    from_d = (d - timedelta(days=20)).strftime("%Y-%m-%d")
    to_d = d.strftime("%Y-%m-%d")
    bars, err = _fetch_bars(symbol, "1D", from_d, to_d, session="RTH")
    if err:
        return "prev_unfilled_gap: unknown"
    prior = [b for b in bars if b.get("trade_date", "") < date_str]
    if len(prior) < 2:
        return "prev_unfilled_gap: unknown"
    b1 = prior[-2]
    b2 = prior[-1]
    if float(b2["low"]) > float(b1["high"]):
        return "prev_unfilled_gap: yesterday left bullish daily gap"
    if float(b2["high"]) < float(b1["low"]):
        return "prev_unfilled_gap: yesterday left bearish daily gap"
    return "prev_unfilled_gap: none"


@mcp.tool()
def analyze_gaps(
    symbol: str = "MES",
    date: str = "",
    cutoff: str = "",
    tick: Optional[float] = None,
) -> str:
    """Analyze intraday structural gaps and lifecycle states.

    Returns JSON: {key_levels, prev_unfilled_gap, gaps, events, g5_heartbeat}
    """
    if not date:
        return _err("date is required, format: YYYY-MM-DD")

    try:
        rth_to = f"{date} {cutoff}" if cutoff else f"{date} 16:00"
        bars_5m, err = _fetch_bars(symbol, "5", f"{date} 09:30", rth_to, session="RTH")
        if err:
            return err

        d = datetime.strptime(date, "%Y-%m-%d").date()
        overnight_from = f"{(d - timedelta(days=1)).strftime('%Y-%m-%d')} 16:00"
        overnight_to = f"{date} 09:30"
        overnight_bars, err = _fetch_bars(symbol, "5", overnight_from, overnight_to, session="ETH")
        if err:
            return err

        pd_levels, err = _prev_day_levels(symbol, date)
        if err:
            return err

        or_bars = bars_5m[:OR_BAR_COUNT]
        or_high = max((float(b["high"]) for b in or_bars), default=None)
        or_low = min((float(b["low"]) for b in or_bars), default=None)

        overnight_high = max((float(b["high"]) for b in overnight_bars), default=None)
        overnight_low = min((float(b["low"]) for b in overnight_bars), default=None)

        key_levels = {
            "PDH": pd_levels.get("PDH"),
            "PDL": pd_levels.get("PDL"),
            "PDC": pd_levels.get("PDC"),
            "OR_high": or_high,
            "OR_low": or_low,
            "overnight_high": overnight_high,
            "overnight_low": overnight_low,
        }

        cutoff_ts = _cutoff_ts(date, cutoff)
        result = analyze_day(
            bars_5m,
            key_levels=key_levels,
            symbol=symbol,
            tick=(tick if tick is not None else TICK_SIZE.get(symbol, TICK_SIZE["MES"])),
            cutoff_ts=cutoff_ts,
        )
        out = {
            "key_levels": key_levels,
            "prev_unfilled_gap": _prev_unfilled_gap_hint(symbol, date),
            "gaps": result.get("gaps", []),
            "events": result.get("events", []),
            "g5_heartbeat": result.get("g5_heartbeat", {}),
        }
        return json.dumps(out, ensure_ascii=False)
    except ValueError as e:
        return _err(f"Invalid date/cutoff format: {e}")
    except Exception as e:
        return _err(f"analyze_gaps failed: {e}")


@mcp.tool()
def get_key_levels(symbol: str = "MES", date: str = "") -> str:
    """Return key levels for gap analysis."""
    if not date:
        return _err("date is required, format: YYYY-MM-DD")
    try:
        bars_5m, err = _fetch_bars(symbol, "5", f"{date} 09:30", f"{date} 16:00", session="RTH")
        if err:
            return err

        d = datetime.strptime(date, "%Y-%m-%d").date()
        overnight_from = f"{(d - timedelta(days=1)).strftime('%Y-%m-%d')} 16:00"
        overnight_to = f"{date} 09:30"
        overnight_bars, err = _fetch_bars(symbol, "5", overnight_from, overnight_to, session="ETH")
        if err:
            return err

        pd_levels, err = _prev_day_levels(symbol, date)
        if err:
            return err

        or_bars = bars_5m[:OR_BAR_COUNT]
        payload = {
            "PDH": pd_levels.get("PDH"),
            "PDL": pd_levels.get("PDL"),
            "PDC": pd_levels.get("PDC"),
            "OR_high": max((float(b["high"]) for b in or_bars), default=None),
            "OR_low": min((float(b["low"]) for b in or_bars), default=None),
            "overnight_high": max((float(b["high"]) for b in overnight_bars), default=None),
            "overnight_low": min((float(b["low"]) for b in overnight_bars), default=None),
        }
        return json.dumps(payload, ensure_ascii=False)
    except ValueError as e:
        return _err(f"Invalid date format: {e}")
    except Exception as e:
        return _err(f"get_key_levels failed: {e}")


@mcp.tool()
def save_gap_analysis(
    symbol: str,
    timeframe: str,
    session: str,
    bar_from: int,
    bar_to: int,
    summary: str,
    annotations: str,
) -> str:
    """Save a gap analysis with chart annotations."""
    try:
        ann_list = json.loads(annotations)
    except (json.JSONDecodeError, TypeError) as e:
        return _err(f"annotations must be a valid JSON array string: {e}")
    if not isinstance(ann_list, list):
        return _err("annotations must be a JSON array of annotation objects")

    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "session": session,
        "bar_from": bar_from,
        "bar_to": bar_to,
        "summary": summary,
        "annotations": ann_list,
    }

    data, err = _request("POST", "/api/skill/analysis", json=payload)
    if err:
        return err
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
def list_gap_analyses(
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    active_only: bool = False,
) -> str:
    """List saved analyses."""
    params: dict = {"active_only": str(active_only).lower()}
    if symbol:
        params["symbol"] = symbol
    if timeframe:
        params["timeframe"] = timeframe

    data, err = _request("GET", "/api/skill/analyses", params=params)
    if err:
        return err
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
def toggle_analysis(analysis_id: int, active: bool) -> str:
    """Toggle analysis visibility."""
    data, err = _request(
        "PUT",
        f"/api/skill/analyses/{analysis_id}/active",
        params={"active": str(active).lower()},
    )
    if err:
        return err
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
def delete_analysis(analysis_id: int) -> str:
    """Delete an analysis record."""
    data, err = _request("DELETE", f"/api/skill/analyses/{analysis_id}")
    if err:
        return err
    return json.dumps(data, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
