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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, Response
from pydantic import BaseModel
from contextlib import asynccontextmanager

import config
import db
from ib_data_fetcher import IBDataFetcher, _bar_to_dict
from google_sheets_sync import GoogleSheetsSync
from price_action_analyzer import PriceActionAnalyzer
from order_manager import IBOrderManager
from trade_log_parser import load_all_trades, parse_csv_content
from test_data import generate_bars
import strategy_backtest

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
_prev_completed_bar:  dict = {"5min": None}  # for DB write on completion


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
        "type": "bar", "bar_size": bar_size_key, "bar": bar, "symbol": MES_SYM,
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
    stored = db.get_bars(MES_SYM, "5min")
    if stored:
        fetcher.bars["5min"] = stored[-config.MAX_BARS_IN_MEMORY:]
        logger.info("Loaded %d 5min bars from DB", len(fetcher.bars["5min"]))

    has_db_data = bool(fetcher.bars.get("5min"))

    # ── Step 2: connect to IB and fetch only the bars we're missing ───────────
    ib_ok = False
    try:
        await fetcher.connect()

        since_5min = db.get_latest_ts(MES_SYM, "5min")
        await fetcher.load_history(since_5min=since_5min)

        # Persist the freshly fetched bars
        if fetcher.bars["5min"]:
            saved = db.insert_bars(MES_SYM, "5min", fetcher.bars["5min"])
            logger.info("Saved %d 5min bars to DB", saved)

        ib_ok = True
    except Exception as e:
        logger.error("IB connect/history error: %s", e)

    # ── Step 2b: prefetch extra symbols if DB is empty ───────────────────────
    if ib_ok and fetcher.ib and fetcher.ib.isConnected():
        from ib_insync import ContFuture

        # All symbols to prefetch (primary + extras)
        all_symbols = [
            {"symbol": MES_SYM, "ib_symbol": config.MES_SYMBOL,
             "exchange": config.MES_EXCHANGE, "currency": config.MES_CURRENCY},
        ] + config.EXTRA_SYMBOLS

        for sym_cfg in all_symbols:
            sym_name = sym_cfg["symbol"]
            contract = ContFuture(
                symbol=sym_cfg.get("ib_symbol", sym_name),
                exchange=sym_cfg["exchange"],
                currency=sym_cfg["currency"],
            )
            try:
                qualified = await asyncio.wait_for(
                    fetcher.ib.qualifyContractsAsync(contract), timeout=30.0,
                )
                if not qualified:
                    logger.warning("IB returned no contract for %s — skipping", sym_name)
                    continue
                qc = qualified[0]

                # ── 5min bars (skip if already populated) ─────────────────
                if sym_name != MES_SYM:  # MES 5min already fetched above
                    existing_5m = db.get_bars(sym_name, "5min")
                    if not existing_5m:
                        raw = await asyncio.wait_for(
                            fetcher.ib.reqHistoricalDataAsync(
                                qc, endDateTime="",
                                durationStr=config.HISTORY_DURATION_5MIN,
                                barSizeSetting="5 mins", whatToShow="TRADES",
                                useRTH=False, formatDate=2,
                            ), timeout=60.0,
                        )
                        bars5 = [_bar_to_dict(b) for b in raw]
                        if bars5:
                            db.insert_bars(sym_name, "5min", bars5)
                            logger.info("Prefetched %d 5min bars for %s", len(bars5), sym_name)

                # ── 1D bars (always refresh to keep daily chart up to date) ─
                since_1d = db.get_latest_ts(sym_name, "1D")
                existing_1d = db.get_bars(sym_name, "1D")
                if existing_1d and since_1d:
                    import time as _time
                    gap_days = (_time.time() - since_1d) / 86400
                    if gap_days < 1:
                        logger.info("1D bars for %s are up to date — skip fetch", sym_name)
                        continue
                raw_1d = await asyncio.wait_for(
                    fetcher.ib.reqHistoricalDataAsync(
                        qc, endDateTime="",
                        durationStr=config.HISTORY_DURATION_1D,
                        barSizeSetting="1 day", whatToShow="TRADES",
                        useRTH=True, formatDate=2,
                    ), timeout=60.0,
                )
                bars_1d = [_bar_to_dict(b) for b in raw_1d]
                if bars_1d:
                    saved_1d = db.insert_bars(sym_name, "1D", bars_1d)
                    logger.info("Prefetched %d 1D bars for %s", saved_1d, sym_name)

            except Exception as e:
                logger.warning("Prefetch %s failed: %s", sym_name, e)

    # ── Step 3: synthetic fallback only when NOTHING is available ─────────────
    if not ib_ok and not has_db_data:
        logger.info("Loading synthetic test data (500 bars)…")
        fetcher.bars["5min"] = generate_bars(n=500, bar_minutes=5)
        db.insert_bars(MES_SYM, "5min", fetcher.bars["5min"])
        logger.info("Saved %d synthetic bars to DB", len(fetcher.bars["5min"]))

    # ── Step 4: real-time subscription (independent of step 2 success) ────────
    if ib_ok:
        try:
            fetcher.add_new_bar_callback(on_new_bar)
            await fetcher.subscribe_mktdata()   # swap for subscribe_realtime() for 5s bars
            logger.info("IB real-time streaming started.")

            # Create order manager with the qualified contract
            _order_mgr = IBOrderManager(fetcher.ib, fetcher._contract)

            # Wire order-status events → WebSocket broadcast + bracket auto-cancel
            def _on_order_status(trade):
                asyncio.create_task(broadcast({
                    "type":  "order_update",
                    "order": IBOrderManager._trade_to_dict(trade),
                }))
                # Auto-cancel bracket siblings when any member is cancelled
                status = trade.orderStatus.status
                if status in ("Cancelled", "ApiCancelled", "Inactive"):
                    oid = trade.order.orderId
                    if _order_mgr:
                        _order_mgr._cancel_bracket_siblings(oid)
            fetcher.ib.orderStatusEvent += _on_order_status

        except Exception as e:
            logger.warning("IB real-time subscription failed: %s", e)

    # ── Step 5: Google Sheets ─────────────────────────────────────────────────
    if sheets.authenticate():
        sheets.initial_upload(fetcher.get_bars("5min"))

    # ── Step 6: initial price-action analysis ─────────────────────────────────
    global _latest_analysis
    bars_5min = fetcher.get_bars("5min")
    if bars_5min:
        _latest_analysis = analyzer.get_analysis(bars_5min)

    yield   # ── server is running ────────────────────────────────────────────

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("Shutting down…")
    # Flush any in-progress bars to DB
    bar = _prev_completed_bar.get("5min")
    if bar:
        db.insert_bars(MES_SYM, "5min", [bar])
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
        "supported_resolutions": ["5", "15", "60", "1D"],
        "exchanges": [{"value": "CME", "name": "CME", "desc": "Chicago Mercantile Exchange"}],
        "symbols_types": [{"name": "Futures", "value": "futures"}],
        "supports_marks": False,
        "supports_timescale_marks": False,
        "supports_time": True,
    }


@app.get("/api/symbols")
async def get_symbols(symbol: str = Query("MES")):
    _SYMBOL_META = {
        "MES": {
            "name": "MES", "full_name": "CME:MES",
            "description": "Micro E-mini S&P 500 Futures",
            "exchange": "CME", "listed_exchange": "CME",
            "pricescale": 100, "minmov": 25,
            "timezone": "America/New_York",
            "session_eth": "1800-1700:1234567",
            "session_rth": "0930-1600:23456",
            "ib_symbol": "MES", "ib_exchange": "CME",
        },
        "MNQ": {
            "name": "MNQ", "full_name": "CME:MNQ",
            "description": "Micro E-mini Nasdaq-100 Futures",
            "exchange": "CME", "listed_exchange": "CME",
            "pricescale": 100, "minmov": 25,
            "timezone": "America/New_York",
            "session_eth": "1800-1700:1234567",
            "session_rth": "0930-1600:23456",
            "ib_symbol": "MNQ", "ib_exchange": "CME",
        },
        "NK225MC": {
            "name": "NK225MC", "full_name": "OSE:NK225MC",
            "description": "Micro Nikkei 225 Futures",
            "exchange": "OSE", "listed_exchange": "OSE",
            "pricescale": 1, "minmov": 5,
            "timezone": "Asia/Tokyo",
            "session_eth": "0845-1545,1700-0600:23456",
            "session_rth": "0845-1545:23456",
            "ib_symbol": "N225MC", "ib_exchange": "OSE.JPN",
        },
        "MGC": {
            "name": "MGC", "full_name": "COMEX:MGC",
            "description": "Micro Gold Futures",
            "exchange": "COMEX", "listed_exchange": "COMEX",
            "pricescale": 10, "minmov": 1,
            "timezone": "America/New_York",
            "session_eth": "1800-1700:1234567",
            "session_rth": "0930-1700:23456",
            "ib_symbol": "MGC", "ib_exchange": "COMEX",
        },
    }
    base = symbol.upper()
    meta = _SYMBOL_META.get(base, _SYMBOL_META["MES"])
    return {
        **meta,
        "type": "futures", "format": "price",
        "session": meta["session_rth"],     # must match default subsession_id
        "has_intraday": True,
        "supported_resolutions": ["5", "15", "60", "1D"],
        "intraday_multipliers": ["5", "15", "60"],
        "has_no_volume": False, "volume_precision": 0,
        "data_status": "streaming",
        # TradingView native subsession selector (bottom status bar)
        "subsession_id": "regular",
        "subsessions": [
            {"id": "regular",  "description": "Regular Trading Hours", "session": meta["session_rth"]},
            {"id": "extended", "description": "Extended Hours",        "session": meta["session_eth"]},
        ],
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
    from ib_data_fetcher import resolution_to_key
    key  = resolution_to_key(resolution)
    sym  = symbol.upper()
    bars = db.get_bars(sym, key, from_ts=from_ts, to_ts=to_ts)

    # Auto-fetch from IB whenever the scroll goes before our earliest stored bar.
    # Guard: only attempt when _ib_ready is True (contract resolved).
    # Without this guard, a timed-out qualifyContractsAsync would cause every
    # scroll request to hang for 30 s before returning empty.
    earliest_db  = db.get_earliest_ts(sym, key)
    needs_older  = (earliest_db is None or from_ts < earliest_db)
    if needs_older and sym == MES_SYM:
        if fetcher._ib_ready and fetcher.ib and fetcher.ib.isConnected():
            # Use earliest_db as the end point so we fetch data BEFORE what we have
            fetch_end = earliest_db if earliest_db else to_ts
            logger.info(
                "Scroll past DB boundary: auto-fetching %s bars from IB "
                "(from=%s earliest_db=%s fetch_end=%s)", key, from_ts, earliest_db, fetch_end
            )
            try:
                fetched = await fetcher.fetch_range(key, from_ts, fetch_end)
                if fetched:
                    db.insert_bars(sym, key, fetched)
                    bars = db.get_bars(sym, key, from_ts=from_ts, to_ts=to_ts)
                    logger.info("Auto-fetched %d %s bars for scroll request", len(fetched), key)
                else:
                    logger.info("IB returned 0 bars for %s range %s→%s", key, from_ts, fetch_end)
            except Exception as e:
                logger.warning("On-demand IB fetch failed: %s", e)
        else:
            logger.debug(
                "Scroll past DB boundary for %s but IB not ready — skipping auto-fetch", key
            )

    # Final fallback: in-memory cache (only valid for the primary symbol MES)
    if not bars and sym == MES_SYM:
        bars = fetcher.get_bars(key, from_ts=from_ts, to_ts=to_ts)

    if countback and len(bars) > countback:
        bars = bars[-countback:]

    if not bars:
        # Provide nextTime so TradingView keeps requesting older data
        next_ts = db.get_latest_ts_before(sym, key, from_ts)
        resp: dict = {"s": "no_data"}
        if next_ts is not None:
            resp["nextTime"] = next_ts
        return resp

    return {
        "s": "ok",
        "t": [b["time"]   for b in bars],
        "o": [b["open"]   for b in bars],
        "h": [b["high"]   for b in bars],
        "l": [b["low"]    for b in bars],
        "c": [b["close"]  for b in bars],
        "v": [b["volume"] for b in bars],
    }


@app.get("/api/watchlist_prices")
async def get_watchlist_prices():
    """Return latest close price and daily change for all watchlist symbols."""
    symbols = ["MES", "MNQ", "NK225MC", "MGC"]
    result = {}
    for sym in symbols:
        bars = db.get_bars(sym, "5min")
        if not bars:
            result[sym] = {"close": None, "change_pct": None}
            continue
        close = bars[-1]["close"]
        # Find the open of the earliest bar today (or first available bar)
        # Simple approach: use the open of the first bar
        open_price = bars[0]["open"]
        # Try to find the session open (most recent 18:00 ET boundary)
        import time as _time
        now = int(_time.time())
        # Look for bars from last 24h for a recent session reference
        recent = [b for b in bars if b["time"] > now - 86400]
        if recent:
            open_price = recent[0]["open"]
        chg_pct = ((close - open_price) / open_price * 100) if open_price else 0
        result[sym] = {"close": close, "change_pct": round(chg_pct, 2)}
    return result


@app.get("/api/time")
async def get_time():
    return int(time.time())


@app.get("/api/analysis")
async def get_analysis(symbol: str = Query("MES")):
    sym = symbol.upper()
    if sym == MES_SYM:
        return _latest_analysis or {
            "support_levels": [], "resistance_levels": [],
            "market_cycle": "unknown", "cycle_ranges": [],
        }
    # On-demand analysis for non-MES symbols
    bars = db.get_bars(sym, "5min")
    if not bars:
        return {"support_levels": [], "resistance_levels": [],
                "market_cycle": "unknown", "cycle_ranges": []}
    return analyzer.get_analysis(bars)


# ─── Skill: Market Cycle Analysis Endpoints ───────────────────────────────────

class AnalysisAnnotation(BaseModel):
    label: str                    # e.g. "Opening Range", "Bull Breakout"
    type: str                     # "range" | "hline" | "label" | "trend line"
    start_time: int               # Unix seconds
    end_time: Optional[int] = None       # For range / trend line type
    price: Optional[float] = None        # For hline / label
    price_high: Optional[float] = None   # For range type
    price_low: Optional[float] = None    # For range type
    price_start: Optional[float] = None  # For trend line type (start point price)
    price_end: Optional[float] = None    # For trend line type (end point price)
    color: Optional[str] = None
    style: Optional[str] = None          # "solid" | "dashed" | "dotted"
    linewidth: Optional[int] = None      # For trend line type

class AnalysisPayload(BaseModel):
    symbol: str = "MES"
    timeframe: str = "5"
    session: str = "RTH"
    bar_from: int
    bar_to: int
    summary: str                  # Concise textual summary of the analysis
    annotations: List[AnalysisAnnotation]

@app.get("/api/skill/bars")
async def skill_get_bars(
    symbol: str = Query("MES"),
    resolution: str = Query("5"),
    session: str = Query("RTH"),
    from_ts: int = Query(None, alias="from"),
    to_ts: int = Query(None, alias="to"),
    from_dt: str = Query(None),  # "YYYY-MM-DD HH:MM" or "YYYY-MM-DD"
    to_dt: str = Query(None),    # "YYYY-MM-DD HH:MM" or "YYYY-MM-DD"
):
    """
    Skill-facing K-line data endpoint.

    Returns OHLCV bars as a JSON array (easier for LLM consumption than
    the TradingView UDF arrays-of-columns format).
    Supports session filter: RTH drops bars outside 09:30-16:00 ET.
    
    Time parameters (priority: datetime strings > unix timestamps):
      - from_dt/to_dt: Human-readable datetime strings "YYYY-MM-DD HH:MM" or "YYYY-MM-DD"
      - from/to: Unix timestamps (legacy, for backward compatibility)
    """
    from ib_data_fetcher import resolution_to_key
    from datetime import datetime as _dt
    import zoneinfo
    
    # Parse datetime strings if provided (takes priority over timestamps)
    if from_dt:
        try:
            # Parse datetime string and convert to Unix timestamp
            # Assume ET timezone for trading hours
            et = zoneinfo.ZoneInfo("America/New_York")
            if len(from_dt) == 10:  # "YYYY-MM-DD" format
                dt_obj = _dt.strptime(from_dt, "%Y-%m-%d")
            else:  # "YYYY-MM-DD HH:MM" format
                dt_obj = _dt.strptime(from_dt, "%Y-%m-%d %H:%M")
            # Create timezone-aware datetime in ET timezone
            dt_obj_et = _dt(dt_obj.year, dt_obj.month, dt_obj.day,
                           dt_obj.hour, dt_obj.minute, dt_obj.second, tzinfo=et)
            from_ts = int(dt_obj_et.timestamp())
        except ValueError as e:
            return JSONResponse(
                {"error": f"Invalid from_dt format: {e}. Use 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'"},
                status_code=400
            )
    elif from_ts is None:
        from_ts = 0
    
    if to_dt:
        try:
            et = zoneinfo.ZoneInfo("America/New_York")
            if len(to_dt) == 10:  # "YYYY-MM-DD" format, use end of day
                dt_obj = _dt.strptime(to_dt, "%Y-%m-%d")
                dt_obj_et = _dt(dt_obj.year, dt_obj.month, dt_obj.day,
                               23, 59, 59, tzinfo=et)
            else:  # "YYYY-MM-DD HH:MM" format
                dt_obj = _dt.strptime(to_dt, "%Y-%m-%d %H:%M")
                dt_obj_et = _dt(dt_obj.year, dt_obj.month, dt_obj.day,
                               dt_obj.hour, dt_obj.minute, dt_obj.second, tzinfo=et)
            to_ts = int(dt_obj_et.timestamp())
        except ValueError as e:
            return JSONResponse(
                {"error": f"Invalid to_dt format: {e}. Use 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'"},
                status_code=400
            )
    elif to_ts is None:
        to_ts = 9_999_999_999
    
    key = resolution_to_key(resolution)
    sym = symbol.upper()
    
    # Debug logging
    logger.info(f"skill_get_bars: sym={sym}, key={key}, from_ts={from_ts}, to_ts={to_ts}")
    logger.info(f"  from_dt={from_dt}, to_dt={to_dt}")
    
    bars = db.get_bars(sym, key, from_ts=from_ts, to_ts=to_ts)
    logger.info(f"  Retrieved {len(bars)} bars from database")

    if session.upper() == "RTH" and key in ("5min", "1min", "3min", "15min", "30min", "60min"):
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        import zoneinfo
        et = zoneinfo.ZoneInfo("America/New_York")
        filtered = []
        for b in bars:
            dt = _dt.fromtimestamp(b["time"], tz=_tz.utc).astimezone(et)
            # RTH: 09:30 - 16:00 ET
            t = dt.hour * 60 + dt.minute
            if 570 <= t < 960:        # 9:30=570  16:00=960
                filtered.append(b)
        bars = filtered

    return {
        "symbol": sym,
        "resolution": resolution,
        "session": session,
        "count": len(bars),
        "bars": bars,
    }

@app.post("/api/skill/analysis")
async def skill_save_analysis(payload: AnalysisPayload):
    """
    Writeback: save LLM market cycle analysis results to DB.

    The annotations include typed shapes (ranges, hlines, labels) that the
    frontend will render on the chart.
    """
    from datetime import datetime as _dt, timezone as _tz
    created_at = _dt.now(_tz.utc).isoformat()
    annotations_json = json.dumps([a.dict() for a in payload.annotations])
    row_id = db.save_analysis(
        symbol=payload.symbol.upper(),
        timeframe=payload.timeframe,
        session=payload.session,
        created_at=created_at,
        bar_from=payload.bar_from,
        bar_to=payload.bar_to,
        summary=payload.summary,
        annotations=annotations_json,
    )
    logger.info("Saved market cycle analysis #%d for %s/%s",
                row_id, payload.symbol, payload.timeframe)

    # Broadcast to WebSocket clients so chart updates live
    msg = {
        "type": "cycle_analysis",
        "analysis": {
            "id": row_id,
            "symbol": payload.symbol.upper(),
            "timeframe": payload.timeframe,
            "session": payload.session,
            "created_at": created_at,
            "bar_from": payload.bar_from,
            "bar_to": payload.bar_to,
            "summary": payload.summary,
            "annotations": [a.dict() for a in payload.annotations],
            "active": 1,
        },
    }
    await broadcast(msg)

    return {"success": True, "id": row_id}

@app.get("/api/skill/analyses")
async def skill_list_analyses(
    symbol: str = Query(None),
    timeframe: str = Query(None),
    active_only: bool = Query(False),
):
    """List all saved market cycle analyses, optionally filtered."""
    rows = db.get_analyses(symbol=symbol, timeframe=timeframe, active_only=active_only)
    # Parse annotations JSON for each row
    for r in rows:
        try:
            r["annotations"] = json.loads(r.get("annotations", "[]"))
        except (json.JSONDecodeError, TypeError):
            r["annotations"] = []
    return rows

@app.put("/api/skill/analyses/{analysis_id}/active")
async def skill_toggle_analysis(analysis_id: int, active: bool = Query(...)):
    """Toggle an analysis active/inactive (shows/hides on chart)."""
    ok = db.update_analysis_active(analysis_id, active)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "not found"})

    # Broadcast state change
    msg = {"type": "cycle_analysis_toggle", "id": analysis_id, "active": active}
    await broadcast(msg)

    return {"success": True, "id": analysis_id, "active": active}

@app.delete("/api/skill/analyses/{analysis_id}")
async def skill_delete_analysis(analysis_id: int):
    """Permanently delete an analysis record."""
    ok = db.delete_analysis(analysis_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "not found"})

    msg = {"type": "cycle_analysis_delete", "id": analysis_id}
    await broadcast(msg)

    return {"success": True}


@app.get("/api/trades")
async def get_trades():
    """Return parsed historical trades from log files in data/."""
    try:
        return load_all_trades()
    except Exception as e:
        logger.error("Trade log load error: %s", e)
        return []


@app.get("/api/trades/files")
async def list_trade_files():
    """List trade CSV files in data/ directory."""
    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    files = []
    patterns = ["trade_log_topstep*", "trade_log_IB*", "trade_log_lucid*"]
    seen = set()
    for pat in patterns:
        for fp in sorted(data_dir.glob(pat)):
            if fp.name not in seen and fp.is_file():
                seen.add(fp.name)
                files.append({"name": fp.name, "size": fp.stat().st_size})
    return files


@app.get("/api/trades/file/{filename}")
async def get_trades_from_file(filename: str):
    """Parse and return trades from a specific CSV file."""
    data_dir = Path(__file__).parent / "data"
    filepath = (data_dir / filename).resolve()
    # Prevent path traversal
    if not str(filepath).startswith(str(data_dir.resolve())):
        return JSONResponse(status_code=400, content={"error": "Invalid filename"})
    if not filepath.exists():
        return JSONResponse(status_code=404, content={"error": "File not found"})
    try:
        text = filepath.read_text(encoding="utf-8-sig", errors="replace")
        trades = parse_csv_content(text)
        return trades
    except Exception as e:
        logger.error("Trade file parse error: %s", e)
        return []


@app.post("/api/trades/upload")
async def upload_trades(file: UploadFile):
    """Save uploaded CSV to data/ folder and return parsed trades."""
    try:
        content = await file.read()
        text = content.decode("utf-8-sig", errors="replace")
        trades = parse_csv_content(text)
        if not trades:
            return {"filename": None, "trades": []}
        # Save to data/ folder with original filename
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        save_name = file.filename or "trade_log_upload.csv"
        save_path = (data_dir / save_name).resolve()
        if not str(save_path).startswith(str(data_dir.resolve())):
            return JSONResponse(status_code=400, content={"error": "Invalid filename"})
        save_path.write_bytes(content)
        logger.info("Saved trade CSV to %s (%d trades)", save_name, len(trades))
        return {"filename": save_name, "trades": trades}
    except Exception as e:
        logger.error("Trade upload parse error: %s", e)
        return {"filename": None, "trades": []}


@app.delete("/api/trades/file/{filename}")
async def delete_trade_file(filename: str):
    """Delete a trade CSV file from data/ directory."""
    data_dir = Path(__file__).parent / "data"
    filepath = (data_dir / filename).resolve()
    if not str(filepath).startswith(str(data_dir.resolve())):
        return JSONResponse(status_code=400, content={"error": "Invalid filename"})
    if not filepath.exists():
        return JSONResponse(status_code=404, content={"error": "File not found"})
    filepath.unlink()
    logger.info("Deleted trade file: %s", filename)
    return {"success": True}


# ─── Order Endpoints ──────────────────────────────────────────────────────────

class OrderRequest(BaseModel):
    action:      str              # "BUY" | "SELL"
    quantity:    int
    order_type:  str              # "market"|"limit"|"stop"|"stop_limit"
    limit_price: Optional[float] = None
    stop_price:  Optional[float] = None
    tif:         str = "DAY"


class BracketOrderRequest(BaseModel):
    action:      str              # "BUY" | "SELL"
    quantity:    int
    order_type:  str              # entry order type
    limit_price: Optional[float] = None
    stop_price:  Optional[float] = None
    tp_price:    Optional[float] = None   # take-profit limit
    sl_price:    Optional[float] = None   # stop-loss stop
    tif:         str = "DAY"


class ModifyOrderRequest(BaseModel):
    limit_price: Optional[float] = None
    stop_price:  Optional[float] = None


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


@app.post("/api/order/bracket")
async def place_bracket_order(req: BracketOrderRequest):
    if _order_mgr is None:
        return JSONResponse({"success": False, "error": "IB not connected"}, status_code=503)
    try:
        results = _order_mgr.place_bracket_order(
            action=req.action, quantity=req.quantity,
            order_type=req.order_type,
            limit_price=req.limit_price, stop_price=req.stop_price,
            tp_price=req.tp_price, sl_price=req.sl_price,
            tif=req.tif,
        )
        return {"success": True, "orders": results}
    except ValueError as e:
        logger.error("place_bracket_order validation error: %s", e)
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    except Exception as e:
        logger.error("place_bracket_order error: %s", e)
        return JSONResponse({"success": False, "error": "Order submission failed"}, status_code=400)


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


@app.put("/api/order/{order_id}")
async def modify_order(order_id: int, req: ModifyOrderRequest):
    if _order_mgr is None:
        return JSONResponse({"success": False, "error": "IB not connected"}, status_code=503)
    try:
        result = _order_mgr.modify_order(
            order_id,
            limit_price=req.limit_price,
            stop_price=req.stop_price,
        )
        return {"success": True, **result}
    except Exception as e:
        logger.error("modify_order error: %s", e)
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@app.delete("/api/orders")
async def cancel_all_orders():
    if _order_mgr is None:
        return JSONResponse({"success": False, "error": "IB not connected"}, status_code=503)
    count = _order_mgr.cancel_all_orders()
    return {"success": True, "cancelled": count}


@app.post("/api/flatten")
async def flatten_position():
    if _order_mgr is None:
        return JSONResponse({"success": False, "error": "IB not connected"}, status_code=503)
    try:
        result = _order_mgr.flatten_position()
        if result is None:
            return {"success": True, "message": "No open position"}
        return {"success": True, **result}
    except ValueError as e:
        logger.error("flatten validation error: %s", e)
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    except Exception as e:
        logger.error("flatten error: %s", e)
        return JSONResponse({"success": False, "error": "Flatten failed"}, status_code=400)


@app.get("/api/position")
async def get_position():
    if _order_mgr is None:
        return {"symbol": "MES", "position": 0, "avg_cost": 0.0, "side": "FLAT"}
    return _order_mgr.get_position()


# ─── Chart Layout Save/Load (TradingView save_load_adapter) ──────────────────

@app.get("/api/charts")
async def list_charts():
    return db.get_all_charts()


@app.post("/api/charts")
async def save_chart_endpoint(request: Request):
    data = await request.json()
    chart_id = db.save_chart(
        chart_id=data.get("id"),
        name=data["name"],
        symbol=data.get("symbol", ""),
        resolution=data.get("resolution", ""),
        content=data.get("content", ""),
        timestamp=data.get("timestamp", int(time.time())),
    )
    return {"id": chart_id}


@app.get("/api/charts/{chart_id}")
async def get_chart(chart_id: int):
    content = db.get_chart_content(chart_id)
    if content is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"content": content}


@app.delete("/api/charts/{chart_id}")
async def delete_chart(chart_id: int):
    db.remove_chart(chart_id)
    return {"ok": True}


# ── Study templates
@app.get("/api/study_templates")
async def list_study_templates():
    return db.get_all_study_templates()


@app.post("/api/study_templates")
async def save_study_template_endpoint(request: Request):
    data = await request.json()
    db.save_study_template(data["name"], data.get("content", ""))
    return {"ok": True}


@app.get("/api/study_templates/{name}")
async def get_study_template(name: str):
    content = db.get_study_template_content(name)
    if content is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"content": content}


@app.delete("/api/study_templates/{name}")
async def delete_study_template(name: str):
    db.remove_study_template(name)
    return {"ok": True}


# ── Drawing templates
@app.get("/api/drawing_templates/{tool_name}")
async def list_drawing_templates(tool_name: str):
    return db.get_drawing_templates(tool_name)


@app.post("/api/drawing_templates")
async def save_drawing_template_endpoint(request: Request):
    data = await request.json()
    db.save_drawing_template(data["tool_name"], data["template_name"], data.get("content", ""))
    return {"ok": True}


@app.get("/api/drawing_templates/{tool_name}/{template_name}")
async def get_drawing_template(tool_name: str, template_name: str):
    content = db.load_drawing_template(tool_name, template_name)
    if content is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"content": content}


@app.delete("/api/drawing_templates/{tool_name}/{template_name}")
async def delete_drawing_template(tool_name: str, template_name: str):
    db.remove_drawing_template(tool_name, template_name)
    return {"ok": True}


# ── Chart templates
@app.get("/api/chart_templates")
async def list_chart_templates():
    return db.get_all_chart_templates()


@app.post("/api/chart_templates")
async def save_chart_template_endpoint(request: Request):
    data = await request.json()
    db.save_chart_template(data["name"], json.dumps(data.get("content", {})))
    return {"ok": True}


@app.get("/api/chart_templates/{name}")
async def get_chart_template(name: str):
    content = db.get_chart_template_content(name)
    if content is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return json.loads(content)


@app.delete("/api/chart_templates/{name}")
async def delete_chart_template(name: str):
    db.remove_chart_template(name)
    return {"ok": True}


# ─── Strategy Backtest API ────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    symbol:             str   = "MES"
    timeframe:          str   = "5min"
    from_ts:            int   = 0
    to_ts:              int   = 9_999_999_999
    ibs_threshold:      float = 0.70
    rr_ratio:           float = 1.0
    use_context_filter: bool  = True
    max_stop_loss:      float = 200.0


@app.post("/api/strategy/backtest")
async def run_strategy_backtest(req: BacktestRequest):
    try:
        result = strategy_backtest.run_backtest(
            symbol=req.symbol,
            timeframe=req.timeframe,
            from_ts=req.from_ts,
            to_ts=req.to_ts,
            ibs_threshold=req.ibs_threshold,
            rr_ratio=req.rr_ratio,
            use_context_filter=req.use_context_filter,
            max_stop_loss=req.max_stop_loss,
        )
        return result
    except Exception as e:
        logger.error("Backtest error: %s", e, exc_info=True)
        return JSONResponse({"error": "Backtest failed. Check server logs for details."}, status_code=500)


@app.get("/api/strategy/backtests")
async def list_backtests():
    rows = db.get_all_backtests()
    for r in rows:
        r["params"]  = json.loads(r.pop("params_json",  "{}"))
        r["summary"] = json.loads(r.pop("summary_json", "{}"))
    return rows


@app.get("/api/strategy/backtests/{backtest_id}/trades")
async def get_backtest_trades(backtest_id: str):
    row = db.get_backtest_by_id(backtest_id)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    trades = db.get_trades_for_backtest(backtest_id)
    return {"backtest_id": backtest_id, "trades": trades}


@app.delete("/api/strategy/backtests/{backtest_id}")
async def delete_backtest(backtest_id: str):
    ok = db.delete_backtest(backtest_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


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
