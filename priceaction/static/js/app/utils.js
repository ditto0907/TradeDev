import { MES_TICK } from './constants.js';

// ── HTML/JS escaping helpers (XSS safety) ─────────────────────────────────────
// _escHtml: for text/HTML content contexts.
// _escJsAttr: for values interpolated into a single-quoted JS string that itself
//   sits inside a double-quoted HTML attribute (e.g. onclick="fn('${x}')").

export function _escHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

export function _escJsAttr(s) {
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// Format a {currency: amount} map into a compact P&L string, keeping each
// currency separate (never sum JPY into USD).  e.g. {USD:120,JPY:-3000}
// → "+$120  −¥3,000".
const _CCY_SYMBOL = { USD: '$', JPY: '¥', EUR: '€', GBP: '£' };

export function _fmtPnlByCcy(map) {
  const keys = Object.keys(map);
  if (!keys.length) return '$0';
  return keys.sort().map(ccy => {
    const v = map[ccy];
    const sym = _CCY_SYMBOL[ccy] || (ccy + ' ');
    const sign = v >= 0 ? '+' : '−';
    return `${sign}${sym}${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  }).join('  ');
}

/** Snap a price to the nearest MES tick (0.25). */
export function snapToTick(price) {
  return Math.round(price / MES_TICK) * MES_TICK;
}

// ── DOM helpers ───────────────────────────────────────────────────────────────

export function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

export function showToast(message, type = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add('toast-show'));
  setTimeout(() => {
    toast.classList.remove('toast-show');
    toast.addEventListener('transitionend', () => toast.remove(), { once: true });
  }, 3000);
}

export function setWsStatus(state, text) {
  const dot   = document.getElementById('ws-dot');
  const label = document.getElementById('ws-text');
  if (dot)   dot.className    = `status-dot ${state}`;
  if (label) label.textContent = text;
}
