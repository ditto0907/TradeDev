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
        self._brackets = {}   # orderId (int) → list[int]  (entry → [tp_id, sl_id])

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
        oca_group   = f"oca_bracket_{uuid.uuid4().hex[:8]}"

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

        # Track bracket group: every member maps to all siblings
        all_ids = [r["order_id"] for r in results]
        for oid in all_ids:
            self._brackets[oid] = [x for x in all_ids if x != oid]

        return results

    # ─── Modify ───────────────────────────────────────────────────────────────

    def modify_order(
        self,
        order_id:    int,
        limit_price: Optional[float] = None,
        stop_price:  Optional[float] = None,
    ) -> dict:
        """Modify the price of an existing order. Returns updated order dict."""
        trade = self._find_trade(order_id)
        if trade is None:
            raise ValueError(f"Order {order_id} not found")
        order = trade.order
        if limit_price is not None:
            order.lmtPrice = limit_price
        if stop_price is not None:
            order.auxPrice = stop_price
        trade = self._ib.placeOrder(self._contract, order)
        self._trades[order_id] = trade
        logger.info("Order modified: orderId=%s lmt=%s stp=%s",
                     order_id, limit_price, stop_price)
        return self._trade_to_dict(trade)

    # ─── Cancel ──────────────────────────────────────────────────────────────

    def cancel_order(self, order_id: int) -> bool:
        trade = self._find_trade(order_id)
        if trade is None:
            logger.warning("cancel_order: orderId %s not found", order_id)
            return False
        self._ib.cancelOrder(trade.order)
        logger.info("Order cancel requested: orderId=%s", order_id)
        # Auto-cancel bracket siblings
        self._cancel_bracket_siblings(order_id)
        return True

    def _cancel_bracket_siblings(self, order_id: int):
        """Cancel all sibling orders in the same bracket group."""
        siblings = self._brackets.get(order_id, [])
        for sib_id in siblings:
            trade = self._find_trade(sib_id)
            if trade and trade.orderStatus.status not in _TERMINAL_STATUSES:
                try:
                    self._ib.cancelOrder(trade.order)
                    logger.info("Bracket sibling auto-cancelled: orderId=%s (parent=%s)",
                                sib_id, order_id)
                except Exception as e:
                    logger.warning("Bracket sibling cancel failed: orderId=%s: %s", sib_id, e)

    def cancel_all_orders(self) -> int:
        """Cancel all open (non-terminal) orders. Returns count cancelled."""
        count = 0
        for trade in self._ib.openTrades():
            if (trade.contract.conId == self._contract.conId
                    and trade.orderStatus.status not in _TERMINAL_STATUSES):
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

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _find_trade(self, order_id: int):
        """Look up a Trade by orderId — check local cache first, then IB."""
        trade = self._trades.get(order_id)
        if trade is not None:
            return trade
        # Search IB's full trade list (open + recent)
        for t in self._ib.trades():
            if t.order.orderId == order_id:
                self._trades[order_id] = t
                return t
        return None

    def _sync_from_ib(self):
        """Sync local _trades cache from IB — IB is the source of truth."""
        for t in self._ib.trades():
            if t.contract.conId == self._contract.conId:
                self._trades[t.order.orderId] = t

    # ─── Query ───────────────────────────────────────────────────────────────

    def get_open_orders(self) -> list:
        """Return all open orders from IB (source of truth)."""
        result = []
        for t in self._ib.openTrades():
            if t.contract.conId == self._contract.conId:
                self._trades[t.order.orderId] = t   # keep cache in sync
                result.append(self._trade_to_dict(t))
        return result

    def get_all_orders(self) -> list:
        """Return all orders (open + filled + cancelled) from IB."""
        self._sync_from_ib()
        return [self._trade_to_dict(t) for t in self._trades.values()
                if t.contract.conId == self._contract.conId]

    # ─── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _trade_to_dict(trade) -> dict:
        s = trade.orderStatus
        o = trade.order
        # Filter out sentinel values: 0, None, and IB's DBL_MAX (~1.7976e+308)
        def _price(v):
            if v is None or v == 0 or v > 1e200:
                return None
            return v
        return {
            "order_id":   o.orderId,
            "action":     o.action,
            "quantity":   int(o.totalQuantity),
            "order_type": o.orderType,
            "lmt_price":  _price(o.lmtPrice),
            "stp_price":  _price(o.auxPrice),
            "tif":        o.tif,
            "status":     s.status,
            "filled":     s.filled,
            "remaining":  s.remaining,
            "avg_fill":   _price(s.avgFillPrice),
        }
