import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.state import AppState, MES_SYM
from app.websocket import broadcast
from core import config
from marketdata import data_manager, data_validator, realtime_builder
from marketdata.ib_fetcher import _bar_to_dict
from storage import db
from trading.order_manager import IBOrderManager
from trading.trade_log_parser import load_all_trades

logger = logging.getLogger(__name__)

def _make_order_status_handler(state: AppState):
    """Return an IB order-status event handler that closes over *state*."""
    def _on_order_status(trade):
        asyncio.create_task(broadcast(state, {
            "type": "order_update",
            "order": IBOrderManager._trade_to_dict(trade),
        }))
        status = trade.orderStatus.status
        if status in ("Cancelled", "ApiCancelled", "Inactive"):
            oid = trade.order.orderId
            if state.order_mgr:
                state.order_mgr._cancel_bracket_siblings(oid)
    return _on_order_status



def on_new_bar(state: AppState, bar_size_key: str, bar: dict, symbol: str = None):
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
    if symbol is None:
        symbol = MES_SYM

    prev_key = (symbol, bar_size_key)
    prev = state.prev_completed_bar.get(prev_key)
    if prev is not None and bar["time"] > prev["time"]:
        saved = realtime_builder.persist_completed_bar(
            state.fetcher, symbol, bar_size_key, prev,
        )
        if saved:
            logger.debug("Completed bar written to DB: %s/%s ts=%s",
                         symbol, bar_size_key, prev["time"])
        if symbol == MES_SYM:
            state.sheets.buffer_bar(bar_size_key, prev)
    state.prev_completed_bar[prev_key] = dict(bar)

    realtime_builder.persist_inprogress_bar(symbol, bar_size_key, bar)

    analysis_updated = False
    if symbol == MES_SYM and bar_size_key == "5min" and bar["time"] > state.last_analysis_bar_ts:
        state.last_analysis_bar_ts = bar["time"]
        state.latest_analysis = state.analyzer.get_analysis(state.fetcher.get_bars("5min"))
        analysis_updated = True

    asyncio.create_task(broadcast(state, {
        "type": "bar", "bar_size": bar_size_key, "bar": bar, "symbol": symbol,
    }))
    if analysis_updated:
        asyncio.create_task(broadcast(state, {
            "type": "analysis", "data": state.latest_analysis,
        }))


async def _fill_internal_gaps(sym: str, tf: str, fetcher_obj, max_age_days: int = 7):
    """Scan DB bars for internal gaps and fill them from IB.

    Uses the instrument's TradingCalendar for gap classification so that
    maintenance breaks, weekends, and holidays are skipped correctly for
    ALL symbols (not just US futures).

    Only scans bars within the last *max_age_days* to avoid re-fetching
    ancient data.
    """
    from marketdata.ib_fetcher import _key_to_ib
    from marketdata.trading_calendar import get_calendar

    _, interval = _key_to_ib(tf)

    cutoff_ts = int(time.time()) - max_age_days * 86400
    bars = db.get_bars(sym, tf, from_ts=cutoff_ts)
    if len(bars) < 2:
        return

    try:
        cal = get_calendar(sym)
        detected_gaps = cal.find_gaps(bars, interval)
        gaps = [
            (g["gap_start"], g["gap_end"], g["gap_seconds"])
            for g in detected_gaps
            if g["gap_type"] == "data_gap"
        ]
    except Exception:
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
                gap_bars = [b for b in ib_bars if from_ts < b["time"] < to_ts]
                if gap_bars:
                    _saved = fetcher_obj.persist_bars(sym, tf, gap_bars, source="ib_historical")
                    saved = _saved.get("inserted", 0) if isinstance(_saved, dict) else int(_saved or 0)
                    total_filled += saved
                    logger.info("[%s/%s] Filled %d bars for gap %s→%s (%ds)",
                                sym, tf, saved, from_ts, to_ts, gap_sec)
                else:
                    logger.debug("[%s/%s] Gap %s→%s (%ds) — IB has no interior bars",
                                 sym, tf, from_ts, to_ts, gap_sec)
            else:
                logger.debug("[%s/%s] Gap %s→%s (%ds) — IB returned 0 bars",
                             sym, tf, from_ts, to_ts, gap_sec)
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning("[%s/%s] Gap fill failed %s→%s: %s",
                           sym, tf, from_ts, to_ts, e)

    if total_filled:
        logger.info("[%s/%s] Gap fill complete: %d bars inserted", sym, tf, total_filled)
    else:
        logger.info("[%s/%s] Gap fill complete: no new bars needed", sym, tf)

    if total_filled:
        try:
            await data_manager.notify_history_ready(
                sym, tf,
                from_ts=cutoff_ts,
                to_ts=int(time.time()),
                added_bars=total_filled,
            )
        except Exception as e:
            logger.debug("history_ready broadcast skipped: %s", e)


async def _prefetch_extra_symbols(state: AppState, fetcher, ib_ok):
    """Prefetch extra symbols in the background (non-blocking)."""
    if not (ib_ok and fetcher.ib and fetcher.ib.isConnected()):
        return
    from ib_insync import ContFuture

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

            if sym_name != MES_SYM:
                since_5m = db.get_latest_ts(sym_name, "5min")
                should_fetch_5m = False
                if since_5m is None:
                    logger.info("[%s] No 5min bars in DB — full fetch", sym_name)
                    dur_5m = config.HISTORY_DURATION_5MIN
                    should_fetch_5m = True
                    filter_since = None
                else:
                    gap_sec = int(time.time()) - since_5m
                    if gap_sec >= 300:
                        from marketdata.ib_fetcher import ib_duration
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
                        saved = fetcher.persist_bars(sym_name, "5min", bars5,
                                                     source="ib_historical")
                        n_saved = saved.get("inserted", 0) if isinstance(saved, dict) else int(saved or 0)
                        logger.info("[%s] Saved %d new 5min bars to DB", sym_name, n_saved)
                        if n_saved:
                            try:
                                await data_manager.notify_history_ready(
                                    sym_name, "5min",
                                    from_ts=bars5[0]["time"],
                                    to_ts=bars5[-1]["time"],
                                    added_bars=n_saved,
                                )
                            except Exception:
                                pass
                    else:
                        logger.info("[%s] IB returned 0 new 5min bars", sym_name)

            since_1d = db.get_latest_ts(sym_name, "1D")
            existing_1d = db.get_bars(sym_name, "1D")
            _skip_1d = False
            if existing_1d and since_1d:
                gap_days = (time.time() - since_1d) / 86400
                if gap_days < 1:
                    logger.info("[%s] 1D bars up to date — skip fetch", sym_name)
                    _skip_1d = True
            if not _skip_1d:
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
                    _saved_1d = fetcher.persist_bars(sym_name, "1D", bars_1d,
                                                     source="ib_historical")
                    saved_1d = _saved_1d.get("inserted", 0) if isinstance(_saved_1d, dict) else int(_saved_1d or 0)
                    logger.info("[%s] Saved %d 1D bars to DB", sym_name, saved_1d)

            await _fill_internal_gaps(sym_name, "5min", fetcher)

        except Exception as e:
            logger.warning("Prefetch %s failed: %s", sym_name, e)


def _get_new_bar_callback(state: AppState):
    callback = getattr(state, "_on_new_bar_callback", None)
    if callback is None:
        def _callback(bar_size_key: str, bar: dict, symbol: str = None):
            on_new_bar(state, bar_size_key, bar, symbol)
        setattr(state, "_on_new_bar_callback", _callback)
        callback = _callback
    return callback


async def _ib_background_init(state: AppState):
    """Background task: connect to IB, fetch missing bars, subscribe to real-time.

    Runs after the server is already serving requests (DB data available).
    This keeps startup to ~100ms (DB load only) while IB work happens async.
    """
    has_db_data = bool(state.fetcher.bars.get("5min"))
    ib_ok = False

    try:
        await state.fetcher.connect()

        since_5min = db.get_latest_ts(MES_SYM, "5min")
        await state.fetcher.load_history(since_5min=since_5min)

        if state.fetcher.bars["5min"]:
            _saved = state.fetcher.persist_bars(MES_SYM, "5min", state.fetcher.bars["5min"],
                                                source="ib_historical")
            saved = _saved.get("inserted", 0) if isinstance(_saved, dict) else int(_saved or 0)
            logger.info("Saved %d 5min bars to DB", saved)

        await _fill_internal_gaps(MES_SYM, "5min", state.fetcher)

        ib_ok = True
    except Exception as e:
        logger.error("IB connect/history error: %s", e)

    asyncio.create_task(_prefetch_extra_symbols(state, state.fetcher, ib_ok))

    if ib_ok:
        try:
            callback = _get_new_bar_callback(state)
            state.fetcher.add_new_bar_callback(callback)
            await state.fetcher.subscribe_mktdata_all()
            logger.info("IB real-time streaming started (all symbols).")

            state.order_mgr = IBOrderManager(state.fetcher.ib, state.fetcher._contract)

            _on_order_status = _make_order_status_handler(state)
            state.fetcher.ib.orderStatusEvent += _on_order_status

        except Exception as e:
            logger.warning("IB real-time subscription failed: %s", e)

    if state.sheets.authenticate():
        state.sheets.initial_upload(state.fetcher.get_bars("5min"))

    bars_5min = state.fetcher.get_bars("5min")
    if bars_5min:
        state.latest_analysis = state.analyzer.get_analysis(bars_5min)

    logger.info("IB background initialization complete.")
    if not ib_ok and has_db_data:
        logger.info("Server continuing in DB-only mode with cached bars.")


async def _ib_reconnect_loop(state: AppState):
    """Background loop: retry IB connection every 60 s when disconnected.

    Handles both the initial-startup failure case (all 3 connect attempts
    timed out) and mid-session disconnects (e.g. TWS restart, network blip).
    On successful reconnect it re-fetches missing bars, re-subscribes to
    real-time, and recreates the order manager — so the server recovers
    fully without a restart.
    """
    while True:
        await asyncio.sleep(60)
        try:
            already_ok = (
                state.fetcher._ib_ready
                and state.fetcher.ib is not None
                and state.fetcher.ib.isConnected()
            )
            if already_ok:
                continue

            logger.info("[IB Reconnect] IB not connected — retrying…")

            if state.fetcher.ib:
                try:
                    state.fetcher.ib.disconnect()
                except Exception:
                    pass
            state.fetcher._ib_ready = False
            state.fetcher._contract = None
            state.fetcher._contract_cache.clear()

            await state.fetcher.connect()

            since_5min = db.get_latest_ts(MES_SYM, "5min")
            await state.fetcher.load_history(since_5min=since_5min)
            if state.fetcher.bars["5min"]:
                _saved = state.fetcher.persist_bars(MES_SYM, "5min", state.fetcher.bars["5min"],
                                                    source="ib_historical")
                saved = _saved.get("inserted", 0) if isinstance(_saved, dict) else int(_saved or 0)
                logger.info("[IB Reconnect] Saved %d new 5min bars to DB", saved)

            callback = _get_new_bar_callback(state)
            if callback not in state.fetcher._new_bar_callbacks:
                state.fetcher.add_new_bar_callback(callback)
            await state.fetcher.subscribe_mktdata_all()
            logger.info("[IB Reconnect] Real-time streaming resumed.")

            state.order_mgr = IBOrderManager(state.fetcher.ib, state.fetcher._contract)

            _on_order_status = _make_order_status_handler(state)
            state.fetcher.ib.orderStatusEvent += _on_order_status

            asyncio.create_task(_prefetch_extra_symbols(state, state.fetcher, True))

            logger.info("[IB Reconnect] Reconnect complete — IB ready.")

            await broadcast(state, {
                "type": "ib_connection_status",
                "status": "connected",
                "message": "IB TWS connected - chart updates resumed",
            })

        except Exception as e:
            logger.warning("[IB Reconnect] Attempt failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = getattr(app.state, "app_state", None)
    if state is None:
        state = AppState()
    app.state.app_state = state

    logger.info("Starting up…")

    async def _broadcast(message: dict):
        await broadcast(state, message)

    data_manager.set_broadcaster(_broadcast)

    db.init_db()

    try:
        _trades = load_all_trades()
        if _trades:
            n = db.upsert_trade_logs(_trades)
            logger.info("Ingested %d trade log rows from data/", n)
    except Exception as exc:
        logger.warning("Trade log ingestion at startup failed: %s", exc)

    all_symbols = [MES_SYM] + [cfg["symbol"] for cfg in config.EXTRA_SYMBOLS]
    for _sym_name in all_symbols:
        state.fetcher._ensure_symbol_state(_sym_name)
        _sym_bars = db.get_bars(_sym_name, "5min")
        if _sym_bars:
            state.fetcher._symbol_bars[_sym_name]["5min"] = _sym_bars[-config.MAX_BARS_IN_MEMORY:]
            logger.info("Loaded %d 5min bars for %s from DB",
                        len(state.fetcher._symbol_bars[_sym_name]["5min"]), _sym_name)
    state.fetcher._sync_legacy_bars()

    from marketdata.ib_fetcher import _key_to_ib as _k2ib
    _now_ts = int(time.time())
    for rt_row in db.get_all_realtime_bars():
        rt_sym = rt_row["symbol"]
        rt_tf = rt_row["timeframe"]
        rt_bar = {k: rt_row[k] for k in ("time", "open", "high", "low", "close", "volume")}
        try:
            _, _interval = _k2ib(rt_tf)
        except Exception:
            _interval = 300
        _current_bar_ts = (_now_ts // _interval) * _interval
        if rt_bar["time"] == _current_bar_ts:
            state.prev_completed_bar[(rt_sym, rt_tf)] = rt_bar
            logger.info("Restored realtime bar %s/%s ts=%s from DB", rt_sym, rt_tf, rt_bar["time"])
            state.fetcher._rt_current[f"{rt_sym}:{rt_tf}"] = rt_bar
            logger.debug("Pre-seeded _rt_current %s/%s ts=%s from realtime_bars",
                         rt_sym, rt_tf, rt_bar["time"])

    bars_5min = state.fetcher.get_bars("5min")
    if bars_5min:
        state.latest_analysis = state.analyzer.get_analysis(bars_5min)

    _ib_init_task = asyncio.create_task(_ib_background_init(state))

    def _on_ib_disconnect_notify():
        asyncio.create_task(broadcast(state, {
            "type": "ib_connection_status",
            "status": "disconnected",
            "message": "IB TWS disconnected - reconnecting...",
        }))
    state.fetcher.add_disconnect_callback(_on_ib_disconnect_notify)

    _ib_reconnect_task = asyncio.create_task(_ib_reconnect_loop(state))

    async def _bg_validate_after_init():
        """Wait for IB init, then run background validation silently.

        Auto-fix is enabled by default so missing/incorrect bars are healed
        from IB on every startup.  Disable with env BG_VALIDATE_FIX=0.
        """
        try:
            await _ib_init_task
        except Exception:
            pass
        await asyncio.sleep(30)
        f = state.fetcher if (state.fetcher._ib_ready and state.fetcher.ib and state.fetcher.ib.isConnected()) else None
        do_fix = os.environ.get("BG_VALIDATE_FIX", "1") != "0"
        await data_validator.background_validate(fetcher=f, fix=do_fix)
        if f is not None:
            try:
                lookback = int(os.environ.get("REALTIME_RECOVER_LOOKBACK", "86400"))
                await data_validator.recover_realtime_bars(f, lookback_seconds=lookback)
            except Exception as e:
                logger.warning("Realtime-bar recovery sweep failed: %s", e)
    _bg_validate_task = asyncio.create_task(_bg_validate_after_init())

    logger.info("Server ready to accept requests (DB-only mode).")

    yield

    logger.info("Shutting down…")
    _ib_reconnect_task.cancel()
    _bg_validate_task.cancel()
    state.sheets.flush_buffer()
    state.fetcher.unsubscribe_realtime()
    state.fetcher.disconnect()
