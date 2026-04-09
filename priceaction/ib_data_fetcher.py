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
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ib_insync import IB, ContFuture, util

import config

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
        self.ib = IB()
        self.bars: Dict[str, List[dict]] = {"1min": [], "5min": []}
        self._realtime_subscriptions: Dict[str, object] = {}  # bar_size → BarDataList
        self._new_bar_callbacks: List[Callable[[str, dict], None]] = []

    # ─── Connection ──────────────────────────────────────────────────────────

    async def connect(self):
        """Connect to IB TWS/Gateway. Retries up to 3 times."""
        for attempt in range(1, 4):
            try:
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
        if self.ib.isConnected():
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
        Subscribe to real-time bars using keepUpToDate=True.
        New/updated bars are detected and dispatched to registered callbacks.
        """
        contract = await self._get_contract()

        for bar_size in ("1 min", "5 mins"):
            key = "1min" if "1 min" in bar_size else "5min"
            # Use keepUpToDate=True — IB keeps streaming updates to the returned BarDataList
            bars_obj = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="60 S",      # small window; keepUpToDate streams new bars
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=False,
                formatDate=2,
                keepUpToDate=True,
            )
            self._realtime_subscriptions[key] = bars_obj

            # Attach update handler via ib_insync's BarDataList.updateEvent
            def make_handler(k):
                def on_update(bars_list, has_new_bar):
                    if has_new_bar and len(bars_list) > 0:
                        new_bar = _bar_to_dict(bars_list[-1])
                        self._append_bar(k, new_bar)
                        for cb in self._new_bar_callbacks:
                            try:
                                cb(k, new_bar)
                            except Exception as exc:
                                logger.error("Callback error: %s", exc)
                return on_update

            bars_obj.updateEvent += make_handler(key)
            logger.info("Subscribed to real-time %s bars", key)

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
        """Cancel all real-time subscriptions."""
        for key, bars_obj in self._realtime_subscriptions.items():
            self.ib.cancelHistoricalData(bars_obj)
            logger.info("Cancelled real-time subscription for %s", key)
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
