"""
IB Order Manager — submit, cancel, and track orders via ib_insync.

Usage:
    mgr = IBOrderManager(ib_instance, qualified_contract)
    result = mgr.place_order("BUY", 1, "limit", limit_price=6800.0)
    mgr.cancel_order(result["order_id"])
    open_orders = mgr.get_open_orders()
"""
import logging
import uuid
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

    # ─── Bracket Order (OCA group) ───────────────────────────────────────────

    def place_bracket_order(
        self,
        action:       str,                   # "BUY" | "SELL"
        quantity:     int,
        order_type:   str,                   # entry order type
        limit_price:  Optional[float] = None,
        stop_price:   Optional[float] = None,
        tp_price:     Optional[float] = None,  # take-profit (limit exit)
        sl_price:     Optional[float] = None,  # stop-loss (stop exit)
        tif:          str = "DAY",
    ) -> list:
        """
        Place a bracket order: entry + take-profit + stop-loss.
        TP and SL are placed as an OCA group so filling one cancels the other.
        Returns a list of order dicts [entry, tp, sl].
        """
        # Place the entry order first
        entry = self.place_order(action, quantity, order_type,
                                 limit_price=limit_price, stop_price=stop_price,
                                 tif=tif)
        results = [entry]

        exit_action = "SELL" if action.upper() == "BUY" else "BUY"
        oca_group   = f"bracket_{uuid.uuid4().hex[:8]}"

        # Take-profit (limit order on exit side)
        if tp_price is not None:
            tp_order = LimitOrder(exit_action, quantity, tp_price, tif=tif.upper())
            tp_order.ocaGroup = oca_group
            tp_order.ocaType  = 1  # Cancel remaining on fill
            tp_trade = self._ib.placeOrder(self._contract, tp_order)
            self._trades[tp_trade.order.orderId] = tp_trade
            results.append(self._trade_to_dict(tp_trade))
            logger.info("Bracket TP placed: %s %d LMT @ %s ocaGroup=%s orderId=%s",
                        exit_action, quantity, tp_price, oca_group, tp_trade.order.orderId)

        # Stop-loss (stop order on exit side)
        if sl_price is not None:
            sl_order = StopOrder(exit_action, quantity, sl_price, tif=tif.upper())
            sl_order.ocaGroup = oca_group
            sl_order.ocaType  = 1
            sl_trade = self._ib.placeOrder(self._contract, sl_order)
            self._trades[sl_trade.order.orderId] = sl_trade
            results.append(self._trade_to_dict(sl_trade))
            logger.info("Bracket SL placed: %s %d STP @ %s ocaGroup=%s orderId=%s",
                        exit_action, quantity, sl_price, oca_group, sl_trade.order.orderId)

        return results

    # ─── Cancel ──────────────────────────────────────────────────────────────

    def cancel_order(self, order_id: int) -> bool:
        trade = self._trades.get(order_id)
        if trade is None:
            logger.warning("cancel_order: orderId %s not found", order_id)
            return False
        self._ib.cancelOrder(trade.order)
        logger.info("Order cancel requested: orderId=%s", order_id)
        return True

    def cancel_all_orders(self) -> int:
        """Cancel all open (non-terminal) orders. Returns count cancelled."""
        count = 0
        for trade in list(self._trades.values()):
            if trade.orderStatus.status not in _TERMINAL_STATUSES:
                try:
                    self._ib.cancelOrder(trade.order)
                    count += 1
                except Exception as e:
                    logger.warning("cancel_all: failed orderId=%s: %s",
                                   trade.order.orderId, e)
        logger.info("Cancel all requested: %d orders", count)
        return count

    # ─── Flatten ─────────────────────────────────────────────────────────────

    def flatten_position(self) -> Optional[dict]:
        """
        Close the current position with a market order.
        Returns the closing order dict or None if no position.
        """
        positions = self._ib.positions()
        for pos in positions:
            if (pos.contract.symbol == self._contract.symbol
                    and pos.contract.secType in ("FUT", "CONTFUT")
                    and pos.position != 0):
                action = "SELL" if pos.position > 0 else "BUY"
                qty = abs(int(pos.position))
                logger.info("Flatten: %s %d %s (current pos: %s)",
                            action, qty, self._contract.localSymbol, pos.position)
                return self.place_order(action, qty, "market")
        logger.info("Flatten: no open position found for %s", self._contract.symbol)
        return None

    # ─── Position Query ──────────────────────────────────────────────────────

    def get_position(self) -> dict:
        """Return current position for the managed contract."""
        positions = self._ib.positions()
        for pos in positions:
            if (pos.contract.symbol == self._contract.symbol
                    and pos.contract.secType in ("FUT", "CONTFUT")):
                return {
                    "symbol":    pos.contract.symbol,
                    "position":  int(pos.position),
                    "avg_cost":  pos.avgCost,
                    "side":      "LONG" if pos.position > 0 else ("SHORT" if pos.position < 0 else "FLAT"),
                }
        return {"symbol": self._contract.symbol, "position": 0, "avg_cost": 0.0, "side": "FLAT"}

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
