import json
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from storage import db
from strategy import backtest as strategy_backtest

logger = logging.getLogger(__name__)

router = APIRouter()


class BacktestRequest(BaseModel):
    symbol: str = "MES"
    timeframe: str = "5min"
    from_ts: int = 0
    to_ts: int = db.MAX_TIMESTAMP
    ibs_threshold: float = 0.70
    rr_ratio: float = 1.0
    use_context_filter: bool = True
    max_stop_loss: float = 200.0
    session: str = "all"
    time_filter: str = ""
    include_filtered: bool = True


@router.post("/api/strategy/backtest")
async def run_strategy_backtest(req: BacktestRequest):
    try:
        result = strategy_backtest.run_backtest(
            symbol=req.symbol,
            timeframe=req.timeframe,
            from_ts=req.from_ts,
            to_ts=req.to_ts,
            ibs_threshold=req.ibs_threshold,
            rr_ratio=req.rr_ratio,
            use_context_filter=req.use_context_filter,
            max_stop_loss=req.max_stop_loss,
            session=req.session,
            time_filter=req.time_filter,
            include_filtered=req.include_filtered,
        )
        return result
    except Exception as e:
        logger.error("Backtest error: %s", e, exc_info=True)
        return JSONResponse({"error": "Backtest failed. Check server logs for details."}, status_code=500)


@router.get("/api/strategy/backtests")
async def list_backtests():
    rows = db.get_all_backtests()
    for r in rows:
        r["params"] = json.loads(r.pop("params_json", "{}"))
        r["summary"] = json.loads(r.pop("summary_json", "{}"))
    return rows


@router.get("/api/strategy/backtests/{backtest_id}/trades")
async def get_backtest_trades(backtest_id: str):
    row = db.get_backtest_by_id(backtest_id)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    trades = db.get_trades_for_backtest(backtest_id)
    return {"backtest_id": backtest_id, "trades": trades}


@router.delete("/api/strategy/backtests/{backtest_id}")
async def delete_backtest(backtest_id: str):
    ok = db.delete_backtest(backtest_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}
