import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.routers.datavalid import _safe_data_path
from storage import db
from trading.trade_log_parser import (
    load_all_trades,
    parse_csv_content,
    set_match_strategy,
)
from trading.trade_stats import compute_tradelog_stats

logger = logging.getLogger(__name__)

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parents[2]


class TradeLogPatch(BaseModel):
    trade_type: Optional[str] = None
    entry_reason: Optional[str] = None
    market_cycle: Optional[str] = None
    sup_res: Optional[str] = None
    notes: Optional[str] = None


@router.get("/api/trades")
async def get_trades():
    """Return parsed historical trades from log files in data/."""
    try:
        return load_all_trades()
    except Exception as e:
        logger.error("Trade log load error: %s", e)
        return []


@router.get("/api/trades/files")
async def list_trade_files():
    """List trade CSV files in data/ directory."""
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    files = []
    patterns = ["trade_log_topstep*", "trade_log_IB*", "trade_log_lucid*"]
    seen = set()
    for pat in patterns:
        for fp in sorted(data_dir.glob(pat)):
            if fp.name not in seen and fp.is_file():
                seen.add(fp.name)
                files.append({"name": fp.name, "size": fp.stat().st_size})
    return files


@router.get("/api/trades/file/{filename}")
async def get_trades_from_file(filename: str):
    """Parse and return trades from a specific CSV file."""
    data_dir = BASE_DIR / "data"
    filepath = _safe_data_path(data_dir, filename)
    if filepath is None:
        return JSONResponse(status_code=400, content={"error": "Invalid filename"})
    if not filepath.exists():
        return JSONResponse(status_code=404, content={"error": "File not found"})
    try:
        text = filepath.read_text(encoding="utf-8-sig", errors="replace")
        trades = parse_csv_content(text, source_file=filepath.name)
        return trades
    except Exception as e:
        logger.error("Trade file parse error: %s", e)
        return []


@router.post("/api/trades/upload")
async def upload_trades(file: UploadFile):
    """Save uploaded CSV to data/ folder and return parsed trades."""
    try:
        save_name = os.path.basename(file.filename or "trade_log_upload.csv")
        if not save_name.lower().endswith(".csv"):
            return JSONResponse(status_code=400,
                                content={"error": "Only .csv files are accepted"})
        content = await file.read()
        _MAX_UPLOAD_BYTES = 10 * 1024 * 1024
        if len(content) > _MAX_UPLOAD_BYTES:
            return JSONResponse(status_code=413,
                                content={"error": "File too large (max 10 MB)"})
        text = content.decode("utf-8-sig", errors="replace")
        trades = parse_csv_content(text, source_file=save_name)
        if not trades:
            return {"filename": None, "trades": []}
        data_dir = BASE_DIR / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        save_path = _safe_data_path(data_dir, save_name)
        if save_path is None:
            return JSONResponse(status_code=400, content={"error": "Invalid filename"})
        save_path.write_bytes(content)
        try:
            db.upsert_trade_logs(trades)
        except Exception as exc:
            logger.warning("upsert_trade_logs after upload failed: %s", exc)
        logger.info("Saved trade CSV to %s (%d trades)", save_name, len(trades))
        return {"filename": save_name, "trades": trades}
    except Exception as e:
        logger.error("Trade upload parse error: %s", e)
        return {"filename": None, "trades": []}


@router.delete("/api/trades/file/{filename}")
async def delete_trade_file(filename: str):
    """Delete a trade CSV file from data/ directory and its DB rows.

    The trade panel is now DB-backed, so rows may exist for a source_file
    whose CSV was already removed from disk.  We therefore delete the DB rows
    regardless of whether the file is still present, and unlink the file only
    if it exists.
    """
    data_dir = BASE_DIR / "data"
    filepath = _safe_data_path(data_dir, filename)
    if filepath is None:
        return JSONResponse(status_code=400, content={"error": "Invalid filename"})
    file_existed = filepath.exists()
    if file_existed:
        try:
            filepath.unlink()
        except Exception as exc:
            logger.warning("Failed to unlink %s: %s", filepath.name, exc)
    removed = 0
    try:
        removed = db.delete_trade_logs_by_source(filepath.name)
    except Exception as exc:
        logger.warning("delete_trade_logs_by_source failed: %s", exc)
    if not file_existed and removed == 0:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    logger.info("Deleted trade source: %s (file=%s, %d DB rows)",
                filepath.name, file_existed, removed)
    return {"success": True, "deleted_rows": removed}


@router.get("/api/tradelogs")
async def list_tradelogs(
    broker: Optional[str] = None,
    symbol: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    trade_type: Optional[str] = None,
    entry_reason: Optional[str] = None,
    market_cycle: Optional[str] = None,
    sup_res: Optional[str] = None,
    source_file: Optional[str] = None,
    limit: Optional[int] = None,
):
    """List trade_logs filtered by any combination of fields."""
    try:
        rows = db.list_trade_logs(
            broker=broker, symbol=symbol,
            date_from=date_from, date_to=date_to,
            trade_type=trade_type, entry_reason=entry_reason,
            market_cycle=market_cycle, sup_res=sup_res,
            source_file=source_file, limit=limit,
        )
        return rows
    except Exception as e:
        logger.error("list_tradelogs error: %s", e)
        return []


@router.patch("/api/tradelogs/{trade_id}")
async def patch_tradelog(trade_id: int, patch: TradeLogPatch):
    """Update user-input fields on a trade log row."""
    fields = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not fields:
        return JSONResponse(status_code=400, content={"error": "No fields"})
    ok = db.update_trade_log(trade_id, fields)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return {"success": True}


@router.delete("/api/tradelogs/{trade_id}")
async def delete_tradelog(trade_id: int):
    ok = db.delete_trade_log(trade_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return {"success": True}


@router.get("/api/tradelogs/distinct/{field}")
async def tradelog_distinct(field: str):
    """Return distinct values for a filterable column (build dropdowns)."""
    return db.trade_log_distinct(field)


@router.get("/api/tradelogs/stats")
async def tradelog_stats(
    broker: Optional[str] = None,
    symbol: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    trade_type: Optional[str] = None,
    entry_reason: Optional[str] = None,
    market_cycle: Optional[str] = None,
    sup_res: Optional[str] = None,
):
    """Return aggregate statistics across the filtered trade set.

    Includes win-rate, profit factor, R:R, P&L curve, daily P&L, trade
    duration & win-rate buckets — matching the analytics dashboard layout.
    """
    rows = db.list_trade_logs(
        broker=broker, symbol=symbol,
        date_from=date_from, date_to=date_to,
        trade_type=trade_type, entry_reason=entry_reason,
        market_cycle=market_cycle, sup_res=sup_res,
    )
    return compute_tradelog_stats(rows)


@router.post("/api/tradelogs/match_strategy")
async def set_tradelog_match_strategy(strategy: str):
    """Switch matching strategy at runtime (FILO/FIFO) and re-ingest."""
    s = (strategy or "").upper()
    if s not in ("FILO", "FIFO"):
        return JSONResponse(status_code=400, content={"error": "Invalid strategy"})
    set_match_strategy(s)
    trades = load_all_trades()
    db.upsert_trade_logs(trades)
    return {"success": True, "strategy": s, "count": len(trades)}


@router.get("/api/tradelogs/match_strategy")
async def get_tradelog_match_strategy():
    from trading.trade_log_parser import MATCH_STRATEGY as _ms
    return {"strategy": _ms}
