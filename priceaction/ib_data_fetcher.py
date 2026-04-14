"""
IB Data Fetcher — connects to Interactive Brokers TWS/Gateway via ib_insync,
fetches MES 5min historical OHLCV bars, and streams real-time updates.
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
    import datetime as _dt_mod
    dt = bar.date
    if isinstance(dt, str):
        dt = datetime.strptime(dt, "%Y%m%d %H:%M:%S %Z") if " " in dt else datetime.strptime(dt, "%Y%m%d")
    # datetime.date (daily bars) has no tzinfo — promote to datetime first
    if isinstance(dt, _dt_mod.date) and not isinstance(dt, datetime):
        dt = datetime(dt.year, dt.month, dt.day)
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


# ─── Resolution Mapping ──────────────────────────────────────────────────────
#
# TradingView resolution → (DB key, IB barSizeSetting, interval_seconds)

RESOLUTION_MAP = {
    "5":   ("5min",  "5 mins",  300),
    "15":  ("15min", "15 mins", 900),
    "60":  ("60min", "1 hour",  3600),
    "1D":  ("1D",    "1 day",   86400),
}

def resolution_to_key(resolution: str) -> str:
    """Map TradingView resolution string to DB timeframe key."""
    return RESOLUTION_MAP.get(resolution, RESOLUTION_MAP["5"])[0]

def _key_to_ib(key: str) -> tuple:
    """Return (ib_bar_size_str, interval_seconds) for a DB key."""
    for _res, (k, bar_size, interval) in RESOLUTION_MAP.items():
        if k == key:
            return bar_size, interval
    return "5 mins", 300


def ib_duration(gap_sec: int, max_days: int = 30) -> str:
    """
    Convert a time gap (seconds) to an IB durationStr string.
    Supports up to 1 year — weeks/year strings allow fetching any
    historical date range when the chart scrolls past cached data.
    
    Args:
        gap_sec: Time gap in seconds
        max_days: Maximum duration in days (default 30 to avoid IB timeouts)
    """
    gap_sec += 3_600          # +1h buffer to ensure the boundary bar is included
    days = gap_sec / 86_400
    
    # Cap at max_days to avoid IB timeouts on large gaps (e.g., weekends + inactive contracts)
    if days > max_days:
        days = max_days
    
    if days < 1:
        return f"{max(int(gap_sec), 3_600)} S"
    if days <= 7:
        return f"{math.ceil(days)} D"
    weeks = days / 7
    if weeks <= 52:
        return f"{math.ceil(weeks)} W"
    return "1 Y"


# ─── Contract Rollover ───────────────────────────────────────────────────────
#
# For each symbol, determine the front-month contract at a given timestamp.
# Rollover is approximated as day 10 of the contract month.
# Uses config.INSTRUMENTS for per-symbol contract cycle.


def _contract_month_for_ts(ts: int, symbol: str = "MES") -> str:
    """Return YYYYMM for the contract likely front-month at timestamp *ts*."""
    inst = config.INSTRUMENTS.get(symbol)
    months = inst["contract_months"] if inst else [3, 6, 9, 12]

    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    y, m, d = dt.year, dt.month, dt.day
    for qm in months:
        if m < qm or (m == qm and d <= 10):
            return f"{y}{qm:02d}"
    # Wrap to next year, first contract month
    return f"{y + 1}{months[0]:02d}"


def _prev_contract_month(yyyymm: str, symbol: str = "MES") -> str:
    """Return the YYYYMM of the previous contract for *symbol*."""
    inst = config.INSTRUMENTS.get(symbol)
    months = inst["contract_months"] if inst else [3, 6, 9, 12]

    y = int(yyyymm[:4])
    m = int(yyyymm[4:])
    if m not in months:
        # When the contract month derived from a timestamp doesn't exactly match
        # any month in the symbol's contract cycle (e.g. querying NK225MC with a
        # month of 7 which isn't in its cycle), fall back to the nearest earlier
        # contract month.
        prev = [q for q in months if q < m]
        if prev:
            return f"{y}{prev[-1]:02d}"
        return f"{y - 1}{months[-1]:02d}"
    idx = months.index(m)
    if idx == 0:
        return f"{y - 1}{months[-1]:02d}"
    return f"{y}{months[idx - 1]:02d}"


def _next_contract_month(yyyymm: str, symbol: str = "MES") -> str:
    """Return the YYYYMM of the next contract for *symbol*."""
    inst = config.INSTRUMENTS.get(symbol)
    months = inst["contract_months"] if inst else [3, 6, 9, 12]

    y = int(yyyymm[:4])
    m = int(yyyymm[4:])
    if m not in months:
        nxt = [q for q in months if q > m]
        if nxt:
            return f"{y}{nxt[0]:02d}"
        return f"{y + 1}{months[0]:02d}"
    idx = months.index(m)
    if idx == len(months) - 1:
        return f"{y + 1}{months[0]:02d}"
    return f"{y}{months[idx + 1]:02d}"


# ─── IBDataFetcher ────────────────────────────────────────────────────────────

class IBDataFetcher:
    """
    Async wrapper around ib_insync for fetching and streaming OHLCV data
    for multiple symbols (MES, MNQ, NK225MC, MGC, etc.).

    In-memory bar stores (self.bars) serve as a fast cache for the HTTP/WS
    layer.  The SQLite DB (db.py) is the durable store — managed by server.py.
    """

    def __init__(self):
        self.ib: Optional[IB] = None
        self.bars: Dict[str, List[dict]] = {"5min": []}
        self._contract = None                        # cached qualified ContFuture (MES)
        self._contract_cache: Dict[str, object] = {}   # YYYYMM → qualified Future
        self._ib_ready: bool = False                 # True only after contract is resolved
        self._realtime_subscriptions: Dict[str, object] = {}
        self._new_bar_callbacks: List[Callable] = []
        # Aggregated in-progress bars (built from ticks / 5s bars)
        # Keyed by "symbol:bar_size_key" for multi-symbol, with "5min" as legacy key for MES
        self._rt_current: Dict[str, Optional[dict]] = {"5min": None}
        # reqMktData tick state — per-symbol
        self._tick_state: Dict[str, dict] = {}  # symbol → {prev_price, prev_size, last_broadcast}
        # Legacy single-symbol tick state (kept for backward compat with MES)
        self._prev_tick_price: float = float("nan")
        self._prev_tick_size:  float = float("nan")
        self._last_tick_broadcast: float = 0.0
        # Multi-symbol bars cache: symbol → {bar_size_key → [bars]}
        self._symbol_bars: Dict[str, Dict[str, List[dict]]] = {}

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

    async def _get_future_for_month(self, yyyymm: str, symbol: str = "MES"):
        """
        Return a qualified Future contract for a specific expiry month.
        Results are cached so repeated scrolls don't re-qualify.
        Uses config.INSTRUMENTS for IB symbol/exchange/currency.
        """
        inst = config.INSTRUMENTS.get(symbol)
        ib_sym   = inst["ib_symbol"] if inst else symbol
        exchange = inst["exchange"]  if inst else "CME"
        currency = inst["currency"]  if inst else "USD"

        cache_key = f"{symbol}_{yyyymm}"
        if cache_key in self._contract_cache:
            return self._contract_cache[cache_key]
        logger.info("Qualifying Future contract for %s %s…", symbol, yyyymm)
        contract = Future(
            symbol=ib_sym,
            exchange=exchange,
            currency=currency,
            lastTradeDateOrContractMonth=yyyymm,
        )
        contract.includeExpired = True
        try:
            result = await asyncio.wait_for(
                self.ib.qualifyContractsAsync(contract),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"qualifyContractsAsync timed out for {symbol} {yyyymm}")
        if not result:
            raise ValueError(f"IB returned no contract for {symbol} Future {yyyymm}")
        qualified = result[0]
        self._contract_cache[cache_key] = qualified
        logger.info("Qualified Future: %s  expiry=%s  localSymbol=%s  conId=%s",
                    qualified.symbol,
                    qualified.lastTradeDateOrContractMonth,
                    qualified.localSymbol,
                    qualified.conId)
        return qualified

    # ─── Historical Data ─────────────────────────────────────────────────────

    async def load_history(
        self,
        since_5min: Optional[int] = None,
    ):
        """
        Fetch 5min bars from IB.

        If since_5min is provided, only fetch bars newer than that timestamp
        (for startup incremental sync from DB). Falls back to full default
        duration if the gap exceeds IB's limit.
        """
        contract = await self._get_contract()

        key = "5min"
        bar_size_str = "5 mins"
        since_ts = since_5min
        default_dur = config.HISTORY_DURATION_5MIN

        now = int(time.time())
        max_gap = 86_400 * 365

        if since_ts and (now - since_ts) <= max_gap:
            duration_str = ib_duration(now - since_ts)
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
            return
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

    async def fetch_range(self, bar_size_key: str, from_ts: int, to_ts: int,
                         symbol: str = "MES") -> List[dict]:
        """
        Fetch a specific historical time range from IB on demand.
        Used by the server when the chart scrolls to an uncached region.

        For MES (and symbols with the same contract cycle), uses month-specific
        Future contracts with rollover fallback.
        For all symbols, falls back to ContFuture if month-specific fetch fails.
        Returns bars filtered to [from_ts, to_ts].
        """
        bar_size, interval = _key_to_ib(bar_size_key)

        start_ts   = (from_ts // interval) * interval
        end_ts     = ((to_ts + interval - 1) // interval) * interval
        end_dt     = datetime.fromtimestamp(end_ts, tz=timezone.utc)
        end_str    = end_dt.strftime("%Y%m%d %H:%M:%S UTC")
        dur_str    = ib_duration(end_ts - start_ts)

        inst = config.INSTRUMENTS.get(symbol)

        # ── Strategy 1: month-specific Future contracts (with rollover) ──────
        target_month = _contract_month_for_ts(end_ts, symbol)
        months_to_try = [
            target_month,
            _next_contract_month(target_month, symbol),
            _prev_contract_month(target_month, symbol),
        ]
        # Deduplicate while preserving order
        seen = set()
        months_to_try = [m for m in months_to_try if not (m in seen or seen.add(m))]

        for month in months_to_try:
            try:
                contract = await self._get_future_for_month(month, symbol)
            except Exception as e:
                logger.warning("[%s] Cannot qualify Future %s: %s", symbol, month, e)
                continue

            logger.info("[%s] On-demand fetch (Future %s): %s  %s → %s  (%s)",
                        symbol, month, bar_size_key,
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
                logger.error("[%s] On-demand fetch timed out (Future %s)",
                            symbol, contract.localSymbol)
                continue

            bars = [b for b in (_bar_to_dict(r) for r in raw)
                    if b["time"] >= from_ts and b["time"] <= to_ts]
            bars.sort(key=lambda b: b["time"])

            if bars:
                logger.info("[%s] On-demand fetch: got %d %s bars from Future %s",
                            symbol, len(bars), bar_size_key, contract.localSymbol)
                return bars

            logger.info("[%s] Future %s returned 0 bars for %s, trying next",
                        symbol, contract.localSymbol, bar_size_key)

        # ── Strategy 2: ContFuture (continuous contract) as fallback ─────────
        # IB does not allow setting endDateTime for ContFuture (error 10339),
        # so we use endDateTime="" to fetch the most recent bars, then filter.
        logger.info("[%s] Trying ContFuture fallback for %s %s→%s",
                    symbol, bar_size_key, from_ts, to_ts)
        try:
            ib_sym   = inst["ib_symbol"] if inst else symbol
            exchange = inst["exchange"]  if inst else "CME"
            currency = inst["currency"]  if inst else "USD"
            cont_contract = ContFuture(symbol=ib_sym, exchange=exchange, currency=currency)
            qualified = await asyncio.wait_for(
                self.ib.qualifyContractsAsync(cont_contract), timeout=30.0,
            )
            if qualified:
                raw = await asyncio.wait_for(
                    self.ib.reqHistoricalDataAsync(
                        qualified[0],
                        endDateTime="",
                        durationStr=dur_str,
                        barSizeSetting=bar_size,
                        whatToShow="TRADES",
                        useRTH=False,
                        formatDate=2,
                    ),
                    timeout=60.0,
                )
                bars = [b for b in (_bar_to_dict(r) for r in raw)
                        if b["time"] >= from_ts and b["time"] <= to_ts]
                bars.sort(key=lambda b: b["time"])
                if bars:
                    logger.info("[%s] ContFuture fallback: got %d %s bars",
                                symbol, len(bars), bar_size_key)
                    return bars
        except Exception as e:
            logger.warning("[%s] ContFuture fallback failed: %s", symbol, e)

        logger.info("[%s] On-demand fetch: no data from any contract for %s %s→%s",
                    symbol, bar_size_key, from_ts, to_ts)
        return []

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

        key = "5min"
        interval = 300
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
        Subscribes to MES only (legacy). Use subscribe_mktdata_all() for all symbols.
        """
        contract = await self._get_contract()
        self._seed_rt_current()

        ticker = self.ib.reqMktData(contract, "", False, False)
        self._realtime_subscriptions["mktdata"] = ticker
        ticker.updateEvent += self._on_tick
        logger.info("Subscribed to market data ticks (≤250 ms chart updates)")

    async def subscribe_mktdata_all(self):
        """Subscribe to tick-level streaming for ALL configured symbols."""
        from ib_insync import ContFuture as _ContFuture
        import db as _db

        # Subscribe MES first (uses cached contract)
        await self.subscribe_mktdata()

        # Subscribe extra symbols
        for sym_cfg in config.EXTRA_SYMBOLS:
            sym_name = sym_cfg["symbol"]
            try:
                contract = _ContFuture(
                    symbol=sym_cfg.get("ib_symbol", sym_name),
                    exchange=sym_cfg["exchange"],
                    currency=sym_cfg["currency"],
                )
                qualified = await asyncio.wait_for(
                    self.ib.qualifyContractsAsync(contract), timeout=30.0,
                )
                if not qualified:
                    logger.warning("[%s] No contract for realtime — skipping", sym_name)
                    continue

                # Initialize per-symbol state
                rt_key = f"{sym_name}:5min"
                self._rt_current[rt_key] = None
                self._tick_state[sym_name] = {
                    "prev_price": float("nan"),
                    "prev_size": float("nan"),
                    "last_broadcast": 0.0,
                }

                # Seed rt_current from DB if available
                db_bars = _db.get_bars(sym_name, "5min", limit=1)
                if db_bars:
                    now_ts = int(time.time())
                    bar_ts = (now_ts // 300) * 300
                    last = dict(db_bars[-1])
                    if last["time"] == bar_ts:
                        self._rt_current[rt_key] = last

                # Initialize in-memory bars for symbol
                if sym_name not in self._symbol_bars:
                    self._symbol_bars[sym_name] = {"5min": []}
                    # Load recent bars from DB
                    recent = _db.get_bars(sym_name, "5min")
                    if recent:
                        self._symbol_bars[sym_name]["5min"] = recent[-config.MAX_BARS_IN_MEMORY:]

                ticker = self.ib.reqMktData(qualified[0], "", False, False)
                sub_key = f"mktdata_{sym_name}"
                self._realtime_subscriptions[sub_key] = ticker

                # Create per-symbol tick handler
                def make_handler(symbol):
                    def handler(t):
                        self._on_tick_multi(t, symbol)
                    return handler

                ticker.updateEvent += make_handler(sym_name)
                logger.info("[%s] Subscribed to market data ticks", sym_name)

            except Exception as e:
                logger.warning("[%s] Realtime subscription failed: %s", sym_name, e)

    def _seed_rt_current(self):
        """Pre-fill _rt_current from last historical bar to avoid startup gap."""
        now_ts = int(time.time())
        key = "5min"
        interval = 300
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
        key = "5min"
        interval = 300
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
            cur = self._rt_current[key]
            if cur:
                self._dispatch(key, cur)

    def _on_tick_multi(self, ticker, symbol: str):
        """Tick handler for non-MES symbols."""
        price = ticker.last
        size  = ticker.lastSize
        if price is None or math.isnan(price) or price <= 0:
            return
        if size is None or math.isnan(size):
            size = 0.0

        state = self._tick_state.get(symbol, {})
        prev_price = state.get("prev_price", float("nan"))
        prev_size = state.get("prev_size", float("nan"))

        vol_delta = 0.0
        if price != prev_price or size != prev_size:
            vol_delta = float(size)
            state["prev_price"] = price
            state["prev_size"] = size

        wall_ts = int(time.time())
        rt_key = f"{symbol}:5min"
        interval = 300
        bar_ts = (wall_ts // interval) * interval
        cur = self._rt_current.get(rt_key)

        sym_bars = self._symbol_bars.get(symbol, {}).get("5min", [])

        if cur is None or bar_ts > cur["time"]:
            if cur is not None:
                self._append_bar_multi(symbol, "5min", cur)
                self._dispatch_multi(symbol, "5min", cur)
            cur = {"time": bar_ts, "open": price, "high": price,
                   "low": price, "close": price, "volume": vol_delta}
            self._rt_current[rt_key] = cur
            self._append_bar_multi(symbol, "5min", cur)
        else:
            cur["high"]    = max(cur["high"], price)
            cur["low"]     = min(cur["low"],  price)
            cur["close"]   = price
            cur["volume"] += vol_delta
            if sym_bars and sym_bars[-1]["time"] == bar_ts:
                sym_bars[-1] = cur

        now = time.monotonic()
        last_broadcast = state.get("last_broadcast", 0.0)
        if now - last_broadcast >= self._TICK_BROADCAST_INTERVAL:
            state["last_broadcast"] = now
            cur = self._rt_current.get(rt_key)
            if cur:
                self._dispatch_multi(symbol, "5min", cur)

    # ─── Shared helpers ───────────────────────────────────────────────────────

    def add_new_bar_callback(self, callback: Callable):
        self._new_bar_callbacks.append(callback)

    def _dispatch(self, key: str, bar: dict):
        for cb in self._new_bar_callbacks:
            try:
                cb(key, dict(bar))
            except Exception as exc:
                logger.error("Callback error: %s", exc)

    def _dispatch_multi(self, symbol: str, key: str, bar: dict):
        """Dispatch bar update with symbol info for multi-symbol callbacks."""
        for cb in self._new_bar_callbacks:
            try:
                cb(key, dict(bar), symbol=symbol)
            except Exception as exc:
                logger.error("Callback error for %s: %s", symbol, exc)

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

    def _append_bar_multi(self, symbol: str, key: str, bar: dict):
        """Append bar to per-symbol in-memory store."""
        if symbol not in self._symbol_bars:
            self._symbol_bars[symbol] = {}
        if key not in self._symbol_bars[symbol]:
            self._symbol_bars[symbol][key] = []
        bars = self._symbol_bars[symbol][key]

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
        # Cancel all mktdata subscriptions (MES + extra symbols)
        for sub_key, ticker in list(self._realtime_subscriptions.items()):
            if sub_key.startswith("mktdata"):
                try:
                    self.ib.cancelMktData(ticker)
                except Exception as e:
                    logger.warning("Error cancelling market data %s: %s", sub_key, e)
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

    def get_bars_for_symbol(
        self,
        symbol: str,
        bar_size_key: str,
        from_ts: Optional[int] = None,
        to_ts: Optional[int]   = None,
    ) -> List[dict]:
        """Get in-memory bars for a specific symbol."""
        if symbol == "MES":
            return self.get_bars(bar_size_key, from_ts, to_ts)
        bars = self._symbol_bars.get(symbol, {}).get(bar_size_key, [])
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
