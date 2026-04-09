"""
IB Data Fetcher — connects to Interactive Brokers TWS/Gateway via ib_insync,
fetches MES 1min/5min historical OHLCV bars, and streams real-time updates.

Usage:
    fetcher = IBDataFetcher()
    await fetcher.connect()
    await fetcher.load_history()
    fetcher.subscribe_realtime(on_new_bar_callback)
"""
import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ib_insync import IB, ContFuture, RealTimeBar, util

import config
import ib_log_translator  # auto-installs translation filter on import

logger = logging.getLogger(__name__)


def _bar_to_dict(bar) -> dict:
    """Convert ib_insync BarData to a plain dict with UTC ms timestamp."""
    dt = bar.date
    # bar.date may be a datetime or a string depending on the request type
    if isinstance(dt, str):
        dt = datetime.strptime(dt, "%Y%m%d %H:%M:%S %Z") if " " in dt else datetime.strptime(dt, "%Y%m%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return {
        "time": int(dt.timestamp()),       # seconds since epoch (TradingView format)
        "open": float(bar.open),
        "high": float(bar.high),
        "low": float(bar.low),
        "close": float(bar.close),
        "volume": float(bar.volume),
    }


class IBDataFetcher:
    """
    Async wrapper around ib_insync for fetching and streaming MES OHLCV data.

    Maintains two in-memory stores:
        self.bars["1min"]  — list of bar dicts sorted by time
        self.bars["5min"]  — list of bar dicts sorted by time
    """

    def __init__(self):
        # NOTE: IB() is created lazily inside connect() so it binds to the
        # running event loop (uvicorn's asyncio loop), not the import-time loop.
        self.ib: Optional[IB] = None
        self.bars: Dict[str, List[dict]] = {"1min": [], "5min": []}
        self._realtime_subscriptions: Dict[str, object] = {}  # key → subscription object
        self._new_bar_callbacks: List[Callable[[str, dict], None]] = []
        # Running aggregated bars for 1min/5min (built from ticks or 5s bars)
        self._rt_current: Dict[str, Optional[dict]] = {"1min": None, "5min": None}
        # reqMktData tick state
        self._prev_tick_price: float = float("nan")
        self._prev_tick_size:  float = float("nan")
        self._last_tick_broadcast: float = 0.0   # monotonic time of last WS push

    # ─── Connection ──────────────────────────────────────────────────────────

    async def connect(self):
        """Connect to IB TWS/Gateway. Retries up to 3 times."""
        # ib_insync uses asyncio.get_event_loop() internally. In Python 3.10+,
        # uvicorn's loop is not set as the thread-default loop, so we set it
        # explicitly here so ib_insync's socket operations use the correct loop.
        asyncio.set_event_loop(asyncio.get_running_loop())

        for attempt in range(1, 4):
            try:
                # Create IB() here so it captures the running event loop
                self.ib = IB()
                await self.ib.connectAsync(
                    config.IB_HOST, config.IB_PORT, clientId=config.IB_CLIENT_ID
                )
                logger.info("Connected to IB TWS at %s:%s", config.IB_HOST, config.IB_PORT)
                return
            except Exception as e:
                logger.warning("IB connect attempt %d failed: %s", attempt, e)
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt)
        raise ConnectionError(f"Cannot connect to IB TWS at {config.IB_HOST}:{config.IB_PORT}")

    def disconnect(self):
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB TWS")

    # ─── Contract ────────────────────────────────────────────────────────────

    async def _get_contract(self):
        """Return a qualified MES continuous front-month contract."""
        contract = ContFuture(
            symbol=config.MES_SYMBOL,
            exchange=config.MES_EXCHANGE,
            currency=config.MES_CURRENCY,
        )
        [qualified] = await self.ib.qualifyContractsAsync(contract)
        logger.info("Qualified contract: %s %s %s", qualified.symbol, qualified.lastTradeDateOrContractMonth, qualified.localSymbol)
        return qualified

    # ─── Historical Data ─────────────────────────────────────────────────────

    async def load_history(self):
        """Fetch historical 1min and 5min bars from IB and populate in-memory stores."""
        contract = await self._get_contract()

        for bar_size, duration in [
            ("1 min", config.HISTORY_DURATION_1MIN),
            ("5 mins", config.HISTORY_DURATION_5MIN),
        ]:
            key = "1min" if "1 min" in bar_size else "5min"
            logger.info("Fetching historical %s bars (duration=%s)...", bar_size, duration)
            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",          # up to now
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=False,
                formatDate=2,            # UTC datetime
            )
            self.bars[key] = [_bar_to_dict(b) for b in bars]
            # Keep last MAX_BARS_IN_MEMORY
            if len(self.bars[key]) > config.MAX_BARS_IN_MEMORY:
                self.bars[key] = self.bars[key][-config.MAX_BARS_IN_MEMORY:]
            logger.info("Loaded %d %s bars", len(self.bars[key]), key)

    # ─── Real-time Streaming ──────────────────────────────────────────────────

    def add_new_bar_callback(self, callback: Callable[[str, dict], None]):
        """Register a callback(bar_size_key, bar_dict) for each new completed bar."""
        self._new_bar_callbacks.append(callback)

    async def subscribe_realtime(self):
        """
        Subscribe to real-time 5-second bars via reqRealTimeBars.

        IB streams a new 5-second OHLCV bar every 5 seconds via event callback.
        We aggregate those into 1min and 5min bars and notify registered callbacks
        on every tick so the frontend chart updates every ~5 seconds.
        """
        contract = await self._get_contract()

        # Seed _rt_current from last historical bar (same reason as subscribe_mktdata)
        now_ts = int(time.time())
        for key, interval in [("1min", 60), ("5min", 300)]:
            if self.bars[key]:
                last = dict(self.bars[key][-1])
                if last["time"] == (now_ts // interval) * interval:
                    self._rt_current[key] = last

        rt_bars = self.ib.reqRealTimeBars(contract, 5, "TRADES", False)
        self._realtime_subscriptions["rt"] = rt_bars
        rt_bars.updateEvent += self._on_rt_bar
        logger.info("Subscribed to 5-second real-time bars (updates every ~5s)")

    def _on_rt_bar(self, rt_bars, has_new_bar: bool):
        """
        Called by ib_insync every 5 seconds with the latest 5-second bar.
        Aggregates into 1min and 5min OHLCV bars and dispatches callbacks.
        """
        if not has_new_bar or not rt_bars:
            return

        rb = rt_bars[-1]   # latest RealTimeBar namedtuple
        # rb.time is a datetime in newer ib_insync versions, an int in older ones
        rt_ts = int(rb.time.timestamp()) if isinstance(rb.time, datetime) else int(rb.time)
        rb_open = float(rb.open_)   # ib_insync uses open_ to avoid Python keyword clash
        rb_high = float(rb.high)
        rb_low  = float(rb.low)
        rb_close= float(rb.close)
        rb_vol  = float(rb.volume)

        for key, interval in [("1min", 60), ("5min", 300)]:
            bar_ts = (rt_ts // interval) * interval  # floor to bar boundary
            cur = self._rt_current[key]

            if cur is None or bar_ts > cur["time"]:
                # Bar boundary crossed — old bar is complete, start a new one
                if cur is not None:
                    # Finalise old bar in store and notify
                    self._append_bar(key, cur)
                    self._dispatch(key, cur)
                    logger.debug("Completed %s bar: time=%s close=%s", key, cur["time"], cur["close"])
                # Initialise new in-progress bar
                cur = {"time": bar_ts, "open": rb_open, "high": rb_high,
                       "low": rb_low, "close": rb_close, "volume": rb_vol}
                self._rt_current[key] = cur
                self._append_bar(key, cur)
            else:
                # Same bar — update OHLCV in place
                cur["high"]   = max(cur["high"], rb_high)
                cur["low"]    = min(cur["low"],  rb_low)
                cur["close"]  = rb_close
                cur["volume"] += rb_vol
                if self.bars[key] and self.bars[key][-1]["time"] == bar_ts:
                    self.bars[key][-1] = cur

            # Notify on every tick (both in-progress and new-bar)
            self._dispatch(key, cur)

    def _dispatch(self, key: str, bar: dict):
        """Call all registered callbacks with a copy of bar."""
        for cb in self._new_bar_callbacks:
            try:
                cb(key, dict(bar))
            except Exception as exc:
                logger.error("Callback error: %s", exc)

    # ─── reqMktData (tick-level, sub-second) ─────────────────────────────────

    _TICK_BROADCAST_INTERVAL = 0.25   # broadcast to WebSocket at most 4×/second

    async def subscribe_mktdata(self):
        """
        Subscribe to tick-level market data via reqMktData.

        Fires on every last-price change — far faster than 5-second bars.
        Ticks are aggregated into in-progress 1min/5min OHLCV bars and
        broadcast to WebSocket clients at most every 250 ms so we don't
        flood slow connections.

        Call subscribe_realtime() instead for the 5-second bar approach.
        """
        contract = await self._get_contract()

        # Seed _rt_current from the last historical bar so the tick aggregator
        # continues building the in-progress bar rather than starting fresh.
        # Without this, the first tick would reset open/high/low/vol to zero,
        # creating a visible gap at the right edge of the chart.
        now_ts = int(time.time())
        for key, interval in [("1min", 60), ("5min", 300)]:
            if self.bars[key]:
                last = dict(self.bars[key][-1])
                bar_ts = (now_ts // interval) * interval
                if last["time"] == bar_ts:
                    # Last historical bar is the current in-progress bar — seed it
                    self._rt_current[key] = last
                    logger.debug("Seeded %s rt_current from history: time=%s", key, last["time"])

        ticker = self.ib.reqMktData(contract, "", False, False)
        self._realtime_subscriptions["mktdata"] = ticker
        ticker.updateEvent += self._on_tick
        logger.info("Subscribed to market data ticks (≤250 ms chart updates)")

    def _on_tick(self, ticker):
        """Fires on every bid/ask/last change from reqMktData."""
        price = ticker.last
        size  = ticker.lastSize

        # Skip ticks with no valid last-trade price
        if price is None or math.isnan(price) or price <= 0:
            return
        if size is None or math.isnan(size):
            size = 0.0

        # Only add volume on a genuine new trade (last price or size changed)
        vol_delta = 0.0
        if price != self._prev_tick_price or size != self._prev_tick_size:
            vol_delta = float(size)
            self._prev_tick_price = price
            self._prev_tick_size  = size

        wall_ts = int(time.time())

        for key, interval in [("1min", 60), ("5min", 300)]:
            bar_ts = (wall_ts // interval) * interval
            cur = self._rt_current[key]

            if cur is None or bar_ts > cur["time"]:
                # Bar boundary — finalise old bar, open a new one
                if cur is not None:
                    self._append_bar(key, cur)
                    self._dispatch(key, cur)
                    logger.debug("Completed %s bar: time=%s close=%s", key, cur["time"], cur["close"])
                cur = {"time": bar_ts, "open": price, "high": price,
                       "low": price, "close": price, "volume": vol_delta}
                self._rt_current[key] = cur
                self._append_bar(key, cur)
            else:
                cur["high"]    = max(cur["high"], price)
                cur["low"]     = min(cur["low"],  price)
                cur["close"]   = price
                cur["volume"] += vol_delta
                if self.bars[key] and self.bars[key][-1]["time"] == bar_ts:
                    self.bars[key][-1] = cur

        # Throttled broadcast — push at most every 250 ms
        now = time.monotonic()
        if now - self._last_tick_broadcast >= self._TICK_BROADCAST_INTERVAL:
            self._last_tick_broadcast = now
            for key in ("1min", "5min"):
                cur = self._rt_current[key]
                if cur:
                    self._dispatch(key, cur)

    def _append_bar(self, key: str, bar: dict):
        """Append or update the last bar in the in-memory store."""
        bars = self.bars[key]
        if bars and bars[-1]["time"] == bar["time"]:
            bars[-1] = bar  # update in-progress bar
        else:
            bars.append(bar)
            if len(bars) > config.MAX_BARS_IN_MEMORY:
                bars.pop(0)

    def unsubscribe_realtime(self):
        """Cancel whichever real-time subscription is active."""
        if not self.ib:
            return
        rt = self._realtime_subscriptions.get("rt")
        if rt is not None:
            try:
                self.ib.cancelRealTimeBars(rt)
                logger.info("Cancelled 5-second real-time bar subscription")
            except Exception as e:
                logger.warning("Error cancelling real-time bars: %s", e)
        mktdata = self._realtime_subscriptions.get("mktdata")
        if mktdata is not None:
            try:
                self.ib.cancelMktData(mktdata)
                logger.info("Cancelled market data tick subscription")
            except Exception as e:
                logger.warning("Error cancelling market data: %s", e)
        self._realtime_subscriptions.clear()

    # ─── Convenience ─────────────────────────────────────────────────────────

    def get_bars(self, bar_size_key: str, from_ts: Optional[int] = None, to_ts: Optional[int] = None) -> List[dict]:
        """Return bars optionally filtered by timestamp range (seconds)."""
        bars = self.bars.get(bar_size_key, [])
        if from_ts is not None:
            bars = [b for b in bars if b["time"] >= from_ts]
        if to_ts is not None:
            bars = [b for b in bars if b["time"] <= to_ts]
        return bars


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def main():
        fetcher = IBDataFetcher()
        await fetcher.connect()
        await fetcher.load_history()

        print(f"\n5min bars (last 5):")
        for b in fetcher.get_bars("5min")[-5:]:
            dt = datetime.fromtimestamp(b["time"], tz=timezone.utc)
            print(f"  {dt}  O={b['open']}  H={b['high']}  L={b['low']}  C={b['close']}  V={b['volume']}")

        fetcher.disconnect()

    asyncio.run(main())
