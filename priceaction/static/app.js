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
const MES_MULTIPLIER = 5;  // Contract multiplier ($5 per point)

// Default bracket offsets (in ticks → converted to points internally)
let DEFAULT_TP_TICKS = 80;    // take-profit 80 ticks ($100) from entry
let DEFAULT_SL_TICKS = 80;    // stop-loss 80 ticks ($100) from entry

// Position polling interval
const POSITION_POLL_MS = 5000;

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
let _showSupport      = false;
let _showResistance   = false;

// Trade markers
let _tradeShapes  = [];
let _showTrades   = true;
let _tradesLoaded = false;

// RTH / ETH
window._rthMode = true;

// Right-click order price — updated via crossHairMoved
window._chartCursorPrice = null;

// Order line tracking — orderId → shape id on chart
let _orderLineShapes = {};

// Current active symbol (base name without _RTH suffix)
let _currentSymbol = 'MES';

// Position state
let _currentPosition = { symbol: 'MES', position: 0, avg_cost: 0, side: 'FLAT' };

// ── DOMContentLoaded ──────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  initChart();
  initBottomTabs();
  initBottomResize();
  initOrderForm();
  initBracketConfig();
  initSRLegendDrag();
  initPositionPolling();
  initWatchlistClick();
  fetchWatchlistPrices();
  fetchWatchlistContractInfo();
  // Refresh watchlist prices every 60s
  setInterval(fetchWatchlistPrices, 60000);
});

// ── Save/Load Adapter (TradingView chart layout persistence) ──────────────────

function createSaveLoadAdapter() {
  return {
    async getAllCharts() {
      const res = await fetch('/api/charts');
      return await res.json();
    },
    async removeChart(id) {
      await fetch(`/api/charts/${id}`, { method: 'DELETE' });
    },
    async saveChart(chartData) {
      const res = await fetch('/api/charts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: chartData.id,
          name: chartData.name,
          symbol: chartData.symbol,
          resolution: chartData.resolution,
          content: chartData.content,
          timestamp: Math.floor(Date.now() / 1000),
        }),
      });
      const data = await res.json();
      return data.id;
    },
    async getChartContent(chartId) {
      const res = await fetch(`/api/charts/${chartId}`);
      const data = await res.json();
      return data.content;
    },
    async getAllStudyTemplates() {
      const res = await fetch('/api/study_templates');
      return await res.json();
    },
    async removeStudyTemplate(info) {
      await fetch(`/api/study_templates/${encodeURIComponent(info.name)}`, { method: 'DELETE' });
    },
    async saveStudyTemplate(data) {
      await fetch('/api/study_templates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: data.name, content: data.content }),
      });
    },
    async getStudyTemplateContent(info) {
      const res = await fetch(`/api/study_templates/${encodeURIComponent(info.name)}`);
      const data = await res.json();
      return data.content;
    },
    async getDrawingTemplates(toolName) {
      const res = await fetch(`/api/drawing_templates/${encodeURIComponent(toolName)}`);
      return await res.json();
    },
    async loadDrawingTemplate(toolName, templateName) {
      const res = await fetch(`/api/drawing_templates/${encodeURIComponent(toolName)}/${encodeURIComponent(templateName)}`);
      const data = await res.json();
      return data.content;
    },
    async removeDrawingTemplate(toolName, templateName) {
      await fetch(`/api/drawing_templates/${encodeURIComponent(toolName)}/${encodeURIComponent(templateName)}`, { method: 'DELETE' });
    },
    async saveDrawingTemplate(toolName, templateName, content) {
      await fetch('/api/drawing_templates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool_name: toolName, template_name: templateName, content }),
      });
    },
    async getAllChartTemplates() {
      const res = await fetch('/api/chart_templates');
      return await res.json();
    },
    async saveChartTemplate(templateName, content) {
      await fetch('/api/chart_templates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: templateName, content }),
      });
    },
    async removeChartTemplate(templateName) {
      await fetch(`/api/chart_templates/${encodeURIComponent(templateName)}`, { method: 'DELETE' });
    },
    async getChartTemplateContent(templateName) {
      const res = await fetch(`/api/chart_templates/${encodeURIComponent(templateName)}`);
      return await res.json();
    },
  };
}

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
    symbol:       'MES_RTH',
    interval:     '5',
    library_path: '/charting_library/',
    locale:       'en',
    timezone:     'America/New_York',
    theme:        'dark',
    toolbar_bg:   '#1e222d',
    loading_screen: { backgroundColor: '#131722', foregroundColor: '#2962ff' },
    load_last_chart: true,
    save_load_adapter: createSaveLoadAdapter(),
    enabled_features: [
      'use_localstorage_for_settings',
      'move_logo_to_main_pane',
      'header_saveload',
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

    // ── S-Bar Count custom indicator (ported from Pine Script) ───────────
    custom_indicators_getter: function (PineJS) {
      return Promise.resolve([
        {
          name: 'S-Bar Count',
          metainfo: {
            _metainfoVersion: 51,
            id: 'SBarCount@tv-basicstudies-1',
            description: 'S-Bar Count',
            shortDescription: 'S-Bar Count',
            is_price_study: false,
            isCustomIndicator: true,
            format: { type: 'price', precision: 0 },
            plots: [{ id: 'plot_0', type: 'line' }],
            defaults: {
              styles: {
                plot_0: {
                  linestyle: 0,
                  visible: true,
                  linewidth: 1,
                  plottype: 5,       // columns
                  trackPrice: false,
                  color: 'rgba(20, 0, 0, 0.30)',
                  transparency: 0,
                }
              },
              inputs: { displayEvery: 3 }
            },
            styles: {
              plot_0: { title: 'Bar #', histogramBase: 0 }
            },
            inputs: [
              { id: 'displayEvery', name: 'Display every X bars', type: 'integer', defval: 3 },
            ],
          },
          constructor: function () {
            this.init = function (context, inputCallback) {
              this._context = context;
              this._input = inputCallback;
            };
            this.main = function (context, inputCallback) {
              this._context = context;
              this._input = inputCallback;

              var displayEvery = inputCallback(0);

              // Detect new day: dayofweek changes or first bar
              var dow = PineJS.Std.dayofweek(context);
              if (!this._prevDow) this._prevDow = context.new_var(NaN);
              var prevDow = this._prevDow.get(0);
              this._prevDow.set(dow);

              if (!this._barCount) this._barCount = context.new_var(0);
              var count = this._barCount.get(0);

              var isDaily = PineJS.Std.isdwm(context);
              if (isDaily) {
                // Daily: use day of month as count
                count = PineJS.Std.dayofmonth(context);
              } else if (isNaN(prevDow) || dow !== prevDow) {
                // New day: reset
                count = 1;
              } else {
                count = count + 1;
              }
              this._barCount.set(count);

              // Show at bar 1, then every displayEvery bars
              if (count === 1 || count % displayEvery === 0) {
                return [count];
              }
              return [NaN];
            };
          }
        }
      ]);
    },
  });

  _widget.onChartReady(() => {
    setWsStatus('live', 'Live');
    const chart = _widget.activeChart();

    // ── Right-click context menu for quick order placement ─────────────
    _widget.onContextMenu(function(unixTime, price) {
      return _buildContextMenuItems(price);
    });

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

    // Volume sub-pane — skip if already loaded from saved layout
    const existingStudies = chart.getAllStudies();
    const existingVolume = existingStudies.find(s => s.name === 'Volume');
    if (existingVolume) {
      _volumeStudyId = existingVolume.id;
    } else if (!_volumeStudyId) {
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

    // Bar Count sub-pane (disabled by default — uncomment to enable)
    // chart.createStudy('S-Bar Count', false, false).catch(() => {});

    // Load S/R analysis
    fetch(`/api/analysis?symbol=${_currentSymbol || 'MES'}`)
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

    // Load existing working orders
    loadWorkingOrders();
  });

  connectPriceFeed(datafeed);
}

async function loadWorkingOrders() {
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

// ── Right-click Context Menu ───────────────────────────────────────────────────

/**
 * Snap a price to the nearest MES tick (0.25).
 */
function snapToTick(price) {
  return Math.round(price / MES_TICK) * MES_TICK;
}

function _buildContextMenuItems(rawPrice) {
  if (rawPrice == null || isNaN(rawPrice)) return [];
  console.log('_buildContextMenuItems price:', rawPrice);

  const price     = snapToTick(rawPrice);
  const lastPrice = _lastBar ? _lastBar.close : price;
  const isAbove   = price >= lastPrice;
  const pStr      = price.toFixed(2);
  const qty       = parseInt(document.getElementById('order-qty')?.value) || 1;

  const items = [];
  try {
    items.push({ text: '-', position: 'top' });   // separator

    // ── Conditional orders based on position relative to last price ──────
    if (isAbove) {
      items.push({
        position: 'top',
        text: `Buy Stop  @ ${pStr}  (${qty} ct)`,
        click: () => showOrderConfirm('BUY', 'stop', null, price, qty),
      });
      items.push({
        position: 'top',
        text: `Sell Limit @ ${pStr}  (${qty} ct)`,
        click: () => showOrderConfirm('SELL', 'limit', price, null, qty),
      });
    } else {
      items.push({
        position: 'top',
        text: `Buy Limit  @ ${pStr}  (${qty} ct)`,
        click: () => showOrderConfirm('BUY', 'limit', price, null, qty),
      });
      items.push({
        position: 'top',
        text: `Sell Stop  @ ${pStr}  (${qty} ct)`,
        click: () => showOrderConfirm('SELL', 'stop', null, price, qty),
      });
    }

    items.push({ text: '-', position: 'top' });   // separator

    // ── Bracket orders (entry + TP + SL) ────────────────────────────────
    if (isAbove) {
      items.push({
        position: 'top',
        text: `Bracket Buy Stop  @ ${pStr}  (TP+SL)`,
        click: () => showBracketConfirm('BUY', 'stop', null, price, qty),
      });
    } else {
      items.push({
        position: 'top',
        text: `Bracket Buy Limit @ ${pStr}  (TP+SL)`,
        click: () => showBracketConfirm('BUY', 'limit', price, null, qty),
      });
    }
    if (!isAbove) {
      items.push({
        position: 'top',
        text: `Bracket Sell Stop  @ ${pStr}  (TP+SL)`,
        click: () => showBracketConfirm('SELL', 'stop', null, price, qty),
      });
    } else {
      items.push({
        position: 'top',
        text: `Bracket Sell Limit @ ${pStr}  (TP+SL)`,
        click: () => showBracketConfirm('SELL', 'limit', price, null, qty),
      });
    }

    items.push({ text: '-', position: 'top' });   // separator

    // ── Market orders always available ──────────────────────────────────
    items.push({
      position: 'top',
      text: `Market Buy  (${qty} ct)`,
      click: () => showOrderConfirm('BUY', 'market', null, null, qty),
    });
    items.push({
      position: 'top',
      text: `Market Sell  (${qty} ct)`,
      click: () => showOrderConfirm('SELL', 'market', null, null, qty),
    });

    items.push({ text: '-', position: 'top' });   // separator

    // ── Position management ─────────────────────────────────────────────
    if (_currentPosition.position !== 0) {
      const posLabel = `${_currentPosition.side} ${Math.abs(_currentPosition.position)}`;
      items.push({
        position: 'top',
        text: `⚡ Flatten Position (${posLabel})`,
        click: () => showFlattenConfirm(),
      });
    }

    // ── Cancel all ──────────────────────────────────────────────────────
    items.push({
      position: 'top',
      text: '✕ Cancel All Orders',
      click: () => showCancelAllConfirm(),
    });
  } catch (e) {
    console.warn('Context menu build error:', e);
    return [];
  }

  return items;
}

// ── Order Confirmation Dialog ─────────────────────────────────────────────────

function showOrderConfirm(action, orderType, limitPrice, stopPrice, qty) {
  const price     = limitPrice || stopPrice;
  const typeLabel = orderType === 'market' ? 'MARKET'
                  : orderType === 'limit'  ? `LIMIT @ ${price?.toFixed(2)}`
                  : orderType === 'stop'   ? `STOP @ ${price?.toFixed(2)}`
                  : 'STP LMT';
  const side      = action === 'BUY' ? 'buy' : 'sell';

  showConfirmDialog({
    title: `Confirm ${action} Order`,
    body:  `<div class="confirm-order-details">
              <div class="confirm-row"><span>Action</span><strong class="${side}">${action}</strong></div>
              <div class="confirm-row"><span>Type</span><strong>${typeLabel}</strong></div>
              <div class="confirm-row"><span>Quantity</span><strong>${qty} ct</strong></div>
              <div class="confirm-row"><span>Symbol</span><strong>MES</strong></div>
            </div>`,
    confirmClass: side,
    confirmText:  `${action} ${qty} MES`,
    onConfirm:    () => placeQuickOrder(action, orderType, limitPrice, stopPrice),
  });
}

function showBracketConfirm(action, orderType, limitPrice, stopPrice, qty) {
  const entryPrice = limitPrice || stopPrice;
  const isBuy      = action === 'BUY';
  const tpOffset   = DEFAULT_TP_TICKS * MES_TICK;
  const slOffset   = DEFAULT_SL_TICKS * MES_TICK;
  const tpDefault  = snapToTick(isBuy ? entryPrice + tpOffset : entryPrice - tpOffset);
  const slDefault  = snapToTick(isBuy ? entryPrice - slOffset : entryPrice + slOffset);
  const side       = isBuy ? 'buy' : 'sell';
  const typeLabel  = orderType === 'limit' ? `LIMIT @ ${entryPrice?.toFixed(2)}`
                   : `STOP @ ${entryPrice?.toFixed(2)}`;

  showConfirmDialog({
    title: `Confirm Bracket ${action}`,
    body:  `<div class="confirm-order-details">
              <div class="confirm-row"><span>Entry</span><strong class="${side}">${action} ${typeLabel}</strong></div>
              <div class="confirm-row"><span>Quantity</span><strong>${qty} ct</strong></div>
              <div class="confirm-row">
                <span>Take Profit</span>
                <input type="number" id="bracket-tp" class="confirm-input" value="${tpDefault.toFixed(2)}" step="0.25" />
              </div>
              <div class="confirm-row">
                <span>Stop Loss</span>
                <input type="number" id="bracket-sl" class="confirm-input" value="${slDefault.toFixed(2)}" step="0.25" />
              </div>
            </div>`,
    confirmClass: side,
    confirmText:  `${action} Bracket`,
    onConfirm:    () => {
      const tpRaw = parseFloat(document.getElementById('bracket-tp')?.value);
      const slRaw = parseFloat(document.getElementById('bracket-sl')?.value);
      if (isNaN(tpRaw) || tpRaw <= 0) {
        showToast('Invalid take-profit price', 'error');
        return;
      }
      if (isNaN(slRaw) || slRaw <= 0) {
        showToast('Invalid stop-loss price', 'error');
        return;
      }
      const tp = snapToTick(tpRaw);
      const sl = snapToTick(slRaw);
      placeBracketOrder(action, orderType, limitPrice, stopPrice, tp, sl);
    },
  });
}

function showFlattenConfirm() {
  const side = _currentPosition.side;
  const qty  = Math.abs(_currentPosition.position);
  showConfirmDialog({
    title: 'Flatten Position',
    body:  `<div class="confirm-order-details">
              <div class="confirm-row"><span>Current Position</span><strong>${side} ${qty} MES</strong></div>
              <div class="confirm-row"><span>Action</span><strong>Market Close All</strong></div>
            </div>
            <p style="color:var(--orange);font-size:11px;margin-top:8px">⚠ This will close your entire position at market.</p>`,
    confirmClass: 'sell',
    confirmText:  'Flatten Now',
    onConfirm:    () => flattenPosition(),
  });
}

function showCancelAllConfirm() {
  showConfirmDialog({
    title: 'Cancel All Orders',
    body:  `<p style="margin:12px 0">Cancel <strong>all</strong> working orders?</p>`,
    confirmClass: 'sell',
    confirmText:  'Cancel All',
    onConfirm:    () => cancelAllOrders(),
  });
}

/**
 * Generic confirmation dialog.
 * Options: { title, body (HTML), confirmClass, confirmText, onConfirm, onCancel? }
 */
function showConfirmDialog({ title, body, confirmClass, confirmText, onConfirm, onCancel }) {
  // Remove any existing dialog
  document.getElementById('order-confirm-overlay')?.remove();

  const overlay = document.createElement('div');
  overlay.id = 'order-confirm-overlay';
  overlay.innerHTML = `
    <div class="confirm-dialog">
      <div class="confirm-title">${title}</div>
      <div class="confirm-body">${body}</div>
      <div class="confirm-actions">
        <button class="confirm-btn cancel" id="confirm-cancel">Cancel</button>
        <button class="confirm-btn ${confirmClass}" id="confirm-ok">${confirmText}</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  // Focus the confirm button
  const okBtn     = document.getElementById('confirm-ok');
  const cancelBtn = document.getElementById('confirm-cancel');
  okBtn.focus();

  const close = () => overlay.remove();
  const dismiss = () => { close(); if (onCancel) onCancel(); };

  cancelBtn.addEventListener('click', dismiss);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) dismiss();
  });
  okBtn.addEventListener('click', () => {
    onConfirm();
    close();
  });

  // ESC to dismiss
  const onKey = (e) => {
    if (e.key === 'Escape') { dismiss(); document.removeEventListener('keydown', onKey); }
    if (e.key === 'Enter')  { onConfirm(); close(); document.removeEventListener('keydown', onKey); }
  };
  document.addEventListener('keydown', onKey);
}

async function placeQuickOrder(action, orderType, limitPrice, stopPrice) {
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

async function placeBracketOrder(action, orderType, limitPrice, stopPrice, tpPrice, slPrice) {
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

async function cancelAllOrders() {
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

async function flattenPosition() {
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

// ── Visual Order Lines on Chart ───────────────────────────────────────────────

function drawOrderLine(order) {
  if (!_widget) return;
  const price = order.lmt_price || order.stp_price;
  if (!price) return;  // Market orders don't get lines

  let chart;
  try { chart = _widget.activeChart(); } catch { return; }

  // Remove existing line for this order if any
  if (_orderLineShapes[order.order_id]) {
    try { _orderLineShapes[order.order_id].remove(); } catch {}
    delete _orderLineShapes[order.order_id];
  }

  const isBuy   = order.action === 'BUY';
  const color   = isBuy ? '#26a69a' : '#ef5350';
  const bgColor = isBuy ? 'rgba(38,166,154,0.15)' : 'rgba(239,83,80,0.15)';
  const label   = `#${order.order_id} ${order.action} ${order.quantity}`;
  const isStop  = order.order_type === 'STP' || order.order_type === 'stop';

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

async function modifyOrderPrice(orderId, orderType, newPrice) {
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

function removeOrderLine(orderId) {
  if (!_widget) return;
  const line = _orderLineShapes[orderId];
  if (!line) return;

  try { line.remove(); } catch {}
  delete _orderLineShapes[orderId];
}

function clearAllOrderLines() {
  if (!_widget) return;
  for (const [oid, line] of Object.entries(_orderLineShapes)) {
    try { line.remove(); } catch {}
  }
  _orderLineShapes = {};
}

// ── Position Polling ──────────────────────────────────────────────────────────

function initPositionPolling() {
  fetchPosition();
  setInterval(fetchPosition, POSITION_POLL_MS);
}

async function fetchPosition() {
  try {
    const res = await fetch('/api/position');
    _currentPosition = await res.json();
    updatePositionPanel();
  } catch {}
}

function updatePositionPanel() {
  const tbody = document.getElementById('pos-tbody');
  if (!tbody) return;

  if (_currentPosition.position === 0) {
    tbody.innerHTML = '<tr><td colspan="8"><div class="empty-table">No open positions</div></td></tr>';
    return;
  }

  const lastPrice = _lastBar ? _lastBar.close : 0;
  const avgPrice  = _currentPosition.avg_cost / MES_MULTIPLIER;
  const qty       = Math.abs(_currentPosition.position);
  const unrealPnl = lastPrice > 0 ? (lastPrice - avgPrice) * _currentPosition.position * MES_MULTIPLIER : 0;
  const pnlClass  = unrealPnl >= 0 ? 'up' : 'down';
  const value     = lastPrice > 0 ? (lastPrice * MES_MULTIPLIER * qty) : 0;

  tbody.innerHTML = `
    <tr>
      <td>MES</td>
      <td class="${_currentPosition.side === 'LONG' ? 'up' : 'down'}">${_currentPosition.side}</td>
      <td>${qty}</td>
      <td>${avgPrice.toFixed(2)}</td>
      <td>${lastPrice > 0 ? lastPrice.toFixed(2) : '—'}</td>
      <td class="${pnlClass}">${unrealPnl >= 0 ? '+' : ''}$${unrealPnl.toFixed(2)}</td>
      <td>—</td>
      <td>$${value.toLocaleString()}</td>
    </tr>
  `;
}

// ── RTH / ETH Toggle ──────────────────────────────────────────────────────────

function toggleRTH() {
  window._rthMode = !window._rthMode;
  const btn = document.getElementById('rth-btn');
  if (btn) {
    btn.textContent = window._rthMode ? 'RTH' : 'ETH';
    btn.classList.toggle('rth-active', window._rthMode);
  }
  console.log('[RTH/ETH] toggled → rthMode =', window._rthMode);
  if (_widget) {
    try {
      const sym = window._rthMode ? `${_currentSymbol}_RTH` : _currentSymbol;
      const chart = _widget.activeChart();
      const currentRes = chart.resolution();
      console.log('[RTH/ETH] → new symbol:', sym);
      _widget.setSymbol(sym, currentRes, () => {
        console.log('[RTH/ETH] symbol change completed to', sym);
      });
    } catch (e) {
      console.warn('[RTH/ETH] setSymbol error:', e);
    }
  } else {
    console.warn('[RTH/ETH] _widget is null, cannot toggle');
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

function initWatchlistClick() {
  document.querySelectorAll('.watch-item').forEach(item => {
    item.addEventListener('click', () => {
      const sym = item.dataset.symbol;
      if (!sym || !_widget) return;
      // Update active state
      document.querySelectorAll('.watch-item').forEach(i => i.classList.remove('active'));
      item.classList.add('active');
      // Switch chart symbol
      _currentSymbol = sym;
      const chartSym = window._rthMode ? `${sym}_RTH` : sym;
      try {
        const res = _widget.activeChart().resolution();
        _widget.setSymbol(chartSym, res, () => {
          console.log('[Watchlist] switched to', chartSym);
          // Reload S/R analysis for new symbol
          fetch(`/api/analysis?symbol=${sym}`)
            .then(r => r.json())
            .then(analysis => {
              _lastAnalysis = analysis;
              updateAnnotations(analysis);
              updateCycleBadge(analysis.market_cycle);
              updateSRPanel(analysis);
            })
            .catch(e => console.warn('Analysis fetch error:', e));
        });
      } catch (e) {
        console.warn('[Watchlist] setSymbol error:', e);
      }
    });
  });
}

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

async function fetchWatchlistPrices() {
  try {
    const res = await fetch('/api/watchlist_prices');
    const data = await res.json();
    for (const [sym, info] of Object.entries(data)) {
      if (sym === 'MES') continue; // MES updated via WebSocket
      const key = sym.toLowerCase();
      const priceEl = document.getElementById(`wl-${key}-price`);
      const chgEl   = document.getElementById(`wl-${key}-chg`);
      if (priceEl) priceEl.textContent = info.close != null ? info.close.toFixed(2) : '—';
      if (chgEl && info.change_pct != null) {
        chgEl.textContent = `${info.change_pct >= 0 ? '+' : ''}${info.change_pct.toFixed(2)}%`;
        chgEl.className   = `watch-change ${info.change_pct >= 0 ? 'up' : 'down'}`;
      }
    }
  } catch (e) {
    console.warn('fetchWatchlistPrices error:', e);
  }
}

async function fetchWatchlistContractInfo() {
  const symbols = ['MES', 'MNQ', 'NK225MC', 'MGC'];
  for (const sym of symbols) {
    try {
      const res = await fetch(`/api/symbols?symbol=${sym}`);
      const info = await res.json();
      const key = sym.toLowerCase();
      const exchEl = document.getElementById(`wl-${key}-exch`);
      if (exchEl) {
        const ibSym = info.ib_symbol || sym;
        const exch  = info.exchange || info.listed_exchange || '';
        // Show: "CME · MESM6" style but we only have the root symbol, use ib_symbol
        exchEl.textContent = ibSym !== sym ? `${exch} · ${ibSym}` : exch;
      }
    } catch (e) {
      console.warn('fetchWatchlistContractInfo error for', sym, e);
    }
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

let _bracketMode = 'dollar';  // 'ticks' or 'dollar'

function initBracketConfig() {
  const tpInput = document.getElementById('bracket-tp-val');
  const slInput = document.getElementById('bracket-sl-val');
  if (!tpInput || !slInput) return;

  // Load from localStorage
  const savedTp   = localStorage.getItem('bracket_tp_ticks');
  const savedSl   = localStorage.getItem('bracket_sl_ticks');
  const savedMode = localStorage.getItem('bracket_mode');
  if (savedTp) DEFAULT_TP_TICKS = parseInt(savedTp);
  if (savedSl) DEFAULT_SL_TICKS = parseInt(savedSl);
  if (savedMode === 'dollar' || savedMode === 'ticks') _bracketMode = savedMode;

  _applyBracketMode();

  tpInput.addEventListener('change', () => {
    _onBracketInput('tp', tpInput);
  });
  slInput.addEventListener('change', () => {
    _onBracketInput('sl', slInput);
  });
}

function setBracketMode(mode) {
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
    tpInput.value = DEFAULT_TP_TICKS;
    slInput.value = DEFAULT_SL_TICKS;
    tpInput.step = '1';
    slInput.step = '1';
    if (tpUnit) tpUnit.textContent = 'ticks';
    if (slUnit) slUnit.textContent = 'ticks';
  } else {
    tpInput.value = (DEFAULT_TP_TICKS * MES_TICK_$).toFixed(2);
    slInput.value = (DEFAULT_SL_TICKS * MES_TICK_$).toFixed(2);
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
    DEFAULT_TP_TICKS = ticks;
    localStorage.setItem('bracket_tp_ticks', ticks);
  } else {
    DEFAULT_SL_TICKS = ticks;
    localStorage.setItem('bracket_sl_ticks', ticks);
  }
  updateBracketSummary();
}

function updateBracketSummary() {
  const tpPts = (DEFAULT_TP_TICKS * MES_TICK).toFixed(2);
  const slPts = (DEFAULT_SL_TICKS * MES_TICK).toFixed(2);
  const tpDol = (DEFAULT_TP_TICKS * MES_TICK_$).toFixed(2);
  const slDol = (DEFAULT_SL_TICKS * MES_TICK_$).toFixed(2);
  if (_bracketMode === 'ticks') {
    setText('bracket-tp-pts', `${tpPts} pts / $${tpDol}`);
    setText('bracket-sl-pts', `${slPts} pts / $${slDol}`);
  } else {
    setText('bracket-tp-pts', `${DEFAULT_TP_TICKS} ticks / ${tpPts} pts`);
    setText('bracket-sl-pts', `${DEFAULT_SL_TICKS} ticks / ${slPts} pts`);
  }
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
  const contractValue = (price && !isNaN(price)) ? (price * MES_MULTIPLIER * qty).toFixed(0) : '—';
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
      btn.textContent = _orderSide === 'buy' ? 'BUY MES' : 'SELL MES';
    }
  }
}

// ── Working Orders Table ───────────────────────────────────────────────────────

const _TERMINAL_STATUSES = ['Filled', 'Cancelled', 'Inactive', 'ApiCancelled'];

function addWorkingOrderRow(order) {
  // Only show active (non-terminal) orders in Working Orders
  if (_TERMINAL_STATUSES.includes(order.status)) return;
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

function updateWorkingOrderRow(order) {
  const isTerminal = _TERMINAL_STATUSES.includes(order.status);

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

function addFilledOrderRow(order) {
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

function addOrderHistoryRow(order) {
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
                             : _TERMINAL_STATUSES.includes(order.status) ? 'var(--text-faint)'
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
                    : _TERMINAL_STATUSES.includes(order.status) ? 'var(--text-faint)'
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
  const main = document.getElementById('main');
  const minBtn = document.getElementById('bottom-minimize');

  document.querySelectorAll('.btab').forEach(tab => {
    tab.addEventListener('click', () => {
      const pane = tab.dataset.pane;
      // If minimized, restore on tab click
      if (main.classList.contains('bottom-minimized')) {
        main.classList.remove('bottom-minimized');
        if (minBtn) { minBtn.textContent = '▼'; minBtn.title = 'Minimize'; }
      }
      document.querySelectorAll('.btab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.btab-pane').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById(`pane-${pane}`)?.classList.add('active');
    });
  });

  if (minBtn) {
    minBtn.addEventListener('click', () => {
      const minimized = main.classList.toggle('bottom-minimized');
      minBtn.textContent = minimized ? '▲' : '▼';
      minBtn.title = minimized ? 'Restore' : 'Minimize';
    });
  }
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
    document.body.style.userSelect = 'none';
    // Block pointer events on iframes so mousemove isn't swallowed
    document.querySelectorAll('iframe').forEach(f => f.style.pointerEvents = 'none');
    e.preventDefault();
  });
  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const newH = Math.max(100, Math.min(500, startH + (startY - e.clientY)));
    main.style.gridTemplateRows = `1fr ${newH}px`;
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    document.querySelectorAll('iframe').forEach(f => f.style.pointerEvents = '');
  });
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
