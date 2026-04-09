/**
 * app.js — Trading Terminal UI logic
 *
 * Responsibilities:
 *  1. Initialize TradingView widget
 *  2. Wire WebSocket price updates → topbar OHLC, watchlist, bid/ask
 *  3. Draw S/R lines and market cycle ranges on the chart
 *  4. Update cycle badge + key levels panel
 *  5. Order entry panel interactions (UI only — wire to backend when ready)
 *  6. Bottom panel tab switching
 *  7. Bottom panel resize handle
 */

'use strict';

// ── Constants ──────────────────────────────────────────────────────────────────

const CYCLE_COLORS = {
  markup:       'rgba(38,166,154,0.10)',
  markdown:     'rgba(239,83,80,0.10)',
  accumulation: 'rgba(33,150,243,0.10)',
  distribution: 'rgba(255,152,0,0.10)',
};

const CYCLE_LABELS = {
  markup:       'Markup (Uptrend)',
  markdown:     'Markdown (Downtrend)',
  accumulation: 'Accumulation',
  distribution: 'Distribution',
  unknown:      '—',
};

// MES multiplier: each point = $5
const MES_TICK   = 0.25;
const MES_TICK_$ = 1.25;
const MES_MARGIN = 1650;   // approximate initial margin per contract (USD)

// ── State ──────────────────────────────────────────────────────────────────────

let _widget      = null;   // TradingView widget instance
let _activeShapes = [];    // IDs of drawn S/R / cycle shapes
let _lastBar     = null;   // most recent 5min bar
let _openPrice   = null;   // day open price (first bar of session)
let _orderSide   = 'buy';  // current order side

// ── DOMContentLoaded ──────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  initChart();
  initBottomTabs();
  initBottomResize();
  initOrderForm();
});

// ── Chart Init ────────────────────────────────────────────────────────────────

function initChart() {
  const datafeed = new MESDatafeed();

  datafeed.setAnalysisCallback((analysis) => {
    updateAnnotations(analysis);
    updateCycleBadge(analysis.market_cycle);
    updateSRPanel(analysis);
  });

  _widget = new TradingView.widget({
    container:    'tv-chart',
    datafeed:     datafeed,
    symbol:       'MES',
    interval:     '5',

    library_path: '/charting_library/',
    locale:       'en',
    timezone:     'America/New_York',

    theme:        'dark',
    toolbar_bg:   '#1e222d',
    loading_screen: { backgroundColor: '#131722', foregroundColor: '#2962ff' },

    enabled_features: [
      'use_localstorage_for_settings',
      'move_logo_to_main_pane',
    ],
    disabled_features: [
      'header_symbol_search',
      'header_compare',
      'display_market_status',
    ],

    autosize: true,

    overrides: {
      'paneProperties.background':          '#131722',
      'paneProperties.backgroundType':      'solid',
      'paneProperties.vertGridProperties.color': '#1e222d',
      'paneProperties.horzGridProperties.color': '#1e222d',
      'scalesProperties.textColor':         '#787b86',
    },
  });

  _widget.onChartReady(() => {
    setWsStatus('live', 'Live');

    fetch('/api/analysis')
      .then(r => r.json())
      .then(analysis => {
        updateAnnotations(analysis);
        updateCycleBadge(analysis.market_cycle);
        updateSRPanel(analysis);
      })
      .catch(e => console.warn('Analysis fetch error:', e));
  });

  // Connect WebSocket for live price/bar updates
  connectPriceFeed(datafeed);
}

// ── WebSocket price feed ───────────────────────────────────────────────────────

function connectPriceFeed(datafeed) {
  // Reuse the datafeed's WebSocket — hook into it before it connects
  // by overriding the onmessage handler wrapper
  const origEnsure = datafeed._ensureWebSocket.bind(datafeed);
  datafeed._ensureWebSocket = function() {
    origEnsure();
    if (datafeed._ws) {
      const origOnMsg = datafeed._ws.onmessage;
      datafeed._ws.onmessage = function(event) {
        if (origOnMsg) origOnMsg.call(datafeed._ws, event);
        handlePriceMessage(event);
      };
    }
  };
  // Trigger WebSocket connection now
  datafeed._ensureWebSocket();
}

function handlePriceMessage(event) {
  let msg;
  try { msg = JSON.parse(event.data); } catch { return; }

  if (msg.type === 'bar' && msg.bar_size === '5min') {
    updateTopbarOHLC(msg.bar);
    updateWatchlistMES(msg.bar.close);
    updateBidAsk(msg.bar.close);
    _lastBar = msg.bar;

  } else if (msg.type === 'snapshot' && msg.bars_5min && msg.bars_5min.length > 0) {
    const latest = msg.bars_5min[msg.bars_5min.length - 1];
    updateTopbarOHLC(latest);
    updateWatchlistMES(latest.close);
    updateBidAsk(latest.close);
    _lastBar = latest;
    setWsStatus('live', 'Live');
  }
}

// ── Topbar OHLC ───────────────────────────────────────────────────────────────

function updateTopbarOHLC(bar) {
  const fmt = v => v != null ? v.toFixed(2) : '—';
  setText('tb-open',  fmt(bar.open));
  setText('tb-high',  fmt(bar.high));
  setText('tb-low',   fmt(bar.low));
  setText('tb-close', fmt(bar.close));
  setText('tb-vol',   bar.volume != null ? bar.volume.toLocaleString() : '—');
  setText('last-price', fmt(bar.close));

  const el = document.getElementById('last-price');
  if (el) el.style.color = '#d1d4dc';

  // Price change vs open
  if (_openPrice == null) _openPrice = bar.open;
  const chg    = bar.close - _openPrice;
  const chgPct = (_openPrice > 0) ? (chg / _openPrice * 100) : 0;
  const chgEl  = document.getElementById('price-change');
  if (chgEl) {
    chgEl.textContent = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)} (${chgPct >= 0 ? '+' : ''}${chgPct.toFixed(2)}%)`;
    chgEl.className   = chg >= 0 ? 'up' : 'down';
  }
}

// ── Watchlist ─────────────────────────────────────────────────────────────────

function updateWatchlistMES(price) {
  const priceEl = document.getElementById('wl-mes-price');
  const chgEl   = document.getElementById('wl-mes-chg');
  if (priceEl) priceEl.textContent = price != null ? price.toFixed(2) : '—';
  if (chgEl && _openPrice) {
    const chg    = price - _openPrice;
    const chgPct = (chg / _openPrice * 100).toFixed(2);
    chgEl.textContent = `${chg >= 0 ? '+' : ''}${chgPct}%`;
    chgEl.className   = `watch-change ${chg >= 0 ? 'up' : 'down'}`;
  }
}

// ── Bid / Ask ─────────────────────────────────────────────────────────────────

function updateBidAsk(lastPrice) {
  if (lastPrice == null) return;
  // Simulate bid/ask spread (0.25 pt = 1 tick for MES)
  const bid = (lastPrice - MES_TICK).toFixed(2);
  const ask = (lastPrice + MES_TICK).toFixed(2);
  setText('bid-price', bid);
  setText('ask-price', ask);
  setText('bid-size', '—');
  setText('ask-size', '—');

  // Pre-fill limit price input if empty
  const priceInput = document.getElementById('order-price');
  if (priceInput && !priceInput.value) {
    priceInput.value = _orderSide === 'buy' ? bid : ask;
    updateSummary();
  }
}

// ── Cycle Badge ───────────────────────────────────────────────────────────────

function updateCycleBadge(cycle) {
  const el = document.getElementById('cycle-badge');
  if (!el) return;
  el.textContent = CYCLE_LABELS[cycle] || cycle || '—';
  el.className   = cycle && cycle !== 'unknown' ? cycle : '';
}

// ── S/R Panel (right side) ────────────────────────────────────────────────────

function updateSRPanel(analysis) {
  const container = document.getElementById('sr-levels-list');
  if (!container) return;

  const sup = (analysis.support_levels    || []).slice(0, 3);
  const res = (analysis.resistance_levels || []).slice(0, 3);

  let html = '';
  res.reverse().forEach(l => {
    html += `<div class="sr-level-item">
      <div class="sr-dot" style="background:#ef5350"></div>
      <span class="sr-price res">${l.price.toFixed(2)}</span>
    </div>`;
  });
  sup.forEach(l => {
    html += `<div class="sr-level-item">
      <div class="sr-dot" style="background:#26a69a"></div>
      <span class="sr-price sup">${l.price.toFixed(2)}</span>
    </div>`;
  });

  container.innerHTML = html || '<span style="color:var(--text-faint);font-size:11px;grid-column:1/-1">No levels detected</span>';
}

// ── Chart Annotations ─────────────────────────────────────────────────────────

function updateAnnotations(analysis) {
  if (!_widget) return;

  let chart;
  try { chart = _widget.activeChart(); } catch { return; }

  // Clear previous shapes
  _activeShapes.forEach(id => { try { chart.removeEntity(id); } catch {} });
  _activeShapes = [];

  // Cycle range backgrounds
  (analysis.cycle_ranges || []).slice(-8).forEach(range => {
    const color = CYCLE_COLORS[range.type] || 'rgba(128,128,128,0.06)';
    try {
      const id = chart.createMultipointShape(
        [{ time: range.start_time, price: 0 }, { time: range.end_time, price: 0 }],
        {
          shape: 'rect',
          lock: true,
          disableSelection: true,
          overrides: {
            backgroundColor: color,
            borderColor: 'rgba(0,0,0,0)',
            borderWidth: 0,
            showLabel: true,
            text: range.type,
            textcolor: 'rgba(255,255,255,0.35)',
            fontsize: 10,
          },
        }
      );
      if (id) _activeShapes.push(id);
    } catch {}
  });

  // Support lines
  (analysis.support_levels || []).forEach(level => {
    drawHLine(chart, level.price, '#26a69a', Math.min(level.touches, 3));
  });

  // Resistance lines
  (analysis.resistance_levels || []).forEach(level => {
    drawHLine(chart, level.price, '#ef5350', Math.min(level.touches, 3));
  });
}

function drawHLine(chart, price, color, width) {
  try {
    const id = chart.createShape(
      { price, time: 0 },
      {
        shape: 'horizontal_line',
        lock: true,
        disableSelection: true,
        overrides: {
          linecolor:  color,
          linewidth:  width,
          linestyle:  0,
          showPrice:  true,
          showLabel:  true,
          text:       price.toFixed(2),
          textcolor:  color,
          fontsize:   11,
        },
      }
    );
    if (id) _activeShapes.push(id);
  } catch {}
}

// ── Order Entry Panel ─────────────────────────────────────────────────────────

function initOrderForm() {
  onOrderTypeChange();
  updateSummary();
}

function setOrderSide(side) {
  _orderSide = side;
  const buyTab  = document.getElementById('tab-buy');
  const sellTab = document.getElementById('tab-sell');
  const btn     = document.getElementById('submit-order');

  if (side === 'buy') {
    buyTab.className  = 'order-tab active-buy';
    sellTab.className = 'order-tab';
    btn.className     = 'buy';
    btn.textContent   = 'BUY MES';
    // Pre-fill bid price
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

function onOrderTypeChange() {
  const type      = document.getElementById('order-type').value;
  const priceGrp  = document.getElementById('price-group');
  const stopGrp   = document.getElementById('stop-group');
  const priceLabel = priceGrp ? priceGrp.querySelector('.form-label') : null;

  if (type === 'market') {
    if (priceGrp) priceGrp.style.display = 'none';
    if (stopGrp)  stopGrp.style.display  = 'none';
  } else if (type === 'limit') {
    if (priceGrp) { priceGrp.style.display = ''; if (priceLabel) priceLabel.textContent = 'Limit Price'; }
    if (stopGrp)  stopGrp.style.display  = 'none';
  } else if (type === 'stop') {
    if (priceGrp) priceGrp.style.display = 'none';
    if (stopGrp)  stopGrp.style.display  = '';
  } else if (type === 'stop_limit') {
    if (priceGrp) { priceGrp.style.display = ''; if (priceLabel) priceLabel.textContent = 'Limit Price'; }
    if (stopGrp)  stopGrp.style.display  = '';
  }
  updateSummary();
}

function adjustQty(delta) {
  const inp = document.getElementById('order-qty');
  if (!inp) return;
  const val = Math.max(1, Math.min(50, (parseInt(inp.value) || 1) + delta));
  inp.value = val;
  updateSummary();
}

function updateSummary() {
  const qty  = parseInt(document.getElementById('order-qty')?.value) || 1;
  const type = document.getElementById('order-type')?.value;

  let price = null;
  if (type === 'market') {
    // Use last trade price
    const lastEl = document.getElementById('last-price');
    price = lastEl ? parseFloat(lastEl.textContent) : null;
  } else {
    price = parseFloat(document.getElementById('order-price')?.value);
  }

  // MES contract value = price × $5 per point
  const contractValue = (price && !isNaN(price)) ? (price * 5 * qty).toFixed(0) : '—';
  const margin        = (MES_MARGIN * qty).toLocaleString();

  setText('sum-value',  contractValue !== '—' ? `$${parseInt(contractValue).toLocaleString()}` : '—');
  setText('sum-margin', `$${margin}`);
}

function placeOrder() {
  // UI feedback — actual order submission wired to backend when ready
  const qty   = document.getElementById('order-qty')?.value || 1;
  const type  = document.getElementById('order-type')?.value || 'market';
  const side  = _orderSide.toUpperCase();
  const tif   = document.getElementById('order-tif')?.value?.toUpperCase() || 'DAY';

  let priceStr = '';
  if (type !== 'market') {
    const p = document.getElementById('order-price')?.value;
    if (p) priceStr = ` @ ${p}`;
  }

  console.log(`[Order] ${side} ${qty} MES ${type.toUpperCase()}${priceStr} ${tif}`);
  alert(`Order submitted (UI demo):\n${side} ${qty} MES ${type.toUpperCase()}${priceStr} ${tif}`);
}

// ── Bottom Tabs ───────────────────────────────────────────────────────────────

function initBottomTabs() {
  document.querySelectorAll('.btab').forEach(tab => {
    tab.addEventListener('click', () => {
      const pane = tab.dataset.pane;
      document.querySelectorAll('.btab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.btab-pane').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      const paneEl = document.getElementById(`pane-${pane}`);
      if (paneEl) paneEl.classList.add('active');
    });
  });
}

// ── Bottom Panel Resize ───────────────────────────────────────────────────────

function initBottomResize() {
  const handle = document.getElementById('bottom-resize');
  const main   = document.getElementById('main');
  if (!handle || !main) return;

  let dragging = false;
  let startY   = 0;
  let startH   = 0;

  handle.addEventListener('mousedown', e => {
    dragging = true;
    startY   = e.clientY;
    const bottom = document.getElementById('bottom');
    startH = bottom ? bottom.offsetHeight : 180;
    document.body.style.cursor = 'row-resize';
    e.preventDefault();
  });

  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const delta  = startY - e.clientY;
    const newH   = Math.max(100, Math.min(500, startH + delta));
    main.style.gridTemplateRows = `1fr ${newH}px`;
  });

  document.addEventListener('mouseup', () => {
    dragging = false;
    document.body.style.cursor = '';
  });
}

// ── WebSocket Status ──────────────────────────────────────────────────────────

function setWsStatus(state, text) {
  const dot  = document.getElementById('ws-dot');
  const label = document.getElementById('ws-text');
  if (dot)   { dot.className = `status-dot ${state}`; }
  if (label) { label.textContent = text; }
}

// ── Utility ───────────────────────────────────────────────────────────────────

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}
