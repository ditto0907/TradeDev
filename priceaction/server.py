"""
FastAPI server — TradingView UDF REST API, WebSocket real-time feed,
SQLite persistence, and IB order submission.

Start with:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""
import asyncio
import json
import logging
import logging.handlers
import os
import time
from datetime import datetime, timezone
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
import strategy_backtest

# ─── Logging Setup ─────────────────────────────────────────────────────────────
# Console handler (existing behavior)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")

# Hourly rotating file handler in priceaction/log/
_LOG_DIR = Path(__file__).parent / "log"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_file_handler = logging.handlers.TimedRotatingFileHandler(
    filename=str(_LOG_DIR / "server.log"),
    when="H",             # rotate every hour
    interval=1,
    backupCount=168,      # keep 7 days of hourly logs (24 * 7)
    encoding="utf-8",
    utc=True,
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
_file_handler.suffix = "%Y%m%d_%H"
logging.getLogger().addHandler(_file_handler)

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
_prev_completed_bar:  dict = {}   # {(symbol, bar_size_key): bar_dict}

# Cooldown map: avoid re-fetching from IB when market is closed / no data
# {(symbol, db_key): expiry_unix_ts}
_ib_fetch_cooldown:   dict = {}
_IB_COOLDOWN_NO_DATA: int  = 300   # 5 min cooldown after IB returns 0 bars
_IB_COOLDOWN_ERROR:   int  = 60    # 1 min cooldown after IB fetch exception


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

def on_new_bar(bar_size_key: str, bar: dict, symbol: str = None):
    """
    Called on every tick (reqMktData) or 5-second bar for any symbol.

    Realtime bars are persisted to a separate ``realtime_bars`` table so the
    latest in-progress bar survives a server restart.  They are kept separate
    from the ``bars`` table (IB historical data) to avoid corrupting the
    historical record.  When a bar completes (timestamp advances), the previous
    bar is discarded from memory; the next /api/history call will fill it
    from IB historical data.

    Re-runs analysis on new 5min bars and broadcasts bar+analysis.
    """
    global _latest_analysis, _last_analysis_bar_ts

    # Default to MES for backward compatibility with legacy single-symbol dispatch
    if symbol is None:
        symbol = MES_SYM

    prev_key = (symbol, bar_size_key)
    prev = _prev_completed_bar.get(prev_key)
    if prev is not None and bar["time"] > prev["time"]:
        # The previous bar just completed — write it to DB immediately so
        # getBars requests won't miss it while waiting for IB historical fetch.
        # Use source="realtime_completed" so IB historical can overwrite later.
        try:
            db.insert_bars(symbol, bar_size_key, [prev], source="realtime_completed")
            logger.debug("Completed bar written to DB: %s/%s ts=%s", symbol, bar_size_key, prev["time"])
        except Exception as e:
            logger.warning("Failed to write completed bar for %s/%s: %s", symbol, bar_size_key, e)
        # Also buffer for Google Sheets
        if symbol == MES_SYM:
            sheets.buffer_bar(bar_size_key, prev)
    _prev_completed_bar[prev_key] = dict(bar)

    # Persist the in-progress realtime bar to its own table so it survives
    # a server restart and the chart shows the latest bar immediately on reload.
    # Wrap in try-except to prevent SQLite concurrency errors from flooding logs.
    try:
        db.upsert_realtime_bar(symbol, bar_size_key, bar)
    except Exception as e:
        # Silently ignore DB errors in high-frequency callbacks — realtime_bars
        # is only for crash recovery, occasional failures won't break functionality.
        # Log at DEBUG level to avoid noise but still troubleshoot if needed.
        logger.debug("Failed to upsert realtime bar for %s/%s: %s", symbol, bar_size_key, e)

    # Re-run price-action analysis only when a new 5min bar opens (MES only)
    analysis_updated = False
    if symbol == MES_SYM and bar_size_key == "5min" and bar["time"] > _last_analysis_bar_ts:
        _last_analysis_bar_ts = bar["time"]
        _latest_analysis = analyzer.get_analysis(fetcher.get_bars("5min"))
        analysis_updated = True

    asyncio.create_task(broadcast({
        "type": "bar", "bar_size": bar_size_key, "bar": bar, "symbol": symbol,
    }))
    if analysis_updated:
        asyncio.create_task(broadcast({
            "type": "analysis", "data": _latest_analysis,
        }))


async def _fill_internal_gaps(sym: str, tf: str, fetcher_obj, max_age_days: int = 7):
    """Scan DB bars for internal gaps and fill them from IB.

    Uses the instrument's TradingCalendar for gap classification so that
    maintenance breaks, weekends, and holidays are skipped correctly for
    ALL symbols (not just US futures).

    Only scans bars within the last *max_age_days* to avoid re-fetching
    ancient data.
    """
    from ib_data_fetcher import _key_to_ib
    from trading_calendar import get_calendar

    _, interval = _key_to_ib(tf)

    cutoff_ts = int(time.time()) - max_age_days * 86400
    bars = db.get_bars(sym, tf, from_ts=cutoff_ts)
    if len(bars) < 2:
        return

    # Use calendar-aware gap detection
    try:
        cal = get_calendar(sym)
        detected_gaps = cal.find_gaps(bars, interval)
        gaps = [
            (g["gap_start"], g["gap_end"], g["gap_seconds"])
            for g in detected_gaps
            if g["gap_type"] == "data_gap"
        ]
    except Exception:
        # Fallback: simple interval-based detection
        gaps = []
        for i in range(1, len(bars)):
            gap_sec = bars[i]["time"] - bars[i - 1]["time"]
            if gap_sec > interval * 2:
                gaps.append((bars[i - 1]["time"], bars[i]["time"], gap_sec))

    if not gaps:
        logger.info("[%s/%s] No internal data gaps in last %d days", sym, tf, max_age_days)
        return

    logger.info("[%s/%s] Found %d internal data gaps in last %d days — filling from IB",
                sym, tf, len(gaps), max_age_days)

    total_filled = 0
    for from_ts, to_ts, gap_sec in gaps:
        try:
            ib_bars = await fetcher_obj.fetch_range(tf, from_ts, to_ts, symbol=sym)
            if ib_bars:
                # Only insert bars strictly inside the gap (not the boundary bars)
                gap_bars = [b for b in ib_bars
                            if from_ts < b["time"] < to_ts]
                if gap_bars:
                    saved = db.insert_bars(sym, tf, gap_bars, source="ib_historical")
                    total_filled += saved
                    logger.info("[%s/%s] Filled %d bars for gap %s→%s (%ds)",
                                sym, tf, saved, from_ts, to_ts, gap_sec)
                else:
                    logger.debug("[%s/%s] Gap %s→%s (%ds) — IB has no interior bars",
                                 sym, tf, from_ts, to_ts, gap_sec)
            else:
                logger.debug("[%s/%s] Gap %s→%s (%ds) — IB returned 0 bars",
                             sym, tf, from_ts, to_ts, gap_sec)
            await asyncio.sleep(2)  # IB pacing
        except Exception as e:
            logger.warning("[%s/%s] Gap fill failed %s→%s: %s",
                           sym, tf, from_ts, to_ts, e)

    if total_filled:
        logger.info("[%s/%s] Gap fill complete: %d bars inserted", sym, tf, total_filled)
    else:
        logger.info("[%s/%s] Gap fill complete: no new bars needed", sym, tf)


# ─── Startup / Shutdown ───────────────────────────────────────────────────────


async def _prefetch_extra_symbols(fetcher, ib_ok):
    """Prefetch extra symbols in the background (non-blocking)."""
    if not (ib_ok and fetcher.ib and fetcher.ib.isConnected()):
        return
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

            # ── 5min bars (incremental: fetch only missing) ───────────
            if sym_name != MES_SYM:  # MES 5min already fetched above
                since_5m = db.get_latest_ts(sym_name, "5min")
                should_fetch_5m = False
                if since_5m is None:
                    logger.info("[%s] No 5min bars in DB — full fetch", sym_name)
                    dur_5m = config.HISTORY_DURATION_5MIN
                    should_fetch_5m = True
                    filter_since = None
                else:
                    import time as _time
                    gap_sec = int(_time.time()) - since_5m
                    if gap_sec >= 300:
                        from ib_data_fetcher import ib_duration
                        dur_5m = ib_duration(gap_sec)
                        logger.info("[%s] 5min gap %ds → fetching (duration=%s)", sym_name, gap_sec, dur_5m)
                        should_fetch_5m = True
                        filter_since = since_5m
                    else:
                        logger.info("[%s] 5min bars up to date (gap %ds) — skip", sym_name, gap_sec)
                if should_fetch_5m:
                    raw = await asyncio.wait_for(
                        fetcher.ib.reqHistoricalDataAsync(
                            qc, endDateTime="",
                            durationStr=dur_5m,
                            barSizeSetting="5 mins", whatToShow="TRADES",
                            useRTH=False, formatDate=2,
                        ), timeout=60.0,
                    )
                    bars5 = [_bar_to_dict(b) for b in raw]
                    if filter_since:
                        bars5 = [b for b in bars5 if b["time"] > filter_since]
                    if bars5:
                        saved = db.insert_bars(sym_name, "5min", bars5,
                                               source="ib_historical")
                        logger.info("[%s] Saved %d new 5min bars to DB", sym_name, saved)
                    else:
                        logger.info("[%s] IB returned 0 new 5min bars", sym_name)

            # ── 1D bars (always refresh to keep daily chart up to date) ─
            since_1d = db.get_latest_ts(sym_name, "1D")
            existing_1d = db.get_bars(sym_name, "1D")
            if existing_1d and since_1d:
                import time as _time
                gap_days = (_time.time() - since_1d) / 86400
                if gap_days < 1:
                    logger.info("[%s] 1D bars up to date — skip fetch", sym_name)
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
                saved_1d = db.insert_bars(sym_name, "1D", bars_1d,
                                          source="ib_historical")
                logger.info("[%s] Saved %d 1D bars to DB", sym_name, saved_1d)

            # ── Fill internal gaps in 5min data ──────────────────────
            await _fill_internal_gaps(sym_name, "5min", fetcher)

        except Exception as e:
            logger.warning("Prefetch %s failed: %s", sym_name, e)


async def _ib_background_init():
    """Background task: connect to IB, fetch missing bars, subscribe to real-time.
    
    Runs after the server is already serving requests (DB data available).
    This keeps startup to ~100ms (DB load only) while IB work happens async.
    """
    global _order_mgr

    has_db_data = bool(fetcher.bars.get("5min"))
    ib_ok = False

    try:
        await fetcher.connect()

        since_5min = db.get_latest_ts(MES_SYM, "5min")
        await fetcher.load_history(since_5min=since_5min)

        # Persist the freshly fetched bars
        if fetcher.bars["5min"]:
            saved = db.insert_bars(MES_SYM, "5min", fetcher.bars["5min"],
                                   source="ib_historical")
            logger.info("Saved %d 5min bars to DB", saved)

        # Fill internal gaps in MES 5min data (e.g. server downtime)
        await _fill_internal_gaps(MES_SYM, "5min", fetcher)

        ib_ok = True
    except Exception as e:
        logger.error("IB connect/history error: %s", e)

    # Prefetch extra symbols in background (non-blocking)
    asyncio.create_task(_prefetch_extra_symbols(fetcher, ib_ok))

    # Real-time subscription
    if ib_ok:
        try:
            fetcher.add_new_bar_callback(on_new_bar)
            await fetcher.subscribe_mktdata_all()
            logger.info("IB real-time streaming started (all symbols).")

            _order_mgr = IBOrderManager(fetcher.ib, fetcher._contract)

            def _on_order_status(trade):
                asyncio.create_task(broadcast({
                    "type":  "order_update",
                    "order": IBOrderManager._trade_to_dict(trade),
                }))
                status = trade.orderStatus.status
                if status in ("Cancelled", "ApiCancelled", "Inactive"):
                    oid = trade.order.orderId
                    if _order_mgr:
                        _order_mgr._cancel_bracket_siblings(oid)
            fetcher.ib.orderStatusEvent += _on_order_status

        except Exception as e:
            logger.warning("IB real-time subscription failed: %s", e)

    # Google Sheets
    if sheets.authenticate():
        sheets.initial_upload(fetcher.get_bars("5min"))

    # Initial price-action analysis
    global _latest_analysis
    bars_5min = fetcher.get_bars("5min")
    if bars_5min:
        _latest_analysis = analyzer.get_analysis(bars_5min)

    logger.info("IB background initialization complete.")


async def _ib_reconnect_loop():
    """Background loop: retry IB connection every 60 s when disconnected.

    Handles both the initial-startup failure case (all 3 connect attempts
    timed out) and mid-session disconnects (e.g. TWS restart, network blip).
    On successful reconnect it re-fetches missing bars, re-subscribes to
    real-time, and recreates the order manager — so the server recovers
    fully without a restart.
    """
    global _order_mgr
    while True:
        await asyncio.sleep(60)  # check every minute
        try:
            already_ok = (
                fetcher._ib_ready
                and fetcher.ib is not None
                and fetcher.ib.isConnected()
            )
            if already_ok:
                continue

            logger.info("[IB Reconnect] IB not connected — retrying…")

            # Clean up stale connection state
            if fetcher.ib:
                try:
                    fetcher.ib.disconnect()
                except Exception:
                    pass
            fetcher._ib_ready = False
            fetcher._contract = None
            fetcher._contract_cache.clear()

            await fetcher.connect()

            # Incremental history fetch (only bars we're missing)
            since_5min = db.get_latest_ts(MES_SYM, "5min")
            await fetcher.load_history(since_5min=since_5min)
            if fetcher.bars["5min"]:
                saved = db.insert_bars(MES_SYM, "5min", fetcher.bars["5min"],
                                       source="ib_historical")
                logger.info("[IB Reconnect] Saved %d new 5min bars to DB", saved)

            # Realtime subscription (guard against duplicate callbacks)
            if on_new_bar not in fetcher._new_bar_callbacks:
                fetcher.add_new_bar_callback(on_new_bar)
            await fetcher.subscribe_mktdata_all()
            logger.info("[IB Reconnect] Real-time streaming resumed.")

            # Order manager
            _order_mgr = IBOrderManager(fetcher.ib, fetcher._contract)

            def _on_order_status(trade):
                asyncio.create_task(broadcast({
                    "type":  "order_update",
                    "order": IBOrderManager._trade_to_dict(trade),
                }))
                status = trade.orderStatus.status
                if status in ("Cancelled", "ApiCancelled", "Inactive"):
                    oid = trade.order.orderId
                    if _order_mgr:
                        _order_mgr._cancel_bracket_siblings(oid)
            fetcher.ib.orderStatusEvent += _on_order_status

            # Prefetch extra symbols now that IB is available
            asyncio.create_task(_prefetch_extra_symbols(fetcher, True))

            logger.info("[IB Reconnect] Reconnect complete — IB ready.")

        except Exception as e:
            logger.warning("[IB Reconnect] Attempt failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _order_mgr

    logger.info("Starting up…")

    # ── Step 1: init DB and load historical bars (fast, ~100ms) ──────────────
    db.init_db()

    # Load ALL symbols (including MES) through the unified per-symbol store
    all_symbols = [MES_SYM] + [cfg["symbol"] for cfg in config.EXTRA_SYMBOLS]
    for _sym_name in all_symbols:
        fetcher._ensure_symbol_state(_sym_name)
        _sym_bars = db.get_bars(_sym_name, "5min")
        if _sym_bars:
            fetcher._symbol_bars[_sym_name]["5min"] = _sym_bars[-config.MAX_BARS_IN_MEMORY:]
            logger.info("Loaded %d 5min bars for %s from DB",
                        len(fetcher._symbol_bars[_sym_name]["5min"]), _sym_name)
    # Sync legacy self.bars for backward compat
    fetcher._sync_legacy_bars()

    # ── Step 2: seed _prev_completed_bar from saved realtime bars (crash recovery)
    from ib_data_fetcher import _key_to_ib as _k2ib
    _now_ts = int(time.time())
    for rt_row in db.get_all_realtime_bars():
        rt_sym = rt_row["symbol"]
        rt_tf  = rt_row["timeframe"]
        rt_bar = {k: rt_row[k] for k in ("time", "open", "high", "low", "close", "volume")}
        try:
            _, _interval = _k2ib(rt_tf)
        except Exception:
            _interval = 300
        # Only restore bars that are still within the current bar period
        _current_bar_ts = (_now_ts // _interval) * _interval
        if rt_bar["time"] == _current_bar_ts:
            _prev_completed_bar[(rt_sym, rt_tf)] = rt_bar
            logger.info("Restored realtime bar %s/%s ts=%s from DB", rt_sym, rt_tf, rt_bar["time"])
            # Pre-seed fetcher._rt_current for all symbols (unified approach)
            fetcher._rt_current[f"{rt_sym}:{rt_tf}"] = rt_bar
            logger.debug("Pre-seeded _rt_current %s/%s ts=%s from realtime_bars",
                         rt_sym, rt_tf, rt_bar["time"])

    # ── Step 3: initial price-action analysis from DB data ───────────────────
    global _latest_analysis
    bars_5min = fetcher.get_bars("5min")
    if bars_5min:
        _latest_analysis = analyzer.get_analysis(bars_5min)

    # ── Step 4: IB connect + fetch + realtime in background (non-blocking) ───
    _ib_init_task = asyncio.create_task(_ib_background_init())

    # ── Step 5: Background reconnect loop (retries every 60 s if IB drops) ──
    _ib_reconnect_task = asyncio.create_task(_ib_reconnect_loop())

    # ── Step 6: Background data validation (runs after IB init) ──────────────
    async def _bg_validate_after_init():
        """Wait for IB init, then run background validation silently."""
        try:
            await _ib_init_task
        except Exception:
            pass  # IB init may fail; still try validation with cached data
        await asyncio.sleep(30)  # Give IB time to stabilize
        f = fetcher if (fetcher._ib_ready and fetcher.ib and fetcher.ib.isConnected()) else None
        await data_validator.background_validate(fetcher=f)
    _bg_validate_task = asyncio.create_task(_bg_validate_after_init())

    logger.info("Server ready to accept requests (DB-only mode).")

    yield   # ── server is running ────────────────────────────────────────────

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("Shutting down…")
    _ib_reconnect_task.cancel()
    _bg_validate_task.cancel()
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

        # Skip body logging for large-payload layout endpoints (chart saves,
        # study templates, drawing templates) to avoid flooding the log with
        # multi-KB JSON blobs.
        _LAYOUT_PREFIXES = ("/api/charts", "/api/chart_templates",
                            "/api/study_templates", "/api/drawing_templates")
        if any(request.url.path.startswith(p) for p in _LAYOUT_PREFIXES):
            datafeed_logger.debug("%-6s %-40s → %d (%.1f ms) [body omitted]",
                                  request.method,
                                  str(request.url.path) + ("?" + str(request.url.query) if request.url.query else ""),
                                  response.status_code, elapsed_ms)
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        try:
            payload  = json.loads(body)
            summary  = {}
            for k, v in payload.items():
                if isinstance(v, list) and len(v) > 6:
                    summary[k] = f"[{v[0]!r}…{v[-1]!r}]({len(v)})"
                elif isinstance(v, str) and len(v) > 200:
                    summary[k] = f"({len(v)} chars)"
                else:
                    summary[k] = v
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


@app.get("/datavalid")
async def datavalid_page():
    return FileResponse(str(static_path / "datavalid.html"))


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
            "session_eth": "1800-1700:123456",
            "session_rth": "0930-1600:23456",
            "ib_symbol": "MES", "ib_exchange": "CME",
        },
        "MNQ": {
            "name": "MNQ", "full_name": "CME:MNQ",
            "description": "Micro E-mini Nasdaq-100 Futures",
            "exchange": "CME", "listed_exchange": "CME",
            "pricescale": 100, "minmov": 25,
            "timezone": "America/New_York",
            "session_eth": "1800-1700:123456",
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
            "session_eth": "1800-1700:123456",
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
    to_ts:     int = Query(db.MAX_TIMESTAMP, alias="to"),
    countback: int = Query(None),
):
    """
    TradingView DataFeed: getBars.

    Serves bars from SQLite.  If the DB does not fully cover the requested
    [from_ts, to_ts] range, on-demand fetches from IB fill the gaps and
    persist the result to the DB before returning.

    Gap detection uses the instrument's TradingCalendar to accurately
    distinguish data gaps from expected closures (weekends, holidays,
    maintenance breaks).

    A per-symbol cooldown prevents repeated IB calls when the market is
    closed and IB returns no new data.
    Works for ALL supported symbols (MES, MNQ, NK225MC, MGC, …).
    """
    from ib_data_fetcher import resolution_to_key, _key_to_ib
    from trading_calendar import get_calendar

    key  = resolution_to_key(resolution)
    sym  = symbol.upper()

    # Get trading calendar for session-aware gap classification
    try:
        cal = get_calendar(sym)
    except Exception:
        cal = None

    # ── Step 1: Query existing bars from DB ──────────────────────────────────
    bars = db.get_bars(sym, key, from_ts=from_ts, to_ts=to_ts)
    earliest_db = db.get_earliest_ts(sym, key)
    latest_db   = db.get_latest_ts(sym, key)

    # Convert timestamps to exchange-local timezone for readable logging
    import zoneinfo as _zi
    _inst = config.INSTRUMENTS.get(sym)
    _tz_log = _zi.ZoneInfo(_inst["timezone"]) if _inst else _zi.ZoneInfo("America/New_York")
    def _fmt_ts(ts):
        if ts is None: return "None"
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_tz_log).strftime("%m-%d %H:%M")
    logger.info(
        "[%s/%s] DB check: %d bars in range [%s→%s], DB coverage=[%s→%s]",
        sym, key, len(bars),
        _fmt_ts(from_ts), _fmt_ts(to_ts),
        _fmt_ts(earliest_db), _fmt_ts(latest_db),
    )

    # ── Step 2: Determine missing ranges ─────────────────────────────────────
    _, interval = _key_to_ib(key)
    now_ts = int(time.time())
    fetch_ranges: list = []            # [(from, to), ...]
    right_gap_index: int = -1          # index into fetch_ranges for cooldown
    left_gap_index:  int = -1          # index into fetch_ranges for left-gap cooldown

    if earliest_db is None:
        # ---- Case 1: no data at all for this symbol / timeframe -------------
        capped_to = min(to_ts, now_ts)
        if capped_to > from_ts:
            fetch_ranges.append((from_ts, capped_to))
            logger.info(
                "[%s/%s] No data in DB — will fetch full range [%s→%s] from IB",
                sym, key, from_ts, capped_to,
            )
    else:
        # ---- Case 2: left gap (chart scrolled past oldest bar) --------------
        if from_ts < earliest_db:
            left_cooldown_key = f"left_{sym}_{key}"
            left_cooldown_until = _ib_fetch_cooldown.get(left_cooldown_key, 0)
            if now_ts >= left_cooldown_until:
                left_gap_index = len(fetch_ranges)
                fetch_ranges.append((from_ts, earliest_db))
                logger.info(
                    "[%s/%s] Left gap: request starts before DB coverage "
                    "(from=%s < earliest_db=%s)",
                    sym, key, from_ts, earliest_db,
                )
            else:
                logger.debug(
                    "[%s/%s] Left gap detected but in cooldown "
                    "(%ds remaining) — skipping IB fetch",
                    sym, key, left_cooldown_until - now_ts,
                )

        # ---- Case 4: middle hole (request falls entirely inside a DB gap) ---
        if (not bars
                and not any(r[0] == from_ts for r in fetch_ranges)
                and earliest_db <= from_ts
                and latest_db is not None
                and latest_db >= to_ts):
            capped_to = min(to_ts, now_ts)
            cooldown_key = f"mid_{sym}_{key}_{from_ts}"
            if now_ts >= _ib_fetch_cooldown.get(cooldown_key, 0):
                _ib_fetch_cooldown[cooldown_key] = now_ts + 300
                fetch_ranges.append((from_ts, capped_to))
                logger.info(
                    "[%s/%s] Middle hole: request [%s→%s] falls inside a DB gap "
                    "(earliest_db=%s, latest_db=%s) — fetching from IB",
                    sym, key, from_ts, to_ts, earliest_db, latest_db,
                )

        # ---- Case 3: right gap (stale data, no real-time for this symbol) ---
        if latest_db is not None:
            capped_to = min(to_ts, now_ts)
            gap_right = capped_to - latest_db

            max_gap = 30 * 86400 if interval >= 86400 else 3 * 86400

            if gap_right > interval * 2:
                if gap_right > max_gap:
                    logger.warning(
                        "[%s/%s] Right gap %ds (%.1f days) exceeds max %ds — "
                        "capping fetch to avoid timeout.",
                        sym, key, gap_right, gap_right/86400, max_gap,
                    )
                    capped_to = min(capped_to, latest_db + max_gap)
                    gap_right = capped_to - latest_db

                cooldown_key = (sym, key)
                cooldown_until = _ib_fetch_cooldown.get(cooldown_key, 0)
                if now_ts >= cooldown_until:
                    right_gap_index = len(fetch_ranges)
                    fetch_ranges.append((latest_db, capped_to))
                    logger.info(
                        "[%s/%s] Right gap: latest_db=%s is %ds behind request "
                        "end — will fetch newer data from IB",
                        sym, key, latest_db, gap_right,
                    )
                else:
                    logger.debug(
                        "[%s/%s] Right gap detected (%ds) but in cooldown "
                        "(%ds remaining) — skipping",
                        sym, key, gap_right, cooldown_until - now_ts,
                    )

    # ── Step 3: Fetch each missing range from IB ─────────────────────────────
    any_fetched = False
    ib_ready = fetcher._ib_ready and fetcher.ib and fetcher.ib.isConnected()

    if fetch_ranges and ib_ready:
        for idx, (f_from, f_to) in enumerate(fetch_ranges):
            logger.info(
                "[%s/%s] IB fetch start: range [%s→%s]", sym, key, f_from, f_to,
            )
            try:
                fetched = await fetcher.fetch_range(key, f_from, f_to, symbol=sym)
                if fetched:
                    saved = db.insert_bars(sym, key, fetched,
                                           source="ib_historical")
                    logger.info(
                        "[%s/%s] IB fetch OK: %d bars fetched, %d saved to DB",
                        sym, key, len(fetched), saved,
                    )
                    any_fetched = True
                    _ib_fetch_cooldown.pop((sym, key), None)
                    _ib_fetch_cooldown.pop(f"mid_{sym}_{key}_{f_from}", None)
                    _ib_fetch_cooldown.pop(f"left_{sym}_{key}", None)
                else:
                    logger.info(
                        "[%s/%s] IB returned 0 bars for range [%s→%s]",
                        sym, key, f_from, f_to,
                    )
                    if idx == right_gap_index:
                        _ib_fetch_cooldown[(sym, key)] = now_ts + _IB_COOLDOWN_NO_DATA
                    if idx == left_gap_index:
                        _ib_fetch_cooldown[f"left_{sym}_{key}"] = now_ts + _IB_COOLDOWN_NO_DATA
                        logger.info(
                            "[%s/%s] Left gap: IB returned no data — cooldown %ds",
                            sym, key, _IB_COOLDOWN_NO_DATA,
                        )
            except Exception as e:
                logger.warning(
                    "[%s/%s] IB fetch failed for range [%s→%s]: %s",
                    sym, key, f_from, f_to, e,
                )
                if idx == right_gap_index:
                    _ib_fetch_cooldown[(sym, key)] = now_ts + _IB_COOLDOWN_ERROR
                if idx == left_gap_index:
                    _ib_fetch_cooldown[f"left_{sym}_{key}"] = now_ts + _IB_COOLDOWN_ERROR
    elif fetch_ranges:
        logger.debug(
            "[%s/%s] Data gaps detected but IB not ready — skipping fetch",
            sym, key,
        )
        for r_from, _ in fetch_ranges:
            _ib_fetch_cooldown.pop(f"mid_{sym}_{key}_{r_from}", None)

    # Re-query DB after successful fetches
    if any_fetched:
        bars = db.get_bars(sym, key, from_ts=from_ts, to_ts=to_ts)
        logger.info("[%s/%s] After IB fill: %d bars in range", sym, key, len(bars))

    # ── Step 4: Fill internal gaps using calendar-aware detection ────────────
    if bars and len(bars) >= 2 and ib_ready:
        _internal_gap_cooldown_key = f"internal_{sym}_{key}"
        if now_ts >= _ib_fetch_cooldown.get(_internal_gap_cooldown_key, 0):
            # Use trading calendar for gap classification instead of ad-hoc heuristics
            if cal:
                internal_gaps = [
                    (g["gap_start"], g["gap_end"], g["gap_seconds"])
                    for g in cal.find_gaps(bars, interval)
                    if g["gap_type"] == "data_gap"
                ]
            else:
                # Fallback to simple interval-based detection
                internal_gaps = []
                for i in range(1, len(bars)):
                    gap_sec = bars[i]["time"] - bars[i - 1]["time"]
                    if gap_sec > interval * 2:
                        internal_gaps.append((bars[i - 1]["time"], bars[i]["time"], gap_sec))

            if internal_gaps:
                logger.info("[%s/%s] %d internal data gaps detected — filling from IB",
                            sym, key, len(internal_gaps))
                filled_any = False
                _CHUNK = 7 * 86400
                for g_from, g_to, g_sec in internal_gaps:
                    chunk_start = g_from
                    while chunk_start < g_to:
                        chunk_end = min(chunk_start + _CHUNK, g_to)
                        try:
                            ib_bars = await fetcher.fetch_range(
                                key, chunk_start, chunk_end, symbol=sym)
                            if ib_bars:
                                gap_bars = [b for b in ib_bars
                                            if g_from < b["time"] < g_to]
                                if gap_bars:
                                    saved = db.insert_bars(sym, key, gap_bars,
                                                           source="ib_historical")
                                    filled_any = True
                                    logger.info(
                                        "[%s/%s] Filled chunk %s→%s: %d bars",
                                        sym, key, chunk_start, chunk_end, saved)
                            await asyncio.sleep(2)
                        except Exception as e:
                            logger.warning(
                                "[%s/%s] Chunk fill failed %s→%s: %s",
                                sym, key, chunk_start, chunk_end, e)
                        chunk_start = chunk_end
                if filled_any:
                    bars = db.get_bars(sym, key, from_ts=from_ts, to_ts=to_ts)
                    logger.info("[%s/%s] After internal fill: %d bars", sym, key, len(bars))
                else:
                    _ib_fetch_cooldown[_internal_gap_cooldown_key] = now_ts + 300

    # ── Step 5: Strip unfillable data gaps (calendar-aware) ─────────────────
    # Only strip genuine data gaps — let weekends/holidays/maintenance pass through.
    if bars and len(bars) >= 2:
        _GAP_THRESHOLD = max(interval * 8, 14400)  # at least 4 hours
        last_big_gap_idx = -1
        for i in range(1, len(bars)):
            gap_sec = bars[i]["time"] - bars[i - 1]["time"]
            if gap_sec >= _GAP_THRESHOLD:
                if cal:
                    gap_type = cal.classify_gap(bars[i-1]["time"], bars[i]["time"])
                    if gap_type in ("weekend", "holiday", "maintenance", "normal"):
                        continue  # Expected closure — don't strip
                else:
                    # Fallback: basic heuristics
                    from datetime import timezone as _tz5, timedelta as _td5
                    _et5 = _tz5(_td5(hours=-4))
                    prev_dt = datetime.fromtimestamp(bars[i-1]["time"], tz=_tz5.utc).astimezone(_et5)
                    next_dt = datetime.fromtimestamp(bars[i]["time"],   tz=_tz5.utc).astimezone(_et5)
                    if prev_dt.weekday() == 4 and next_dt.weekday() in (0, 6) and gap_sec < 201600:
                        continue
                    if prev_dt.hour >= 16 and next_dt.hour <= 19 and gap_sec < 14400:
                        continue
                last_big_gap_idx = i
        if last_big_gap_idx > 0:
            logger.info(
                "[%s/%s] Stripping %d bars before unfillable gap at idx %d "
                "(keeping %d bars from continuous segment)",
                sym, key, last_big_gap_idx, last_big_gap_idx,
                len(bars) - last_big_gap_idx,
            )
            bars = bars[last_big_gap_idx:]

    # Final fallback: in-memory cache (only valid for the primary symbol MES)
    if not bars and sym == MES_SYM:
        bars = fetcher.get_bars(key, from_ts=from_ts, to_ts=to_ts)

    if countback and len(bars) > countback:
        effective = max(countback, min(countback * 4, len(bars) - 1))
        bars = bars[-effective:]

    if not bars:
        next_ts = db.get_latest_ts_before(sym, key, from_ts)
        resp: dict = {"s": "no_data"}
        if next_ts is not None:
            resp["nextTime"] = next_ts
        return resp

    # ── Append in-progress realtime bar (if any) ────────────────────────────
    rt_key = (sym, key)
    rt_bar = _prev_completed_bar.get(rt_key)
    if rt_bar and from_ts <= rt_bar["time"] <= to_ts:
        if bars and bars[-1]["time"] == rt_bar["time"]:
            bars[-1] = rt_bar
        elif not bars or rt_bar["time"] > bars[-1]["time"]:
            bars.append(rt_bar)

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

    key = resolution_to_key(resolution)
    sym = symbol.upper()

    # Determine the symbol's local timezone for datetime parsing
    inst = config.INSTRUMENTS.get(sym)
    sym_tz = zoneinfo.ZoneInfo(inst["timezone"]) if inst else zoneinfo.ZoneInfo("America/New_York")
    
    # Parse datetime strings if provided (takes priority over timestamps)
    if from_dt:
        try:
            if len(from_dt) == 10:  # "YYYY-MM-DD" format
                dt_obj = _dt.strptime(from_dt, "%Y-%m-%d")
            else:  # "YYYY-MM-DD HH:MM" format
                dt_obj = _dt.strptime(from_dt, "%Y-%m-%d %H:%M")
            # Create timezone-aware datetime in the symbol's local timezone
            dt_obj_local = _dt(dt_obj.year, dt_obj.month, dt_obj.day,
                               dt_obj.hour, dt_obj.minute, dt_obj.second, tzinfo=sym_tz)
            from_ts = int(dt_obj_local.timestamp())
        except ValueError as e:
            return JSONResponse(
                {"error": f"Invalid from_dt format: {e}. Use 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'"},
                status_code=400
            )
    elif from_ts is None:
        from_ts = 0
    
    if to_dt:
        try:
            if len(to_dt) == 10:  # "YYYY-MM-DD" format, use end of day
                dt_obj = _dt.strptime(to_dt, "%Y-%m-%d")
                dt_obj_local = _dt(dt_obj.year, dt_obj.month, dt_obj.day,
                                   23, 59, 59, tzinfo=sym_tz)
            else:  # "YYYY-MM-DD HH:MM" format
                dt_obj = _dt.strptime(to_dt, "%Y-%m-%d %H:%M")
                dt_obj_local = _dt(dt_obj.year, dt_obj.month, dt_obj.day,
                                   dt_obj.hour, dt_obj.minute, dt_obj.second, tzinfo=sym_tz)
            to_ts = int(dt_obj_local.timestamp())
        except ValueError as e:
            return JSONResponse(
                {"error": f"Invalid to_dt format: {e}. Use 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'"},
                status_code=400
            )
    elif to_ts is None:
        to_ts = db.MAX_TIMESTAMP
    
    # Debug logging
    logger.info(f"skill_get_bars: sym={sym}, key={key}, from_ts={from_ts}, to_ts={to_ts}")
    logger.info(f"  from_dt={from_dt}, to_dt={to_dt}")
    
    bars = db.get_bars(sym, key, from_ts=from_ts, to_ts=to_ts)
    logger.info(f"  Retrieved {len(bars)} bars from database")

    if session.upper() == "RTH" and key in ("5min", "1min", "3min", "15min", "30min", "60min"):
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        import zoneinfo

        # Use per-symbol timezone and RTH window from INSTRUMENTS
        inst = config.INSTRUMENTS.get(sym)
        if inst:
            tz = zoneinfo.ZoneInfo(inst["timezone"])
            rth_start = inst["rth_start"]  # (hour, minute)
            rth_end   = inst["rth_end"]
        else:
            tz = zoneinfo.ZoneInfo("America/New_York")
            rth_start = (9, 30)
            rth_end   = (16, 0)

        start_min = rth_start[0] * 60 + rth_start[1]
        end_min   = rth_end[0]   * 60 + rth_end[1]

        filtered = []
        for b in bars:
            dt = _dt.fromtimestamp(b["time"], tz=_tz.utc).astimezone(tz)
            t = dt.hour * 60 + dt.minute
            if start_min <= t < end_min:
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


# ─── Data Validation API ─────────────────────────────────────────────────────

import data_validator


def _parse_dt_eastern(dt_str: str) -> int:
    """Parse a datetime string and return a UTC Unix timestamp.

    Accepted formats:
      - ``YYYY-MM-DD``              (start of day in US/Eastern)
      - ``YYYY-MM-DD HH:MM``        (space separator)
      - ``YYYY-MM-DDTHH:MM``        (ISO 8601 / datetime-local format)
    """
    import pytz
    from datetime import datetime as _dt
    eastern = pytz.timezone("America/New_York")
    s = dt_str.replace("T", " ")
    fmt = "%Y-%m-%d %H:%M" if " " in s else "%Y-%m-%d"
    return int(eastern.localize(_dt.strptime(s, fmt)).timestamp())


@app.get("/api/data/validate")
async def api_validate_bars(
    symbol: str = "MES",
    timeframe: str = "5min",
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    from_dt: Optional[str] = None,
    to_dt: Optional[str] = None,
    contract_month: Optional[str] = None,
    skip_validated: bool = False,
):
    """Validate DB bars against IB historical data for a time range.
    Returns mismatches without fixing them.
    IB data is fetched via the local ib_fetch_cache to reduce IB requests.
    Supply *contract_month* (e.g. '202503') to restrict validation to bars
    belonging to that specific futures contract only.
    Set *skip_validated* to True to skip already-checked ranges."""
    # Convert datetime strings if provided
    if from_dt and not from_ts:
        from_ts = _parse_dt_eastern(from_dt)
    if to_dt and not to_ts:
        to_ts = _parse_dt_eastern(to_dt)

    # Default: last 24 hours
    if from_ts is None:
        from_ts = int(time.time()) - 86400
    if to_ts is None:
        to_ts = int(time.time())

    # Reuse server's IB connection via fetcher (avoids event-loop deadlocks)
    f = fetcher if (fetcher._ib_ready and fetcher.ib and fetcher.ib.isConnected()) else None
    result = await data_validator.validate_bars(
        symbol, timeframe, from_ts, to_ts,
        fetcher=f, contract_month=contract_month,
        skip_validated=skip_validated,
    )
    return result


class FixBarsRequest(BaseModel):
    symbol: str = "MES"
    timeframe: str = "5min"
    from_ts: Optional[int] = None
    to_ts: Optional[int] = None
    from_dt: Optional[str] = None
    to_dt: Optional[str] = None
    timestamps: Optional[List[int]] = None  # subset of timestamps to fix; None = fix all
    contract_month: Optional[str] = None    # restrict fix to this contract month


@app.post("/api/data/fix")
async def api_fix_bars(req: FixBarsRequest):
    """Fix DB bars using IB data (from local ib_fetch_cache when available).

    *timestamps*: optional list of Unix timestamps to restrict fixing to only
    those specific bars (selected rows from the UI).  When omitted every
    mismatch/missing bar in the range is fixed.
    *contract_month*: optional contract month (e.g. '202503') to restrict fix
    to bars belonging to that specific futures contract only.
    """
    from_ts = req.from_ts
    to_ts   = req.to_ts

    if req.from_dt and not from_ts:
        from_ts = _parse_dt_eastern(req.from_dt)
    if req.to_dt and not to_ts:
        to_ts = _parse_dt_eastern(req.to_dt)

    if from_ts is None:
        from_ts = int(time.time()) - 86400
    if to_ts is None:
        to_ts = int(time.time())

    f = fetcher if (fetcher._ib_ready and fetcher.ib and fetcher.ib.isConnected()) else None
    result = await data_validator.fix_bars(
        req.symbol, req.timeframe, from_ts, to_ts,
        fetcher=f,
        timestamps=req.timestamps,
        contract_month=req.contract_month,
    )
    return result


@app.post("/api/data/validate_all")
async def api_validate_all(fix: bool = False):
    """Scan all symbol/timeframe pairs in DB, validate against IB.
    Set fix=true to auto-correct mismatches. This is a long-running operation."""
    f = fetcher if (fetcher._ib_ready and fetcher.ib and fetcher.ib.isConnected()) else None
    results = await data_validator.validate_all(fix=fix, fetcher=f)
    total_mismatches = sum(r["total_mismatches"] for r in results)
    total_fixed = sum(r.get("total_fixed", 0) for r in results)
    return {
        "pairs_checked": len(results),
        "total_mismatches": total_mismatches,
        "total_fixed": total_fixed,
        "details": results,
    }


@app.get("/api/data/validated_ranges")
async def api_validated_ranges(
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
):
    """Return already-checked (validated) time ranges per symbol/timeframe.
    If symbol+timeframe are provided, also returns merged continuous ranges."""
    ranges = db.get_validated_ranges(symbol=symbol, timeframe=timeframe)
    result: dict = {"ranges": ranges}
    if symbol and timeframe:
        result["merged"] = db.get_merged_validated_ranges(symbol, timeframe)
    return result


@app.post("/api/data/bg_validate")
async def api_trigger_bg_validate():
    """Manually trigger the background validation task."""
    f = fetcher if (fetcher._ib_ready and fetcher.ib and fetcher.ib.isConnected()) else None
    asyncio.create_task(data_validator.background_validate(fetcher=f))
    return {"success": True, "message": "Background validation started"}


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
    to_ts:              int   = db.MAX_TIMESTAMP
    ibs_threshold:      float = 0.70
    rr_ratio:           float = 1.0
    use_context_filter: bool  = True
    max_stop_loss:      float = 200.0
    session:            str   = "all"       # 'all' | 'rth' | 'eth'
    time_filter:        str   = ""          # e.g. '10:00-12:00'
    include_filtered:   bool  = True        # include SR-filtered trades in output


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
            session=req.session,
            time_filter=req.time_filter,
            include_filtered=req.include_filtered,
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


# ─── Data Validation ─────────────────────────────────────────────────────────

@app.get("/api/data/gaps")
async def api_data_gaps(
    symbol: str = "MES",
    timeframe: str = "5min",
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
):
    """Detect K-line continuity gaps for a symbol/timeframe."""
    from ib_data_fetcher import _key_to_ib
    try:
        _, interval = _key_to_ib(timeframe)
    except Exception:
        interval = 300
    gaps = db.find_gaps(symbol, timeframe, expected_interval=interval)
    if from_ts:
        gaps = [g for g in gaps if g["gap_end"] >= from_ts]
    if to_ts:
        gaps = [g for g in gaps if g["gap_start"] <= to_ts]
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "total_gaps": len(gaps),
        "data_gaps": len([g for g in gaps if g.get("gap_type") == "data_gap"]),
        "weekend_gaps": len([g for g in gaps if g.get("gap_type") == "weekend"]),
        "holiday_gaps": len([g for g in gaps if g.get("gap_type") == "holiday"]),
        "maintenance_gaps": len([g for g in gaps if g.get("gap_type") == "maintenance"]),
        "gaps": gaps,
    }


@app.get("/api/data/bars_by_source")
async def api_bars_by_source(
    source: str = "realtime",
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    limit: int = 500,
):
    """Query bars by source (e.g. 'realtime' for auto-assembled bars)."""
    sql = "SELECT symbol, timeframe, ts, open, high, low, close, volume, source FROM bars WHERE source=?"
    params: list = [source]
    if symbol:
        sql += " AND symbol=?"
        params.append(symbol)
    if timeframe:
        sql += " AND timeframe=?"
        params.append(timeframe)
    if from_ts:
        sql += " AND ts>=?"
        params.append(from_ts)
    if to_ts:
        sql += " AND ts<=?"
        params.append(to_ts)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with db._conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    bars = [
        {"symbol": r[0], "timeframe": r[1], "time": r[2],
         "open": r[3], "high": r[4], "low": r[5], "close": r[6],
         "volume": r[7], "source": r[8]}
        for r in rows
    ]
    with db._conn() as conn:
        sources = [r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM bars ORDER BY source"
        ).fetchall()]
    return {"bars": bars, "total": len(bars), "available_sources": sources}


@app.post("/api/data/delete_by_source")
async def api_delete_bars_by_source(source: str = "realtime"):
    """Delete all bars with a given source from the database."""
    deleted = db.delete_bars_by_source(source)
    logger.info("Deleted %d bars with source=%s", deleted, source)
    return {"deleted": deleted, "source": source}


@app.get("/api/data/query")
async def api_data_query(
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    source: Optional[str] = None,
    contract_month: Optional[str] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    page: int = 1,
    page_size: int = 50,
    db_table: str = "bars",
):
    """Paginated query of bars from DB with flexible filter conditions.
    page_size is bounded to 1-500.
    Supply *db_table* ('bars' or 'ib_fetch_cache') to choose the data source.
    Supply *contract_month* (e.g. '202503') to restrict results to bars
    belonging to that specific futures contract."""
    # Whitelist allowed tables to prevent SQL injection
    allowed_tables = {"bars", "ib_fetch_cache"}
    if db_table not in allowed_tables:
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid db_table. Must be one of: {', '.join(sorted(allowed_tables))}"},
        )

    page_size = min(max(1, page_size), 500)  # Bound page_size to 1-500

    # ib_fetch_cache has 'fetched_at' instead of 'source'
    is_cache = db_table == "ib_fetch_cache"

    where_clauses = []
    params: list = []
    if symbol:
        where_clauses.append("symbol=?")
        params.append(symbol)
    if timeframe:
        where_clauses.append("timeframe=?")
        params.append(timeframe)
    if not is_cache and source:
        where_clauses.append("source=?")
        params.append(source)
    if contract_month is not None:
        where_clauses.append("contract_month=?")
        params.append(contract_month)
    if from_ts:
        where_clauses.append("ts>=?")
        params.append(from_ts)
    if to_ts:
        where_clauses.append("ts<=?")
        params.append(to_ts)

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    if is_cache:
        select_cols = "symbol, timeframe, ts, open, high, low, close, volume, fetched_at, contract_month"
    else:
        select_cols = "symbol, timeframe, ts, open, high, low, close, volume, source, contract_month"

    with db._conn() as conn:
        # Total count
        count_row = conn.execute(
            f"SELECT COUNT(*) FROM {db_table}{where_sql}", params
        ).fetchone()
        total = count_row[0]

        # Paginated data
        offset = (max(1, page) - 1) * page_size
        data_sql = (
            f"SELECT {select_cols} "
            f"FROM {db_table}{where_sql} ORDER BY ts ASC LIMIT ? OFFSET ?"
        )
        rows = conn.execute(data_sql, params + [page_size, offset]).fetchall()

        # Available filter values (from selected table)
        symbols = [r[0] for r in conn.execute(
            f"SELECT DISTINCT symbol FROM {db_table} ORDER BY symbol"
        ).fetchall()]
        timeframes = [r[0] for r in conn.execute(
            f"SELECT DISTINCT timeframe FROM {db_table} ORDER BY timeframe"
        ).fetchall()]
        if is_cache:
            sources = []
        else:
            sources = [r[0] for r in conn.execute(
                f"SELECT DISTINCT source FROM {db_table} ORDER BY source"
            ).fetchall()]
        contract_months = [r[0] for r in conn.execute(
            f"SELECT DISTINCT contract_month FROM {db_table} WHERE contract_month != '' ORDER BY contract_month"
        ).fetchall()]

    if is_cache:
        bars = [
            {"symbol": r[0], "timeframe": r[1], "time": r[2],
             "open": r[3], "high": r[4], "low": r[5], "close": r[6],
             "volume": r[7], "fetched_at": r[8], "contract_month": r[9]}
            for r in rows
        ]
    else:
        bars = [
            {"symbol": r[0], "timeframe": r[1], "time": r[2],
             "open": r[3], "high": r[4], "low": r[5], "close": r[6],
             "volume": r[7], "source": r[8], "contract_month": r[9]}
            for r in rows
        ]
    total_pages = max(1, (total + page_size - 1) // page_size)

    return {
        "bars": bars,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "db_table": db_table,
        "available_symbols": symbols,
        "available_timeframes": timeframes,
        "available_sources": sources,
        "available_contract_months": contract_months,
    }


# ─── Data Maintenance Tools API ──────────────────────────────────────────────
# Standard operations for inspecting, diagnosing, and repairing bar data.
# These endpoints power the Data Validation UI and support batch operations.

@app.get("/api/data/integrity")
async def api_data_integrity(
    symbol: str = "MES",
    timeframe: str = "5min",
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
):
    """Generate a data integrity report: counts, source breakdown, OHLCV violations."""
    report = db.get_integrity_report(
        symbol, timeframe,
        from_ts=from_ts or 0,
        to_ts=to_ts or db.MAX_TIMESTAMP,
    )
    return report


@app.get("/api/data/coverage")
async def api_data_coverage():
    """Return coverage summary for all symbol/timeframe pairs in the DB."""
    return db.get_coverage()


@app.get("/api/data/bar")
async def api_get_bar(
    symbol: str = "MES",
    timeframe: str = "5min",
    ts: int = Query(...),
):
    """Point-inspect a single bar by its exact timestamp."""
    bar = db.get_bar_at(symbol, timeframe, ts)
    if bar is None:
        return JSONResponse({"error": "Bar not found"}, status_code=404)
    return bar


class DeleteBarsRangeRequest(BaseModel):
    symbol: str = "MES"
    timeframe: str = "5min"
    from_ts: int
    to_ts: int


@app.post("/api/data/delete_range")
async def api_delete_bars_range(req: DeleteBarsRangeRequest):
    """Delete bars in a specific time range."""
    deleted = db.delete_bars_range(req.symbol, req.timeframe, req.from_ts, req.to_ts)
    logger.info("Deleted %d bars for %s/%s in [%d→%d]",
                deleted, req.symbol, req.timeframe, req.from_ts, req.to_ts)
    return {"deleted": deleted, "symbol": req.symbol, "timeframe": req.timeframe}


class DeleteBarsByTimestampsRequest(BaseModel):
    symbol: str = "MES"
    timeframe: str = "5min"
    timestamps: List[int]


@app.post("/api/data/delete_bars")
async def api_delete_bars_by_timestamps(req: DeleteBarsByTimestampsRequest):
    """Delete specific bars by their exact timestamps."""
    deleted = db.delete_bars_by_timestamps(req.symbol, req.timeframe, req.timestamps)
    logger.info("Deleted %d bars for %s/%s by timestamps",
                deleted, req.symbol, req.timeframe)
    return {"deleted": deleted, "symbol": req.symbol, "timeframe": req.timeframe}


@app.post("/api/data/fix_ohlcv")
async def api_fix_ohlcv(
    symbol: str = "MES",
    timeframe: str = "5min",
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
):
    """Fix OHLCV violations: swap high/low, clamp open/close, delete invalid bars."""
    fixed = db.fix_ohlcv_violations(
        symbol, timeframe,
        from_ts=from_ts or 0,
        to_ts=to_ts or db.MAX_TIMESTAMP,
    )
    return {"fixed": fixed, "symbol": symbol, "timeframe": timeframe}


@app.get("/api/data/calendar_gaps")
async def api_calendar_gaps(
    symbol: str = "MES",
    timeframe: str = "5min",
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
):
    """Detect gaps using the instrument's trading session calendar.
    Returns only genuine data gaps (not weekends/holidays/maintenance)."""
    from trading_calendar import get_calendar
    from ib_data_fetcher import _key_to_ib
    try:
        _, interval = _key_to_ib(timeframe)
    except Exception:
        interval = 300

    try:
        cal = get_calendar(symbol)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    bars = db.get_bars(symbol, timeframe,
                       from_ts=from_ts or 0,
                       to_ts=to_ts or db.MAX_TIMESTAMP)
    if len(bars) < 2:
        return {"symbol": symbol, "timeframe": timeframe, "gaps": [], "data_gaps": 0}

    all_gaps = cal.find_gaps(bars, interval)
    data_gaps = [g for g in all_gaps if g["gap_type"] == "data_gap"]

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "total_gaps": len(all_gaps),
        "data_gaps": len(data_gaps),
        "gaps": all_gaps,
    }


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
