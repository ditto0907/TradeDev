"""
Data Validator — compare DB bars against IB historical data, detect and fix
discrepancies for any symbol / timeframe.

Provides:
  - validate_bars(): compare a time range of DB bars with IB source-of-truth
  - fix_bars():      overwrite mismatched DB bars with IB data
  - validate_all():  scan every symbol/timeframe pair stored in the DB

Designed to be called from the API or standalone scripts.
"""
import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from ib_insync import ContFuture, Future, IB

import config
import db
from ib_data_fetcher import (
    RESOLUTION_MAP,
    _bar_to_dict,
    _contract_month_for_ts,
    _key_to_ib,
    _next_contract_month,
    _prev_contract_month,
    ib_duration,
)

if TYPE_CHECKING:
    from ib_data_fetcher import IBDataFetcher

logger = logging.getLogger(__name__)

# Price tolerance for comparing OHLC values (0.5 tick — covers rounding)
_PRICE_TOL = 0.5
# Volume tolerance: flag when volumes differ by more than this amount
_VOLUME_TOL = 1.0


# ─── IB Connection Helper ────────────────────────────────────────────────────

async def _connect_ib(ib: Optional[IB] = None) -> Tuple[IB, bool]:
    """Return an IB connection. If *ib* is already connected, reuse it.
    Returns (ib, should_disconnect) — caller disconnects only when True."""
    if ib and ib.isConnected():
        return ib, False
    new_ib = IB()
    await new_ib.connectAsync(
        config.IB_HOST, config.IB_PORT,
        clientId=config.IB_CLIENT_ID + 80,  # offset to avoid collisions
        timeout=20,
    )
    return new_ib, True


# Cache for contract qualification results to avoid retrying dead contracts
_qualified_cache: Dict[str, object] = {}   # "SYM_YYYYMM" -> qualified contract
_failed_contracts: set = set()              # "SYM_YYYYMM" contracts that failed


async def _fetch_ib_bars(
    ib: IB,
    symbol: str,
    bar_size_key: str,
    from_ts: int,
    to_ts: int,
) -> List[dict]:
    """Fetch historical bars from IB for a single range.
    Tries month-specific Future first, then ContFuture fallback.
    Caches contract qualifications to avoid re-trying dead contracts."""
    bar_size, interval = _key_to_ib(bar_size_key)
    inst = config.INSTRUMENTS.get(symbol)
    if not inst:
        logger.error("Unknown symbol: %s", symbol)
        return []

    ib_sym   = inst["ib_symbol"]
    exchange = inst["exchange"]
    currency = inst["currency"]

    end_ts  = ((to_ts + interval - 1) // interval) * interval
    end_dt  = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    end_str = end_dt.strftime("%Y%m%d %H:%M:%S UTC")
    dur_str = ib_duration(end_ts - from_ts)

    # Strategy 1: month-specific Future (with contract cache)
    target  = _contract_month_for_ts(end_ts, symbol)
    months  = [target, _next_contract_month(target, symbol),
               _prev_contract_month(target, symbol)]
    seen_m: set = set()
    months = [m for m in months if not (m in seen_m or seen_m.add(m))]

    for month in months:
        cache_key = f"{symbol}_{month}"

        # Skip previously failed contracts
        if cache_key in _failed_contracts:
            continue

        try:
            # Use cached qualified contract or qualify new one
            if cache_key in _qualified_cache:
                qualified_contract = _qualified_cache[cache_key]
            else:
                contract = Future(symbol=ib_sym, exchange=exchange,
                                  currency=currency,
                                  lastTradeDateOrContractMonth=month)
                contract.includeExpired = True
                qualified = await asyncio.wait_for(
                    ib.qualifyContractsAsync(contract), timeout=20)
                if not qualified:
                    _failed_contracts.add(cache_key)
                    continue
                qualified_contract = qualified[0]
                _qualified_cache[cache_key] = qualified_contract

            raw = await asyncio.wait_for(
                ib.reqHistoricalDataAsync(
                    qualified_contract, endDateTime=end_str,
                    durationStr=dur_str, barSizeSetting=bar_size,
                    whatToShow="TRADES", useRTH=False, formatDate=2),
                timeout=60)
            bars = [b for b in (_bar_to_dict(r) for r in raw)
                    if from_ts <= b["time"] <= to_ts]
            if bars:
                return sorted(bars, key=lambda b: b["time"])
        except asyncio.TimeoutError:
            logger.debug("[%s] Future %s qualify timed out, caching as failed", symbol, month)
            _failed_contracts.add(cache_key)
        except Exception as e:
            logger.debug("[%s] Future %s fetch failed: %s", symbol, month, e)
            _failed_contracts.add(cache_key)

    # Strategy 2: ContFuture fallback
    try:
        cont_key = f"{symbol}_CONT"
        if cont_key in _qualified_cache:
            cont_contract = _qualified_cache[cont_key]
        else:
            contract = ContFuture(symbol=ib_sym, exchange=exchange, currency=currency)
            qualified = await asyncio.wait_for(
                ib.qualifyContractsAsync(contract), timeout=20)
            if qualified:
                cont_contract = qualified[0]
                _qualified_cache[cont_key] = cont_contract
            else:
                return []
        raw = await asyncio.wait_for(
            ib.reqHistoricalDataAsync(
                cont_contract, endDateTime="",
                durationStr=dur_str, barSizeSetting=bar_size,
                whatToShow="TRADES", useRTH=False, formatDate=2),
            timeout=60)
        bars = [b for b in (_bar_to_dict(r) for r in raw)
                if from_ts <= b["time"] <= to_ts]
        if bars:
            return sorted(bars, key=lambda b: b["time"])
    except Exception as e:
        logger.debug("[%s] ContFuture fallback failed: %s", symbol, e)

    return []


# ─── IB Fetch Cache Helper ────────────────────────────────────────────────────

async def get_ib_bars_with_cache(
    symbol: str,
    bar_size_key: str,
    from_ts: int,
    to_ts: int,
    ib: Optional[IB] = None,
    fetcher: Optional["IBDataFetcher"] = None,
) -> List[dict]:
    """Return IB bars for [from_ts, to_ts] using local cache to avoid redundant
    IB requests.

    Algorithm:
      1. Query ib_fetch_cache for the requested range.
      2. Identify missing boundary and internal sub-ranges.
      3. Fetch only the missing sub-ranges from IB.
      4. Persist newly fetched bars to ib_fetch_cache.
      5. Return all cached bars for the full range.

    'Missing' is determined by comparing expected bar timestamps (based on the
    interval) against what is already cached.  A small overlap buffer is added
    when fetching to guarantee boundary bars are never missed.
    """
    _, interval = _key_to_ib(bar_size_key)

    # Align boundaries to interval grid
    aligned_from = (from_ts // interval) * interval
    aligned_to   = ((to_ts + interval - 1) // interval) * interval

    cached_ts_set = set(db.get_ib_cache_coverage(symbol, bar_size_key, aligned_from, aligned_to))

    # Build list of expected bar timestamps in the range (coarse — every interval)
    expected: List[int] = []
    t = aligned_from
    while t <= aligned_to:
        expected.append(t)
        t += interval

    # Find contiguous missing sub-ranges
    missing_ranges: List[Tuple[int, int]] = []
    gap_start: Optional[int] = None
    for ts in expected:
        if ts not in cached_ts_set:
            if gap_start is None:
                gap_start = ts
            gap_end = ts
        else:
            if gap_start is not None:
                missing_ranges.append((gap_start, gap_end))
                gap_start = None
    if gap_start is not None:
        missing_ranges.append((gap_start, expected[-1] if expected else aligned_to))

    # Merge adjacent/overlapping ranges (with a 1-interval buffer to avoid gaps at edges)
    merged: List[Tuple[int, int]] = []
    for rng_from, rng_to in missing_ranges:
        fetch_from = max(aligned_from, rng_from - interval)
        fetch_to   = min(aligned_to,   rng_to   + interval)
        if merged and fetch_from <= merged[-1][1] + interval:
            merged[-1] = (merged[-1][0], max(merged[-1][1], fetch_to))
        else:
            merged.append((fetch_from, fetch_to))

    # Fetch missing sub-ranges from IB and store in cache
    for sub_from, sub_to in merged:
        logger.info("[%s/%s] IB cache miss: fetching [%s→%s] from IB",
                    symbol, bar_size_key, sub_from, sub_to)
        try:
            if fetcher:
                fetched = await fetcher.fetch_range(bar_size_key, sub_from, sub_to, symbol=symbol)
            else:
                fetched = await _fetch_ib_bars(ib, symbol, bar_size_key, sub_from, sub_to)

            if fetched:
                saved = db.insert_ib_cache_bars(symbol, bar_size_key, fetched)
                logger.info("[%s/%s] Cached %d IB bars for [%s→%s]",
                            symbol, bar_size_key, saved, sub_from, sub_to)
            else:
                logger.debug("[%s/%s] IB returned 0 bars for [%s→%s]",
                             symbol, bar_size_key, sub_from, sub_to)

            await asyncio.sleep(2)  # IB pacing
        except Exception as e:
            logger.warning("[%s/%s] IB fetch for cache [%s→%s] failed: %s",
                           symbol, bar_size_key, sub_from, sub_to, e)

    # Return full range from cache (includes both pre-existing and newly fetched)
    return db.get_ib_cache_bars(symbol, bar_size_key, aligned_from, aligned_to)


# ─── Validate ─────────────────────────────────────────────────────────────────

def _compare_bars(
    db_bars: List[dict],
    ib_bars: List[dict],
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Compare DB bars against IB bars.
    Returns (mismatches, db_only, ib_only).
    Each mismatch: {time, db: {…}, ib: {…}, diffs: {field: (db_val, ib_val)}}
    Compares OHLCV fields — Open/High/Low/Close use _PRICE_TOL, Volume uses _VOLUME_TOL.
    """
    db_map = {b["time"]: b for b in db_bars}
    ib_map = {b["time"]: b for b in ib_bars}

    all_ts = sorted(set(db_map) | set(ib_map))
    mismatches: List[dict] = []
    db_only:    List[dict] = []
    ib_only:    List[dict] = []

    for ts in all_ts:
        if ts not in ib_map:
            db_only.append(db_map[ts])
            continue
        if ts not in db_map:
            ib_only.append(ib_map[ts])
            continue

        d = db_map[ts]
        i = ib_map[ts]
        diffs: Dict[str, tuple] = {}
        for fld in ("open", "high", "low", "close"):
            if abs(d[fld] - i[fld]) > _PRICE_TOL:
                diffs[fld] = (d[fld], i[fld])
        if abs(d.get("volume", 0) - i.get("volume", 0)) > _VOLUME_TOL:
            diffs["volume"] = (d.get("volume", 0), i.get("volume", 0))
        if diffs:
            mismatches.append({
                "time": ts, "db": d, "ib": i, "diffs": diffs,
            })

    return mismatches, db_only, ib_only


async def validate_bars(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    ib: Optional[IB] = None,
    fetcher: Optional["IBDataFetcher"] = None,
) -> dict:
    """Validate DB bars against IB for a given range.
    Returns summary dict with mismatches/counts.

    Uses the local IB fetch cache to avoid redundant IB requests.
    If *fetcher* is provided, uses its IB connection (avoids event-loop deadlocks).
    """
    db_bars = db.get_bars(symbol, timeframe, from_ts, to_ts)
    ib_bars = await get_ib_bars_with_cache(
        symbol, timeframe, from_ts, to_ts, ib=ib, fetcher=fetcher
    )

    mismatches, db_only, ib_only = _compare_bars(db_bars, ib_bars)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "db_count": len(db_bars),
        "ib_count": len(ib_bars),
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
        "db_only_count": len(db_only),
        "ib_only_count": len(ib_only),
        "ib_only": ib_only,
    }


async def fix_bars(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    ib: Optional[IB] = None,
    fetcher: Optional["IBDataFetcher"] = None,
    timestamps: Optional[List[int]] = None,
) -> dict:
    """Validate and fix: overwrite mismatched DB bars with IB data,
    insert bars that exist only in IB.

    If *timestamps* is provided, only fix bars whose timestamps are in that list
    (selective fix — for the UI's per-row checkbox workflow).

    Uses the local IB fetch cache; no additional IB request is made if the cache
    already has data for the requested range (populated during validate_bars).
    Returns summary.
    """
    db_bars = db.get_bars(symbol, timeframe, from_ts, to_ts)
    ib_bars = await get_ib_bars_with_cache(
        symbol, timeframe, from_ts, to_ts, ib=ib, fetcher=fetcher
    )

    mismatches, db_only, ib_only = _compare_bars(db_bars, ib_bars)

    # Filter to selected timestamps when provided
    ts_filter: Optional[set] = set(timestamps) if timestamps is not None else None

    fixed_bars: List[dict] = []
    # Fix mismatched bars with IB data
    for m in mismatches:
        if ts_filter is not None and m["time"] not in ts_filter:
            continue
        bar = dict(m["ib"])
        bar["source"] = "ib_validated"
        fixed_bars.append(bar)

    # Insert bars only in IB (missing from DB)
    for bar in ib_only:
        if ts_filter is not None and bar["time"] not in ts_filter:
            continue
        b = dict(bar)
        b["source"] = "ib_validated"
        fixed_bars.append(b)

    saved = 0
    if fixed_bars:
        saved = db.insert_bars(symbol, timeframe, fixed_bars, source="ib_validated")

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "db_count": len(db_bars),
        "ib_count": len(ib_bars),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "ib_only_inserted": len(ib_only),
        "ib_only": ib_only,
        "fixed_count": saved,
    }


async def validate_all(
    fix: bool = False,
    ib: Optional[IB] = None,
    fetcher: Optional["IBDataFetcher"] = None,
    chunk_seconds: int = 86400,  # validate one day at a time
) -> List[dict]:
    """Scan all symbol/timeframe pairs in the DB, validate against IB
    in day-sized chunks. If fix=True, overwrite bad data.

    Prefer passing *fetcher* (the server's IBDataFetcher) which reuses
    the existing IB connection — avoids event-loop deadlocks from having
    two ib_insync clients on the same loop.

    IB has pacing limits (~6 requests per 10 seconds for historical data),
    so we rate-limit and work in chunks.  The IB fetch cache is used here so
    subsequent fix runs skip already-cached data.

    Daily (1D) bars use 30-day chunks to reduce IB requests.
    Data older than 365 days is skipped since IB may not serve expired
    contracts reliably.
    """
    # Only create a standalone IB connection when no fetcher is available
    conn_ib = None
    should_disconnect = False
    if not fetcher:
        conn_ib, should_disconnect = await _connect_ib(ib)

    # Skip data older than 1 year — IB can't reliably serve expired contracts
    max_age_ts = int(time.time()) - 365 * 86400

    results: List[dict] = []

    try:
        # Discover all (symbol, timeframe) pairs
        with db._conn() as conn:
            pairs = conn.execute(
                "SELECT DISTINCT symbol, timeframe FROM bars"
            ).fetchall()

        total_fixed = 0
        total_mismatches = 0
        total_ib_only = 0

        for sym, tf in pairs:
            earliest = db.get_earliest_ts(sym, tf)
            latest   = db.get_latest_ts(sym, tf)
            if earliest is None or latest is None:
                continue

            _, interval = _key_to_ib(tf)

            # Use larger chunks for daily bars to reduce IB requests
            if tf == "1D":
                effective_chunk = 30 * 86400  # 30 days per chunk
            else:
                effective_chunk = chunk_seconds

            # Skip data older than max_age_ts
            effective_earliest = max(earliest, max_age_ts)
            if effective_earliest > latest:
                logger.info("[validate_all] %s/%s: all data older than 1 year, skipping",
                            sym, tf)
                continue

            logger.info("[validate_all] Scanning %s/%s  %s → %s",
                        sym, tf, effective_earliest, latest)

            chunk_start = effective_earliest
            sym_result = {
                "symbol": sym,
                "timeframe": tf,
                "from_ts": earliest,
                "to_ts": latest,
                "chunks_checked": 0,
                "total_mismatches": 0,
                "total_fixed": 0,
                "total_ib_only_inserted": 0,
                "mismatch_details": [],
            }

            while chunk_start <= latest:
                chunk_end = min(chunk_start + effective_chunk, latest)

                try:
                    if fix:
                        r = await fix_bars(sym, tf, chunk_start, chunk_end,
                                          ib=conn_ib, fetcher=fetcher)
                        sym_result["total_fixed"] += r["fixed_count"]
                        sym_result["total_ib_only_inserted"] += r["ib_only_inserted"]
                    else:
                        r = await validate_bars(sym, tf, chunk_start, chunk_end,
                                               ib=conn_ib, fetcher=fetcher)

                    sym_result["chunks_checked"] += 1
                    sym_result["total_mismatches"] += r["mismatch_count"]

                    if r["mismatch_count"] > 0:
                        for m in r["mismatches"]:
                            sym_result["mismatch_details"].append({
                                "time": m["time"],
                                "diffs": {k: {"db": v[0], "ib": v[1]}
                                          for k, v in m["diffs"].items()},
                            })

                    logger.info("[validate_all] %s/%s chunk %s→%s: %d mismatches",
                                sym, tf, chunk_start, chunk_end, r["mismatch_count"])

                except Exception as e:
                    logger.warning("[validate_all] %s/%s chunk %s-%s failed: %s",
                                   sym, tf, chunk_start, chunk_end, e)

                chunk_start = chunk_end + interval

                # IB pacing: ~2 second pause between requests
                await asyncio.sleep(2)

            total_mismatches += sym_result["total_mismatches"]
            total_fixed += sym_result["total_fixed"]
            total_ib_only += sym_result["total_ib_only_inserted"]

            results.append(sym_result)
            logger.info("[validate_all] %s/%s done: %d mismatches, %d fixed",
                        sym, tf, sym_result["total_mismatches"],
                        sym_result["total_fixed"])

        logger.info("[validate_all] Complete: %d pairs, %d total mismatches, "
                    "%d total fixed, %d IB-only inserted",
                    len(pairs), total_mismatches, total_fixed, total_ib_only)

    finally:
        if should_disconnect and conn_ib:
            conn_ib.disconnect()

    return results

