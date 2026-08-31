import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from app.dependencies import get_app_state
from pydantic import BaseModel

from app.websocket import broadcast
from core import config
from storage import db

logger = logging.getLogger(__name__)

router = APIRouter()




class AnalysisAnnotation(BaseModel):
    label: str
    type: str
    start_time: int
    end_time: Optional[int] = None
    price: Optional[float] = None
    price_high: Optional[float] = None
    price_low: Optional[float] = None
    price_start: Optional[float] = None
    price_end: Optional[float] = None
    color: Optional[str] = None
    style: Optional[str] = None
    linewidth: Optional[int] = None


class AnalysisPayload(BaseModel):
    symbol: str = "MES"
    timeframe: str = "5"
    session: str = "RTH"
    bar_from: int
    bar_to: int
    summary: str
    annotations: List[AnalysisAnnotation]


@router.get("/api/skill/bars")
async def skill_get_bars(
    symbol: str = Query("MES"),
    resolution: str = Query("5"),
    session: str = Query("RTH"),
    from_ts: int = Query(None, alias="from"),
    to_ts: int = Query(None, alias="to"),
    from_dt: str = Query(None),
    to_dt: str = Query(None),
):
    """
    Skill-facing K-line data endpoint.

    Returns OHLCV bars as a JSON array (easier for LLM consumption than
    the TradingView UDF arrays-of-columns format).
    Supports session filter: RTH drops bars outside 09:30-16:00 ET.

    Time parameters (priority: datetime strings > unix timestamps):
      - from_dt/to_dt: Human-readable datetime strings "YYYY-MM-DD HH:MM" or "YYYY-MM-DD"
      - from/to: Unix timestamps (legacy, for backward compatibility)
    """
    from marketdata.ib_fetcher import resolution_to_key
    from datetime import datetime as _dt
    import zoneinfo

    key = resolution_to_key(resolution)
    sym = symbol.upper()

    inst = config.INSTRUMENTS.get(sym)
    sym_tz = zoneinfo.ZoneInfo(inst["timezone"]) if inst else zoneinfo.ZoneInfo("America/New_York")

    if from_dt:
        try:
            if len(from_dt) == 10:
                dt_obj = _dt.strptime(from_dt, "%Y-%m-%d")
            else:
                dt_obj = _dt.strptime(from_dt, "%Y-%m-%d %H:%M")
            dt_obj_local = _dt(dt_obj.year, dt_obj.month, dt_obj.day,
                               dt_obj.hour, dt_obj.minute, dt_obj.second, tzinfo=sym_tz)
            from_ts = int(dt_obj_local.timestamp())
        except ValueError:
            return JSONResponse(
                {"error": "Invalid from_dt format. Use 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'"},
                status_code=400
            )
    elif from_ts is None:
        from_ts = 0

    if to_dt:
        try:
            if len(to_dt) == 10:
                dt_obj = _dt.strptime(to_dt, "%Y-%m-%d")
                dt_obj_local = _dt(dt_obj.year, dt_obj.month, dt_obj.day,
                                   23, 59, 59, tzinfo=sym_tz)
            else:
                dt_obj = _dt.strptime(to_dt, "%Y-%m-%d %H:%M")
                dt_obj_local = _dt(dt_obj.year, dt_obj.month, dt_obj.day,
                                   dt_obj.hour, dt_obj.minute, dt_obj.second, tzinfo=sym_tz)
            to_ts = int(dt_obj_local.timestamp())
        except ValueError:
            return JSONResponse(
                {"error": "Invalid to_dt format. Use 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'"},
                status_code=400
            )
    elif to_ts is None:
        to_ts = db.MAX_TIMESTAMP

    logger.info("skill_get_bars: sym=%s, key=%s, from_ts=%s, to_ts=%s", sym, key, from_ts, to_ts)
    logger.info("  from_dt=%s, to_dt=%s", from_dt, to_dt)

    bars = db.get_bars(sym, key, from_ts=from_ts, to_ts=to_ts)
    logger.info("  Retrieved %d bars from database", len(bars))

    if session.upper() == "RTH" and key in ("5min", "1min", "3min", "15min", "30min", "60min"):
        from datetime import datetime as _dt, timezone as _tz
        import zoneinfo

        inst = config.INSTRUMENTS.get(sym)
        if inst:
            tz = zoneinfo.ZoneInfo(inst["timezone"])
            rth_start = inst["rth_start"]
            rth_end = inst["rth_end"]
        else:
            tz = zoneinfo.ZoneInfo("America/New_York")
            rth_start = (9, 30)
            rth_end = (16, 0)

        start_min = rth_start[0] * 60 + rth_start[1]
        end_min = rth_end[0] * 60 + rth_end[1]

        filtered = []
        for b in bars:
            dt = _dt.fromtimestamp(b["time"], tz=_tz.utc).astimezone(tz)
            t = dt.hour * 60 + dt.minute
            if start_min <= t < end_min:
                filtered.append(b)
        bars = filtered

    if key == "1D":
        from datetime import datetime as _dt2, timezone as _tz2
        for b in bars:
            b["trade_date"] = _dt2.fromtimestamp(
                b["time"], tz=_tz2.utc
            ).strftime("%Y-%m-%d")

    return {
        "symbol": sym,
        "resolution": resolution,
        "session": session,
        "count": len(bars),
        "bars": bars,
    }


@router.post("/api/skill/analysis")
async def skill_save_analysis(request: Request, payload: AnalysisPayload):
    """
    Writeback: save LLM market cycle analysis results to DB.

    The annotations include typed shapes (ranges, hlines, labels) that the
    frontend will render on the chart.
    """
    from datetime import datetime as _dt, timezone as _tz

    state = get_app_state(request)
    created_at = _dt.now(_tz.utc).isoformat()
    annotations_json = json.dumps([a.dict() for a in payload.annotations])
    row_id = db.save_analysis(
        symbol=payload.symbol.upper(),
        timeframe=payload.timeframe,
        session=payload.session,
        created_at=created_at,
        bar_from=payload.bar_from,
        bar_to=payload.bar_to,
        summary=payload.summary,
        annotations=annotations_json,
    )
    logger.info("Saved market cycle analysis #%d for %s/%s",
                row_id, payload.symbol, payload.timeframe)

    msg = {
        "type": "cycle_analysis",
        "analysis": {
            "id": row_id,
            "symbol": payload.symbol.upper(),
            "timeframe": payload.timeframe,
            "session": payload.session,
            "created_at": created_at,
            "bar_from": payload.bar_from,
            "bar_to": payload.bar_to,
            "summary": payload.summary,
            "annotations": [a.dict() for a in payload.annotations],
            "active": 1,
        },
    }
    await broadcast(state, msg)

    return {"success": True, "id": row_id}


@router.get("/api/skill/analyses")
async def skill_list_analyses(
    symbol: str = Query(None),
    timeframe: str = Query(None),
    active_only: bool = Query(False),
):
    """List all saved market cycle analyses, optionally filtered."""
    rows = db.get_analyses(symbol=symbol, timeframe=timeframe, active_only=active_only)
    for r in rows:
        try:
            r["annotations"] = json.loads(r.get("annotations", "[]"))
        except (json.JSONDecodeError, TypeError):
            r["annotations"] = []
    return rows


@router.put("/api/skill/analyses/{analysis_id}/active")
async def skill_toggle_analysis(request: Request, analysis_id: int, active: bool = Query(...)):
    """Toggle an analysis active/inactive (shows/hides on chart)."""
    state = get_app_state(request)
    ok = db.update_analysis_active(analysis_id, active)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "not found"})

    msg = {"type": "cycle_analysis_toggle", "id": analysis_id, "active": active}
    await broadcast(state, msg)

    return {"success": True, "id": analysis_id, "active": active}


@router.delete("/api/skill/analyses/{analysis_id}")
async def skill_delete_analysis(request: Request, analysis_id: int):
    """Permanently delete an analysis record."""
    state = get_app_state(request)
    ok = db.delete_analysis(analysis_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "not found"})

    msg = {"type": "cycle_analysis_delete", "id": analysis_id}
    await broadcast(state, msg)

    return {"success": True}
