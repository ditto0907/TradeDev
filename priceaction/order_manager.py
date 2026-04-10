"""
IB Order Manager — submit, cancel, and track orders via ib_insync.

Usage:
    mgr = IBOrderManager(ib_instance, qualified_contract)
    result = mgr.place_order("BUY", 1, "limit", limit_price=6800.0)
    mgr.cancel_order(result["order_id"])
    open_orders = mgr.get_open_orders()
"""
import logging
from typing import Optional
from ib_insync import IB, Contract, MarketOrder, LimitOrder, StopOrder, Order

logger = logging.getLogger(__name__)

# IB order statuses that mean the order is no longer active
_TERMINAL_STATUSES = {"Filled", "Cancelled", "Inactive", "ApiCancelled"}


class IBOrderManager:
    def __init__(self, ib: IB, contract: Contract):
        self._ib       = ib
        self._contract = contract
        self._trades   = {}   # orderId (int) → Trade

    # ─── Place ───────────────────────────────────────────────────────────────

    def place_order(
        self,
        action:      str,                    # "BUY" | "SELL"
        quantity:    int,
        order_type:  str,                    # "market"|"limit"|"stop"|"stop_limit"
        limit_price: Optional[float] = None,
        stop_price:  Optional[float] = None,
        tif:         str = "DAY",
    ) -> dict:
        action     = action.upper()
        order_type = order_type.lower()
        tif        = tif.upper()

        if order_type == "market":
            order = MarketOrder(action, quantity, tif=tif)
        elif order_type == "limit":
            if limit_price is None:
                raise ValueError("limit_price required for LIMIT order")
            order = LimitOrder(action, quantity, limit_price, tif=tif)
        elif order_type == "stop":
            if stop_price is None:
                raise ValueError("stop_price required for STOP order")
            order = StopOrder(action, quantity, stop_price, tif=tif)
        elif order_type == "stop_limit":
            if limit_price is None or stop_price is None:
                raise ValueError("limit_price and stop_price required for STOP LIMIT")
            order = Order(
                orderType="STP LMT",
                action=action,
                totalQuantity=quantity,
                lmtPrice=limit_price,
                auxPrice=stop_price,
                tif=tif,
            )
        else:
            raise ValueError(f"Unknown order type: {order_type!r}")

        trade = self._ib.placeOrder(self._contract, order)
        oid   = trade.order.orderId
        self._trades[oid] = trade

        logger.info(
            "Order placed: %s %d %s %s lmt=%s stp=%s tif=%s orderId=%s",
            action, quantity, order_type.upper(), self._contract.localSymbol,
            limit_price, stop_price, tif, oid,
        )
        return self._trade_to_dict(trade)

    # ─── Cancel ──────────────────────────────────────────────────────────────

    def cancel_order(self, order_id: int) -> bool:
        trade = self._trades.get(order_id)
        if trade is None:
            logger.warning("cancel_order: orderId %s not found", order_id)
            return False
        self._ib.cancelOrder(trade.order)
        logger.info("Order cancel requested: orderId=%s", order_id)
        return True

    # ─── Query ───────────────────────────────────────────────────────────────

    def get_open_orders(self) -> list:
        return [
            self._trade_to_dict(t) for t in self._trades.values()
            if t.orderStatus.status not in _TERMINAL_STATUSES
        ]

    def get_all_orders(self) -> list:
        return [self._trade_to_dict(t) for t in self._trades.values()]

    # ─── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _trade_to_dict(trade) -> dict:
        s = trade.orderStatus
        o = trade.order
        return {
            "order_id":   o.orderId,
            "action":     o.action,
            "quantity":   int(o.totalQuantity),
            "order_type": o.orderType,
            "lmt_price":  o.lmtPrice  if o.lmtPrice  not in (0, None) else None,
            "stp_price":  o.auxPrice  if o.auxPrice   not in (0, None) else None,
            "tif":        o.tif,
            "status":     s.status,
            "filled":     s.filled,
            "remaining":  s.remaining,
            "avg_fill":   s.avgFillPrice if s.avgFillPrice not in (0, None) else None,
        }
