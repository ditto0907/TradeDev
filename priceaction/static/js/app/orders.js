import { state, getChart } from './state.js';
import { showToast, snapToTick } from './utils.js';
import { showConfirmDialog } from './dialogs.js';
import { addWorkingOrderRow, addOrderHistoryRow } from './orders-table.js';

// Order line tracking — orderId → order line object on chart
let _orderLineShapes = {};

export async function loadWorkingOrders() {
  try {
    // Load active orders → working orders table + chart lines
    const res = await fetch('/api/orders');
    const orders = await res.json();
    console.log('loadWorkingOrders: fetched', orders?.length, 'active orders');
    if (Array.isArray(orders)) {
      orders.forEach(order => {
        addWorkingOrderRow(order);
        drawOrderLine(order);
      });
    }
    // Load all orders → order history table
    const histRes = await fetch('/api/orders?all=true');
    const allOrders = await histRes.json();
    if (Array.isArray(allOrders)) {
      allOrders.forEach(order => addOrderHistoryRow(order));
    }
  } catch (e) {
    console.warn('Failed to load working orders:', e);
  }
}

export async function placeQuickOrder(action, orderType, limitPrice, stopPrice) {
  const qty = parseInt(document.getElementById('order-qty')?.value) || 1;
  const tif = document.getElementById('order-tif')?.value || 'day';

  // Snap prices to tick
  if (limitPrice != null) limitPrice = snapToTick(limitPrice);
  if (stopPrice  != null) stopPrice  = snapToTick(stopPrice);

  const body = {
    action,
    quantity:    qty,
    order_type:  orderType,
    limit_price: limitPrice,
    stop_price:  stopPrice,
    tif,
  };
  console.log('placeQuickOrder →', body);

  try {
    const res  = await fetch('/api/order', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    const data = await res.json();
    if (data.success) {
      const typeLabel = orderType === 'market' ? 'MKT'
                      : orderType === 'limit'  ? `LMT ${limitPrice?.toFixed(2)}`
                      : orderType === 'stop'   ? `STP ${stopPrice?.toFixed(2)}`
                      : `STP LMT`;
      showToast(`#${data.order_id}  ${action} ${qty} MES ${typeLabel}`, 'success');
      addWorkingOrderRow(data);
      addOrderHistoryRow(data);
      drawOrderLine(data);
    } else {
      showToast(`Order failed: ${data.error}`, 'error');
    }
  } catch (e) {
    showToast(`Order error: ${e.message}`, 'error');
  }
}

export async function placeBracketOrder(action, orderType, limitPrice, stopPrice, tpPrice, slPrice) {
  const qty = parseInt(document.getElementById('order-qty')?.value) || 1;
  const tif = document.getElementById('order-tif')?.value || 'day';

  const body = {
    action,
    quantity:    qty,
    order_type:  orderType,
    limit_price: limitPrice != null ? snapToTick(limitPrice) : null,
    stop_price:  stopPrice  != null ? snapToTick(stopPrice)  : null,
    tp_price:    snapToTick(tpPrice),
    sl_price:    snapToTick(slPrice),
    tif,
  };

  try {
    const res  = await fetch('/api/order/bracket', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    const data = await res.json();
    if (data.success) {
      const orders = data.orders;
      showToast(`Bracket: ${orders.length} orders placed`, 'success');
      orders.forEach(o => {
        addWorkingOrderRow(o);
        addOrderHistoryRow(o);
        drawOrderLine(o);
      });
      // Switch to orders tab
      document.querySelector('.btab[data-pane="orders"]')?.click();
    } else {
      showToast(`Bracket order failed: ${data.error}`, 'error');
    }
  } catch (e) {
    showToast(`Bracket error: ${e.message}`, 'error');
  }
}

export async function cancelAllOrders() {
  try {
    const res  = await fetch('/api/orders', { method: 'DELETE' });
    const data = await res.json();
    if (data.success) {
      showToast(`Cancelled ${data.cancelled} orders`, 'success');
      clearAllOrderLines();
    } else {
      showToast('Cancel all failed', 'error');
    }
  } catch (e) {
    showToast(`Cancel all error: ${e.message}`, 'error');
  }
}

export async function flattenPosition() {
  try {
    const res  = await fetch('/api/flatten', { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      if (data.order_id) {
        showToast(`Flatten: ${data.action} ${data.quantity} MES (MKT)`, 'success');
        addWorkingOrderRow(data);
        addOrderHistoryRow(data);
      } else {
        showToast(data.message || 'No position to flatten', 'info');
      }
    } else {
      showToast(`Flatten failed: ${data.error}`, 'error');
    }
  } catch (e) {
    showToast(`Flatten error: ${e.message}`, 'error');
  }
}

export async function cancelOrder(orderId) {
  try {
    const res  = await fetch(`/api/order/${orderId}`, { method: 'DELETE' });
    const data = await res.json();
    if (!data.success) showToast('Cancel failed', 'error');
  } catch (e) {
    showToast(`Cancel error: ${e.message}`, 'error');
  }
}

// ── Visual Order Lines on Chart ───────────────────────────────────────────────

export function drawOrderLine(order) {
  const price = order.lmt_price || order.stp_price;
  if (!price) return;  // Market orders don't get lines

  const chart = getChart();
  if (!chart) return;

  // Remove existing line for this order if any
  if (_orderLineShapes[order.order_id]) {
    try { _orderLineShapes[order.order_id].remove(); } catch {}
    delete _orderLineShapes[order.order_id];
  }

  const isBuy   = order.action === 'BUY';
  const color   = isBuy ? '#26a69a' : '#ef5350';
  const bgColor = isBuy ? 'rgba(38,166,154,0.15)' : 'rgba(239,83,80,0.15)';
  const label   = `#${order.order_id} ${order.action} ${order.quantity}`;

  try {
    const line = chart.createOrderLine()
      .setPrice(price)
      .setText(label)
      .setQuantity(order.quantity.toString())
      .setEditable(true)
      .setCancellable(true)
      .setLineStyle(2)       // dashed
      .setLineWidth(1)
      .setLineColor(color)
      .setBodyTextColor(color)
      .setBodyBorderColor(color)
      .setBodyBackgroundColor(bgColor)
      .setQuantityTextColor('#fff')
      .setQuantityBorderColor(color)
      .setQuantityBackgroundColor(color)
      .setCancelButtonBorderColor(color)
      .setCancelButtonBackgroundColor(bgColor)
      .setCancelButtonIconColor(color)
      .setTooltip(`${order.action} ${order.order_type} #${order.order_id}`)
      .setCancelTooltip('Cancel order')
      .setModifyTooltip('Modify order');

    // Track original price for revert on cancel
    let _origPrice = price;

    // On drag complete → show confirm dialog
    line.onMove(function() {
      const newPrice = snapToTick(line.getPrice());
      line.setPrice(newPrice);  // snap to tick
      if (newPrice === _origPrice) return;

      showConfirmDialog({
        title: 'Confirm Order Move',
        body: `<div class="confirm-order-details">
                 <div class="confirm-row"><span>Order</span><strong>#${order.order_id} ${order.action} ${order.order_type}</strong></div>
                 <div class="confirm-row"><span>From</span><strong>${_origPrice.toFixed(2)}</strong></div>
                 <div class="confirm-row"><span>To</span><strong>${newPrice.toFixed(2)}</strong></div>
               </div>`,
        confirmClass: isBuy ? 'buy' : 'sell',
        confirmText: `Move to ${newPrice.toFixed(2)}`,
        onConfirm: () => {
          modifyOrderPrice(order.order_id, order.order_type, newPrice).then(ok => {
            if (ok) {
              _origPrice = newPrice;
              showToast(`Order #${order.order_id} moved to ${newPrice.toFixed(2)}`, 'success');
            } else {
              // Revert on failure
              line.setPrice(_origPrice);
            }
          });
        },
        onCancel: () => {
          // Revert line to original price
          line.setPrice(_origPrice);
        },
      });
    });

    // On cancel button click
    line.onCancel(function() {
      cancelOrder(order.order_id);
    });

    _orderLineShapes[order.order_id] = line;
  } catch (e) {
    console.debug('drawOrderLine error:', e);
  }
}

export async function modifyOrderPrice(orderId, orderType, newPrice) {
  const isStop = ['STP', 'stop'].includes(orderType);
  const body = isStop
    ? { stop_price: newPrice }
    : { limit_price: newPrice };
  try {
    const res = await fetch(`/api/order/${orderId}`, {
      method:  'PUT',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.success) {
      showToast(`Modify failed: ${data.error}`, 'error');
      return false;
    }
    return true;
  } catch (e) {
    showToast(`Modify error: ${e.message}`, 'error');
    return false;
  }
}

export function removeOrderLine(orderId) {
  if (!state.widget) return;
  const line = _orderLineShapes[orderId];
  if (!line) return;

  try { line.remove(); } catch {}
  delete _orderLineShapes[orderId];
}

export function clearAllOrderLines() {
  if (!state.widget) return;
  for (const line of Object.values(_orderLineShapes)) {
    try { line.remove(); } catch {}
  }
  _orderLineShapes = {};
}
