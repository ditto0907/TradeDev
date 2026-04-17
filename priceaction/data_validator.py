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
                # Tag bars with the specific contract month used
                for b in bars:
                    b["contract_month"] = month
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
            # Tag bars with the contract month derived from each bar's timestamp
            for b in bars:
                b["contract_month"] = _contract_month_for_ts(b["time"], symbol)
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
    if gap_start is not None and expected:
        missing_ranges.append((gap_start, expected[-1]))

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


def _fmt_range(from_ts: int, to_ts: int) -> str:
    """Format a UTC timestamp pair as readable ET date strings."""
    from_dt = datetime.fromtimestamp(from_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    to_dt   = datetime.fromtimestamp(to_ts,   tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    return f"{from_dt} → {to_dt} UTC"


async def validate_bars(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    ib: Optional[IB] = None,
    fetcher: Optional["IBDataFetcher"] = None,
    contract_month: Optional[str] = None,
    skip_validated: bool = False,
) -> dict:
    """Validate DB bars against IB for a given range.

    Performs three levels of validation:
      1. **IB comparison**: Compare DB bars against IB source-of-truth
      2. **OHLCV integrity**: Check for bars with high < low, non-positive prices, etc.
      3. **Completeness**: Use trading calendar to check for expected-but-missing bars

    If *contract_month* is provided (e.g. '202503'), only bars tagged with that
    contract are fetched from DB and compared — prevents cross-contract comparison
    of bars in the same time range.

    If *skip_validated* is True, sub-ranges already in the ``validated_ranges``
    table are skipped, reducing IB API calls.

    Uses the local IB fetch cache to avoid redundant IB requests.
    If *fetcher* is provided, uses its IB connection (avoids event-loop deadlocks).
    """
    # When skip_validated is set, check if the entire range is already validated
    if skip_validated and db.is_range_validated(symbol, timeframe, from_ts, to_ts):
        logger.info("[DATA VALIDATE] %s/%s range already validated, skipping", symbol, timeframe)
        db_bars = db.get_bars(symbol, timeframe, from_ts, to_ts, contract_month=contract_month)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "contract_month": contract_month or "",
            "from_ts": from_ts,
            "to_ts": to_ts,
            "db_count": len(db_bars),
            "ib_count": 0,
            "mismatches": [],
            "mismatch_count": 0,
            "db_only_count": 0,
            "ib_only_count": 0,
            "ib_only": [],
            "ohlcv_violations": [],
            "ohlcv_violation_count": 0,
            "calendar_missing_count": 0,
            "calendar_missing": [],
            "already_validated": True,
        }

    rng = _fmt_range(from_ts, to_ts)
    cm_info = f"  contract={contract_month}" if contract_month else ""
    logger.info("=== [DATA VALIDATE] START  %s/%s  %s%s ===", symbol, timeframe, rng, cm_info)
    t0 = time.monotonic()

    db_bars = db.get_bars(symbol, timeframe, from_ts, to_ts, contract_month=contract_month)
    ib_bars = await get_ib_bars_with_cache(
        symbol, timeframe, from_ts, to_ts, ib=ib, fetcher=fetcher
    )
    # Filter IB bars to the same contract when specified, so we only compare
    # bars that belong to the same futures contract.
    if contract_month is not None:
        ib_bars = [b for b in ib_bars if b.get("contract_month", "") == contract_month]

    mismatches, db_only, ib_only = _compare_bars(db_bars, ib_bars)

    # ── OHLCV integrity check ────────────────────────────────────────────
    ohlcv_violations = []
    try:
        from trading_calendar import get_calendar
        cal = get_calendar(symbol)
        for b in db_bars:
            issues = cal.validate_bar(b)
            if issues:
                ohlcv_violations.append({
                    "time": b["time"],
                    "issues": issues,
                    "bar": b,
                })
    except Exception:
        # Fallback: basic validation without calendar
        for b in db_bars:
            issues = []
            o, h, l, c = b.get("open", 0), b.get("high", 0), b.get("low", 0), b.get("close", 0)
            if h < l:
                issues.append(f"high ({h}) < low ({l})")
            if any(p <= 0 for p in (o, h, l, c)):
                issues.append(f"non-positive price")
            if issues:
                ohlcv_violations.append({"time": b["time"], "issues": issues, "bar": b})

    # ── Completeness check (calendar-aware) ──────────────────────────────
    calendar_missing = []
    try:
        from trading_calendar import get_calendar
        cal = get_calendar(symbol)
        _, interval = _key_to_ib(timeframe)
        actual_ts = [b["time"] for b in db_bars]
        missing = cal.find_missing_bars(actual_ts, from_ts, to_ts, interval)
        calendar_missing = missing[:100]  # Cap to avoid huge responses
    except Exception:
        pass

    elapsed = time.monotonic() - t0
    logger.info(
        "=== [DATA VALIDATE] DONE   %s/%s  %s%s  "
        "db=%d ib=%d mismatches=%d db_only=%d ib_only=%d "
        "ohlcv_violations=%d calendar_missing=%d  (%.1fs) ===",
        symbol, timeframe, rng, cm_info,
        len(db_bars), len(ib_bars), len(mismatches), len(db_only), len(ib_only),
        len(ohlcv_violations), len(calendar_missing),
        elapsed,
    )

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "contract_month": contract_month or "",
        "from_ts": from_ts,
        "to_ts": to_ts,
        "db_count": len(db_bars),
        "ib_count": len(ib_bars),
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
        "db_only_count": len(db_only),
        "ib_only_count": len(ib_only),
        "ib_only": ib_only,
        "ohlcv_violations": ohlcv_violations,
        "ohlcv_violation_count": len(ohlcv_violations),
        "calendar_missing_count": len(calendar_missing),
        "calendar_missing": calendar_missing,
    }


async def fix_bars(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    ib: Optional[IB] = None,
    fetcher: Optional["IBDataFetcher"] = None,
    timestamps: Optional[List[int]] = None,
    contract_month: Optional[str] = None,
) -> dict:
    """Validate and fix: overwrite mismatched DB bars with IB data,
    insert bars that exist only in IB.

    If *timestamps* is provided, only fix bars whose timestamps are in that list
    (selective fix — for the UI's per-row checkbox workflow).

    If *contract_month* is provided (e.g. '202503'), only bars tagged with that
    contract are fetched from DB and compared — prevents cross-contract fixes.

    Uses the local IB fetch cache; no additional IB request is made if the cache
    already has data for the requested range (populated during validate_bars).
    Returns summary.
    """
    rng = _fmt_range(from_ts, to_ts)
    cm_info = f"  contract={contract_month}" if contract_month else ""
    sel_info = f"  selected={len(timestamps)}" if timestamps is not None else ""
    logger.info("=== [DATA FIX] START  %s/%s  %s%s%s ===", symbol, timeframe, rng, cm_info, sel_info)
    t0 = time.monotonic()

    db_bars = db.get_bars(symbol, timeframe, from_ts, to_ts, contract_month=contract_month)
    ib_bars = await get_ib_bars_with_cache(
        symbol, timeframe, from_ts, to_ts, ib=ib, fetcher=fetcher
    )
    # Filter IB bars to the same contract when specified
    if contract_month is not None:
        ib_bars = [b for b in ib_bars if b.get("contract_month", "") == contract_month]

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

    elapsed = time.monotonic() - t0
    logger.info(
        "=== [DATA FIX] DONE   %s/%s  %s%s  "
        "db=%d ib=%d mismatches=%d ib_only=%d fixed=%d  (%.1fs) ===",
        symbol, timeframe, rng, cm_info,
        len(db_bars), len(ib_bars), len(mismatches), len(ib_only), saved,
        elapsed,
    )

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "contract_month": contract_month or "",
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
    in day-sized chunks per contract month. If fix=True, overwrite bad data.

    Validates each contract month separately to prevent cross-contract comparison
    of bars in the same time range (e.g. MES202503 vs MES202506 overlap).
    Pairs that have no contract_month tags fall back to the original (unfiltered)
    per-chunk approach for backward compatibility.

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
    logger.info("=== [DATA VALIDATE ALL] START  fix=%s ===", fix)
    t0_all = time.monotonic()

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

            # Determine contract months to iterate (for per-contract validation)
            contract_months = db.get_distinct_contract_months(sym, tf)
            # Pairs without any contract tags fall back to unfiltered validation
            if not contract_months:
                contract_months = [None]  # None means no contract filter

            logger.info("[validate_all] Scanning %s/%s  %s → %s  contracts=%s",
                        sym, tf, effective_earliest, latest,
                        contract_months if contract_months[0] else ["(all)"])

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
                "contracts": [cm for cm in contract_months if cm],
            }

            for cm in contract_months:
                chunk_start = effective_earliest
                while chunk_start <= latest:
                    chunk_end = min(chunk_start + effective_chunk, latest)

                    try:
                        if fix:
                            r = await fix_bars(sym, tf, chunk_start, chunk_end,
                                              ib=conn_ib, fetcher=fetcher,
                                              contract_month=cm)
                            sym_result["total_fixed"] += r["fixed_count"]
                            sym_result["total_ib_only_inserted"] += r["ib_only_inserted"]
                        else:
                            r = await validate_bars(sym, tf, chunk_start, chunk_end,
                                                   ib=conn_ib, fetcher=fetcher,
                                                   contract_month=cm)

                        sym_result["chunks_checked"] += 1
                        sym_result["total_mismatches"] += r["mismatch_count"]

                        if r["mismatch_count"] > 0:
                            for m in r["mismatches"]:
                                sym_result["mismatch_details"].append({
                                    "time": m["time"],
                                    "contract_month": cm or "",
                                    "diffs": {k: {"db": v[0], "ib": v[1]}
                                              for k, v in m["diffs"].items()},
                                })

                        logger.info("[validate_all] %s/%s%s chunk %s→%s: %d mismatches",
                                    sym, tf,
                                    f" [{cm}]" if cm else "",
                                    chunk_start, chunk_end, r["mismatch_count"])

                    except Exception as e:
                        logger.warning("[validate_all] %s/%s%s chunk %s-%s failed: %s",
                                       sym, tf,
                                       f" [{cm}]" if cm else "",
                                       chunk_start, chunk_end, e)

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

        elapsed_all = time.monotonic() - t0_all
        logger.info(
            "=== [DATA VALIDATE ALL] DONE  fix=%s  pairs=%d  mismatches=%d  "
            "fixed=%d  ib_only=%d  (%.1fs) ===",
            fix, len(pairs), total_mismatches, total_fixed, total_ib_only, elapsed_all,
        )

    finally:
        if should_disconnect and conn_ib:
            conn_ib.disconnect()

    return results


# ─── Background Validation Task ──────────────────────────────────────────────


async def background_validate(
    fetcher: Optional["IBDataFetcher"] = None,
    chunk_seconds: int = 86400,
) -> None:
    """Background task that silently validates data integrity.

    For each symbol/timeframe pair, scans from the most recent stored data
    backwards to the oldest, skipping ranges already validated (persisted in
    the ``validated_ranges`` table).

    Validated ranges are recorded after each chunk so the task is resumable
    and never re-checks the same data.
    """
    logger.info("=== [BG VALIDATE] Starting background validation task ===")
    t0 = time.monotonic()

    # Skip data older than 365 days
    max_age_ts = int(time.time()) - 365 * 86400

    try:
        with db._conn() as conn:
            pairs = conn.execute(
                "SELECT DISTINCT symbol, timeframe FROM bars"
            ).fetchall()

        for sym, tf in pairs:
            earliest = db.get_earliest_ts(sym, tf)
            latest = db.get_latest_ts(sym, tf)
            if earliest is None or latest is None:
                continue

            _, interval = _key_to_ib(tf)

            # Use larger chunks for daily bars
            if tf == "1D":
                effective_chunk = 30 * 86400
            else:
                effective_chunk = chunk_seconds

            effective_earliest = max(earliest, max_age_ts)
            if effective_earliest > latest:
                logger.info("[BG VALIDATE] %s/%s: all data older than 1 year, skipping", sym, tf)
                continue

            # Get unchecked ranges (skip already validated)
            unchecked = db.get_unchecked_ranges(sym, tf, effective_earliest, latest)
            if not unchecked:
                logger.info("[BG VALIDATE] %s/%s: fully validated, skipping", sym, tf)
                continue

            logger.info("[BG VALIDATE] %s/%s: %d unchecked range(s) to validate",
                        sym, tf, len(unchecked))

            for uc_range in unchecked:
                # Process each unchecked range in chunks (newest first)
                uc_from = uc_range["from_ts"]
                uc_to = uc_range["to_ts"]

                # Work backwards from newest to oldest
                chunk_end = uc_to
                while chunk_end >= uc_from:
                    chunk_start = max(uc_from, chunk_end - effective_chunk)

                    try:
                        result = await validate_bars(
                            sym, tf, chunk_start, chunk_end,
                            fetcher=fetcher,
                        )

                        # Record this chunk as validated
                        db.insert_validated_range(
                            sym, tf, chunk_start, chunk_end,
                            mismatches=result.get("mismatch_count", 0),
                        )

                        if result.get("mismatch_count", 0) > 0:
                            logger.info(
                                "[BG VALIDATE] %s/%s chunk [%s→%s]: %d mismatches found",
                                sym, tf, chunk_start, chunk_end,
                                result["mismatch_count"],
                            )

                    except Exception as e:
                        logger.warning(
                            "[BG VALIDATE] %s/%s chunk [%s→%s] failed: %s",
                            sym, tf, chunk_start, chunk_end, e,
                        )

                    chunk_end = chunk_start - interval
                    # IB pacing
                    await asyncio.sleep(2)

    except Exception as e:
        logger.error("[BG VALIDATE] Background validation error: %s", e)

    elapsed = time.monotonic() - t0
    logger.info("=== [BG VALIDATE] Complete (%.1fs) ===", elapsed)

