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
from contextlib import asynccontextmanager
from contextvars import ContextVar
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


@asynccontextmanager
async def _null_gate_cm():
    """No-op async context manager used when no fetcher is available."""
    yield


class _NullGate:
    """Sentinel that mimics ``fetcher.bg_gate()`` when fetcher is None."""

    def __call__(self):
        return _null_gate_cm()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


_NULL_GATE = _NullGate()

logger = logging.getLogger(__name__)

# Price tolerance for comparing OHLC values (0.5 tick — covers rounding)
_PRICE_TOL = 0.5
# Volume tolerance: flag when volumes differ by more than this amount
_VOLUME_TOL = 1.0

# Per-task counter: incremented every time we actually issue an IB historical
# request (cache miss). Callers reset it before a validate/fix call and read
# it after to decide whether the IB-pacing sleep is needed. Cache-only paths
# leave it at 0 → no sleep.
_ib_request_counter: ContextVar[int] = ContextVar("_ib_request_counter", default=0)


def _ib_req_reset() -> None:
    _ib_request_counter.set(0)


def _ib_req_incr() -> None:
    _ib_request_counter.set(_ib_request_counter.get() + 1)


def _ib_req_count() -> int:
    return _ib_request_counter.get()


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

    # Build list of expected bar timestamps in the range using the trading
    # calendar so weekends / holidays are excluded.  Without this, 1D fetches
    # perpetually re-fetch because Sat/Sun are never in the cache yet always
    # appear in the naïve every-interval expected list.
    try:
        from trading_calendar import get_calendar as _get_cal
        _cal = _get_cal(symbol)
        expected: List[int] = _cal.expected_bars(aligned_from, aligned_to, interval)
    except Exception:
        # Fallback for unknown symbols: every interval tick (may cause extra
        # re-fetches for weekend/holiday slots, but is safe).
        expected = []
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
            _ib_req_incr()
            if fetcher:
                fetched = await fetcher.fetch_range(bar_size_key, sub_from, sub_to, symbol=symbol)
            else:
                fetched = await _fetch_ib_bars(ib, symbol, bar_size_key, sub_from, sub_to)

            if fetched:
                # v3: tag each bar with contract_token so per-contract
                # rows coexist in the cache.  Monthly fetches → MONTH:YYYYMM;
                # ContFuture-derived bars → CONT (back-adjusted reference).
                for b in fetched:
                    if b.get("contract_token"):
                        continue
                    if b.get("source") == "ib_continuous":
                        b["contract_token"] = "CONT"
                    else:
                        cm = b.get("contract_month") or ""
                        b["contract_token"] = f"MONTH:{cm}" if cm else "CONT"
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

    NOTE on continuous-contract (ContFuture) bars:
    Bars whose ``contract_month`` is empty come from IB's continuous
    contract.  IB re-applies a back-adjustment to the *entire* ContFuture
    history every time the front month rolls, so OHLC and volume of any
    given timestamp drift between fetches.  This drift is not a data
    error — it is by design — so we skip the OHLCV diff on continuous
    bars.  Missing-bar detection (``db_only`` / ``ib_only``) still
    applies, which is what we actually need from validation on these
    rows.
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
        # Skip OHLCV strict compare on continuous-contract bars (see docstring).
        if not d.get("contract_month") or not i.get("contract_month"):
            continue
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


def _check_price_continuity(
    db_bars: List[dict],
    symbol: str,
    interval: int,
    tol: float,
) -> List[dict]:
    """Detect adjacent-bar price discontinuities.

    For each pair of consecutive bars (same contract_month, time delta == interval,
    no session-boundary gap) where ``|open_n - close_(n-1)| > tol``, return a
    violation record.  Catches mis-recorded realtime bars whose open price was
    captured mid-window and therefore does not chain to the previous close.
    """
    if not db_bars or len(db_bars) < 2:
        return []
    try:
        from trading_calendar import get_calendar
        cal = get_calendar(symbol)
    except Exception:
        cal = None

    # Group by contract month so cross-contract boundaries (which legitimately
    # have price discontinuity) are not flagged.
    groups: Dict[str, List[dict]] = {}
    for b in db_bars:
        groups.setdefault(b.get("contract_month", "") or "", []).append(b)

    violations: List[dict] = []
    for cm, bars in groups.items():
        bars = sorted(bars, key=lambda x: x["time"])
        for i in range(1, len(bars)):
            prev, cur = bars[i - 1], bars[i]
            if cur["time"] - prev["time"] != interval:
                continue  # gap or duplicate — handled elsewhere
            # Skip session boundaries (overnight maintenance, weekends, holidays)
            if cal is not None:
                gap_type = cal.classify_gap(prev["time"], cur["time"])
                if gap_type not in ("normal", "data_gap"):
                    continue
            diff = cur["open"] - prev["close"]
            if abs(diff) > tol:
                violations.append({
                    "time": cur["time"],
                    "prev_time": prev["time"],
                    "prev_close": prev["close"],
                    "open": cur["open"],
                    "diff": diff,
                    "contract_month": cm,
                    "prev_source": prev.get("source", ""),
                    "source": cur.get("source", ""),
                })
    return violations


def _fmt_range(from_ts: int, to_ts: int) -> str:
    """Format a UTC timestamp pair as readable ET date strings."""
    from_dt = datetime.fromtimestamp(from_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    to_dt   = datetime.fromtimestamp(to_ts,   tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    return f"{from_dt} → {to_dt} UTC"


def _align_to_grid(from_ts: int, to_ts: int, timeframe: str) -> Tuple[int, int]:
    """Floor (from_ts, to_ts) to the timeframe interval grid.

    Returns closed-closed ``[aligned_from, aligned_to]`` where every bar in
    the range has a ``ts`` that is a multiple of the interval.

    Boundary policy (matches the validation chunk-splitting rules):
      - 1D  → UTC 00:00:00   (one day = ``[00:00, 24:00)`` half-open)
      - 1H  → top of the hour
      - 5min→ multiples of 5 minutes within the hour

    Off-grid inputs (e.g. drift caused by realtime aggregation) are floored
    so chunk boundaries are deterministic and don't leak bars between
    adjacent windows.  ``aligned_to`` is always the inclusive timestamp of
    the LAST bar in the half-open window.
    """
    _, interval = _key_to_ib(timeframe)
    aligned_from = (from_ts // interval) * interval
    aligned_to   = (to_ts   // interval) * interval
    if aligned_to < aligned_from:
        aligned_to = aligned_from
    return aligned_from, aligned_to


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
    # Align inputs to the timeframe grid so chunk boundaries are deterministic
    # and don't leak bars between adjacent windows.
    from_ts, to_ts = _align_to_grid(from_ts, to_ts, timeframe)

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
            "continuity_violations": [],
            "continuity_violation_count": 0,
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

    # ── Price continuity check ──────────────────────────────────────────
    # Detects realtime-source bars whose open was captured mid-window and
    # therefore does not chain to the previous bar's close.
    # Skip for 1D timeframe: daily bars always have an overnight gap between
    # close and next open — flagging every trading day pair as a violation
    # would permanently mark 1D chunks as dirty and cause endless re-fetches.
    continuity_violations: List[dict] = []
    if timeframe != "1D":
        try:
            _, _interval = _key_to_ib(timeframe)
            tol = getattr(config, "PRICE_CONTINUITY_TOL", 1.0)
            continuity_violations = _check_price_continuity(
                db_bars, symbol, _interval, tol,
            )
        except Exception as e:
            logger.debug("price-continuity check failed for %s/%s: %s", symbol, timeframe, e)

    elapsed = time.monotonic() - t0
    logger.info(
        "=== [DATA VALIDATE] DONE   %s/%s  %s%s  "
        "db=%d ib=%d mismatches=%d db_only=%d ib_only=%d "
        "ohlcv_violations=%d calendar_missing=%d continuity=%d  (%.1fs) ===",
        symbol, timeframe, rng, cm_info,
        len(db_bars), len(ib_bars), len(mismatches), len(db_only), len(ib_only),
        len(ohlcv_violations), len(calendar_missing), len(continuity_violations),
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
        "continuity_violations": continuity_violations,
        "continuity_violation_count": len(continuity_violations),
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
    # Align inputs to the timeframe grid so the same window the validator
    # checks is also the window the fixer overwrites.
    from_ts, to_ts = _align_to_grid(from_ts, to_ts, timeframe)

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

    # ── Promote matched bars whose DB source is still provisional ───────────
    # Bars that exist in both DB and IB with values within tolerance are
    # already correct, but if their DB source is ``realtime_completed`` (or
    # ``unknown``) we re-save with source ``ib_validated`` so subsequent page
    # refreshes can show "everything except the most recent live bar is
    # IB-verified" without doing any extra work.
    promoted = 0
    mismatch_ts = {m["time"] for m in mismatches}
    ib_only_ts  = {b["time"] for b in ib_only}
    db_map = {b["time"]: b for b in db_bars}
    ib_map = {b["time"]: b for b in ib_bars}
    _PROVISIONAL_SOURCES = {"realtime_completed", "unknown", ""}
    for ts, db_bar in db_map.items():
        if ts in mismatch_ts or ts in ib_only_ts:
            continue
        if ts not in ib_map:
            continue
        if db_bar.get("source") not in _PROVISIONAL_SOURCES:
            continue
        # Skip continuous-contract bars (they're skipped in compare too).
        if not db_bar.get("contract_month") or not ib_map[ts].get("contract_month"):
            continue
        if ts_filter is not None and ts not in ts_filter:
            continue
        b = dict(ib_map[ts])
        b["source"] = "ib_validated"
        fixed_bars.append(b)
        promoted += 1

    saved = 0
    if fixed_bars:
        saved = db.insert_bars(symbol, timeframe, fixed_bars, source="ib_validated")

    elapsed = time.monotonic() - t0
    logger.info(
        "=== [DATA FIX] DONE   %s/%s  %s%s  "
        "db=%d ib=%d mismatches=%d ib_only=%d promoted=%d fixed=%d  (%.1fs) ===",
        symbol, timeframe, rng, cm_info,
        len(db_bars), len(ib_bars), len(mismatches), len(ib_only),
        promoted, saved,
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
        "promoted_count": promoted,
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
                # Align to interval grid and iterate **newest → oldest** in
                # half-open [chunk_start, chunk_excl_end) windows so the most
                # recent (and most likely to be looked at) data is validated
                # first.  See `background_validate` for the chunk-splitting
                # policy.
                aligned_earliest = (effective_earliest // interval) * interval
                aligned_latest_excl = ((latest // interval) * interval) + interval

                chunk_excl_end = aligned_latest_excl
                while chunk_excl_end > aligned_earliest:
                    chunk_start = max(aligned_earliest, chunk_excl_end - effective_chunk)
                    chunk_incl_end = chunk_excl_end - interval

                    _ib_req_reset()
                    try:
                        if fix:
                            r = await fix_bars(sym, tf, chunk_start, chunk_incl_end,
                                              ib=conn_ib, fetcher=fetcher,
                                              contract_month=cm)
                            sym_result["total_fixed"] += r["fixed_count"]
                            sym_result["total_ib_only_inserted"] += r["ib_only_inserted"]
                        else:
                            r = await validate_bars(sym, tf, chunk_start, chunk_incl_end,
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

                        logger.info("[validate_all] %s/%s%s chunk [%s→%s): %d mismatches",
                                    sym, tf,
                                    f" [{cm}]" if cm else "",
                                    chunk_start, chunk_excl_end, r["mismatch_count"])

                    except Exception as e:
                        logger.warning("[validate_all] %s/%s%s chunk [%s→%s) failed: %s",
                                       sym, tf,
                                       f" [{cm}]" if cm else "",
                                       chunk_start, chunk_excl_end, e)

                    # Half-open: previous window's start becomes this end.
                    chunk_excl_end = chunk_start

                    # IB pacing: only sleep if this chunk actually hit IB.
                    if _ib_req_count() > 0:
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
    fix: bool = False,
    symbols: Optional[List[str]] = None,
    timeframes: Optional[List[str]] = None,
) -> None:
    """Background task that silently validates data integrity.

    For each symbol/timeframe pair, scans from the most recent stored data
    backwards to the oldest, skipping ranges already validated **clean**
    (``mismatches=0`` rows in ``validated_ranges``).  Ranges with outstanding
    issues are re-checked on every run so problems do not get silently
    "frozen" once recorded.

    When *fix* is True, mismatches and ib-only bars are written back into the
    DB using IB as source-of-truth.

    Optional ``symbols`` / ``timeframes`` filters narrow which (sym, tf)
    pairs are visited — useful when the caller wants to validate just one
    symbol without waiting for a long MES backlog to drain.
    """
    sym_filter = set(symbols) if symbols else None
    tf_filter  = set(timeframes) if timeframes else None
    if sym_filter or tf_filter:
        logger.info(
            "=== [BG VALIDATE] Starting (symbols=%s timeframes=%s fix=%s) ===",
            sorted(sym_filter) if sym_filter else "ALL",
            sorted(tf_filter) if tf_filter else "ALL",
            fix,
        )
    else:
        logger.info("=== [BG VALIDATE] Starting background validation task ===")
    t0 = time.monotonic()

    # Skip data older than 365 days
    max_age_ts = int(time.time()) - 365 * 86400

    try:
        with db._conn() as conn:
            raw_pairs = conn.execute(
                "SELECT DISTINCT symbol, timeframe FROM bars"
            ).fetchall()

        # Apply optional filters
        filtered = []
        for row in raw_pairs:
            sym, tf = row[0], row[1]
            if sym_filter and sym not in sym_filter: continue
            if tf_filter and tf not in tf_filter:    continue
            filtered.append((sym, tf))

        # Round-robin reorder: walk timeframes (1D first as it's tiny, then
        # 60min, then 5min) and within each tf cycle through symbols.  This
        # gives every symbol some validation progress instead of starving the
        # later ones behind a huge MES/5min backlog.
        tf_order = {"1D": 0, "60min": 1, "1hour": 1, "1H": 1, "5min": 2, "15min": 3}
        filtered.sort(key=lambda p: (tf_order.get(p[1], 99), p[0]))
        pairs = filtered
        logger.info("[BG VALIDATE] %d pair(s) queued: %s",
                    len(pairs), ", ".join(f"{s}/{tf}" for s, tf in pairs))

        # Group pairs by symbol → run symbols in parallel, timeframes serial
        # within a symbol.  IB allows multiple concurrent historical requests
        # for *different* contracts; bursting parallel requests for the same
        # contract triggers pacing violations, so we keep per-symbol serial.
        by_symbol: Dict[str, List[Tuple[str, str]]] = {}
        for s, tf in pairs:
            by_symbol.setdefault(s, []).append((s, tf))

        async def _run_pair(sym: str, tf: str) -> None:
            earliest = db.get_earliest_ts(sym, tf)
            latest = db.get_latest_ts(sym, tf)
            if earliest is None or latest is None:
                return

            _, interval = _key_to_ib(tf)

            # Use larger chunks for daily bars
            if tf == "1D":
                effective_chunk = 30 * 86400
            else:
                effective_chunk = chunk_seconds

            effective_earliest = max(earliest, max_age_ts)
            if effective_earliest > latest:
                logger.info("[BG VALIDATE] %s/%s: all data older than 1 year, skipping", sym, tf)
                return

            unchecked = db.get_unchecked_ranges(sym, tf, effective_earliest, latest)
            if not unchecked:
                logger.info("[BG VALIDATE] %s/%s: fully validated, skipping", sym, tf)
                return

            # Walk newest → oldest: reverse the ascending list so the most
            # recent unchecked range is validated first.
            unchecked = list(reversed(unchecked))
            logger.info("[BG VALIDATE] %s/%s: %d unchecked range(s) to validate (newest first)",
                        sym, tf, len(unchecked))

            for uc_range in unchecked:
                # Process each unchecked range in chunks (newest first).
                #
                # Chunk-splitting rules (half-open [start, excl_end) windows
                # aligned to natural time boundaries):
                #   - 1D : daily windows on UTC midnight     [00:00, 24:00)
                #   - 1H : hourly windows on top-of-hour     [HH:00, HH+1:00)
                #   - 5min: 5-minute windows on /5 minutes   [HH:MM, HH:MM+5)
                # The bar at ``excl_end`` belongs to the NEXT chunk, never
                # the current one — this prevents the same bar from being
                # compared in two adjacent windows.
                uc_from = (uc_range["from_ts"] // interval) * interval
                uc_to_incl = (uc_range["to_ts"] // interval) * interval

                # Work backwards from newest to oldest using half-open math.
                chunk_excl_end = uc_to_incl + interval
                while chunk_excl_end > uc_from:
                    chunk_start = max(uc_from, chunk_excl_end - effective_chunk)
                    chunk_start = (chunk_start // interval) * interval
                    # Closed-closed end passed to validate/fix (last bar inside
                    # the half-open window).
                    chunk_incl_end = chunk_excl_end - interval

                    try:
                        # Wrap each chunk through the fetcher's BG gate so
                        # chart on-demand requests preempt validation work.
                        gate = (
                            fetcher.bg_gate() if fetcher is not None
                            else _NULL_GATE
                        )
                        _ib_req_reset()
                        async with gate:
                            if fix:
                                result = await fix_bars(
                                    sym, tf, chunk_start, chunk_incl_end,
                                    fetcher=fetcher,
                                )
                                # Re-validate after the fix to compute residual issues.
                                result = await validate_bars(
                                    sym, tf, chunk_start, chunk_incl_end,
                                    fetcher=fetcher,
                                )
                            else:
                                result = await validate_bars(
                                    sym, tf, chunk_start, chunk_incl_end,
                                    fetcher=fetcher,
                                )

                        # Roll all detectable issues into a single counter so a
                        # range with calendar gaps or one-sided bars is not
                        # treated as "clean" on subsequent runs.
                        issue_count = (
                            result.get("mismatch_count", 0)
                            + result.get("db_only_count", 0)
                            + result.get("ib_only_count", 0)
                            + result.get("ohlcv_violation_count", 0)
                            + result.get("calendar_missing_count", 0)
                            + result.get("continuity_violation_count", 0)
                        )

                        # Record this chunk as validated (closed-closed)
                        db.insert_validated_range(
                            sym, tf, chunk_start, chunk_incl_end,
                            mismatches=issue_count,
                        )

                        if issue_count > 0:
                            logger.warning(
                                "[BG VALIDATE] %s/%s chunk [%s→%s): "
                                "%d issue(s) found "
                                "(mismatch=%d db_only=%d ib_only=%d "
                                "ohlcv=%d calendar_missing=%d continuity=%d)",
                                sym, tf, chunk_start, chunk_excl_end, issue_count,
                                result.get("mismatch_count", 0),
                                result.get("db_only_count", 0),
                                result.get("ib_only_count", 0),
                                result.get("ohlcv_violation_count", 0),
                                result.get("calendar_missing_count", 0),
                                result.get("continuity_violation_count", 0),
                            )

                    except Exception as e:
                        logger.warning(
                            "[BG VALIDATE] %s/%s chunk [%s→%s) failed: %s",
                            sym, tf, chunk_start, chunk_excl_end, e,
                        )

                    # Half-open: previous chunk's exclusive end is this chunk's start.
                    chunk_excl_end = chunk_start
                    # IB pacing: only sleep if this chunk actually hit IB.
                    if _ib_req_count() > 0:
                        await asyncio.sleep(2)

        async def _run_symbol(sym: str, sym_pairs: List[Tuple[str, str]]) -> None:
            logger.info("[BG VALIDATE] >>> symbol %s started (%d tf)",
                        sym, len(sym_pairs))
            t_sym = time.monotonic()
            for s, tf in sym_pairs:
                try:
                    await _run_pair(s, tf)
                except Exception as e:
                    logger.warning("[BG VALIDATE] %s/%s aborted: %s", s, tf, e)
            logger.info("[BG VALIDATE] <<< symbol %s done (%.1fs)",
                        sym, time.monotonic() - t_sym)

        # Run all per-symbol tasks concurrently
        await asyncio.gather(
            *(_run_symbol(sym, sps) for sym, sps in by_symbol.items()),
            return_exceptions=True,
        )

    except Exception as e:
        logger.error("[BG VALIDATE] Background validation error: %s", e)

    elapsed = time.monotonic() - t0
    logger.info("=== [BG VALIDATE] Complete (%.1fs) ===", elapsed)


# ─── Realtime-bar delayed revalidation ────────────────────────────────────────

async def revalidate_realtime_bar(
    fetcher: "IBDataFetcher",
    symbol: str,
    timeframe: str,
    bar_ts: int,
    delay: Optional[int] = None,
) -> None:
    """Wait *delay* seconds then re-validate a single just-completed realtime
    bar against IB historical data; auto-fix any mismatch.

    Realtime bars are written with ``source="realtime_completed"`` and may
    contain an open-price captured mid-window (instead of the true session
    open) — this is invisible to single-bar OHLCV checks but becomes a
    price-continuity violation once the next bar arrives.  This task
    self-heals such bars without waiting for the next full bg_validate pass.

    To force a fresh IB read, the IB-fetch-cache row for *bar_ts* (if any)
    is purged before fix_bars runs.
    """
    if delay is None:
        delay = getattr(config, "REALTIME_REVALIDATE_DELAY", 180)
    try:
        await asyncio.sleep(max(0, int(delay)))
    except asyncio.CancelledError:
        return
    try:
        # Force a fresh IB read for this single timestamp.
        try:
            db.delete_ib_cache_bars(symbol, timeframe, bar_ts, bar_ts)
        except Exception as e:
            logger.debug("revalidate: cache invalidate failed %s/%s ts=%s: %s",
                         symbol, timeframe, bar_ts, e)

        result = await fix_bars(
            symbol, timeframe, bar_ts, bar_ts, fetcher=fetcher,
        )
        if result.get("fixed_count", 0) > 0:
            logger.info(
                "[REVALIDATE] %s/%s ts=%s: corrected %d bar(s) from IB",
                symbol, timeframe, bar_ts, result["fixed_count"],
            )
        else:
            logger.debug(
                "[REVALIDATE] %s/%s ts=%s: clean (db=%d ib=%d)",
                symbol, timeframe, bar_ts,
                result.get("db_count", 0), result.get("ib_count", 0),
            )
    except Exception as e:
        logger.warning(
            "[REVALIDATE] %s/%s ts=%s failed: %s",
            symbol, timeframe, bar_ts, e,
        )


async def recover_realtime_bars(
    fetcher: "IBDataFetcher",
    lookback_seconds: int = 86400,
    stagger: float = 2.0,
) -> int:
    """Scan the DB for recently-written ``source='realtime_completed'`` bars
    and re-validate them against IB.

    Bars whose values match IB are *promoted* to ``ib_validated`` (so a page
    refresh shows only the most recent live bar as realtime, everything else
    as IB-verified).  Bars that don't match are overwritten with IB data.

    Adjacent timestamps for the same (symbol, timeframe, contract_month) are
    coalesced into one IB fetch to avoid pacing penalties.

    Called once at server startup so bars persisted by an earlier run are
    self-healed before the user refreshes the chart.

    *lookback_seconds* — how far back to scan (default 24 h).
    *stagger* — seconds between IB requests (avoids pacing).
    Returns the number of (symbol, timeframe, range) groups scheduled.
    """
    cutoff = int(time.time()) - max(0, int(lookback_seconds))
    rows: List[Tuple[str, str, int, str]] = []
    try:
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT symbol, timeframe, ts, COALESCE(contract_month,'') "
                "FROM bars "
                "WHERE source='realtime_completed' AND ts >= ? "
                "ORDER BY symbol, timeframe, contract_month, ts",
                (cutoff,),
            ).fetchall()
    except Exception as e:
        logger.warning("[RECOVER] Could not query realtime bars: %s", e)
        return 0

    if not rows:
        logger.info("[RECOVER] No realtime_completed bars in last %ds", lookback_seconds)
        return 0

    # ── Coalesce adjacent timestamps into ranges per (sym, tf, cm) ──────────
    # Two timestamps are "adjacent" if the gap between them is <= 1h
    # (covers most session continuity; non-adjacent bars get separate fetches).
    _MERGE_GAP = 3600
    groups: List[Tuple[str, str, str, int, int]] = []  # (sym, tf, cm, from_ts, to_ts)
    cur_sym = cur_tf = cur_cm = None
    cur_from = cur_to = 0
    for sym, tf, ts, cm in rows:
        if (sym, tf, cm) != (cur_sym, cur_tf, cur_cm) or ts - cur_to > _MERGE_GAP:
            if cur_sym is not None:
                groups.append((cur_sym, cur_tf, cur_cm, cur_from, cur_to))
            cur_sym, cur_tf, cur_cm = sym, tf, cm
            cur_from = cur_to = ts
        else:
            cur_to = ts
    if cur_sym is not None:
        groups.append((cur_sym, cur_tf, cur_cm, cur_from, cur_to))

    logger.info(
        "[RECOVER] Re-validating %d realtime bar(s) in %d group(s) "
        "(lookback=%ds, stagger=%.1fs)",
        len(rows), len(groups), lookback_seconds, stagger,
    )

    async def _runner():
        total_promoted = 0
        total_fixed = 0
        for sym, tf, cm, ts_from, ts_to in groups:
            try:
                # Force a fresh IB read for this range — the existing cache
                # rows (if any) may have been written before realtime caught
                # up, so we want the current authoritative IB snapshot.
                try:
                    db.delete_ib_cache_bars(sym, tf, ts_from, ts_to)
                except Exception:
                    pass
                _ib_req_reset()
                result = await fix_bars(
                    sym, tf, ts_from, ts_to,
                    fetcher=fetcher,
                    contract_month=cm or None,
                )
                total_promoted += result.get("promoted_count", 0)
                total_fixed += result.get("fixed_count", 0)
            except Exception as e:
                logger.debug(
                    "[RECOVER] %s/%s cm=%s [%s→%s] failed: %s",
                    sym, tf, cm, ts_from, ts_to, e,
                )
            # IB pacing: only sleep if this group actually hit IB.
            if _ib_req_count() > 0:
                await asyncio.sleep(stagger)
        logger.info(
            "[RECOVER] Sweep complete: %d group(s), promoted=%d fixed=%d",
            len(groups), total_promoted, total_fixed,
        )

    asyncio.create_task(_runner())
    return len(groups)


# ─── Public helper API (single validation entry point) ────────────────────────
#
# These are thin façades over existing logic.  Callers outside this module
# (server, data_manager, realtime_builder, IBfetch) should use ONLY these
# helpers — not ``trading_calendar`` or ``db``'s private gap/OHLCV checks.

def validate_bar(bar: dict, symbol: str = "MES") -> List[str]:
    """Return a list of OHLCV integrity violations for a single bar.

    An empty list means the bar is valid.  Delegates to
    :meth:`trading_calendar.TradingCalendar.validate_bar` which performs
    high/low, non-positive price, and OHLC relational checks.

    Callers (realtime_builder, IBfetch) should reject any bar returning a
    non-empty list instead of persisting it.
    """
    try:
        from trading_calendar import get_calendar
        cal = get_calendar(symbol)
        return list(cal.validate_bar(bar))
    except Exception as e:
        logger.debug("validate_bar(%s) fallback (%s)", symbol, e)
        # Fallback: simplified local check.
        violations: List[str] = []
        try:
            o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
            v = bar["volume"]
            if h < l:
                violations.append(f"high({h}) < low({l})")
            if any(p <= 0 for p in (o, h, l, c)):
                violations.append(f"non-positive price O={o} H={h} L={l} C={c}")
            if v < 0:
                violations.append(f"negative volume {v}")
        except Exception as e2:
            violations.append(f"malformed bar: {e2}")
        return violations


def classify_gaps(
    bars: List[dict],
    symbol: str,
    interval: int,
) -> List[dict]:
    """Classify gaps between consecutive bars using the trading calendar.

    Returns records with keys::

        {"gap_start", "gap_end", "gap_seconds",
         "gap_type": "weekend"|"holiday"|"maintenance"|"data_gap"|"normal"}

    This is the single place gap-type logic lives; data_manager and
    server both call it.  Falls back to a simple interval-multiple
    heuristic only if the calendar is unavailable.
    """
    if not bars or len(bars) < 2:
        return []
    try:
        from trading_calendar import get_calendar
        cal = get_calendar(symbol)
        return list(cal.find_gaps(bars, interval))
    except Exception as e:
        logger.debug("classify_gaps(%s) fallback (%s)", symbol, e)
        out: List[dict] = []
        for i in range(1, len(bars)):
            gap = bars[i]["time"] - bars[i - 1]["time"]
            if gap > interval * 2:
                out.append({
                    "gap_start":   bars[i - 1]["time"],
                    "gap_end":     bars[i]["time"],
                    "gap_seconds": gap,
                    "gap_type":    "data_gap",
                })
        return out


def data_gaps_only(
    bars: List[dict],
    symbol: str,
    interval: int,
) -> List[Tuple[int, int, int]]:
    """Convenience wrapper: return only ``data_gap`` gaps as
    ``(from_ts, to_ts, gap_seconds)`` tuples suitable for IB refill.
    """
    return [
        (g["gap_start"], g["gap_end"], g["gap_seconds"])
        for g in classify_gaps(bars, symbol, interval)
        if g.get("gap_type") == "data_gap"
    ]


