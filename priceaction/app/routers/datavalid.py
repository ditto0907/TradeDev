import asyncio
import logging
import os
import time
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from app.dependencies import get_app_state
from pydantic import BaseModel

from marketdata import data_validator
from storage import db

logger = logging.getLogger(__name__)

router = APIRouter()




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


def _safe_data_path(data_dir: Path, filename: str) -> Optional[Path]:
    """Resolve *filename* within *data_dir*, rejecting path traversal.

    Returns the resolved Path if it is strictly inside ``data_dir``, else None.
    ``os.path.basename`` strips any directory components first so that inputs
    like ``../../etc/passwd`` cannot escape the data folder.
    """
    base = os.path.basename(filename or "")
    if not base or base in (".", ".."):
        return None
    resolved = (data_dir / base).resolve()
    try:
        resolved.relative_to(data_dir.resolve())
    except ValueError:
        return None
    return resolved


class FixBarsRequest(BaseModel):
    symbol: str = "MES"
    timeframe: str = "5min"
    from_ts: Optional[int] = None
    to_ts: Optional[int] = None
    from_dt: Optional[str] = None
    to_dt: Optional[str] = None
    timestamps: Optional[List[int]] = None
    contract_month: Optional[str] = None


class DeleteBarsRangeRequest(BaseModel):
    symbol: str = "MES"
    timeframe: str = "5min"
    from_ts: int
    to_ts: int


class DeleteBarsByTimestampsRequest(BaseModel):
    symbol: str = "MES"
    timeframe: str = "5min"
    timestamps: List[int]


@router.get("/api/data/validate")
async def api_validate_bars(
    request: Request,
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
    if from_dt and not from_ts:
        from_ts = _parse_dt_eastern(from_dt)
    if to_dt and not to_ts:
        to_ts = _parse_dt_eastern(to_dt)

    if from_ts is None:
        from_ts = int(time.time()) - 86400
    if to_ts is None:
        to_ts = int(time.time())

    state = get_app_state(request)
    f = state.fetcher if (state.fetcher._ib_ready and state.fetcher.ib and state.fetcher.ib.isConnected()) else None
    result = await data_validator.validate_bars(
        symbol, timeframe, from_ts, to_ts,
        fetcher=f, contract_month=contract_month,
        skip_validated=skip_validated,
    )
    return result


@router.post("/api/data/fix")
async def api_fix_bars(request: Request, req: FixBarsRequest):
    """Fix DB bars using IB data (from local ib_fetch_cache when available).

    *timestamps*: optional list of Unix timestamps to restrict fixing to only
    those specific bars (selected rows from the UI).  When omitted every
    mismatch/missing bar in the range is fixed.
    *contract_month*: optional contract month (e.g. '202503') to restrict fix
    to bars belonging to that specific futures contract only.
    """
    from_ts = req.from_ts
    to_ts = req.to_ts

    if req.from_dt and not from_ts:
        from_ts = _parse_dt_eastern(req.from_dt)
    if req.to_dt and not to_ts:
        to_ts = _parse_dt_eastern(req.to_dt)

    if from_ts is None:
        from_ts = int(time.time()) - 86400
    if to_ts is None:
        to_ts = int(time.time())

    state = get_app_state(request)
    f = state.fetcher if (state.fetcher._ib_ready and state.fetcher.ib and state.fetcher.ib.isConnected()) else None
    result = await data_validator.fix_bars(
        req.symbol, req.timeframe, from_ts, to_ts,
        fetcher=f,
        timestamps=req.timestamps,
        contract_month=req.contract_month,
    )
    return result


@router.post("/api/data/validate_all")
async def api_validate_all(request: Request, fix: bool = False):
    """Scan all symbol/timeframe pairs in DB, validate against IB.
    Set fix=true to auto-correct mismatches. This is a long-running operation."""
    state = get_app_state(request)
    f = state.fetcher if (state.fetcher._ib_ready and state.fetcher.ib and state.fetcher.ib.isConnected()) else None
    results = await data_validator.validate_all(fix=fix, fetcher=f)
    total_mismatches = sum(r["total_mismatches"] for r in results)
    total_fixed = sum(r.get("total_fixed", 0) for r in results)
    return {
        "pairs_checked": len(results),
        "total_mismatches": total_mismatches,
        "total_fixed": total_fixed,
        "details": results,
    }


@router.get("/api/data/validated_ranges")
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


@router.post("/api/data/bg_validate")
async def api_trigger_bg_validate(
    request: Request,
    fix: bool = False,
    symbols: Optional[str] = None,
    timeframes: Optional[str] = None,
):
    """Manually trigger the background validation task.

    Set ``fix=true`` to also auto-correct mismatches/missing bars from IB.
    Optional comma-separated ``symbols`` / ``timeframes`` narrow the scan
    (e.g. ``symbols=MGC,NK225MC&timeframes=5min``) so users can target a
    specific instrument without waiting for the MES backlog to drain.
    """
    state = get_app_state(request)
    f = state.fetcher if (state.fetcher._ib_ready and state.fetcher.ib and state.fetcher.ib.isConnected()) else None
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    tf_list = [t.strip() for t in timeframes.split(",") if t.strip()] if timeframes else None
    asyncio.create_task(
        data_validator.background_validate(
            fetcher=f, fix=fix, symbols=sym_list, timeframes=tf_list,
        )
    )
    return {
        "success": True,
        "message": "Background validation started",
        "fix": fix,
        "symbols": sym_list,
        "timeframes": tf_list,
    }


@router.get("/api/data/issues")
async def api_data_issues(
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
):
    """Return validated ranges that still have outstanding issues
    (``mismatches > 0``).  Useful for a topbar warning indicator."""
    ranges = db.get_validated_ranges(symbol=symbol, timeframe=timeframe)
    dirty = [r for r in ranges if r.get("mismatches", 0) > 0]
    return {
        "count": len(dirty),
        "total_issues": sum(r["mismatches"] for r in dirty),
        "ranges": dirty,
    }


@router.get("/api/data/gaps")
async def api_data_gaps(
    symbol: str = "MES",
    timeframe: str = "5min",
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
):
    """Detect K-line continuity gaps for a symbol/timeframe."""
    from marketdata.ib_fetcher import _key_to_ib
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


@router.get("/api/data/bars_by_source")
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


@router.post("/api/data/delete_by_source")
async def api_delete_bars_by_source(source: str = "realtime"):
    """Delete all bars with a given source from the database."""
    deleted = db.delete_bars_by_source(source)
    logger.info("Deleted %d bars with source=%s", deleted, source)
    return {"deleted": deleted, "source": source}


@router.get("/api/data/query")
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
    allowed_tables = {"bars", "ib_fetch_cache"}
    if db_table not in allowed_tables:
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid db_table. Must be one of: {', '.join(sorted(allowed_tables))}"},
        )

    page_size = min(max(1, page_size), 500)

    safe_table = "ib_fetch_cache" if db_table == "ib_fetch_cache" else "bars"
    is_cache = safe_table == "ib_fetch_cache"

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
        count_row = conn.execute(
            f"SELECT COUNT(*) FROM {safe_table}{where_sql}", params
        ).fetchone()
        total = count_row[0]

        offset = (max(1, page) - 1) * page_size
        data_sql = (
            f"SELECT {select_cols} "
            f"FROM {safe_table}{where_sql} ORDER BY ts ASC LIMIT ? OFFSET ?"
        )
        rows = conn.execute(data_sql, params + [page_size, offset]).fetchall()

        symbols = [r[0] for r in conn.execute(
            f"SELECT DISTINCT symbol FROM {safe_table} ORDER BY symbol"
        ).fetchall()]
        timeframes = [r[0] for r in conn.execute(
            f"SELECT DISTINCT timeframe FROM {safe_table} ORDER BY timeframe"
        ).fetchall()]
        if is_cache:
            sources = []
        else:
            sources = [r[0] for r in conn.execute(
                f"SELECT DISTINCT source FROM {safe_table} ORDER BY source"
            ).fetchall()]
        contract_months = [r[0] for r in conn.execute(
            f"SELECT DISTINCT contract_month FROM {safe_table} WHERE contract_month != '' ORDER BY contract_month"
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


@router.get("/api/data/integrity")
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


@router.get("/api/data/coverage")
async def api_data_coverage():
    """Return coverage summary for all symbol/timeframe pairs in the DB."""
    return db.get_coverage()


@router.get("/api/data/bar")
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


@router.post("/api/data/delete_range")
async def api_delete_bars_range(req: DeleteBarsRangeRequest):
    """Delete bars in a specific time range."""
    deleted = db.delete_bars_range(req.symbol, req.timeframe, req.from_ts, req.to_ts)
    logger.info("Deleted %d bars for %s/%s in [%d→%d]",
                deleted, req.symbol, req.timeframe, req.from_ts, req.to_ts)
    return {"deleted": deleted, "symbol": req.symbol, "timeframe": req.timeframe}


@router.post("/api/data/delete_bars")
async def api_delete_bars_by_timestamps(req: DeleteBarsByTimestampsRequest):
    """Delete specific bars by their exact timestamps."""
    deleted = db.delete_bars_by_timestamps(req.symbol, req.timeframe, req.timestamps)
    logger.info("Deleted %d bars for %s/%s by timestamps",
                deleted, req.symbol, req.timeframe)
    return {"deleted": deleted, "symbol": req.symbol, "timeframe": req.timeframe}


@router.post("/api/data/fix_ohlcv")
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


@router.get("/api/data/calendar_gaps")
async def api_calendar_gaps(
    symbol: str = "MES",
    timeframe: str = "5min",
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
):
    """Detect gaps using the instrument's trading session calendar.
    Returns only genuine data gaps (not weekends/holidays/maintenance)."""
    from marketdata.trading_calendar import get_calendar
    from marketdata.ib_fetcher import _key_to_ib
    try:
        _, interval = _key_to_ib(timeframe)
    except Exception:
        interval = 300

    try:
        cal = get_calendar(symbol)
    except ValueError:
        return JSONResponse({"error": "Unsupported symbol"}, status_code=400)

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


@router.get("/api/data/coverage_calendar")
async def api_coverage_calendar(
    symbol: str = "MES",
    timeframe: str = "5min",
    year: int = Query(default=None),
):
    """Per-day coverage data for a given symbol/timeframe/year.

    Used by the DataStatus calendar view to colour each calendar cell
    according to whether the day has bars, has been validated, or has
    known data gaps.

    Returns::

        {
          "symbol": "MES",
          "timeframe": "5min",
          "year": 2024,
          "days": {
            "2024-03-04": {
              "count": 72,
              "validated": true,
              "has_data_gap": false
            },
            ...
          }
        }
    """
    from datetime import date, datetime, timezone

    if year is None:
        year = date.today().year

    year_start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    year_end = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())

    with db._conn() as conn:
        day_rows = conn.execute(
            "SELECT DATE(ts, 'unixepoch') AS day, COUNT(*) AS cnt "
            "FROM bars "
            "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<? "
            "GROUP BY day ORDER BY day",
            (symbol, timeframe, year_start, year_end),
        ).fetchall()

    day_counts: dict = {r[0]: r[1] for r in day_rows}

    merged = db.get_merged_validated_ranges(symbol, timeframe)
    validated_days: set = set()
    for rng in merged:
        r_from = rng["from_ts"]
        r_to = rng["to_ts"]
        if r_to < year_start or r_from >= year_end:
            continue
        cur = max(r_from, year_start)
        while cur < min(r_to, year_end):
            day_str = datetime.fromtimestamp(cur, tz=timezone.utc).strftime("%Y-%m-%d")
            validated_days.add(day_str)
            cur += 86400

    gap_days: set = set()
    from marketdata.trading_calendar import get_calendar
    from marketdata.ib_fetcher import _key_to_ib
    try:
        _, interval = _key_to_ib(timeframe)
    except Exception:
        interval = 300
    try:
        cal = get_calendar(symbol)
        bars = db.get_bars(symbol, timeframe, from_ts=year_start, to_ts=year_end)
        if len(bars) >= 2:
            for g in cal.find_gaps(bars, interval):
                if g["gap_type"] == "data_gap":
                    gs = datetime.fromtimestamp(g["gap_start"], tz=timezone.utc).strftime("%Y-%m-%d")
                    gap_days.add(gs)
    except Exception:
        pass

    days_out: dict = {}
    for day_str, cnt in day_counts.items():
        days_out[day_str] = {
            "count": cnt,
            "validated": day_str in validated_days,
            "has_data_gap": day_str in gap_days,
        }

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "year": year,
        "days": days_out,
    }
