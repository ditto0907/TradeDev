import logging
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from app.dependencies import get_app_state
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()




class OrderRequest(BaseModel):
    action: str
    quantity: int
    order_type: str
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    tif: str = "DAY"


class BracketOrderRequest(BaseModel):
    action: str
    quantity: int
    order_type: str
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    tif: str = "DAY"


class ModifyOrderRequest(BaseModel):
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None


@router.post("/api/order")
async def place_order(request: Request, req: OrderRequest):
    state = get_app_state(request)
    if state.order_mgr is None:
        return JSONResponse({"success": False, "error": "IB not connected"}, status_code=503)
    try:
        result = state.order_mgr.place_order(
            action=req.action, quantity=req.quantity,
            order_type=req.order_type,
            limit_price=req.limit_price, stop_price=req.stop_price,
            tif=req.tif,
        )
        return {"success": True, **result}
    except ValueError as e:
        logger.error("place_order validation error: %s", e)
        return JSONResponse({"success": False, "error": "Invalid order request"}, status_code=400)
    except Exception as e:
        logger.error("place_order error: %s", e)
        return JSONResponse({"success": False, "error": "Order submission failed"}, status_code=400)


@router.post("/api/order/bracket")
async def place_bracket_order(request: Request, req: BracketOrderRequest):
    state = get_app_state(request)
    if state.order_mgr is None:
        return JSONResponse({"success": False, "error": "IB not connected"}, status_code=503)
    try:
        results = state.order_mgr.place_bracket_order(
            action=req.action, quantity=req.quantity,
            order_type=req.order_type,
            limit_price=req.limit_price, stop_price=req.stop_price,
            tp_price=req.tp_price, sl_price=req.sl_price,
            tif=req.tif,
        )
        return {"success": True, "orders": results}
    except ValueError as e:
        logger.error("place_bracket_order validation error: %s", e)
        return JSONResponse({"success": False, "error": "Invalid bracket order request"}, status_code=400)
    except Exception as e:
        logger.error("place_bracket_order error: %s", e)
        return JSONResponse({"success": False, "error": "Order submission failed"}, status_code=400)


@router.get("/api/orders")
async def get_orders(request: Request, all: bool = Query(False)):
    state = get_app_state(request)
    if state.order_mgr is None:
        return []
    return state.order_mgr.get_all_orders() if all else state.order_mgr.get_open_orders()


@router.delete("/api/order/{order_id}")
async def cancel_order(request: Request, order_id: int):
    state = get_app_state(request)
    if state.order_mgr is None:
        return JSONResponse({"success": False, "error": "IB not connected"}, status_code=503)
    ok = state.order_mgr.cancel_order(order_id)
    return {"success": ok}


@router.put("/api/order/{order_id}")
async def modify_order(request: Request, order_id: int, req: ModifyOrderRequest):
    state = get_app_state(request)
    if state.order_mgr is None:
        return JSONResponse({"success": False, "error": "IB not connected"}, status_code=503)
    try:
        result = state.order_mgr.modify_order(
            order_id,
            limit_price=req.limit_price,
            stop_price=req.stop_price,
        )
        return {"success": True, **result}
    except ValueError as e:
        logger.error("modify_order validation error: %s", e)
        return JSONResponse({"success": False, "error": "Invalid order modification request"}, status_code=400)
    except Exception as e:
        logger.error("modify_order error: %s", e)
        return JSONResponse({"success": False, "error": "Order modification failed"}, status_code=400)


@router.delete("/api/orders")
async def cancel_all_orders(request: Request):
    state = get_app_state(request)
    if state.order_mgr is None:
        return JSONResponse({"success": False, "error": "IB not connected"}, status_code=503)
    count = state.order_mgr.cancel_all_orders()
    return {"success": True, "cancelled": count}


@router.post("/api/flatten")
async def flatten_position(request: Request):
    state = get_app_state(request)
    if state.order_mgr is None:
        return JSONResponse({"success": False, "error": "IB not connected"}, status_code=503)
    try:
        result = state.order_mgr.flatten_position()
        if result is None:
            return {"success": True, "message": "No open position"}
        return {"success": True, **result}
    except ValueError as e:
        logger.error("flatten validation error: %s", e)
        return JSONResponse({"success": False, "error": "Invalid flatten request"}, status_code=400)
    except Exception as e:
        logger.error("flatten error: %s", e)
        return JSONResponse({"success": False, "error": "Flatten failed"}, status_code=400)


@router.get("/api/position")
async def get_position(request: Request):
    state = get_app_state(request)
    if state.order_mgr is None:
        return {"symbol": "MES", "position": 0, "avg_cost": 0.0, "side": "FLAT"}
    return state.order_mgr.get_position()
