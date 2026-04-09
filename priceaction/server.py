"""
FastAPI server — serves the TradingView chart frontend, provides REST API
endpoints for the TradingView DataFeed (UDF-style), and pushes real-time
bar/analysis updates over WebSocket.

Start with:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload

Then open:  http://localhost:8000
"""
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from contextlib import asynccontextmanager

import config
from ib_data_fetcher import IBDataFetcher
from google_sheets_sync import GoogleSheetsSync
from price_action_analyzer import PriceActionAnalyzer

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

# ─── Global State ─────────────────────────────────────────────────────────────

fetcher = IBDataFetcher()
sheets = GoogleSheetsSync()
analyzer = PriceActionAnalyzer()
_ws_clients: List[WebSocket] = []
_latest_analysis: dict = {}


# ─── WebSocket Broadcast ──────────────────────────────────────────────────────

async def broadcast(message: dict):
    payload = json.dumps(message)
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


# ─── New Bar Handler ──────────────────────────────────────────────────────────

def on_new_bar(bar_size_key: str, bar: dict):
    """Called by IBDataFetcher on each completed new bar."""
    # Buffer to Google Sheets (non-blocking best-effort)
    sheets.buffer_bar(bar_size_key, bar)

    # Rerun price-action analysis on 5min bars
    if bar_size_key == "5min":
        global _latest_analysis
        bars_5min = fetcher.get_bars("5min")
        _latest_analysis = analyzer.get_analysis(bars_5min)

    # Broadcast to all WebSocket clients
    asyncio.create_task(broadcast({
        "type": "bar",
        "bar_size": bar_size_key,
        "bar": bar,
    }))
    if bar_size_key == "5min":
        asyncio.create_task(broadcast({
            "type": "analysis",
            "data": _latest_analysis,
        }))


# ─── Startup / Shutdown ───────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: connect IB, load history, set up Google Sheets."""
    logger.info("Starting up…")

    # Connect to IB
    try:
        await fetcher.connect()
        await fetcher.load_history()
        fetcher.add_new_bar_callback(on_new_bar)
        await fetcher.subscribe_realtime()
        logger.info("IB data streaming started.")
    except Exception as e:
        logger.error("IB startup error: %s — server running without live data", e)

    # Google Sheets authentication (optional — graceful degradation)
    if sheets.authenticate():
        bars_1min = fetcher.get_bars("1min")
        bars_5min = fetcher.get_bars("5min")
        sheets.initial_upload(bars_1min, bars_5min)

    # Initial price action analysis
    global _latest_analysis
    bars_5min = fetcher.get_bars("5min")
    if bars_5min:
        _latest_analysis = analyzer.get_analysis(bars_5min)

    yield

    # Shutdown
    logger.info("Shutting down…")
    sheets.flush_buffer()
    fetcher.unsubscribe_realtime()
    fetcher.disconnect()


app = FastAPI(title="MES Price Action Server", lifespan=lifespan)

# ─── Static Files ─────────────────────────────────────────────────────────────

# Serve TradingView charting library static assets
charting_lib_path = BASE_DIR / "charting_library"
if charting_lib_path.exists():
    app.mount("/charting_library", StaticFiles(directory=str(charting_lib_path)), name="charting_library")

# Serve frontend files from static/
static_path = BASE_DIR / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(static_path / "index.html"))


# ─── TradingView UDF REST Endpoints ──────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    """TradingView DataFeed: onReady configuration."""
    return {
        "supported_resolutions": ["1", "5", "15", "60", "1D"],
        "exchanges": [{"value": "CME", "name": "CME", "desc": "Chicago Mercantile Exchange"}],
        "symbols_types": [{"name": "Futures", "value": "futures"}],
        "supports_marks": False,
        "supports_timescale_marks": False,
        "supports_time": True,
    }


@app.get("/api/symbols")
async def get_symbols(symbol: str = Query("MES")):
    """TradingView DataFeed: resolveSymbol."""
    return {
        "name": "MES",
        "full_name": "CME:MES",
        "description": "Micro E-mini S&P 500 Futures",
        "type": "futures",
        "exchange": "CME",
        "listed_exchange": "CME",
        "timezone": "America/New_York",
        "format": "price",
        "pricescale": 4,           # 0.25 tick → pricescale 4
        "minmov": 1,
        "session": "0000-2359:23456",  # Nearly 24h Mon–Fri (CME Globex)
        "has_intraday": True,
        "supported_resolutions": ["1", "5", "15", "60", "1D"],
        "intraday_multipliers": ["1", "5"],
        "has_no_volume": False,
        "volume_precision": 0,
        "data_status": "streaming",
    }


@app.get("/api/history")
async def get_history(
    symbol: str = Query("MES"),
    resolution: str = Query("5"),
    from_ts: int = Query(0, alias="from"),
    to_ts: int = Query(9999999999, alias="to"),
    countback: int = Query(None),
):
    """
    TradingView DataFeed: getBars — return OHLCV bars in UDF format.
    """
    key = "1min" if resolution == "1" else "5min"
    bars = fetcher.get_bars(key, from_ts=from_ts, to_ts=to_ts)

    if countback and len(bars) > countback:
        bars = bars[-countback:]

    if not bars:
        return {"s": "no_data"}

    return {
        "s": "ok",
        "t": [b["time"] for b in bars],
        "o": [b["open"] for b in bars],
        "h": [b["high"] for b in bars],
        "l": [b["low"] for b in bars],
        "c": [b["close"] for b in bars],
        "v": [b["volume"] for b in bars],
    }


@app.get("/api/time")
async def get_time():
    """TradingView DataFeed: server time (seconds)."""
    import time
    return int(time.time())


@app.get("/api/analysis")
async def get_analysis():
    """Return current S/R levels and market cycle data."""
    return _latest_analysis or {"support_levels": [], "resistance_levels": [], "market_cycle": "unknown", "cycle_ranges": []}


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws/realtime")
async def websocket_endpoint(websocket: WebSocket):
    """
    Real-time data stream.

    Messages sent to the client:
      {"type": "bar",      "bar_size": "1min"|"5min", "bar": {...}}
      {"type": "analysis", "data": {...}}
      {"type": "snapshot", "bars_5min": [...], "analysis": {...}}   (on connect)
    """
    await websocket.accept()
    _ws_clients.append(websocket)
    logger.info("WebSocket client connected (total: %d)", len(_ws_clients))

    # Send snapshot of recent 5min bars + analysis on connect
    try:
        snapshot_bars = fetcher.get_bars("5min")[-200:]
        await websocket.send_text(json.dumps({
            "type": "snapshot",
            "bars_5min": snapshot_bars,
            "analysis": _latest_analysis,
        }))
    except Exception as e:
        logger.warning("Snapshot send failed: %s", e)

    try:
        while True:
            # Keep connection alive by receiving (client can send pings)
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)
        logger.info("WebSocket client disconnected (total: %d)", len(_ws_clients))


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    # loop="asyncio" is required: ib_insync is not compatible with uvloop
    uvicorn.run("server:app", host=config.SERVER_HOST, port=config.SERVER_PORT,
                reload=False, loop="asyncio")
