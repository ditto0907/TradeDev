import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.dependencies import get_app_state

from app.state import MES_SYM
from core import config
from storage import db
from marketdata import data_manager

logger = logging.getLogger(__name__)

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parents[2]
charting_lib_path = BASE_DIR / "charting_library"
static_path = BASE_DIR / "static"




def mount_static_files(app: FastAPI):
    if charting_lib_path.exists():
        app.mount("/charting_library", StaticFiles(directory=str(charting_lib_path)), name="charting_library")
    if static_path.exists():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


@router.get("/")
async def index():
    return FileResponse(str(static_path / "index.html"))


@router.get("/datavalid")
async def datavalid_page():
    return FileResponse(str(static_path / "datavalid.html"))


@router.get("/trademgmt")
async def trademgmt_page():
    return FileResponse(str(static_path / "trademgmt.html"))


@router.get("/api/config")
async def get_config():
    return {
        "supported_resolutions": ["5", "15", "60", "1D"],
        "exchanges": [{"value": "CME", "name": "CME", "desc": "Chicago Mercantile Exchange"}],
        "symbols_types": [{"name": "Futures", "value": "futures"}],
        "supports_marks": False,
        "supports_timescale_marks": False,
        "supports_time": True,
    }


@router.get("/api/symbol_list")
async def get_symbol_list():
    """Return all routable chart tokens (per design §5.1).

    For each base symbol we expose:
      * ``SYMBOL@CONT_FRONT``  — continuous, no adjustment (default)
      * ``SYMBOL@CONT_RATIO``  — continuous, ratio-adjusted
      * ``SYMBOL@CONT_DIFF``   — continuous, difference-adjusted
      * ``SYMBOL@YYYYMM``      — every contract month with bars on disk

    The frontend renders these in a symbol selector; ``/api/history``
    accepts the resulting ``symbol`` value as a token.
    """
    out: list = []
    base_symbols = sorted({
        "MES",
        *(s["symbol"] for s in getattr(config, "EXTRA_SYMBOLS", [])),
    })
    for sym in base_symbols:
        out.append({"token": f"{sym}@CONT_FRONT",
                    "label": f"{sym} (Continuous, no adjustment)",
                    "kind": "continuous", "method": "front"})
        out.append({"token": f"{sym}@CONT_RATIO",
                    "label": f"{sym} (Continuous, ratio-adjusted)",
                    "kind": "continuous", "method": "cont_ratio"})
        out.append({"token": f"{sym}@CONT_DIFF",
                    "label": f"{sym} (Continuous, difference-adjusted)",
                    "kind": "continuous", "method": "cont_difference"})
        try:
            cms = db.get_distinct_contract_months(sym, "5min")
        except Exception:
            cms = []
        for cm in cms:
            out.append({
                "token": f"{sym}@{cm}",
                "label": f"{sym} {cm[:4]}-{cm[4:]}",
                "kind": "month",
                "contract_month": cm,
            })
    return {"symbols": out}


@router.get("/api/symbols")
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
        "NK225M": {
            "name": "NK225M", "full_name": "OSE:NK225M",
            "description": "Mini Nikkei 225 Futures",
            "exchange": "OSE", "listed_exchange": "OSE",
            "pricescale": 1, "minmov": 5,
            "timezone": "Asia/Tokyo",
            "session_eth": "0845-1545,1700-0600:23456",
            "session_rth": "0845-1545:23456",
            "ib_symbol": "N225M", "ib_exchange": "OSE.JPN",
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
        "session": meta["session_rth"],
        "has_intraday": True,
        "supported_resolutions": ["5", "15", "60", "1D"],
        "intraday_multipliers": ["5", "15", "60"],
        "has_no_volume": False, "volume_precision": 0,
        "data_status": "streaming",
        "subsession_id": "regular",
        "subsessions": [
            {"id": "regular", "description": "Regular Trading Hours", "session": meta["session_rth"]},
            {"id": "extended", "description": "Extended Hours", "session": meta["session_eth"]},
        ],
    }


@router.get("/api/history")
async def get_history(
    request: Request,
    symbol: str = Query("MES"),
    resolution: str = Query("5"),
    from_ts: int = Query(0, alias="from"),
    to_ts: int = Query(db.MAX_TIMESTAMP, alias="to"),
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
    from marketdata.ib_fetcher import resolution_to_key, _key_to_ib
    from marketdata.trading_calendar import get_calendar

    state = get_app_state(request)
    key = resolution_to_key(resolution)
    raw_symbol = symbol

    routed_cm: Optional[str] = None
    routed_method: Optional[str] = None
    if "@" in raw_symbol:
        try:
            from marketdata import continuous_view as _cv
            parsed = _cv.parse_token(raw_symbol)
            sym = parsed["symbol"].upper()
            if parsed["kind"] == "month":
                routed_cm = parsed["contract_month"]
            else:
                routed_method = parsed["method"]
        except Exception as e:
            logger.warning("Bad symbol token %r: %s", raw_symbol, e)
            sym = raw_symbol.split("@", 1)[0].upper()
    else:
        sym = raw_symbol.upper()

    try:
        cal = get_calendar(sym)
    except Exception:
        cal = None

    bars = db.get_bars(sym, key, from_ts=from_ts, to_ts=to_ts,
                       contract_month=routed_cm)
    earliest_db = db.get_earliest_ts(sym, key)
    latest_db = db.get_latest_ts(sym, key)

    import zoneinfo as _zi
    _inst = config.INSTRUMENTS.get(sym)
    _tz_log = _zi.ZoneInfo(_inst["timezone"]) if _inst else _zi.ZoneInfo("America/New_York")

    def _fmt_ts(ts):
        if ts is None:
            return "None"
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_tz_log).strftime("%m-%d %H:%M")

    logger.info(
        "[%s/%s] DB check: %d bars in range [%s→%s], DB coverage=[%s→%s]",
        sym, key, len(bars),
        _fmt_ts(from_ts), _fmt_ts(to_ts),
        _fmt_ts(earliest_db), _fmt_ts(latest_db),
    )

    _, interval = _key_to_ib(key)
    now_ts = int(time.time())
    fetch_ranges: list = []
    right_gap_index: int = -1
    left_gap_index: int = -1

    if earliest_db is None:
        capped_to = min(to_ts, now_ts)
        if capped_to > from_ts:
            fetch_ranges.append((from_ts, capped_to))
            logger.info(
                "[%s/%s] No data in DB — will fetch full range [%s→%s] from IB",
                sym, key, from_ts, capped_to,
            )
    else:
        if from_ts < earliest_db:
            left_cooldown_key = f"left_{sym}_{key}"
            left_cooldown_until = state.ib_fetch_cooldown.get(left_cooldown_key, 0)
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

        if (not bars
                and not any(r[0] == from_ts for r in fetch_ranges)
                and earliest_db <= from_ts
                and latest_db is not None
                and latest_db >= to_ts):
            capped_to = min(to_ts, now_ts)
            cooldown_key = f"mid_{sym}_{key}_{from_ts}"
            if now_ts >= state.ib_fetch_cooldown.get(cooldown_key, 0):
                state.ib_fetch_cooldown[cooldown_key] = now_ts + 300
                fetch_ranges.append((from_ts, capped_to))
                logger.info(
                    "[%s/%s] Middle hole: request [%s→%s] falls inside a DB gap "
                    "(earliest_db=%s, latest_db=%s) — fetching from IB",
                    sym, key, from_ts, to_ts, earliest_db, latest_db,
                )

        if latest_db is not None:
            capped_to = min(to_ts, now_ts)
            gap_right = capped_to - latest_db
            max_gap = 30 * 86400 if interval >= 86400 else 3 * 86400

            if gap_right > interval * 2:
                if gap_right > max_gap:
                    logger.warning(
                        "[%s/%s] Right gap %ds (%.1f days) exceeds max %ds — "
                        "capping fetch to avoid timeout.",
                        sym, key, gap_right, gap_right / 86400, max_gap,
                    )
                    capped_to = min(capped_to, latest_db + max_gap)
                    gap_right = capped_to - latest_db

                cooldown_key = (sym, key)
                cooldown_until = state.ib_fetch_cooldown.get(cooldown_key, 0)
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

    any_fetched = False
    ib_ready = state.fetcher._ib_ready and state.fetcher.ib and state.fetcher.ib.isConnected()

    if fetch_ranges and ib_ready:
        async with state.fetcher.chart_priority():
            for idx, (f_from, f_to) in enumerate(fetch_ranges):
                logger.info(
                    "[%s/%s] IB fetch start: range [%s→%s]", sym, key, f_from, f_to,
                )
                try:
                    fetched = await state.fetcher.fetch_range(key, f_from, f_to, symbol=sym)
                    if fetched:
                        _saved = state.fetcher.persist_bars(sym, key, fetched,
                                                            source="ib_historical")
                        saved = _saved.get("inserted", 0) if isinstance(_saved, dict) else int(_saved or 0)
                        logger.info(
                            "[%s/%s] IB fetch OK: %d bars fetched, %d saved to DB",
                            sym, key, len(fetched), saved,
                        )
                        any_fetched = True
                        state.ib_fetch_cooldown.pop((sym, key), None)
                        state.ib_fetch_cooldown.pop(f"mid_{sym}_{key}_{f_from}", None)
                        state.ib_fetch_cooldown.pop(f"left_{sym}_{key}", None)
                    else:
                        logger.info(
                            "[%s/%s] IB returned 0 bars for range [%s→%s]",
                            sym, key, f_from, f_to,
                        )
                        if idx == right_gap_index:
                            state.ib_fetch_cooldown[(sym, key)] = now_ts + state.IB_COOLDOWN_NO_DATA
                        if idx == left_gap_index:
                            state.ib_fetch_cooldown[f"left_{sym}_{key}"] = now_ts + state.IB_COOLDOWN_NO_DATA
                            logger.info(
                                "[%s/%s] Left gap: IB returned no data — cooldown %ds",
                                sym, key, state.IB_COOLDOWN_NO_DATA,
                            )
                except Exception as e:
                    logger.warning(
                        "[%s/%s] IB fetch failed for range [%s→%s]: %s",
                        sym, key, f_from, f_to, e,
                    )
                    if idx == right_gap_index:
                        state.ib_fetch_cooldown[(sym, key)] = now_ts + state.IB_COOLDOWN_ERROR
                    if idx == left_gap_index:
                        state.ib_fetch_cooldown[f"left_{sym}_{key}"] = now_ts + state.IB_COOLDOWN_ERROR
    elif fetch_ranges:
        logger.debug(
            "[%s/%s] Data gaps detected but IB not ready — skipping fetch",
            sym, key,
        )
        for r_from, _ in fetch_ranges:
            state.ib_fetch_cooldown.pop(f"mid_{sym}_{key}_{r_from}", None)

    if any_fetched:
        bars = db.get_bars(sym, key, from_ts=from_ts, to_ts=to_ts,
                           contract_month=routed_cm)
        logger.info("[%s/%s] After IB fill: %d bars in range", sym, key, len(bars))

    if bars and len(bars) >= 2 and ib_ready:
        _internal_gap_cooldown_key = f"internal_{sym}_{key}"
        if now_ts >= state.ib_fetch_cooldown.get(_internal_gap_cooldown_key, 0):
            if cal:
                internal_gaps = [
                    (g["gap_start"], g["gap_end"], g["gap_seconds"])
                    for g in cal.find_gaps(bars, interval)
                    if g["gap_type"] == "data_gap"
                ]
            else:
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
                            ib_bars = await state.fetcher.fetch_range(
                                key, chunk_start, chunk_end, symbol=sym)
                            if ib_bars:
                                gap_bars = [b for b in ib_bars
                                            if g_from < b["time"] < g_to]
                                if gap_bars:
                                    _saved = state.fetcher.persist_bars(sym, key, gap_bars,
                                                                        source="ib_historical")
                                    saved = _saved.get("inserted", 0) if isinstance(_saved, dict) else int(_saved or 0)
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
                    bars = db.get_bars(sym, key, from_ts=from_ts, to_ts=to_ts,
                                       contract_month=routed_cm)
                    logger.info("[%s/%s] After internal fill: %d bars", sym, key, len(bars))
                else:
                    state.ib_fetch_cooldown[_internal_gap_cooldown_key] = now_ts + 300

    if routed_method:
        from marketdata import continuous_view as _cv
        bars = _cv.assemble_continuous(
            sym, key, from_ts, to_ts, method=routed_method,
        )

    if bars and len(bars) >= 2:
        _GAP_THRESHOLD = max(interval * 8, 14400)
        last_big_gap_idx = -1
        for i in range(1, len(bars)):
            gap_sec = bars[i]["time"] - bars[i - 1]["time"]
            if gap_sec >= _GAP_THRESHOLD:
                if cal:
                    gap_type = cal.classify_gap(bars[i - 1]["time"], bars[i]["time"])
                    if gap_type in ("weekend", "holiday", "maintenance", "normal"):
                        continue
                else:
                    from datetime import timezone as _tz5, timedelta as _td5
                    _et5 = _tz5(_td5(hours=-4))
                    prev_dt = datetime.fromtimestamp(bars[i - 1]["time"], tz=_tz5.utc).astimezone(_et5)
                    next_dt = datetime.fromtimestamp(bars[i]["time"], tz=_tz5.utc).astimezone(_et5)
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

    if not bars and sym == MES_SYM:
        bars = state.fetcher.get_bars(key, from_ts=from_ts, to_ts=to_ts)

    if countback and len(bars) > countback:
        effective = max(countback, min(countback * 4, len(bars) - 1))
        bars = bars[-effective:]

    if not bars:
        next_ts = db.get_latest_ts_before(sym, key, from_ts)
        resp: dict = {"s": "no_data"}
        if next_ts is not None:
            resp["nextTime"] = next_ts
        return resp

    _append_rt = (routed_cm is None) and (routed_method in (None, "front"))
    if _append_rt:
        rt_key = (sym, key)
        rt_bar = state.prev_completed_bar.get(rt_key)
        if rt_bar and from_ts <= rt_bar["time"] <= to_ts:
            if bars and bars[-1]["time"] == rt_bar["time"]:
                bars[-1] = rt_bar
            elif not bars or rt_bar["time"] > bars[-1]["time"]:
                bars.append(rt_bar)

    return {
        "s": "ok",
        "t": [b["time"] for b in bars],
        "o": [b["open"] for b in bars],
        "h": [b["high"] for b in bars],
        "l": [b["low"] for b in bars],
        "c": [b["close"] for b in bars],
        "v": [b["volume"] for b in bars],
    }


@router.get("/api/watchlist_prices")
async def get_watchlist_prices():
    """Return latest close price and daily change for all watchlist symbols."""
    symbols = ["MES", "MNQ", "NK225M", "NK225MC", "MGC"]
    result = {}
    for sym in symbols:
        bars = db.get_bars(sym, "5min")
        if not bars:
            result[sym] = {"close": None, "change_pct": None}
            continue
        close = bars[-1]["close"]
        open_price = bars[0]["open"]
        now = int(time.time())
        recent = [b for b in bars if b["time"] > now - 86400]
        if recent:
            open_price = recent[0]["open"]
        chg_pct = ((close - open_price) / open_price * 100) if open_price else 0
        result[sym] = {"close": close, "change_pct": round(chg_pct, 2)}
    return result


@router.get("/api/time")
async def get_time():
    return int(time.time())


@router.get("/api/analysis")
async def get_analysis(request: Request, symbol: str = Query("MES")):
    state = get_app_state(request)
    sym = symbol.upper()
    if sym == MES_SYM:
        return state.latest_analysis or {
            "support_levels": [], "resistance_levels": [],
            "market_cycle": "unknown", "cycle_ranges": [],
        }
    bars = db.get_bars(sym, "5min")
    if not bars:
        return {"support_levels": [], "resistance_levels": [],
                "market_cycle": "unknown", "cycle_ranges": []}
    return state.analyzer.get_analysis(bars)
