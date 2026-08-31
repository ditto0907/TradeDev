import { state } from './state.js';
import { MES_MULTIPLIER, POSITION_POLL_MS } from './constants.js';

export function initPositionPolling() {
  fetchPosition();
  setInterval(fetchPosition, POSITION_POLL_MS);
}

export async function fetchPosition() {
  try {
    const res = await fetch('/api/position');
    state.currentPosition = await res.json();
    updatePositionPanel();
  } catch {}
}

export function updatePositionPanel() {
  const tbody = document.getElementById('pos-tbody');
  if (!tbody) return;

  const pos = state.currentPosition;
  if (pos.position === 0) {
    tbody.innerHTML = '<tr><td colspan="8"><div class="empty-table">No open positions</div></td></tr>';
    return;
  }

  const lastPrice = state.lastBar ? state.lastBar.close : 0;
  const avgPrice  = pos.avg_cost / MES_MULTIPLIER;
  const qty       = Math.abs(pos.position);
  const unrealPnl = lastPrice > 0 ? (lastPrice - avgPrice) * pos.position * MES_MULTIPLIER : 0;
  const pnlClass  = unrealPnl >= 0 ? 'up' : 'down';
  const value     = lastPrice > 0 ? (lastPrice * MES_MULTIPLIER * qty) : 0;

  tbody.innerHTML = `
    <tr>
      <td>MES</td>
      <td class="${pos.side === 'LONG' ? 'up' : 'down'}">${pos.side}</td>
      <td>${qty}</td>
      <td>${avgPrice.toFixed(2)}</td>
      <td>${lastPrice > 0 ? lastPrice.toFixed(2) : '—'}</td>
      <td class="${pnlClass}">${unrealPnl >= 0 ? '+' : ''}$${unrealPnl.toFixed(2)}</td>
      <td>—</td>
      <td>$${value.toLocaleString()}</td>
    </tr>
  `;
}
