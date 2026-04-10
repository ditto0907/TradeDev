"""
FastAPI server — TradingView UDF REST API, WebSocket real-time feed,
SQLite persistence, and IB order submission.

Start with:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, Response
from pydantic import BaseModel
from contextlib import asynccontextmanager

import config
import db
from ib_data_fetcher import IBDataFetcher
from google_sheets_sync import GoogleSheetsSync
from price_action_analyzer import PriceActionAnalyzer
from order_manager import IBOrderManager
from test_data import generate_bars

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

DATAFEED_DEBUG = os.environ.get("DATAFEED_DEBUG", "0") == "1"
datafeed_logger = logging.getLogger("datafeed")
if DATAFEED_DEBUG:
    datafeed_logger.setLevel(logging.DEBUG)

BASE_DIR  = Path(__file__).parent
MES_SYM   = "MES"   # symbol key used in DB

# ─── Global State ─────────────────────────────────────────────────────────────

fetcher      = IBDataFetcher()
sheets       = GoogleSheetsSync()
analyzer     = PriceActionAnalyzer()
_order_mgr:  Optional[IBOrderManager] = None   # set after IB connect

_ws_clients:          List[WebSocket] = []
_latest_analysis:     dict = {}
_last_analysis_bar_ts: int = 0
_prev_completed_bar:  dict = {"1min": None, "5min": None}  # for DB write on completion


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


# ─── New Bar / Tick Handler ───────────────────────────────────────────────────

def on_new_bar(bar_size_key: str, bar: dict):
    """
    Called on every tick (reqMktData) or 5-second bar.
    Saves completed bars to DB (when timestamp advances), re-runs analysis
    on new 5min bars, and broadcasts bar+analysis to WebSocket clients.
    """
    global _latest_analysis, _last_analysis_bar_ts, _prev_completed_bar

    prev = _prev_completed_bar[bar_size_key]
    if prev is not None and bar["time"] > prev["time"]:
        # The previous bar just completed — persist it
        db.insert_bars(MES_SYM, bar_size_key, [prev])
        sheets.buffer_bar(bar_size_key, prev)
    _prev_completed_bar[bar_size_key] = dict(bar)

    # Re-run price-action analysis only when a new 5min bar opens
    analysis_updated = False
    if bar_size_key == "5min" and bar["time"] > _last_analysis_bar_ts:
        _last_analysis_bar_ts = bar["time"]
        _latest_analysis = analyzer.get_analysis(fetcher.get_bars("5min"))
        analysis_updated = True

    asyncio.create_task(broadcast({
        "type": "bar", "bar_size": bar_size_key, "bar": bar,
    }))
    if analysis_updated:
        asyncio.create_task(broadcast({
            "type": "analysis", "data": _latest_analysis,
        }))


# ─── Startup / Shutdown ───────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _order_mgr

    logger.info("Starting up…")

    # ── Step 1: init DB and load historical bars from it ─────────────────────
    db.init_db()
    for key in ("1min", "5min"):
        stored = db.get_bars(MES_SYM, key)
        if stored:
            fetcher.bars[key] = stored[-config.MAX_BARS_IN_MEMORY:]
            logger.info("Loaded %d %s bars from DB", len(fetcher.bars[key]), key)

    has_db_data = bool(fetcher.bars.get("5min"))

    # ── Step 2: connect to IB and fetch only the bars we're missing ───────────
    ib_ok = False
    try:
        await fetcher.connect()

        since_1min = db.get_latest_ts(MES_SYM, "1min")
        since_5min = db.get_latest_ts(MES_SYM, "5min")
        await fetcher.load_history(since_1min=since_1min, since_5min=since_5min)

        # Persist the freshly fetched bars
        for key in ("1min", "5min"):
            if fetcher.bars[key]:
                saved = db.insert_bars(MES_SYM, key, fetcher.bars[key])
                logger.info("Saved %d %s bars to DB", saved, key)

        ib_ok = True
    except Exception as e:
        logger.error("IB connect/history error: %s", e)

    # ── Step 3: synthetic fallback only when NOTHING is available ─────────────
    if not ib_ok and not has_db_data:
        logger.info("Loading synthetic test data (500 bars each)…")
        fetcher.bars["5min"] = generate_bars(n=500, bar_minutes=5)
        fetcher.bars["1min"] = generate_bars(n=500, bar_minutes=1)

    # ── Step 4: real-time subscription (independent of step 2 success) ────────
    if ib_ok:
        try:
            fetcher.add_new_bar_callback(on_new_bar)
            await fetcher.subscribe_mktdata()   # swap for subscribe_realtime() for 5s bars
            logger.info("IB real-time streaming started.")

            # Create order manager with the qualified contract
            _order_mgr = IBOrderManager(fetcher.ib, fetcher._contract)

            # Wire order-status events → WebSocket broadcast
            def _on_order_status(trade):
                asyncio.create_task(broadcast({
                    "type":  "order_update",
                    "order": IBOrderManager._trade_to_dict(trade),
                }))
            fetcher.ib.orderStatusEvent += _on_order_status

        except Exception as e:
            logger.warning("IB real-time subscription failed: %s", e)

    # ── Step 5: Google Sheets ─────────────────────────────────────────────────
    if sheets.authenticate():
        sheets.initial_upload(fetcher.get_bars("1min"), fetcher.get_bars("5min"))

    # ── Step 6: initial price-action analysis ─────────────────────────────────
    global _latest_analysis
    bars_5min = fetcher.get_bars("5min")
    if bars_5min:
        _latest_analysis = analyzer.get_analysis(bars_5min)

    yield   # ── server is running ────────────────────────────────────────────

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("Shutting down…")
    # Flush any in-progress bars to DB
    for key in ("1min", "5min"):
        bar = _prev_completed_bar.get(key)
        if bar:
            db.insert_bars(MES_SYM, key, [bar])
    sheets.flush_buffer()
    fetcher.unsubscribe_realtime()
    fetcher.disconnect()


app = FastAPI(title="MES Price Action Server", lifespan=lifespan)


# ─── Debug Middleware ──────────────────────────────────────────────────────────

@app.middleware("http")
async def datafeed_debug_middleware(request: Request, call_next):
    if DATAFEED_DEBUG and request.url.path.startswith("/api"):
        t0  = time.perf_counter()
        response: Response = await call_next(request)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        try:
            payload  = json.loads(body)
            summary  = {}
            for k, v in payload.items():
                summary[k] = f"[{v[0]!r}…{v[-1]!r}]({len(v)})" if isinstance(v, list) and len(v) > 6 else v
            body_log = json.dumps(summary)
        except Exception:
            body_log = body.decode(errors="replace")[:300]
        datafeed_logger.debug("%-6s %-40s → %d (%.1f ms) %s",
                              request.method,
                              str(request.url.path) + ("?" + str(request.url.query) if request.url.query else ""),
                              response.status_code, elapsed_ms, body_log)
        return Response(content=body, status_code=response.status_code,
                        headers=dict(response.headers), media_type=response.media_type)
    return await call_next(request)


# ─── Static Files ─────────────────────────────────────────────────────────────

charting_lib_path = BASE_DIR / "charting_library"
if charting_lib_path.exists():
    app.mount("/charting_library", StaticFiles(directory=str(charting_lib_path)), name="charting_library")

static_path = BASE_DIR / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(static_path / "index.html"))


# ─── TradingView UDF REST Endpoints ──────────────────────────────────────────

@app.get("/api/config")
async def get_config():
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
    return {
        "name": "MES", "full_name": "CME:MES",
        "description": "Micro E-mini S&P 500 Futures",
        "type": "futures", "exchange": "CME", "listed_exchange": "CME",
        "timezone": "America/New_York", "format": "price",
        "pricescale": 4, "minmov": 1,
        "session": "0000-2359:23456",
        "has_intraday": True,
        "supported_resolutions": ["1", "5", "15", "60", "1D"],
        "intraday_multipliers": ["1", "5"],
        "has_no_volume": False, "volume_precision": 0,
        "data_status": "streaming",
    }


@app.get("/api/history")
async def get_history(
    symbol:    str = Query("MES"),
    resolution: str = Query("5"),
    from_ts:   int = Query(0,           alias="from"),
    to_ts:     int = Query(9_999_999_999, alias="to"),
    countback: int = Query(None),
):
    """
    TradingView DataFeed: getBars.

    Serves bars from SQLite.  If countback bars aren't available in the DB
    for the requested range (e.g. chart scrolled past cached history), an
    on-demand fetch from IB is attempted and the result is saved to the DB.
    """
    key  = "1min" if resolution == "1" else "5min"
    bars = db.get_bars(MES_SYM, key, from_ts=from_ts, to_ts=to_ts)

    # Auto-fetch from IB when the chart scrolls into uncached territory
    if (not bars or (countback and len(bars) < countback)):
        earliest_db = db.get_earliest_ts(MES_SYM, key)
        if (earliest_db is None or from_ts < earliest_db) and fetcher.ib and fetcher.ib.isConnected():
            try:
                fetched = await fetcher.fetch_range(key, from_ts, to_ts)
                if fetched:
                    db.insert_bars(MES_SYM, key, fetched)
                    # Also merge into in-memory store
                    for b in fetched:
                        fetcher._append_bar(key, b)
                    bars = db.get_bars(MES_SYM, key, from_ts=from_ts, to_ts=to_ts)
                    logger.info("Auto-fetched %d %s bars for scroll request", len(fetched), key)
            except Exception as e:
                logger.warning("On-demand IB fetch failed: %s", e)

    # Final fallback: in-memory cache (covers current session bars not yet in DB)
    if not bars:
        bars = fetcher.get_bars(key, from_ts=from_ts, to_ts=to_ts)

    if countback and len(bars) > countback:
        bars = bars[-countback:]

    if not bars:
        return {"s": "no_data"}

    return {
        "s": "ok",
        "t": [b["time"]   for b in bars],
        "o": [b["open"]   for b in bars],
        "h": [b["high"]   for b in bars],
        "l": [b["low"]    for b in bars],
        "c": [b["close"]  for b in bars],
        "v": [b["volume"] for b in bars],
    }


@app.get("/api/time")
async def get_time():
    return int(time.time())


@app.get("/api/analysis")
async def get_analysis():
    return _latest_analysis or {
        "support_levels": [], "resistance_levels": [],
        "market_cycle": "unknown", "cycle_ranges": [],
    }


# ─── Order Endpoints ──────────────────────────────────────────────────────────

class OrderRequest(BaseModel):
    action:      str              # "BUY" | "SELL"
    quantity:    int
    order_type:  str              # "market"|"limit"|"stop"|"stop_limit"
    limit_price: Optional[float] = None
    stop_price:  Optional[float] = None
    tif:         str = "DAY"


@app.post("/api/order")
async def place_order(req: OrderRequest):
    if _order_mgr is None:
        return JSONResponse({"success": False, "error": "IB not connected"}, status_code=503)
    try:
        result = _order_mgr.place_order(
            action=req.action, quantity=req.quantity,
            order_type=req.order_type,
            limit_price=req.limit_price, stop_price=req.stop_price,
            tif=req.tif,
        )
        return {"success": True, **result}
    except Exception as e:
        logger.error("place_order error: %s", e)
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@app.get("/api/orders")
async def get_orders(all: bool = Query(False)):
    if _order_mgr is None:
        return []
    return _order_mgr.get_all_orders() if all else _order_mgr.get_open_orders()


@app.delete("/api/order/{order_id}")
async def cancel_order(order_id: int):
    if _order_mgr is None:
        return JSONResponse({"success": False, "error": "IB not connected"}, status_code=503)
    ok = _order_mgr.cancel_order(order_id)
    return {"success": ok}


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws/realtime")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.append(websocket)
    logger.info("WebSocket client connected (total: %d)", len(_ws_clients))

    try:
        snapshot_bars = fetcher.get_bars("5min")[-200:]
        await websocket.send_text(json.dumps({
            "type":      "snapshot",
            "bars_5min": snapshot_bars,
            "analysis":  _latest_analysis,
        }))
    except Exception as e:
        logger.warning("Snapshot send failed: %s", e)

    try:
        while True:
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
    uvicorn.run("server:app", host=config.SERVER_HOST, port=config.SERVER_PORT,
                reload=False, loop="asyncio")
