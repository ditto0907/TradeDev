import { TERMINAL_STATUSES } from './constants.js';
import { removeOrderLine } from './orders.js';
import { fetchPosition } from './position.js';

// Keep a local symbol reference so generated HTML can call cancelOrder(...)
// from this module scope via string interpolation.
const cancelOrder = (orderId) => window.cancelOrder(orderId);

export function addWorkingOrderRow(order) {
  // Only show active (non-terminal) orders in Working Orders
  if (TERMINAL_STATUSES.includes(order.status)) return;
  const tbody = document.getElementById('orders-tbody');
  if (!tbody) { console.warn('addWorkingOrderRow: #orders-tbody not found'); return; }
  // Remove existing row for same order (avoid duplicates)
  const existing = document.getElementById(`order-row-${order.order_id}`);
  if (existing) existing.remove();
  const empty = tbody.querySelector('tr td[colspan]');
  if (empty) empty.closest('tr').remove();
  const priceStr = order.lmt_price ? Number(order.lmt_price).toFixed(2)
                 : order.stp_price ? `STP ${Number(order.stp_price).toFixed(2)}`
                 : 'MKT';
  const sideClass = order.action === 'BUY' ? 'up' : 'down';
  const tr = document.createElement('tr');
  tr.id = `order-row-${order.order_id}`;
  tr.innerHTML = `
    <td>${order.time ? new Date(order.time).toLocaleTimeString() : new Date().toLocaleTimeString()}</td>
    <td>MES</td>
    <td class="${sideClass}">${order.action}</td>
    <td>${order.order_type}</td>
    <td>${order.quantity}</td>
    <td>${priceStr}</td>
    <td id="order-status-${order.order_id}">${order.status || 'Submitted'}</td>
    <td><button class="cancel-btn" onclick="cancelOrder(${order.order_id})">Cancel</button></td>
  `;
  tbody.prepend(tr);
}

export function updateWorkingOrderRow(order) {
  const isTerminal = TERMINAL_STATUSES.includes(order.status);

  if (isTerminal) {
    // Remove from working orders table
    const row = document.getElementById(`order-row-${order.order_id}`);
    if (row) row.remove();
    // Restore empty placeholder if no rows left
    const tbody = document.getElementById('orders-tbody');
    if (tbody && tbody.children.length === 0) {
      const tr = document.createElement('tr');
      tr.innerHTML = '<td colspan="8"><div class="empty-table">No working orders</div></td>';
      tbody.appendChild(tr);
    }
    removeOrderLine(order.order_id);
    // Add to order history
    addOrderHistoryRow(order);
    if (order.status === 'Filled') {
      addFilledOrderRow(order);
      fetchPosition();
    }
  } else {
    // Active order — update or add row
    const statusEl = document.getElementById(`order-status-${order.order_id}`);
    if (statusEl) {
      statusEl.textContent = order.status;
    } else {
      addWorkingOrderRow(order);
    }
  }
}

export function addFilledOrderRow(order) {
  const tbody = document.getElementById('fills-tbody');
  if (!tbody) return;
  const empty = tbody.querySelector('tr td[colspan]');
  if (empty) empty.closest('tr').remove();
  const sideClass = order.action === 'BUY' ? 'up' : 'down';
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td>${order.time ? new Date(order.time).toLocaleTimeString() : new Date().toLocaleTimeString()}</td>
    <td>MES</td>
    <td class="${sideClass}">${order.action}</td>
    <td>${order.order_type}</td>
    <td>${order.quantity}</td>
    <td>${order.avg_fill ? order.avg_fill.toFixed(2) : '—'}</td>
    <td>—</td>
  `;
  tbody.prepend(tr);
}

export function addOrderHistoryRow(order) {
  const tbody = document.getElementById('order-history-tbody');
  if (!tbody) return;
  // Update existing row if present (status change)
  const existing = document.getElementById(`ohist-row-${order.order_id}`);
  if (existing) {
    const statusCell = existing.querySelector('td:last-child');
    const fillCell = existing.querySelector('td:nth-child(7)');
    if (statusCell) {
      statusCell.textContent = order.status || '—';
      statusCell.style.color = order.status === 'Filled' ? 'var(--green)'
                             : TERMINAL_STATUSES.includes(order.status) ? 'var(--text-faint)'
                             : '';
    }
    if (fillCell && order.avg_fill) fillCell.textContent = Number(order.avg_fill).toFixed(2);
    return;
  }
  const empty = tbody.querySelector('tr td[colspan]');
  if (empty) empty.closest('tr').remove();
  const sideClass = order.action === 'BUY' ? 'up' : 'down';
  const priceStr = order.lmt_price ? Number(order.lmt_price).toFixed(2)
                 : order.stp_price ? `STP ${Number(order.stp_price).toFixed(2)}`
                 : 'MKT';
  const statusColor = order.status === 'Filled'  ? 'var(--green)'
                    : TERMINAL_STATUSES.includes(order.status) ? 'var(--text-faint)'
                    : '';
  const tr = document.createElement('tr');
  tr.id = `ohist-row-${order.order_id}`;
  tr.innerHTML = `
    <td>${order.time ? new Date(order.time).toLocaleTimeString() : new Date().toLocaleTimeString()}</td>
    <td>MES</td>
    <td class="${sideClass}">${order.action}</td>
    <td>${order.order_type}</td>
    <td>${order.quantity}</td>
    <td>${priceStr}</td>
    <td>${order.avg_fill ? Number(order.avg_fill).toFixed(2) : '—'}</td>
    <td style="color:${statusColor}">${order.status || '—'}</td>
  `;
  tbody.prepend(tr);
}
