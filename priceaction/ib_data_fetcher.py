"""
IB Data Fetcher — connects to Interactive Brokers TWS/Gateway via ib_insync,
fetches MES 1min/5min historical OHLCV bars, and streams real-time updates.
"""
import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ib_insync import IB, ContFuture, Future, RealTimeBar, util

import config
import ib_log_translator  # auto-installs translation filter on import

logger = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _bar_to_dict(bar) -> dict:
    """Convert ib_insync BarData to a plain dict with UTC seconds timestamp."""
    dt = bar.date
    if isinstance(dt, str):
        dt = datetime.strptime(dt, "%Y%m%d %H:%M:%S %Z") if " " in dt else datetime.strptime(dt, "%Y%m%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return {
        "time":   int(dt.timestamp()),
        "open":   float(bar.open),
        "high":   float(bar.high),
        "low":    float(bar.low),
        "close":  float(bar.close),
        "volume": float(bar.volume),
    }


def _ib_duration(gap_sec: int, key: str) -> str:
    """
    Convert a time gap (seconds) to an IB durationStr string.

    1min bars: capped at 2 days (IB hard limit).
    5min bars: supports up to 1 year — weeks/year strings allow fetching any
               historical date range when the chart scrolls past cached data.
    """
    if key == "1min":
        gap_sec = min(gap_sec + 3_600, 86_400 * 2)   # +1h buffer, 2-day cap
        if gap_sec < 86_400:
            return f"{max(gap_sec, 3_600)} S"
        return f"{math.ceil(gap_sec / 86_400)} D"

    # 5-min bars: no artificial day cap — use weeks/year as needed
    gap_sec += 3_600          # +1h buffer to ensure the boundary bar is included
    days = gap_sec / 86_400
    if days < 1:
        return f"{max(int(gap_sec), 3_600)} S"
    if days <= 7:
        return f"{math.ceil(days)} D"
    weeks = days / 7
    if weeks <= 52:
        return f"{math.ceil(weeks)} W"
    return "1 Y"              # IB max per request for 5-min bars


# ─── IBDataFetcher ────────────────────────────────────────────────────────────

class IBDataFetcher:
    """
    Async wrapper around ib_insync for fetching and streaming MES OHLCV data.

    In-memory bar stores (self.bars) serve as a fast cache for the HTTP/WS
    layer.  The SQLite DB (db.py) is the durable store — managed by server.py.
    """

    def __init__(self):
        self.ib: Optional[IB] = None
        self.bars: Dict[str, List[dict]] = {"1min": [], "5min": []}
        self._contract = None                        # cached qualified contract
        self._ib_ready: bool = False                 # True only after contract is resolved
        self._realtime_subscriptions: Dict[str, object] = {}
        self._new_bar_callbacks: List[Callable[[str, dict], None]] = []
        # Aggregated in-progress bars (built from ticks / 5s bars)
        self._rt_current: Dict[str, Optional[dict]] = {"1min": None, "5min": None}
        # reqMktData tick state
        self._prev_tick_price: float = float("nan")
        self._prev_tick_size:  float = float("nan")
        self._last_tick_broadcast: float = 0.0

    # ─── Connection ──────────────────────────────────────────────────────────

    async def connect(self):
        """Connect to IB TWS/Gateway. Retries up to 3 times."""
        asyncio.set_event_loop(asyncio.get_running_loop())
        for attempt in range(1, 4):
            try:
                self.ib = IB()
                await self.ib.connectAsync(
                    config.IB_HOST, config.IB_PORT, clientId=config.IB_CLIENT_ID
                )
                logger.info("Connected to IB TWS at %s:%s", config.IB_HOST, config.IB_PORT)
                # Brief settle: TWS reports 'Synchronization complete' before all
                # internal subscriptions are ready; skipping this causes
                # qualifyContractsAsync to silently hang on reqContractDetails.
                logger.info("Waiting 2 s for TWS to finish initializing…")
                await asyncio.sleep(2)
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
        """Return a qualified MES continuous front-month contract (cached)."""
        if self._contract is not None:
            return self._contract
        logger.info("Qualifying MES contract (reqContractDetails)…")
        contract = ContFuture(
            symbol=config.MES_SYMBOL,
            exchange=config.MES_EXCHANGE,
            currency=config.MES_CURRENCY,
        )
        try:
            result = await asyncio.wait_for(
                self.ib.qualifyContractsAsync(contract),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                "qualifyContractsAsync timed out after 30 s — "
                "TWS may not have market data permissions or is still loading. "
                "Check that MES futures data is subscribed in TWS."
            )
        if not result:
            raise ValueError("IB returned no contract for MES ContFuture — check symbol/exchange config.")
        [qualified] = result
        self._contract  = qualified
        self._ib_ready  = True           # IB is now fully usable for data requests
        logger.info("Qualified contract: %s  expiry=%s  localSymbol=%s",
                    qualified.symbol,
                    qualified.lastTradeDateOrContractMonth,
                    qualified.localSymbol)
        return qualified

    # ─── Historical Data ─────────────────────────────────────────────────────

    async def load_history(
        self,
        since_1min: Optional[int] = None,
        since_5min: Optional[int] = None,
    ):
        """
        Fetch 1min and 5min bars from IB.

        If since_Xmin is provided, only fetch bars newer than that timestamp
        (for startup incremental sync from DB). Falls back to full default
        duration if the gap exceeds IB's per-size limit.
        """
        contract = await self._get_contract()

        for bar_size_str, key, since_ts, default_dur in [
            ("1 min",  "1min", since_1min, config.HISTORY_DURATION_1MIN),
            ("5 mins", "5min", since_5min, config.HISTORY_DURATION_5MIN),
        ]:
            now = int(time.time())
            # Max gap for incremental startup sync: 2 days for 1min (IB limit),
            # 365 days for 5min (1 year — IB supports this per request).
            max_gap = 86_400 * 2 if key == "1min" else 86_400 * 365

            if since_ts and (now - since_ts) <= max_gap:
                duration_str = _ib_duration(now - since_ts, key)
                logger.info("Fetching %s bars since %s (duration=%s)",
                            key, datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat(),
                            duration_str)
            else:
                duration_str = default_dur
                since_ts     = None
                logger.info("Fetching historical %s bars (duration=%s)", key, duration_str)

            logger.info("Requesting %s historical bars from IB (timeout 60 s)…", key)
            try:
                raw = await asyncio.wait_for(
                    self.ib.reqHistoricalDataAsync(
                        contract,
                        endDateTime="",
                        durationStr=duration_str,
                        barSizeSetting=bar_size_str,
                        whatToShow="TRADES",
                        useRTH=False,
                        formatDate=2,
                    ),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "reqHistoricalDataAsync timed out for %s bars — "
                    "IB pacing limit or no data permission. Skipping.", key
                )
                continue
            new_bars = [_bar_to_dict(b) for b in raw]

            if since_ts:
                # Only keep bars strictly newer than what we already have
                new_bars = [b for b in new_bars if b["time"] > since_ts]
                # Merge with in-memory (from DB load) without duplicates
                existing = {b["time"]: b for b in self.bars[key]}
                for b in new_bars:
                    existing[b["time"]] = b
                self.bars[key] = sorted(existing.values(), key=lambda b: b["time"])
            else:
                self.bars[key] = new_bars

            if len(self.bars[key]) > config.MAX_BARS_IN_MEMORY:
                self.bars[key] = self.bars[key][-config.MAX_BARS_IN_MEMORY:]

            logger.info("Loaded %d %s bars total (%d new from IB)",
                        len(self.bars[key]), key, len(new_bars))

    async def fetch_range(self, bar_size_key: str, from_ts: int, to_ts: int) -> List[dict]:
        """
        Fetch a specific historical time range from IB on demand.
        Used by the server when the chart scrolls to an uncached region.
        Returns bars filtered to [from_ts, to_ts].
        """
        contract   = await self._get_contract()
        interval   = 60 if bar_size_key == "1min" else 300
        bar_size   = "1 min" if bar_size_key == "1min" else "5 mins"

        start_ts   = (from_ts // interval) * interval
        end_ts     = ((to_ts + interval - 1) // interval) * interval
        end_dt     = datetime.fromtimestamp(end_ts, tz=timezone.utc)
        end_str    = end_dt.strftime("%Y%m%d %H:%M:%S UTC")
        dur_str    = _ib_duration(end_ts - start_ts, bar_size_key)

        if getattr(contract, 'secType', '').upper() == 'CONTFUT':
            contract = Future(
                conId=contract.conId,
                symbol=contract.symbol,
                exchange=contract.exchange,
                currency=contract.currency,
                lastTradeDateOrContractMonth=contract.lastTradeDateOrContractMonth,
                localSymbol=getattr(contract, 'localSymbol', ''),
                multiplier=getattr(contract, 'multiplier', ''),
                tradingClass=getattr(contract, 'tradingClass', ''),
            )
            logger.info("Converted ContFuture → Future for on-demand fetch: %s", contract)

        logger.info("On-demand fetch: %s  %s → %s  (%s)",
                    bar_size_key,
                    datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
                    datetime.fromtimestamp(end_ts,   tz=timezone.utc).isoformat(),
                    dur_str)

        try:
            raw = await asyncio.wait_for(
                self.ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime=end_str,
                    durationStr=dur_str,
                    barSizeSetting=bar_size,
                    whatToShow="TRADES",
                    useRTH=False,
                    formatDate=2,
                ),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.error("On-demand fetch timed out for %s bars", bar_size_key)
            return []

        bars = [b for b in (_bar_to_dict(r) for r in raw)
                if b["time"] >= from_ts and b["time"] <= to_ts]
        bars.sort(key=lambda b: b["time"])
        if not bars:
            logger.debug(
                "On-demand IB raw bars=%d start=%s end=%s",
                len(raw),
                raw[0].date if raw else None,
                raw[-1].date if raw else None,
            )
        return bars

    # ─── Real-time (reqRealTimeBars — 5-second bars) ─────────────────────────

    async def subscribe_realtime(self):
        """5-second bar streaming (kept as fallback; use subscribe_mktdata for speed)."""
        contract = await self._get_contract()
        self._seed_rt_current()

        rt_bars = self.ib.reqRealTimeBars(contract, 5, "TRADES", False)
        self._realtime_subscriptions["rt"] = rt_bars
        rt_bars.updateEvent += self._on_rt_bar
        logger.info("Subscribed to 5-second real-time bars")

    def _on_rt_bar(self, rt_bars, has_new_bar: bool):
        if not has_new_bar or not rt_bars:
            return
        rb     = rt_bars[-1]
        rt_ts  = int(rb.time.timestamp()) if isinstance(rb.time, datetime) else int(rb.time)
        rb_open  = float(rb.open_)
        rb_high  = float(rb.high)
        rb_low   = float(rb.low)
        rb_close = float(rb.close)
        rb_vol   = float(rb.volume)

        for key, interval in [("1min", 60), ("5min", 300)]:
            bar_ts = (rt_ts // interval) * interval
            cur    = self._rt_current[key]

            if cur is None or bar_ts > cur["time"]:
                if cur is not None:
                    self._append_bar(key, cur)
                    self._dispatch(key, cur)
                cur = {"time": bar_ts, "open": rb_open, "high": rb_high,
                       "low": rb_low, "close": rb_close, "volume": rb_vol}
                self._rt_current[key] = cur
                self._append_bar(key, cur)
            else:
                cur["high"]    = max(cur["high"], rb_high)
                cur["low"]     = min(cur["low"],  rb_low)
                cur["close"]   = rb_close
                cur["volume"] += rb_vol
                if self.bars[key] and self.bars[key][-1]["time"] == bar_ts:
                    self.bars[key][-1] = cur

            self._dispatch(key, cur)

    # ─── Real-time (reqMktData — tick level) ─────────────────────────────────

    _TICK_BROADCAST_INTERVAL = 0.25   # max 4 WebSocket pushes per second

    async def subscribe_mktdata(self):
        """
        Tick-level streaming via reqMktData — updates chart every ~250 ms.
        Seeds _rt_current from last historical bar to avoid a gap at startup.
        """
        contract = await self._get_contract()
        self._seed_rt_current()

        ticker = self.ib.reqMktData(contract, "", False, False)
        self._realtime_subscriptions["mktdata"] = ticker
        ticker.updateEvent += self._on_tick
        logger.info("Subscribed to market data ticks (≤250 ms chart updates)")

    def _seed_rt_current(self):
        """Pre-fill _rt_current from last historical bar to avoid startup gap."""
        now_ts = int(time.time())
        for key, interval in [("1min", 60), ("5min", 300)]:
            if self.bars[key]:
                last   = dict(self.bars[key][-1])
                bar_ts = (now_ts // interval) * interval
                if last["time"] == bar_ts:
                    self._rt_current[key] = last
                    logger.debug("Seeded %s rt_current from history ts=%s", key, last["time"])

    def _on_tick(self, ticker):
        price = ticker.last
        size  = ticker.lastSize
        if price is None or math.isnan(price) or price <= 0:
            return
        if size is None or math.isnan(size):
            size = 0.0

        vol_delta = 0.0
        if price != self._prev_tick_price or size != self._prev_tick_size:
            vol_delta = float(size)
            self._prev_tick_price = price
            self._prev_tick_size  = size

        wall_ts = int(time.time())
        for key, interval in [("1min", 60), ("5min", 300)]:
            bar_ts = (wall_ts // interval) * interval
            cur    = self._rt_current[key]

            if cur is None or bar_ts > cur["time"]:
                if cur is not None:
                    self._append_bar(key, cur)
                    self._dispatch(key, cur)
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

        now = time.monotonic()
        if now - self._last_tick_broadcast >= self._TICK_BROADCAST_INTERVAL:
            self._last_tick_broadcast = now
            for key in ("1min", "5min"):
                cur = self._rt_current[key]
                if cur:
                    self._dispatch(key, cur)

    # ─── Shared helpers ───────────────────────────────────────────────────────

    def add_new_bar_callback(self, callback: Callable[[str, dict], None]):
        self._new_bar_callbacks.append(callback)

    def _dispatch(self, key: str, bar: dict):
        for cb in self._new_bar_callbacks:
            try:
                cb(key, dict(bar))
            except Exception as exc:
                logger.error("Callback error: %s", exc)

    def _append_bar(self, key: str, bar: dict):
        bars = self.bars[key]
        if bars and bars[-1]["time"] == bar["time"]:
            bars[-1] = bar
            return

        if not bars or bar["time"] > bars[-1]["time"]:
            bars.append(bar)
        else:
            inserted = False
            for idx, existing in enumerate(bars):
                if existing["time"] == bar["time"]:
                    bars[idx] = bar
                    inserted = True
                    break
                if existing["time"] > bar["time"]:
                    bars.insert(idx, bar)
                    inserted = True
                    break
            if not inserted:
                bars.append(bar)

        if len(bars) > config.MAX_BARS_IN_MEMORY:
            bars.pop(0)

    def unsubscribe_realtime(self):
        if not self.ib:
            return
        rt = self._realtime_subscriptions.get("rt")
        if rt is not None:
            try:
                self.ib.cancelRealTimeBars(rt)
            except Exception as e:
                logger.warning("Error cancelling real-time bars: %s", e)
        mktdata = self._realtime_subscriptions.get("mktdata")
        if mktdata is not None:
            try:
                self.ib.cancelMktData(mktdata)
            except Exception as e:
                logger.warning("Error cancelling market data: %s", e)
        self._realtime_subscriptions.clear()
        logger.info("Real-time subscriptions cancelled")

    def get_bars(
        self,
        bar_size_key: str,
        from_ts: Optional[int] = None,
        to_ts: Optional[int]   = None,
    ) -> List[dict]:
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
        for b in fetcher.get_bars("5min")[-5:]:
            dt = datetime.fromtimestamp(b["time"], tz=timezone.utc)
            print(f"  {dt}  O={b['open']}  H={b['high']}  L={b['low']}  C={b['close']}  V={b['volume']}")
        fetcher.disconnect()

    asyncio.run(main())
