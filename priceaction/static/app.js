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
const MES_TICK   = 0.25;
const MES_TICK_$ = 1.25;
const MES_MARGIN = 1650;

// ── State ──────────────────────────────────────────────────────────────────────

let _widget        = null;
let _volumeStudyId = null;
let _lastBar       = null;
let _openPrice     = null;
let _orderSide     = 'buy';
let _lastAnalysis  = null;

// S/R shape tracking
let _supportShapes    = [];
let _resistanceShapes = [];
let _cycleShapes      = [];
let _showSupport      = true;
let _showResistance   = true;

// Trade markers
let _tradeShapes  = [];
let _showTrades   = true;
let _tradesLoaded = false;

// RTH / ETH
window._rthMode = false;

// Right-click order price — updated via crossHairMoved
window._chartCursorPrice = null;

// ── DOMContentLoaded ──────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  initChart();
  initBottomTabs();
  initBottomResize();
  initOrderForm();
  initSRLegendDrag();
});

// ── Chart Init ────────────────────────────────────────────────────────────────

function initChart() {
  const datafeed = new MESDatafeed();

  datafeed.setAnalysisCallback((analysis) => {
    _lastAnalysis = analysis;
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
      'create_volume_indicator_by_default',
    ],
    autosize: true,
    overrides: {
      'paneProperties.background':               '#131722',
      'paneProperties.backgroundType':           'solid',
      'paneProperties.vertGridProperties.color': '#1e222d',
      'paneProperties.horzGridProperties.color': '#1e222d',
      'scalesProperties.textColor':              '#787b86',
    },

    // ── Right-click context menu for quick order placement ─────────────────
    context_menu: {
      items_processor: (defaultItems, actionsFactory) => {
        return _buildContextMenuItems(defaultItems, actionsFactory);
      },
    },
  });

  _widget.onChartReady(() => {
    setWsStatus('live', 'Live');
    const chart = _widget.activeChart();

    // Track crosshair price for right-click orders
    try {
      chart.crossHairMoved(({ price }) => {
        if (price != null && !isNaN(price) && price > 0) {
          window._chartCursorPrice = price;
        }
      });
    } catch (e) {
      console.warn('crossHairMoved subscribe error:', e);
    }

    // Volume sub-pane
    if (!_volumeStudyId) {
      const p = chart.createStudy('Volume', false, false, [], {
        'volume.color.0':    'rgba(239,83,80,0.55)',
        'volume.color.1':    'rgba(38,166,154,0.55)',
        'volume ma.visible': false,
      });
      const afterCreate = (id) => {
        _volumeStudyId = id;
        try {
          const panes = chart.getPanes();
          if (panes.length > 1) panes[1].setHeight(Math.round(panes[0].getHeight() * 0.15));
        } catch {}
      };
      if (p && typeof p.then === 'function') p.then(afterCreate).catch(() => {});
      else afterCreate(p);
    }

    // Load S/R analysis
    fetch('/api/analysis')
      .then(r => r.json())
      .then(analysis => {
        _lastAnalysis = analysis;
        updateAnnotations(analysis);
        updateCycleBadge(analysis.market_cycle);
        updateSRPanel(analysis);
      })
      .catch(e => console.warn('Analysis fetch error:', e));

    // Load trade markers
    if (_showTrades) initTradeMarkers();
  });

  connectPriceFeed(datafeed);
}

// ── Right-click Context Menu ───────────────────────────────────────────────────

function _buildContextMenuItems(defaultItems, actionsFactory) {
  const price = window._chartCursorPrice;
  if (price == null || isNaN(price)) return defaultItems;

  const lastPrice = _lastBar ? _lastBar.close : price;
  const isAbove   = price >= lastPrice;
  const pStr      = price.toFixed(2);
  const qty       = parseInt(document.getElementById('order-qty')?.value) || 1;

  const extra = [];
  try {
    extra.push(actionsFactory.createSeparator());

    // Conditional orders based on position relative to last price
    if (isAbove) {
      extra.push(actionsFactory.createAction({
        text: `Buy Stop  @ ${pStr}  (${qty} ct)`,
        click: () => placeQuickOrder('BUY', 'stop', null, price),
      }));
      extra.push(actionsFactory.createAction({
        text: `Sell Limit @ ${pStr}  (${qty} ct)`,
        click: () => placeQuickOrder('SELL', 'limit', price, null),
      }));
    } else {
      extra.push(actionsFactory.createAction({
        text: `Buy Limit  @ ${pStr}  (${qty} ct)`,
        click: () => placeQuickOrder('BUY', 'limit', price, null),
      }));
      extra.push(actionsFactory.createAction({
        text: `Sell Stop  @ ${pStr}  (${qty} ct)`,
        click: () => placeQuickOrder('SELL', 'stop', null, price),
      }));
    }

    extra.push(actionsFactory.createSeparator());

    // Market orders always available
    extra.push(actionsFactory.createAction({
      text: `Market Buy  (${qty} ct)`,
      click: () => placeQuickOrder('BUY', 'market', null, null),
    }));
    extra.push(actionsFactory.createAction({
      text: `Market Sell  (${qty} ct)`,
      click: () => placeQuickOrder('SELL', 'market', null, null),
    }));

    extra.push(actionsFactory.createSeparator());
  } catch (e) {
    console.warn('Context menu build error:', e);
    return defaultItems;
  }

  return extra.concat(Array.from(defaultItems));
}

async function placeQuickOrder(action, orderType, limitPrice, stopPrice) {
  const qty = parseInt(document.getElementById('order-qty')?.value) || 1;
  const tif = document.getElementById('order-tif')?.value || 'day';

  const body = {
    action,
    quantity:    qty,
    order_type:  orderType,
    limit_price: limitPrice,
    stop_price:  stopPrice,
    tif,
  };

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
    } else {
      showToast(`Order failed: ${data.error}`, 'error');
    }
  } catch (e) {
    showToast(`Order error: ${e.message}`, 'error');
  }
}

// ── RTH / ETH Toggle ──────────────────────────────────────────────────────────

function toggleRTH() {
  window._rthMode = !window._rthMode;
  const btn = document.getElementById('rth-btn');
  if (btn) {
    btn.textContent = window._rthMode ? 'RTH' : 'ETH';
    btn.classList.toggle('rth-active', window._rthMode);
  }
  if (_widget) {
    try {
      // Use TWO distinct symbol names so TradingView is forced to call
      // resolveSymbol again. Calling setSymbol with the *same* name
      // ('MES') when already on 'MES' is a no-op in TV's caching layer.
      const sym = window._rthMode ? 'MES_RTH' : 'MES';
      _widget.activeChart().setSymbol(sym);
    } catch (e) {
      console.warn('setSymbol RTH/ETH error:', e);
    }
  }
}

// ── Trade Markers ─────────────────────────────────────────────────────────────

async function initTradeMarkers() {
  try {
    const res    = await fetch('/api/trades');
    const trades = await res.json();
    _tradesLoaded = true;
    drawTradeMarkers(trades);
  } catch (e) {
    console.warn('Trade markers load error:', e);
  }
}

function drawTradeMarkers(trades) {
  if (!_widget) return;
  let chart;
  try { chart = _widget.activeChart(); } catch { return; }

  // Clear existing
  _tradeShapes.forEach(id => { try { chart.removeEntity(id); } catch {} });
  _tradeShapes = [];

  if (!_showTrades || !trades.length) return;

  trades.forEach(trade => {
    try {
      const isLong     = trade.direction === 'long';
      const entryColor = isLong ? '#26a69a' : '#ef5350';
      const exitColor  = (trade.pnl != null && trade.pnl >= 0) ? '#26a69a' : '#ef5350';

      // ── Entry arrow ─────────────────────────────────────────────────────
      const entryLabel = `${isLong ? 'L' : 'S'}${trade.qty}`;
      const entryId = chart.createShape(
        { time: trade.entry_time, price: trade.entry_price },
        {
          shape:           isLong ? 'arrow_up' : 'arrow_down',
          lock:            true,
          disableSelection: false,
          overrides: {
            color:    entryColor,
            text:     entryLabel,
            fontsize: 10,
          },
        }
      );
      if (entryId) _tradeShapes.push(entryId);

      // ── Exit arrow ──────────────────────────────────────────────────────
      if (trade.exit_time != null && trade.exit_price != null) {
        const pnlStr   = trade.pnl != null
          ? `${trade.pnl >= 0 ? '+' : ''}${trade.pnl.toFixed(0)}`
          : 'X';
        const exitId = chart.createShape(
          { time: trade.exit_time, price: trade.exit_price },
          {
            shape:           isLong ? 'arrow_down' : 'arrow_up',
            lock:            true,
            disableSelection: false,
            overrides: {
              color:    exitColor,
              text:     pnlStr,
              fontsize: 10,
            },
          }
        );
        if (exitId) _tradeShapes.push(exitId);

        // ── Background rectangle for trade duration ──────────────────────
        const rectBg     = isLong ? 'rgba(38,166,154,0.07)' : 'rgba(239,83,80,0.07)';
        const rectBorder = isLong ? 'rgba(38,166,154,0.30)' : 'rgba(239,83,80,0.30)';
        const priceHi    = Math.max(trade.entry_price, trade.exit_price) + MES_TICK * 4;
        const priceLo    = Math.min(trade.entry_price, trade.exit_price) - MES_TICK * 4;

        const rectId = chart.createMultipointShape(
          [
            { time: trade.entry_time, price: priceLo },
            { time: trade.exit_time,  price: priceHi },
          ],
          {
            shape:           'rect',
            lock:            true,
            disableSelection: true,
            overrides: {
              backgroundColor: rectBg,
              borderColor:     rectBorder,
              borderWidth:     1,
            },
          }
        );
        if (rectId) _tradeShapes.push(rectId);
      }
    } catch (e) {
      console.debug('Trade marker draw error:', e);
    }
  });
}

function toggleTrades() {
  _showTrades = !_showTrades;
  document.getElementById('leg-trades')?.classList.toggle('sr-off', !_showTrades);

  if (!_showTrades) {
    if (_widget) {
      try {
        const chart = _widget.activeChart();
        _tradeShapes.forEach(id => { try { chart.removeEntity(id); } catch {} });
      } catch {}
    }
    _tradeShapes = [];
  } else {
    if (!_tradesLoaded) {
      initTradeMarkers();
    } else {
      // Re-fetch and redraw
      initTradeMarkers();
    }
  }
}

// ── WebSocket price feed ───────────────────────────────────────────────────────

function connectPriceFeed(datafeed) {
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

  } else if (msg.type === 'snapshot' && msg.bars_5min?.length > 0) {
    const latest = msg.bars_5min[msg.bars_5min.length - 1];
    updateTopbarOHLC(latest);
    updateWatchlistMES(latest.close);
    updateBidAsk(latest.close);
    _lastBar = latest;
    setWsStatus('live', 'Live');

  } else if (msg.type === 'order_update') {
    updateWorkingOrderRow(msg.order);
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

  if (_openPrice == null) _openPrice = bar.open;
  const chg    = bar.close - _openPrice;
  const chgPct = _openPrice > 0 ? (chg / _openPrice * 100) : 0;
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
  const bid = (lastPrice - MES_TICK).toFixed(2);
  const ask = (lastPrice + MES_TICK).toFixed(2);
  setText('bid-price', bid);
  setText('ask-price', ask);
  setText('bid-size', '—');
  setText('ask-size', '—');
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

// ── S/R Panel ─────────────────────────────────────────────────────────────────

function updateSRPanel(analysis) {
  const container = document.getElementById('sr-levels-list');
  if (!container) return;
  const sup = (analysis.support_levels    || []).slice(0, 3);
  const res = (analysis.resistance_levels || []).slice(0, 3);
  let html = '';
  [...res].reverse().forEach(l => {
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
  container.innerHTML = html ||
    '<span style="color:var(--text-faint);font-size:11px;grid-column:1/-1">No levels detected</span>';
}

// ── Chart Annotations ─────────────────────────────────────────────────────────

function updateAnnotations(analysis) {
  if (!_widget) return;
  let chart;
  try { chart = _widget.activeChart(); } catch { return; }

  _cycleShapes.forEach(id => { try { chart.removeEntity(id); } catch {} });
  _cycleShapes = [];
  (analysis.cycle_ranges || []).slice(-8).forEach(range => {
    const color = CYCLE_COLORS[range.type] || 'rgba(128,128,128,0.06)';
    try {
      const id = chart.createMultipointShape(
        [{ time: range.start_time, price: 0 }, { time: range.end_time, price: 0 }],
        { shape: 'rect', lock: true, disableSelection: true,
          overrides: { backgroundColor: color, borderColor: 'rgba(0,0,0,0)',
                       borderWidth: 0, showLabel: true, text: range.type,
                       textcolor: 'rgba(255,255,255,0.35)', fontsize: 10 } }
      );
      if (id) _cycleShapes.push(id);
    } catch {}
  });

  if (_showSupport) {
    _supportShapes.forEach(id => { try { chart.removeEntity(id); } catch {} });
    _supportShapes = [];
    (analysis.support_levels || []).forEach(l =>
      drawHLine(chart, l.price, '#26a69a', Math.min(l.touches, 3), _supportShapes));
  }
  if (_showResistance) {
    _resistanceShapes.forEach(id => { try { chart.removeEntity(id); } catch {} });
    _resistanceShapes = [];
    (analysis.resistance_levels || []).forEach(l =>
      drawHLine(chart, l.price, '#ef5350', Math.min(l.touches, 3), _resistanceShapes));
  }
}

function drawHLine(chart, price, color, width, shapeArr) {
  try {
    const id = chart.createShape(
      { price, time: 0 },
      { shape: 'horizontal_line', lock: true, disableSelection: true,
        overrides: { linecolor: color, linewidth: width, linestyle: 0,
                     showPrice: true, showLabel: true,
                     text: price.toFixed(2), textcolor: color, fontsize: 11 } }
    );
    if (id) shapeArr.push(id);
  } catch {}
}

// ── S/R Toggle ────────────────────────────────────────────────────────────────

function toggleSR(type) {
  if (!_widget) return;
  let chart;
  try { chart = _widget.activeChart(); } catch { return; }

  if (type === 'support') {
    _showSupport = !_showSupport;
    if (!_showSupport) {
      _supportShapes.forEach(id => { try { chart.removeEntity(id); } catch {} });
      _supportShapes = [];
    } else if (_lastAnalysis) {
      (_lastAnalysis.support_levels || []).forEach(l =>
        drawHLine(chart, l.price, '#26a69a', Math.min(l.touches, 3), _supportShapes));
    }
    document.getElementById('leg-support')?.classList.toggle('sr-off', !_showSupport);
  } else {
    _showResistance = !_showResistance;
    if (!_showResistance) {
      _resistanceShapes.forEach(id => { try { chart.removeEntity(id); } catch {} });
      _resistanceShapes = [];
    } else if (_lastAnalysis) {
      (_lastAnalysis.resistance_levels || []).forEach(l =>
        drawHLine(chart, l.price, '#ef5350', Math.min(l.touches, 3), _resistanceShapes));
    }
    document.getElementById('leg-resistance')?.classList.toggle('sr-off', !_showResistance);
  }
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

function adjustQty(delta) {
  const inp = document.getElementById('order-qty');
  if (!inp) return;
  inp.value = Math.max(1, Math.min(50, (parseInt(inp.value) || 1) + delta));
  updateSummary();
}

function updateSummary() {
  const qty  = parseInt(document.getElementById('order-qty')?.value) || 1;
  const type = document.getElementById('order-type')?.value;
  let price  = null;
  if (type === 'market') {
    const lastEl = document.getElementById('last-price');
    price = lastEl ? parseFloat(lastEl.textContent) : null;
  } else {
    price = parseFloat(document.getElementById('order-price')?.value);
  }
  const contractValue = (price && !isNaN(price)) ? (price * 5 * qty).toFixed(0) : '—';
  setText('sum-value',  contractValue !== '—' ? `$${parseInt(contractValue).toLocaleString()}` : '—');
  setText('sum-margin', `$${(MES_MARGIN * qty).toLocaleString()}`);
}

async function placeOrder() {
  const qty     = parseInt(document.getElementById('order-qty')?.value) || 1;
  const type    = document.getElementById('order-type')?.value || 'market';
  const tif     = document.getElementById('order-tif')?.value  || 'day';
  const limitPx = parseFloat(document.getElementById('order-price')?.value) || null;
  const stopPx  = parseFloat(document.getElementById('order-stop')?.value)  || null;

  const body = {
    action:      _orderSide.toUpperCase(),
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
      document.querySelector('.btab[data-pane="orders"]')?.click();
    } else {
      showToast(`Order failed: ${data.error}`, 'error');
    }
  } catch (e) {
    showToast(`Network error: ${e.message}`, 'error');
  } finally {
    if (btn) {
      btn.disabled    = false;
      btn.textContent = _orderSide === 'buy' ? 'BUY MES' : 'SELL MES';
    }
  }
}

// ── Working Orders Table ───────────────────────────────────────────────────────

function addWorkingOrderRow(order) {
  const tbody = document.getElementById('orders-tbody');
  if (!tbody) return;
  const empty = tbody.querySelector('tr td[colspan]');
  if (empty) empty.closest('tr').remove();
  const priceStr = order.lmt_price ? order.lmt_price.toFixed(2)
                 : order.stp_price ? `STP ${order.stp_price.toFixed(2)}`
                 : 'MKT';
  const sideClass = order.action === 'BUY' ? 'up' : 'down';
  const tr = document.createElement('tr');
  tr.id = `order-row-${order.order_id}`;
  tr.innerHTML = `
    <td>${new Date().toLocaleTimeString()}</td>
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

function updateWorkingOrderRow(order) {
  const statusEl = document.getElementById(`order-status-${order.order_id}`);
  if (statusEl) {
    statusEl.textContent = order.status;
    if (order.status === 'Filled') {
      statusEl.style.color = 'var(--green)';
      const btn = statusEl.closest('tr')?.querySelector('.cancel-btn');
      if (btn) btn.remove();
    } else if (['Cancelled', 'Inactive'].includes(order.status)) {
      statusEl.style.color = 'var(--text-faint)';
    }
  } else {
    addWorkingOrderRow(order);
  }
}

async function cancelOrder(orderId) {
  try {
    const res  = await fetch(`/api/order/${orderId}`, { method: 'DELETE' });
    const data = await res.json();
    if (!data.success) showToast('Cancel failed', 'error');
  } catch (e) {
    showToast(`Cancel error: ${e.message}`, 'error');
  }
}

// ── SR Legend Drag ────────────────────────────────────────────────────────────

function initSRLegendDrag() {
  const legend = document.getElementById('sr-legend');
  const handle = document.getElementById('sr-legend-handle');
  if (!legend || !handle) return;

  const saved = localStorage.getItem('srLegendPos');
  if (saved) {
    try {
      const { left, top } = JSON.parse(saved);
      legend.style.left = left;
      legend.style.top  = top;
    } catch {}
  }

  let dragging = false, startX = 0, startY = 0, startLeft = 0, startTop = 0;

  handle.addEventListener('mousedown', e => {
    dragging  = true;
    startX    = e.clientX;
    startY    = e.clientY;
    startLeft = parseInt(legend.style.left) || legend.offsetLeft;
    startTop  = parseInt(legend.style.top)  || legend.offsetTop;
    document.body.style.cursor = 'grabbing';
    e.preventDefault();
  });

  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const maxW = window.innerWidth  - legend.offsetWidth;
    const maxH = window.innerHeight - legend.offsetHeight;
    legend.style.left = Math.max(0, Math.min(maxW, startLeft + (e.clientX - startX))) + 'px';
    legend.style.top  = Math.max(0, Math.min(maxH, startTop  + (e.clientY - startY))) + 'px';
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = '';
    localStorage.setItem('srLegendPos', JSON.stringify({
      left: legend.style.left, top: legend.style.top,
    }));
  });
}

// ── Bottom Tabs ───────────────────────────────────────────────────────────────

function initBottomTabs() {
  document.querySelectorAll('.btab').forEach(tab => {
    tab.addEventListener('click', () => {
      const pane = tab.dataset.pane;
      document.querySelectorAll('.btab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.btab-pane').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById(`pane-${pane}`)?.classList.add('active');
    });
  });
}

// ── Bottom Panel Resize ───────────────────────────────────────────────────────

function initBottomResize() {
  const handle = document.getElementById('bottom-resize');
  const main   = document.getElementById('main');
  if (!handle || !main) return;
  let dragging = false, startY = 0, startH = 0;
  handle.addEventListener('mousedown', e => {
    dragging = true; startY = e.clientY;
    startH   = document.getElementById('bottom')?.offsetHeight || 180;
    document.body.style.cursor = 'row-resize';
    e.preventDefault();
  });
  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const newH = Math.max(100, Math.min(500, startH + (startY - e.clientY)));
    main.style.gridTemplateRows = `1fr ${newH}px`;
  });
  document.addEventListener('mouseup', () => { dragging = false; document.body.style.cursor = ''; });
}

// ── Toast Notification ────────────────────────────────────────────────────────

function showToast(message, type = 'info') {
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

// ── WebSocket Status ──────────────────────────────────────────────────────────

function setWsStatus(state, text) {
  const dot   = document.getElementById('ws-dot');
  const label = document.getElementById('ws-text');
  if (dot)   dot.className    = `status-dot ${state}`;
  if (label) label.textContent = text;
}

// ── Utility ───────────────────────────────────────────────────────────────────

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}
