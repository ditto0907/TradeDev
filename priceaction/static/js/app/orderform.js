import { state } from './state.js';
import { MES_TICK, MES_TICK_$, MES_MARGIN, MES_MULTIPLIER } from './constants.js';
import { setText, showToast, snapToTick } from './utils.js';
import { addWorkingOrderRow, addOrderHistoryRow } from './orders-table.js';
import { drawOrderLine } from './orders.js';

let _bracketMode = 'dollar';  // 'ticks' or 'dollar'

export function initOrderForm() {
  onOrderTypeChange();
  updateSummary();
}

export function initBracketConfig() {
  const tpInput = document.getElementById('bracket-tp-val');
  const slInput = document.getElementById('bracket-sl-val');
  if (!tpInput || !slInput) return;

  // Load from localStorage
  const savedTp   = localStorage.getItem('bracket_tp_ticks');
  const savedSl   = localStorage.getItem('bracket_sl_ticks');
  const savedMode = localStorage.getItem('bracket_mode');
  if (savedTp) state.bracket.tpTicks = parseInt(savedTp);
  if (savedSl) state.bracket.slTicks = parseInt(savedSl);
  if (savedMode === 'dollar' || savedMode === 'ticks') _bracketMode = savedMode;

  _applyBracketMode();

  tpInput.addEventListener('change', () => {
    _onBracketInput('tp', tpInput);
  });
  slInput.addEventListener('change', () => {
    _onBracketInput('sl', slInput);
  });
}

export function setBracketMode(mode) {
  _bracketMode = mode;
  localStorage.setItem('bracket_mode', mode);
  _applyBracketMode();
}

function _applyBracketMode() {
  const tpInput = document.getElementById('bracket-tp-val');
  const slInput = document.getElementById('bracket-sl-val');
  const tpUnit  = document.getElementById('bracket-tp-unit');
  const slUnit  = document.getElementById('bracket-sl-unit');
  const modeTicks = document.getElementById('mode-ticks');
  const modeDollar = document.getElementById('mode-dollar');
  if (!tpInput || !slInput) return;

  if (modeTicks) modeTicks.classList.toggle('active', _bracketMode === 'ticks');
  if (modeDollar) modeDollar.classList.toggle('active', _bracketMode === 'dollar');

  if (_bracketMode === 'ticks') {
    tpInput.value = state.bracket.tpTicks;
    slInput.value = state.bracket.slTicks;
    tpInput.step = '1';
    slInput.step = '1';
    if (tpUnit) tpUnit.textContent = 'ticks';
    if (slUnit) slUnit.textContent = 'ticks';
  } else {
    tpInput.value = (state.bracket.tpTicks * MES_TICK_$).toFixed(2);
    slInput.value = (state.bracket.slTicks * MES_TICK_$).toFixed(2);
    tpInput.step = '0.01';
    slInput.step = '0.01';
    if (tpUnit) tpUnit.textContent = '$';
    if (slUnit) slUnit.textContent = '$';
  }
  updateBracketSummary();
}

function _onBracketInput(which, input) {
  let rawVal = parseFloat(input.value);
  if (isNaN(rawVal) || rawVal <= 0) {
    rawVal = which === 'tp' ? (_bracketMode === 'dollar' ? 250 : 200)
                            : (_bracketMode === 'dollar' ? 125 : 100);
    input.value = rawVal;
  }

  let ticks;
  if (_bracketMode === 'dollar') {
    // Convert $ to ticks: ticks = dollars / tick_value
    ticks = Math.round(rawVal / MES_TICK_$);
    if (ticks < 1) ticks = 1;
  } else {
    ticks = Math.max(1, Math.round(rawVal));
    input.value = ticks;
  }

  if (which === 'tp') {
    state.bracket.tpTicks = ticks;
    localStorage.setItem('bracket_tp_ticks', ticks);
  } else {
    state.bracket.slTicks = ticks;
    localStorage.setItem('bracket_sl_ticks', ticks);
  }
  updateBracketSummary();
}

function updateBracketSummary() {
  const { tpTicks, slTicks } = state.bracket;
  const tpPts = (tpTicks * MES_TICK).toFixed(2);
  const slPts = (slTicks * MES_TICK).toFixed(2);
  const tpDol = (tpTicks * MES_TICK_$).toFixed(2);
  const slDol = (slTicks * MES_TICK_$).toFixed(2);
  if (_bracketMode === 'ticks') {
    setText('bracket-tp-pts', `${tpPts} pts / $${tpDol}`);
    setText('bracket-sl-pts', `${slPts} pts / $${slDol}`);
  } else {
    setText('bracket-tp-pts', `${tpTicks} ticks / ${tpPts} pts`);
    setText('bracket-sl-pts', `${slTicks} ticks / ${slPts} pts`);
  }
}

export function setOrderSide(side) {
  state.orderSide = side;
  const buyTab  = document.getElementById('tab-buy');
  const sellTab = document.getElementById('tab-sell');
  const btn     = document.getElementById('submit-order');
  if (side === 'buy') {
    buyTab.className  = 'order-tab active-buy';
    sellTab.className = 'order-tab';
    btn.className     = 'buy';
    btn.textContent   = 'BUY MES';
    const bid = document.getElementById('bid-price').textContent;
    const inp = document.getElementById('order-price');
    if (inp && bid !== '—') inp.value = bid;
  } else {
    buyTab.className  = 'order-tab';
    sellTab.className = 'order-tab active-sell';
    btn.className     = 'sell';
    btn.textContent   = 'SELL MES';
    const ask = document.getElementById('ask-price').textContent;
    const inp = document.getElementById('order-price');
    if (inp && ask !== '—') inp.value = ask;
  }
  updateSummary();
}

export function onOrderTypeChange() {
  const type      = document.getElementById('order-type').value;
  const priceGrp  = document.getElementById('price-group');
  const stopGrp   = document.getElementById('stop-group');
  const priceLabel = priceGrp?.querySelector('.form-label');
  if (type === 'market') {
    if (priceGrp) priceGrp.style.display = 'none';
    if (stopGrp)  stopGrp.style.display  = 'none';
  } else if (type === 'limit') {
    if (priceGrp) { priceGrp.style.display = ''; if (priceLabel) priceLabel.textContent = 'Limit Price'; }
    if (stopGrp)  stopGrp.style.display = 'none';
  } else if (type === 'stop') {
    if (priceGrp) priceGrp.style.display = 'none';
    if (stopGrp)  stopGrp.style.display  = '';
  } else if (type === 'stop_limit') {
    if (priceGrp) { priceGrp.style.display = ''; if (priceLabel) priceLabel.textContent = 'Limit Price'; }
    if (stopGrp)  stopGrp.style.display = '';
  }
  updateSummary();
}

export function adjustQty(delta) {
  const inp = document.getElementById('order-qty');
  if (!inp) return;
  inp.value = Math.max(1, Math.min(50, (parseInt(inp.value) || 1) + delta));
  updateSummary();
}

export function updateSummary() {
  const qty  = parseInt(document.getElementById('order-qty')?.value) || 1;
  const type = document.getElementById('order-type')?.value;
  let price  = null;
  if (type === 'market') {
    // Use last bar close price instead of topbar element
    price = state.lastBar ? state.lastBar.close : null;
  } else {
    price = parseFloat(document.getElementById('order-price')?.value);
  }
  const contractValue = (price && !isNaN(price)) ? (price * MES_MULTIPLIER * qty).toFixed(0) : '—';
  setText('sum-value',  contractValue !== '—' ? `$${parseInt(contractValue).toLocaleString()}` : '—');
  setText('sum-margin', `$${(MES_MARGIN * qty).toLocaleString()}`);
}

export async function placeOrder() {
  const qty     = parseInt(document.getElementById('order-qty')?.value) || 1;
  const type    = document.getElementById('order-type')?.value || 'market';
  const tif     = document.getElementById('order-tif')?.value  || 'day';
  let limitPx = parseFloat(document.getElementById('order-price')?.value);
  let stopPx  = parseFloat(document.getElementById('order-stop')?.value);
  limitPx = isNaN(limitPx) ? null : snapToTick(limitPx);
  stopPx  = isNaN(stopPx)  ? null : snapToTick(stopPx);

  // Validate required prices for the selected order type before submitting.
  if ((type === 'limit' || type === 'stop_limit') && (limitPx == null || limitPx <= 0)) {
    showToast('Enter a valid limit price', 'error');
    return;
  }
  if ((type === 'stop' || type === 'stop_limit') && (stopPx == null || stopPx <= 0)) {
    showToast('Enter a valid stop price', 'error');
    return;
  }

  const body = {
    action:      state.orderSide.toUpperCase(),
    quantity:    qty,
    order_type:  type,
    limit_price: type === 'limit' || type === 'stop_limit' ? limitPx : null,
    stop_price:  type === 'stop'  || type === 'stop_limit' ? stopPx  : null,
    tif,
  };

  const btn = document.getElementById('submit-order');
  if (btn) { btn.disabled = true; btn.textContent = 'Submitting…'; }

  try {
    const res  = await fetch('/api/order', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Order #${data.order_id} submitted: ${body.action} ${qty} MES`, 'success');
      addWorkingOrderRow(data);
      addOrderHistoryRow(data);
      drawOrderLine(data);
      document.querySelector('.btab[data-pane="orders"]')?.click();
    } else {
      showToast(`Order failed: ${data.error}`, 'error');
    }
  } catch (e) {
    showToast(`Network error: ${e.message}`, 'error');
  } finally {
    if (btn) {
      btn.disabled    = false;
      btn.textContent = state.orderSide === 'buy' ? 'BUY MES' : 'SELL MES';
    }
  }
}
