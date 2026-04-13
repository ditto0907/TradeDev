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

// Trade markers  — per-file management
let _tradeFiles     = {};   // { filename: { trades: [], shown: false, expanded: true } }
let _tradeShapesByFile = {};  // { filename: [shapes] }
let _showTrades     = false;  // global chart visibility (legend toggle)

// Market cycle analysis
let _mcAnalyses     = [];   // full records from backend
let _mcShapes       = {};   // analysis_id → [entity_id, ...]

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
  initStrategyTab();
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
      // Strip transient shapes (execution arrows, programmatic trend lines) from
      // the chart layout before saving. These are redrawn from live data on every
      // page load, so persisting them causes stale duplicates on reload.
      let content = chartData.content;
      try {
        const layout = JSON.parse(content);
        if (layout.charts) {
          layout.charts.forEach(chart => {
            (chart.panes || []).forEach(pane => {
              if (pane.sources) {
                pane.sources = pane.sources.filter(
                  s => s.type !== 'LineToolFlagMark' && s.type !== 'LineToolTrendLine'
                );
              }
            });
          });
          content = JSON.stringify(layout);
        }
      } catch (e) {
        console.warn('[SaveChart] Failed to strip transient shapes:', e);
      }
      const res = await fetch('/api/charts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: chartData.id,
          name: chartData.name,
          symbol: chartData.symbol,
          resolution: chartData.resolution,
          content,
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

  datafeed.setCycleAnalysisCallback(handleCycleAnalysisWS);

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
    load_last_chart: true,
    save_load_adapter: createSaveLoadAdapter(),
    enabled_features: [
      'use_localstorage_for_settings',
      'move_logo_to_main_pane',
      'header_saveload',
      'show_exchange_logos',
      'pre_post_market_sessions',
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

    // Load trade file list (don't show on chart by default)
    loadTradeFileList();

    // Load existing working orders
    loadWorkingOrders();

    // Sync _currentSymbol with the chart's actual symbol (may differ from default
    // when load_last_chart restores a previous session)
    try { _currentSymbol = chart.symbol(); } catch {}

    // Reload analyses (and redraw active ones) whenever the chart symbol changes
    chart.onSymbolChanged().subscribe(null, () => {
      try { _currentSymbol = chart.symbol(); } catch {}
      loadCycleAnalyses();
    });

    // Load market cycle analyses for the current symbol
    loadCycleAnalyses();
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

// ── Trade Markers — Per-file Management ───────────────────────────────────────

async function loadTradeFileList() {
  try {
    const res = await fetch('/api/trades/files');
    const files = await res.json();
    // Load file list and eagerly fetch trades for each file
    const fetches = files.map(async f => {
      if (!_tradeFiles[f.name]) {
        _tradeFiles[f.name] = { trades: null, shown: false, expanded: true };
      }
      if (!_tradeFiles[f.name].trades) {
        await loadTradesForFile(f.name);
      }
    });
    await Promise.all(fetches);
    renderTradeTable();
  } catch (e) {
    console.warn('Trade file list load error:', e);
  }
}

function toggleFileExpand(filename) {
  const entry = _tradeFiles[filename];
  if (!entry) return;
  entry.expanded = !entry.expanded;
  renderTradeTable();
}

async function loadTradesForFile(filename) {
  try {
    const res = await fetch(`/api/trades/file/${encodeURIComponent(filename)}`);
    const trades = await res.json();
    if (_tradeFiles[filename]) {
      _tradeFiles[filename].trades = trades;
    }
    return trades;
  } catch (e) {
    console.warn(`Trade file load error (${filename}):`, e);
    return [];
  }
}

async function toggleFileOnChart(filename) {
  const entry = _tradeFiles[filename];
  if (!entry) return;
  entry.shown = !entry.shown;

  if (entry.shown) {
    // Load trades if not yet loaded
    if (!entry.trades) await loadTradesForFile(filename);
    drawTradeMarkersForFile(filename, entry.trades || []);
    // Ensure global toggle is on
    _showTrades = true;
    document.getElementById('leg-trades')?.classList.remove('sr-off');
  } else {
    clearTradeShapesForFile(filename);
    // Check if any file is still shown
    const anyShown = Object.values(_tradeFiles).some(f => f.shown);
    if (!anyShown) {
      _showTrades = false;
      document.getElementById('leg-trades')?.classList.add('sr-off');
    }
  }
  _updateTradeCount();
  renderTradeTable();
}

async function deleteTradeFile(filename) {
  try {
    await fetch(`/api/trades/file/${encodeURIComponent(filename)}`, { method: 'DELETE' });
    clearTradeShapesForFile(filename);
    delete _tradeFiles[filename];
    delete _tradeShapesByFile[filename];
    const anyShown = Object.values(_tradeFiles).some(f => f.shown);
    if (!anyShown) {
      _showTrades = false;
      document.getElementById('leg-trades')?.classList.add('sr-off');
    }
    _updateTradeCount();
    renderTradeTable();
    console.log(`[Trades] Deleted file: ${filename}`);
  } catch (e) {
    console.warn(`Trade file delete error (${filename}):`, e);
  }
}

async function handleTradeCSVUpload(input) {
  const file = input.files?.[0];
  if (!file) return;
  try {
    const formData = new FormData();
    formData.append('file', file);
    const res = await fetch('/api/trades/upload', { method: 'POST', body: formData });
    const data = await res.json();
    if (!data.trades?.length) {
      alert('No filled trades found in CSV.');
      return;
    }
    const filename = data.filename;
    _tradeFiles[filename] = { trades: data.trades, shown: true, expanded: true };
    drawTradeMarkersForFile(filename, data.trades);
    _showTrades = true;
    document.getElementById('leg-trades')?.classList.remove('sr-off');
    _updateTradeCount();
    renderTradeTable();
    console.log(`[Trades] Uploaded ${data.trades.length} trades from ${filename}`);
  } catch (e) {
    console.warn('Trade CSV upload error:', e);
    alert('Failed to parse CSV file.');
  }
  input.value = '';
}

function clearTradeShapesForFile(filename) {
  const shapes = _tradeShapesByFile[filename];
  if (!shapes || !_widget) return;
  let chart;
  try { chart = _widget.activeChart(); } catch { return; }
  shapes.forEach(s => {
    try {
      if (s.type === 'exec') { s.obj.remove(); }
      else if (s.type === 'entity') { chart.removeEntity(s.id); }
    } catch {}
  });
  _tradeShapesByFile[filename] = [];
}

function drawTradeMarkersForFile(filename, trades) {
  if (!_widget) return;
  let chart;
  try { chart = _widget.activeChart(); } catch { return; }

  // Clear existing shapes for this file
  clearTradeShapesForFile(filename);
  _tradeShapesByFile[filename] = [];

  if (!trades.length) return;

  trades.forEach(trade => {
    try {
      const isLong     = trade.direction === 'long';
      const entryDir   = isLong ? 'buy' : 'sell';
      const entryColor = isLong ? '#26a69a' : '#ef5350';

      const entryExec = chart.createExecutionShape()
        .setTime(trade.entry_time)
        .setPrice(trade.entry_price)
        .setDirection(entryDir)
        .setText(`${entryDir === 'buy' ? 'B' : 'S'}${trade.qty}@${trade.entry_price.toFixed(2)}`)
        .setArrowColor(entryColor)
        .setTextColor(entryColor)
        .setArrowHeight(14)
        .setFont('bold 11px Arial');
      _tradeShapesByFile[filename].push({ type: 'exec', obj: entryExec });

      if (trade.exit_time != null && trade.exit_price != null) {
        const exitDir   = isLong ? 'sell' : 'buy';
        const exitColor = isLong ? '#ef5350' : '#26a69a';
        const pnlStr    = trade.pnl != null
          ? ` (${trade.pnl >= 0 ? '+' : ''}$${trade.pnl.toFixed(0)})`
          : '';

        const exitExec = chart.createExecutionShape()
          .setTime(trade.exit_time)
          .setPrice(trade.exit_price)
          .setDirection(exitDir)
          .setText(`${exitDir === 'buy' ? 'B' : 'S'}${trade.qty}@${trade.exit_price.toFixed(2)}${pnlStr}`)
          .setArrowColor(exitColor)
          .setTextColor(exitColor)
          .setArrowHeight(14)
          .setFont('bold 11px Arial');
        _tradeShapesByFile[filename].push({ type: 'exec', obj: exitExec });

        const lineColor = isLong ? 'rgba(38,166,154,0.50)' : 'rgba(239,83,80,0.50)';
        const lineId = chart.createMultipointShape(
          [
            { time: trade.entry_time,  price: trade.entry_price },
            { time: trade.exit_time,   price: trade.exit_price },
          ],
          {
            shape:            'trend_line',
            disableSelection: true,
            disableSave:      true,
            overrides: {
              linecolor:  lineColor,
              linewidth:  2,
              linestyle:  2,
              showLabel:  false,
            },
          }
        );
        if (lineId) _tradeShapesByFile[filename].push({ type: 'entity', id: lineId });
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
    // Hide all files from chart
    Object.keys(_tradeFiles).forEach(fn => {
      if (_tradeFiles[fn].shown) {
        _tradeFiles[fn].shown = false;
        clearTradeShapesForFile(fn);
      }
    });
  } else {
    // Show all files that have loaded trades
    Object.keys(_tradeFiles).forEach(async fn => {
      const entry = _tradeFiles[fn];
      if (!entry.trades) await loadTradesForFile(fn);
      entry.shown = true;
      drawTradeMarkersForFile(fn, entry.trades || []);
    });
  }
  _updateTradeCount();
  renderTradeTable();
}

function _updateTradeCount() {
  const countEl = document.getElementById('trade-count');
  if (!countEl) return;
  let total = 0;
  Object.values(_tradeFiles).forEach(f => {
    if (f.shown && f.trades) total += f.trades.length;
  });
  countEl.textContent = total ? `${total}` : '';
}

function locateTradeOnChart(entryTime, exitTime) {
  if (!_widget) return;
  let chart;
  try { chart = _widget.activeChart(); } catch { return; }
  // Show 30min padding on each side
  const from = entryTime - 1800;
  const to = (exitTime || entryTime) + 1800;
  chart.setVisibleRange({ from, to });
}

function renderTradeTable() {
  const tbody = document.getElementById('history-tbody');
  if (!tbody) return;
  const info = document.getElementById('trade-panel-info');
  const filenames = Object.keys(_tradeFiles).sort();

  if (!filenames.length) {
    tbody.innerHTML = '<tr><td colspan="7"><div class="empty-table">No trade logs — upload a CSV to view</div></td></tr>';
    if (info) info.textContent = '';
    return;
  }

  // Compute global stats
  let totalPnl = 0, wins = 0, losses = 0, totalCount = 0, totalWinAmt = 0, totalLossAmt = 0;
  filenames.forEach(fn => {
    const f = _tradeFiles[fn];
    if (f.trades) {
      totalCount += f.trades.length;
      f.trades.forEach(t => {
        if (t.pnl != null) {
          totalPnl += t.pnl;
          if (t.pnl >= 0) { wins++; totalWinAmt += t.pnl; }
          else { losses++; totalLossAmt += Math.abs(t.pnl); }
        }
      });
    }
  });
  if (info) {
    if (totalCount > 0) {
      const wr = (wins + losses) > 0 ? ((wins / (wins + losses)) * 100).toFixed(0) : '—';
      const avgWin = wins > 0 ? totalWinAmt / wins : 0;
      const avgLoss = losses > 0 ? totalLossAmt / losses : 0;
      const rr = avgLoss > 0 ? (avgWin / avgLoss).toFixed(2) : '—';
      info.innerHTML = `<span style="color:var(--text-dim)">${totalCount} trades</span>` +
        ` &nbsp;|&nbsp; <span style="color:${totalPnl >= 0 ? 'var(--green)' : 'var(--red)'}">P&L: ${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(0)}</span>` +
        ` &nbsp;|&nbsp; WR: ${wr}% (${wins}W ${losses}L)` +
        ` &nbsp;|&nbsp; RR: ${rr}`;
    } else {
      info.textContent = `${filenames.length} file(s)`;
    }
  }

  let html = '';
  filenames.forEach(fn => {
    const f = _tradeFiles[fn];
    const isShown = f.shown;
    const isExpanded = f.expanded !== false;
    const count = f.trades ? f.trades.length : '—';

    // Per-file stats
    let fPnl = 0, fWins = 0, fLosses = 0, fWinAmt = 0, fLossAmt = 0;
    if (f.trades) {
      f.trades.forEach(t => {
        if (t.pnl != null) {
          fPnl += t.pnl;
          if (t.pnl >= 0) { fWins++; fWinAmt += t.pnl; } else { fLosses++; fLossAmt += Math.abs(t.pnl); }
        }
      });
    }
    const fWr = (fWins + fLosses) > 0 ? ((fWins / (fWins + fLosses)) * 100).toFixed(0) : '—';
    const fAvgWin = fWins > 0 ? fWinAmt / fWins : 0;
    const fAvgLoss = fLosses > 0 ? fLossAmt / fLosses : 0;
    const fRr = fAvgLoss > 0 ? (fAvgWin / fAvgLoss).toFixed(2) : '—';
    const fStatsHtml = f.trades && f.trades.length
      ? `<span style="color:var(--text-dim)">${count} trades</span>` +
        ` &nbsp;|&nbsp; <span style="color:${fPnl >= 0 ? 'var(--green)' : 'var(--red)'}">P&L: ${fPnl >= 0 ? '+' : ''}$${fPnl.toFixed(0)}</span>` +
        ` &nbsp;|&nbsp; <span style="color:var(--text-dim)">WR: ${fWr}%</span>` +
        ` &nbsp;|&nbsp; <span style="color:var(--text-dim)">RR: ${fRr}</span>`
      : '';

    const eyeIcon = isShown
      ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>'
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
    const chevron = isExpanded ? '▾' : '▸';

    // File group header row
    html += `<tr class="trade-file-header" style="background:var(--panel);border-bottom:1px solid var(--border)">
      <td colspan="7" style="padding:5px 8px">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="cursor:pointer;font-size:12px;opacity:0.6;user-select:none;flex-shrink:0" onclick="toggleFileExpand('${fn.replace(/'/g, "\\'")}')">${chevron}</span>
          <span style="cursor:pointer;display:inline-flex;align-items:center;opacity:0.7;flex-shrink:0" onclick="toggleFileOnChart('${fn.replace(/'/g, "\\'")}')" title="${isShown ? 'Hide from chart' : 'Show on chart'}">${eyeIcon}</span>
          <span style="font-size:12px;font-weight:500;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:160px;max-width:280px">${fn}</span>
          <span style="font-size:11px;white-space:nowrap;flex-shrink:0;margin-left:12px">${fStatsHtml}</span>
          <span style="margin-left:auto;cursor:pointer;display:inline-flex;align-items:center;opacity:0.5;flex-shrink:0" onclick="if(confirm('Delete ${fn.replace(/'/g, "\\'")}?'))deleteTradeFile('${fn.replace(/'/g, "\\'")}')" title="Delete file">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
          </span>
        </div>
      </td>
    </tr>`;

    // Trade rows — always show when expanded (independent of chart visibility)
    if (isExpanded && f.trades && f.trades.length) {
      f.trades.forEach(t => {
        const side = t.direction === 'long' ? 'BUY' : 'SELL';
        const sideClass = t.direction === 'long' ? 'up' : 'down';
        const dt = t.entry_time ? new Date(t.entry_time * 1000) : null;
        const dateStr = dt ? `${dt.toLocaleDateString()} ${dt.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}` : '—';
        const pnlStr = t.pnl != null ? `${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(0)}` : '—';
        const pnlClass = t.pnl != null ? (t.pnl >= 0 ? 'up' : 'down') : '';
        const locateBtn = t.entry_time
          ? `<span style="cursor:pointer;opacity:0.5;margin-left:4px;display:inline-flex;vertical-align:middle" onclick="locateTradeOnChart(${t.entry_time},${t.exit_time || t.entry_time})" title="Locate on chart"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/></svg></span>`
          : '';
        html += `<tr>
          <td>${dateStr}${locateBtn}</td>
          <td>${t.symbol || 'MES'}</td>
          <td class="${sideClass}">${side}</td>
          <td>${t.qty || 1}</td>
          <td>${t.entry_price != null ? t.entry_price.toFixed(2) : '—'}</td>
          <td>${t.exit_price != null ? t.exit_price.toFixed(2) : '—'}</td>
          <td class="${pnlClass}">${pnlStr}</td>
        </tr>`;
      });
    } else if (isExpanded && (!f.trades || !f.trades.length)) {
      html += `<tr><td colspan="7" style="text-align:center;color:var(--text-dim);font-size:11px;padding:4px">Loading...</td></tr>`;
    }
  });

  tbody.innerHTML = html;
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
    const barSymbol = msg.symbol || 'MES';
    updateWatchlistMES(msg.bar.close);
    if (barSymbol === (_currentSymbol || 'MES')) {
      updateTopbarOHLC(msg.bar);
      updateBidAsk(msg.bar.close);
      _lastBar = msg.bar;
    }

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
  // cycle_analysis messages are handled by handleCycleAnalysisWS via datafeed callback
}

// ── Topbar OHLC (disabled - topbar simplified) ───────────────────────────────

function updateTopbarOHLC(bar) {
  // Topbar no longer displays symbol-specific data
  // Keeping function for compatibility but removing DOM updates
  if (_openPrice == null) _openPrice = bar.open;
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
      // Switch chart symbol (no longer use _RTH suffix)
      _currentSymbol = sym;
      try {
        const res = _widget.activeChart().resolution();
        _widget.setSymbol(sym, res, () => {
          console.log('[Watchlist] switched to', sym);
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
          // Reload market cycle analyses for new symbol
          // (onSymbolChanged also fires but calling here ensures _currentSymbol is already updated)
          loadCycleAnalyses();
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

// ── Cycle Badge (disabled - topbar simplified) ───────────────────────────────

function updateCycleBadge(cycle) {
  // Cycle badge removed from topbar
  // Keeping function for compatibility
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

// ── Market Cycle Annotations ──────────────────────────────────────────────────

function renderCycleAnnotations(analysis) {
  if (!_widget) return;
  let chart;
  try { chart = _widget.activeChart(); } catch { return; }
  
  // Check if analysis is for current symbol
  const currentSym = _widget.symbolInterval().symbol;
  if (!currentSym.includes(analysis.symbol)) {
    console.log('[Cycle] Analysis for', analysis.symbol, 'but chart shows', currentSym);
    return;
  }
  
  // Clear previous annotations
  _cycleShapes.forEach(id => { try { chart.removeEntity(id); } catch {} });
  _cycleShapes = [];
  
  if (!analysis.annotations || !analysis.annotations.length) {
    console.log('[Cycle] No annotations to render');
    return;
  }
  
  // Extract bar_to as default end time for trend lines
  const defaultEndTime = analysis.bar_to || analysis.annotations[0]?.start_time || Date.now() / 1000;
  
  console.log('[Cycle] Rendering', analysis.annotations.length, 'annotations');
  
  // Color mapping based on label names (from SKILL.md color palette)
  const colorMap = {
    'opening range': 'rgba(33,150,243,0.15)',
    'bear': 'rgba(244,67,54,0.15)',
    'bull': 'rgba(76,175,80,0.15)',
    'reversal': 'rgba(255,152,0,0.15)',
    'trading range': 'rgba(158,158,158,0.12)',
    'ttr': 'rgba(158,158,158,0.12)',
    'tight trading range': 'rgba(158,158,158,0.12)',
    'channel': 'rgba(156,39,176,0.15)',
    'measured move': 'rgba(0,188,212,0.15)',
    'mm': 'rgba(0,188,212,0.15)',
    'climax': 'rgba(183,28,28,0.2)',
  };
  
  const lineColorMap = {
    'bear': '#f44336',
    'bull': '#4caf50',
    'reversal': '#ff9800',
    'support': '#26a69a',
    'resistance': '#ef5350',
    'mm': '#00bcd4',
  };
  
  analysis.annotations.forEach(ann => {
    try {
      if (ann.type === 'range') {
        // Rectangle shape
        const labelLower = ann.label.toLowerCase();
        let color = ann.color || 'rgba(158,158,158,0.12)';
        // Auto-select color based on label if not specified
        for (const [key, val] of Object.entries(colorMap)) {
          if (labelLower.includes(key)) {
            color = val;
            break;
          }
        }
        
        const id = chart.createShape(
          { time: ann.start_time, price: ann.price_low },
          {
            shape: 'rectangle',
            lock: false,
            disableSelection: false,
            overrides: {
              color: color,
              transparency: 85,
              borderColor: color.replace('0.15', '0.4').replace('0.12', '0.4').replace('0.2', '0.5'),
              borderWidth: 1,
              extendLeft: false,
              extendRight: false,
              showLabel: true,
              text: ann.label,
              textcolor: '#fff',
              fontsize: 11,
            },
          }
        );
        if (id) {
          chart.setEntityPoints(id, [
            { time: ann.start_time, price: ann.price_high },
            { time: ann.end_time, price: ann.price_low },
          ]);
          _cycleShapes.push(id);
        }
        
      } else if (ann.type === 'hline') {
        // Trend line (horizontal S/R level extending to end of analysis period)
        const labelLower = ann.label.toLowerCase();
        let lineColor = '#888';
        for (const [key, val] of Object.entries(lineColorMap)) {
          if (labelLower.includes(key)) {
            lineColor = val;
            break;
          }
        }
        
        const lineStyle = ann.style === 'dashed' ? 1 : ann.style === 'dotted' ? 2 : 0;
        const endTime = ann.end_time || defaultEndTime;
        
        const id = chart.createShape(
          { time: ann.start_time, price: ann.price },
          {
            shape: 'trend_line',
            lock: false,
            disableSelection: false,
            overrides: {
              linecolor: lineColor,
              linewidth: 1,
              linestyle: lineStyle,
              showLabel: true,
              text: ann.label,
              textcolor: lineColor,
              fontsize: 10,
              horzLabelsAlign: 'right',
              vertLabelsAlign: 'bottom',
            },
          }
        );
        
        if (id) {
          // Set second point (horizontal line at same price)
          chart.setEntityPoints(id, [
            { time: ann.start_time, price: ann.price },
            { time: endTime, price: ann.price }
          ]);
          _cycleShapes.push(id);
        }
        
      } else if (ann.type === 'label') {
        // Text label
        const labelLower = ann.label.toLowerCase();
        let textColor = '#fff';
        let bgColor = '#666';
        for (const [key, val] of Object.entries(lineColorMap)) {
          if (labelLower.includes(key)) {
            bgColor = val;
            break;
          }
        }
        
        const id = chart.createShape(
          { time: ann.start_time, price: ann.price },
          {
            shape: 'text',
            lock: false,
            disableSelection: false,
            zOrder: 'top',
            overrides: {
              text: ann.label,
              fontsize: 12,
              color: textColor,
              backgroundColor: bgColor,
              borderColor: bgColor,
              transparency: 20,
              bold: true,
            },
          }
        );
        if (id) _cycleShapes.push(id);
      }
    } catch (e) {
      console.error('[Cycle] Failed to render annotation:', ann, e);
    }
  });
  
  console.log('[Cycle] Rendered', _cycleShapes.length, 'shapes');
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
    // Use last bar close price instead of topbar element
    price = _lastBar ? _lastBar.close : null;
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
        setTimeout(() => { if (_widget) _widget.resize(); }, 50);
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
      if (minimized) {
        // Save current inline grid size and clear it so CSS class takes effect
        main.dataset.bottomGrid = main.style.gridTemplateRows || '';
        main.style.gridTemplateRows = '';
      } else {
        // Restore previously saved inline grid size
        if (main.dataset.bottomGrid) {
          main.style.gridTemplateRows = main.dataset.bottomGrid;
        }
      }
      setTimeout(() => { if (_widget) _widget.resize(); }, 50);
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

// ── Market Cycle Analysis ─────────────────────────────────────────────────────

const MC_COLORS = {
  'Opening Range':          { bg: 'rgba(33,150,243,0.12)',  border: 'rgba(33,150,243,0.4)',  text: '#2196F3' },
  'Bear Leg':               { bg: 'rgba(239,83,80,0.12)',   border: 'rgba(239,83,80,0.4)',   text: '#ef5350' },
  'Bull Leg':               { bg: 'rgba(38,166,154,0.12)',  border: 'rgba(38,166,154,0.4)',  text: '#26a69a' },
  'Bull Breakout':          { bg: 'rgba(38,166,154,0.18)',  border: 'rgba(38,166,154,0.5)',  text: '#26a69a' },
  'Bear Breakout':          { bg: 'rgba(239,83,80,0.18)',   border: 'rgba(239,83,80,0.5)',   text: '#ef5350' },
  'Reversal / Double Bottom':{ bg: 'rgba(255,152,0,0.12)', border: 'rgba(255,152,0,0.4)',   text: '#ff9800' },
  'Reversal / Double Top':  { bg: 'rgba(255,152,0,0.12)',  border: 'rgba(255,152,0,0.4)',   text: '#ff9800' },
  'Trading Range':          { bg: 'rgba(128,128,128,0.08)', border: 'rgba(128,128,128,0.3)', text: '#9e9e9e' },
  'Tight Trading Range':    { bg: 'rgba(128,128,128,0.06)', border: 'rgba(128,128,128,0.2)', text: '#9e9e9e' },
  'Channel':                { bg: 'rgba(156,39,176,0.10)',  border: 'rgba(156,39,176,0.3)',  text: '#9c27b0' },
  'Measured Move':          { bg: 'rgba(0,188,212,0.10)',   border: 'rgba(0,188,212,0.3)',   text: '#00bcd4' },
  'Climax':                 { bg: 'rgba(244,67,54,0.15)',   border: 'rgba(244,67,54,0.5)',   text: '#f44336' },
};

const MC_DEFAULTS = { bg: 'rgba(100,181,246,0.10)', border: 'rgba(100,181,246,0.3)', text: '#64b5f6' };

function _mcColor(label) {
  return MC_COLORS[label] || MC_DEFAULTS;
}

async function loadCycleAnalyses() {
  try {
    const sym = _currentSymbol || 'MES';
    const res = await fetch(`/api/skill/analyses?active_only=false&symbol=${encodeURIComponent(sym)}`);
    _mcAnalyses = await res.json();
    renderAnalysisTable();
    drawAllActiveAnalyses();
  } catch (e) {
    console.warn('loadCycleAnalyses error:', e);
  }
}

function drawAllActiveAnalyses() {
  if (!_widget) return;
  let chart;
  try { chart = _widget.activeChart(); } catch { return; }

  // Clear all existing analysis shapes
  for (const [id, shapes] of Object.entries(_mcShapes)) {
    shapes.forEach(sid => { try { chart.removeEntity(sid); } catch {} });
  }
  _mcShapes = {};

  // Draw active analyses for the current symbol only
  const sym = _currentSymbol || 'MES';
  _mcAnalyses.filter(a => a.active && (!a.symbol || a.symbol === sym)).forEach(a => drawOneAnalysis(chart, a));
}

function drawOneAnalysis(chart, analysis) {
  const shapes = [];
  (analysis.annotations || []).forEach(ann => {
    try {
      if (ann.type === 'range' && ann.start_time && ann.end_time) {
        const c = _mcColor(ann.label);
        const id = chart.createMultipointShape(
          [{ time: ann.start_time, price: ann.price_low || 0 },
           { time: ann.end_time,   price: ann.price_high || 0 }],
          { shape: 'rectangle', lock: false, disableSelection: false, disableSave: true,
            zOrder: 'top',
            overrides: {
              backgroundColor: ann.color || c.bg,
              color: c.border, linewidth: 1,
              fillBackground: true, transparency: 20,
              showLabel: true, text: ann.label,
              textColor: c.text, fontSize: 10,
              extendLeft: false, extendRight: false,
              vertLabelsAlign: /^bear/i.test(ann.label) ? 'bottom' : 'top',
            } }
        );
        if (id) shapes.push(id);
      } else if (ann.type === 'hline' && ann.price != null) {
        const c = _mcColor(ann.label);
        const lineWidth = Number.isFinite(ann.linewidth) ? ann.linewidth : 1;
        const id = chart.createShape(
          { price: ann.price, time: ann.start_time || 0 },
          { shape: 'horizontal_line', lock: false, disableSelection: false, disableSave: true,
            zOrder: 'top',
            overrides: {
              linecolor: ann.color || c.text,
              linewidth: lineWidth,
              linestyle: ann.style === 'dashed' ? 2 : ann.style === 'dotted' ? 1 : 0,
              showPrice: true, showLabel: true,
              text: `${ann.label} ${ann.price.toFixed(2)}`,
              textcolor: ann.color || c.text, fontsize: 10,
            } }
        );
        if (id) shapes.push(id);
      } else if (ann.type === 'trend line' && ann.start_time && ann.end_time && ann.price_start != null && ann.price_end != null) {
        const c = _mcColor(ann.label);
        const id = chart.createMultipointShape(
          [{ time: ann.start_time, price: ann.price_start },
           { time: ann.end_time,   price: ann.price_end }],
          { shape: 'trend_line', lock: false, disableSelection: false, disableSave: true,
            zOrder: 'top',
            overrides: {
              linecolor: ann.color || c.text,
              linewidth: ann.linewidth || 2,
              linestyle: ann.style === 'dashed' ? 2 : ann.style === 'dotted' ? 1 : 0,
              extendLeft: false, extendRight: false,
            } }
        );
        if (id) shapes.push(id);
      } else if (ann.type === 'label' && ann.start_time && ann.price != null) {
        const c = _mcColor(ann.label);
        const id = chart.createShape(
          { time: ann.start_time, price: ann.price },
          { shape: 'text', lock: false, disableSelection: false, disableSave: true,
            zOrder: 'top',
            overrides: {
              text: ann.label,
              color: ann.color || c.text,
              fontsize: 11,
              bold: true,
            } }
        );
        if (id) shapes.push(id);
      }
    } catch (e) {
      console.warn('drawOneAnalysis annotation error:', e, ann);
    }
  });
  _mcShapes[analysis.id] = shapes;
}

function removeOneAnalysis(chart, analysisId) {
  const shapes = _mcShapes[analysisId] || [];
  shapes.forEach(sid => { try { chart.removeEntity(sid); } catch {} });
  delete _mcShapes[analysisId];
}

function handleCycleAnalysisWS(msg) {
  if (msg.type === 'cycle_analysis') {
    // New analysis arrived
    _mcAnalyses.unshift(msg.analysis);
    renderAnalysisTable();
    if (msg.analysis.active && _widget) {
      try {
        const chart = _widget.activeChart();
        drawOneAnalysis(chart, msg.analysis);
        chart.selectLineTool('cursor');
      } catch {}
    }
  } else if (msg.type === 'cycle_analysis_toggle') {
    const rec = _mcAnalyses.find(a => a.id === msg.id);
    if (rec) {
      rec.active = msg.active ? 1 : 0;
      renderAnalysisTable();
      if (!_widget) return;
      let chart;
      try { chart = _widget.activeChart(); } catch { return; }
      if (rec.active) {
        drawOneAnalysis(chart, rec);
      } else {
        removeOneAnalysis(chart, rec.id);
      }
    }
  } else if (msg.type === 'cycle_analysis_delete') {
    if (_widget) {
      try { removeOneAnalysis(_widget.activeChart(), msg.id); } catch {}
    }
    _mcAnalyses = _mcAnalyses.filter(a => a.id !== msg.id);
    renderAnalysisTable();
  }
}

async function toggleAnalysisActive(id) {
  const rec = _mcAnalyses.find(a => a.id === id);
  if (!rec) return;
  const newActive = !rec.active;
  try {
    await fetch(`/api/skill/analyses/${id}/active?active=${newActive}`, { method: 'PUT' });
  } catch (e) {
    console.warn('toggleAnalysisActive error:', e);
  }
}

async function deleteAnalysis(id) {
  try {
    await fetch(`/api/skill/analyses/${id}`, { method: 'DELETE' });
  } catch (e) {
    console.warn('deleteAnalysis error:', e);
  }
}

// Format analysis time period (bar_from - bar_to) to readable string
function _formatAnalysisPeriod(bar_from, bar_to) {
  if (!bar_from || !bar_to) return '';
  
  const etOptions = { 
    timeZone: 'America/New_York',
    year: 'numeric', 
    month: '2-digit', 
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false
  };
  
  const fromDate = new Date(bar_from * 1000);
  const toDate = new Date(bar_to * 1000);
  
  const fromStr = fromDate.toLocaleString('en-US', etOptions);
  const toStr = toDate.toLocaleString('en-US', etOptions);
  
  // Extract date and time parts
  const [fromDatePart, fromTimePart] = fromStr.split(', ');
  const [toDatePart, toTimePart] = toStr.split(', ');
  
  // Format date as YYYY-MM-DD
  const [fromMonth, fromDay, fromYear] = fromDatePart.split('/');
  const [toMonth, toDay, toYear] = toDatePart.split('/');
  const fromFormatted = `${fromYear}-${fromMonth}-${fromDay}`;
  const toFormatted = `${toYear}-${toMonth}-${toDay}`;
  
  // Same day: "2026-04-08 09:30-11:00"
  if (fromFormatted === toFormatted) {
    return `${fromFormatted} ${fromTimePart}-${toTimePart}`;
  }
  
  // Different days: "2026-04-08 09:30 - 2026-04-09 11:00"
  return `${fromFormatted} ${fromTimePart} - ${toFormatted} ${toTimePart}`;
}

function _formatSummaryHTML(text) {
  if (!text) return '<span style="color:var(--text-faint)">No summary</span>';
  const esc = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const lines = esc.split('\n');
  let html = '';
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (/^#{1,3}\s/.test(trimmed)) {
      html += `<div class="mc-heading">${trimmed.replace(/^#+\s*/, '')}</div>`;
    } else if (/^[•\-\*]\s/.test(trimmed)) {
      let content = trimmed.replace(/^[•\-\*]\s*/, '');
      const colonIdx = content.indexOf(':');
      if (colonIdx > 0 && colonIdx < 30) {
        const key = content.substring(0, colonIdx);
        let val = content.substring(colonIdx + 1).trim();
        const lval = val.toLowerCase();
        let cls = '';
        if (lval.startsWith('bull')) cls = 'mc-val-bull';
        else if (lval.startsWith('bear')) cls = 'mc-val-bear';
        content = `<span class="mc-key">${key}:</span> ${cls ? `<span class="${cls}">${val}</span>` : val}`;
      }
      html += `<div class="mc-bullet">${content}</div>`;
    } else {
      html += `<div class="mc-line">${trimmed}</div>`;
    }
  }
  return html;
}

function showSummaryModal(id) {
  const rec = _mcAnalyses.find(a => a.id === id);
  if (!rec) return;
  const overlay = document.getElementById('mc-modal-overlay');
  const modal = overlay.querySelector('.mc-modal');
  const title = document.getElementById('mc-modal-title');
  const body = document.getElementById('mc-modal-body');
  const label = [rec.symbol, rec.timeframe ? rec.timeframe + 'min' : '', rec.session].filter(Boolean).join(' · ');
  const period = _formatAnalysisPeriod(rec.bar_from, rec.bar_to);
  title.textContent = `Analysis — ${label} ${period}`;
  body.innerHTML = _formatSummaryHTML(rec.summary);
  // Reset position to center
  modal.style.transform = '';
  modal.dataset.dx = '0';
  modal.dataset.dy = '0';
  overlay.classList.add('open');
}

// ── Modal Drag ────────────────────────────────────────────────────────────────
(function initModalDrag() {
  let dragging = false, startX = 0, startY = 0, dx = 0, dy = 0;
  document.addEventListener('mousedown', e => {
    const header = e.target.closest('.mc-modal-header');
    if (!header || e.target.closest('.mc-modal-close')) return;
    const modal = header.closest('.mc-modal');
    dragging = true;
    startX = e.clientX;
    startY = e.clientY;
    dx = parseFloat(modal.dataset.dx) || 0;
    dy = parseFloat(modal.dataset.dy) || 0;
    e.preventDefault();
  });
  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const modal = document.querySelector('.mc-modal');
    if (!modal) return;
    const newDx = dx + (e.clientX - startX);
    const newDy = dy + (e.clientY - startY);
    modal.style.transform = `translate(${newDx}px, ${newDy}px)`;
    modal.dataset.dx = newDx;
    modal.dataset.dy = newDy;
  });
  document.addEventListener('mouseup', () => { dragging = false; });
})();

function closeSummaryModal() {
  document.getElementById('mc-modal-overlay')?.classList.remove('open');
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeSummaryModal();
});

function renderAnalysisTable() {
  const tbody = document.getElementById('analysis-tbody');
  if (!tbody) return;

  if (!_mcAnalyses.length) {
    tbody.innerHTML = '<tr><td colspan="8"><div class="empty-table">No market cycle analyses</div></td></tr>';
    return;
  }

  tbody.innerHTML = _mcAnalyses.map(a => {
    const period = _formatAnalysisPeriod(a.bar_from, a.bar_to);
    const created = a.created_at ? a.created_at.replace('T', ' ').substring(0, 16) : '';
    const annCount = (a.annotations || []).length;
    const activeClass = a.active ? 'mc-active' : 'mc-inactive';
    const toggleIcon = a.active
      ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>'
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
    const toggleTitle = a.active ? 'Hide from chart' : 'Show on chart';
    // Create short summary preview
    const summary = (a.summary || '').length > 60
      ? a.summary.substring(0, 60) + '…'
      : (a.summary || '—');

    return `<tr class="${activeClass}">
      <td>${created}</td>
      <td>${a.symbol || ''}</td>
      <td>${period}</td>
      <td>${a.timeframe || ''}</td>
      <td>${a.session || ''}</td>
      <td class="mc-summary-cell" onclick="showSummaryModal(${a.id})" title="Click to view full summary">${summary}</td>
      <td>${annCount}</td>
      <td class="mc-actions">
        <span class="mc-btn" onclick="toggleAnalysisActive(${a.id})" title="${toggleTitle}">${toggleIcon}</span>
        <span class="mc-btn mc-del" onclick="deleteAnalysis(${a.id})" title="Delete">✕</span>
      </td>
    </tr>`;
  }).join('');
}


// ── Strategy Backtest Tab ──────────────────────────────────────────────────────

let _stratCurrentId    = null;   // currently loaded backtest id
let _stratMarkerShapes = [];     // chart execution shapes for current backtest
let _stratShowMarkers  = false;
let _stratShowFiltered = true;   // show SR-filtered trades on chart & in summary
let _stratBacktestList = [];     // cached list from server
let _stratCurrentTrades = [];    // trades from the current backtest run

const STRAT_TS_MAX = 9999999999;   // sentinel: no upper timestamp bound

function initStrategyTab() {
  // Set default date range: last 60 days
  const now = new Date();
  const past = new Date(now.getTime() - 60 * 24 * 3600 * 1000);
  const fmt = d => d.toISOString().slice(0, 10);
  const fromEl = document.getElementById('strat-from');
  const toEl   = document.getElementById('strat-to');
  if (fromEl) fromEl.value = fmt(past);
  if (toEl)   toEl.value   = fmt(now);

  _loadBacktestList();
}

async function _loadBacktestList() {
  try {
    const res = await fetch('/api/strategy/backtests');
    if (!res.ok) return;
    _stratBacktestList = await res.json();
    _renderBacktestHistorySelect();
  } catch (e) {
    console.warn('[Strategy] Failed to load backtest list:', e);
  }
}

function _renderBacktestHistorySelect() {
  const sel = document.getElementById('strat-history-select');
  if (!sel) return;
  const cur = _stratCurrentId;
  // Keep placeholder
  sel.innerHTML = '<option value="">— select run —</option>';
  for (const bt of _stratBacktestList) {
    const s   = bt.summary || {};
    const p   = bt.params  || {};
    const dt  = (bt.created_at || '').slice(0, 16).replace('T', ' ');
    const wr  = s.win_rate != null ? (s.win_rate * 100).toFixed(0) + '%' : '?';
    const pnlSign = (s.total_pnl ?? 0) >= 0 ? '+' : '';
    const pnl = s.total_pnl != null ? `${pnlSign}$${s.total_pnl.toFixed(0)}` : '';
    const ibsPct = ((p.ibs_threshold || 0.7) * 100).toFixed(0);
    const lbl = `${dt}  ${p.symbol || ''}/${p.timeframe || ''}  IBS${ibsPct}%  ${s.total || 0}T ${wr} ${pnl}`;
    const opt = document.createElement('option');
    opt.value = bt.id;
    opt.textContent = lbl;
    if (bt.id === cur) opt.selected = true;
    sel.appendChild(opt);
  }
}

async function runStrategyBacktest() {
  const btn = document.getElementById('strat-run-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Running…'; }

  try {
    const ibsPct = parseFloat(document.getElementById('strat-ibs')?.value || '70') / 100;
    const ctx    = document.getElementById('strat-ctx')?.checked ?? true;
    const maxStop = parseFloat(document.getElementById('strat-maxstop')?.value || '200');
    const fromEl = document.getElementById('strat-from');
    const toEl   = document.getElementById('strat-to');
    const session = document.getElementById('strat-session')?.value || 'all';
    const timeFilter = document.getElementById('strat-time-filter')?.value || '';

    const from_ts = fromEl?.value ? Math.floor(new Date(fromEl.value).getTime() / 1000) : 0;
    const to_ts   = toEl?.value   ? Math.floor(new Date(toEl.value + 'T23:59:59').getTime() / 1000) : STRAT_TS_MAX;

    const res = await fetch('/api/strategy/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        symbol: 'MES', timeframe: '5min',
        from_ts, to_ts,
        ibs_threshold: ibsPct,
        use_context_filter: ctx,
        rr_ratio: 1.0,
        max_stop_loss: maxStop,
        session: session,
        time_filter: timeFilter,
        include_filtered: true,
      }),
    });

    if (!res.ok) {
      const err = await res.json();
      alert('Backtest error: ' + (err.error || res.status));
      return;
    }

    const data = await res.json();
    _stratCurrentId = data.backtest_id;
    _stratCurrentTrades = data.trades || [];
    _renderStrategySummary(data.summary, _stratShowFiltered);
    _renderStrategyTrades(_stratCurrentTrades, _stratShowFiltered);
    if (_stratShowMarkers) _drawBacktestMarkers(_stratCurrentTrades, _stratShowFiltered);

    // Reload history list and select current
    await _loadBacktestList();
    const sel = document.getElementById('strat-history-select');
    if (sel && _stratCurrentId) sel.value = _stratCurrentId;

  } catch (e) {
    console.error('[Strategy] runStrategyBacktest error:', e);
    alert('Backtest failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '▶ Run Backtest'; }
  }
}

async function loadBacktestHistory(backtest_id) {
  if (!backtest_id) {
    _stratCurrentId = null;
    _stratCurrentTrades = [];
    _clearStrategySummary();
    _clearStrategyTrades();
    _clearBacktestMarkers();
    return;
  }

  try {
    const res = await fetch(`/api/strategy/backtests/${encodeURIComponent(backtest_id)}/trades`);
    if (!res.ok) { alert('Failed to load backtest trades.'); return; }
    const data = await res.json();

    // Find summary from cached list
    const bt = _stratBacktestList.find(b => b.id === backtest_id);
    _stratCurrentId = backtest_id;
    _stratCurrentTrades = data.trades || [];
    _renderStrategySummary(bt?.summary || {}, _stratShowFiltered);
    _renderStrategyTrades(_stratCurrentTrades, _stratShowFiltered);
    if (_stratShowMarkers) _drawBacktestMarkers(_stratCurrentTrades, _stratShowFiltered);
  } catch (e) {
    console.error('[Strategy] loadBacktestHistory error:', e);
  }
}

async function deleteCurrentBacktest() {
  const sel = document.getElementById('strat-history-select');
  const id  = sel?.value;
  if (!id) return;
  if (!confirm('Delete this backtest run and all its trades?')) return;

  try {
    const res = await fetch(`/api/strategy/backtests/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (!res.ok) { alert('Delete failed.'); return; }
    if (_stratCurrentId === id) {
      _stratCurrentId = null;
      _stratCurrentTrades = [];
      _clearStrategySummary();
      _clearStrategyTrades();
      _clearBacktestMarkers();
    }
    await _loadBacktestList();
  } catch (e) {
    console.error('[Strategy] deleteCurrentBacktest error:', e);
  }
}

function toggleBacktestMarkers(show) {
  _stratShowMarkers = show;
  if (!show) {
    _clearBacktestMarkers();
    return;
  }
  // Redraw from current trades
  if (_stratCurrentTrades.length) {
    _drawBacktestMarkers(_stratCurrentTrades, _stratShowFiltered);
    return;
  }
  // Fallback: re-fetch and draw
  const tbody = document.getElementById('strat-trades-tbody');
  if (!tbody || !_stratCurrentId) return;
  fetch(`/api/strategy/backtests/${encodeURIComponent(_stratCurrentId)}/trades`)
    .then(r => r.json())
    .then(d => {
      _stratCurrentTrades = d.trades || [];
      _drawBacktestMarkers(_stratCurrentTrades, _stratShowFiltered);
    })
    .catch(e => console.warn('[Strategy] toggleBacktestMarkers error:', e));
}

function toggleFilteredDisplay(show) {
  _stratShowFiltered = show;
  // Re-render trades table and markers with filtered visibility
  if (_stratCurrentTrades.length) {
    _renderStrategyTrades(_stratCurrentTrades, _stratShowFiltered);
    // Recompute summary from trades when toggling filtered display
    _recomputeSummary(_stratCurrentTrades, _stratShowFiltered);
    if (_stratShowMarkers) _drawBacktestMarkers(_stratCurrentTrades, _stratShowFiltered);
  }
}

// ── Summary rendering ─────────────────────────────────────────────────────────

function _renderStrategySummary(s, showFiltered) {
  const el = document.getElementById('strategy-summary');
  if (el) el.style.display = '';

  const set = (id, val, cls) => {
    const span = document.getElementById(id);
    if (!span) return;
    span.textContent = val;
    span.className = cls || '';
  };

  set('ss-total',   s.total ?? '—');
  set('ss-winrate', s.win_rate != null ? (s.win_rate * 100).toFixed(1) + '%' : '—',
      s.win_rate >= 0.5 ? 'up' : 'dn');
  const pnlStr = s.total_pnl != null ? (s.total_pnl >= 0 ? '+' : '') + '$' + s.total_pnl.toFixed(2) : '—';
  set('ss-pnl',     pnlStr, s.total_pnl >= 0 ? 'up' : 'dn');
  set('ss-avgwin',  s.avg_win  != null ? '+$' + s.avg_win.toFixed(2)  : '—', 'up');
  set('ss-avgloss', s.avg_loss != null ? '$'  + s.avg_loss.toFixed(2) : '—', 'dn');
  set('ss-pf',      s.profit_factor != null ? s.profit_factor.toFixed(2) : '—',
      s.profit_factor >= 1 ? 'up' : 'dn');
  set('ss-dd',      s.max_drawdown != null ? '$' + Math.abs(s.max_drawdown).toFixed(2) : '—', 'dn');
  set('ss-filtered',s.filtered_count ?? '—');
  set('ss-bars',    s.bars_used ?? '—');
}

function _recomputeSummary(trades, showFiltered) {
  // Recompute summary from trade list for the current filtered display mode
  const executed = trades.filter(t => t.context_pass === 1);
  const filteredAll = trades.filter(t => t.context_pass === 0);
  const closed = executed.filter(t => t.outcome === 'win' || t.outcome === 'loss');
  const wins = closed.filter(t => t.outcome === 'win');
  const losses = closed.filter(t => t.outcome === 'loss');

  const totalPnl = closed.reduce((s, t) => s + (t.pnl || 0), 0);
  const grossWin = wins.reduce((s, t) => s + (t.pnl || 0), 0);
  const grossLoss = Math.abs(losses.reduce((s, t) => s + (t.pnl || 0), 0));

  const summary = {
    total: executed.length,
    wins: wins.length,
    losses: losses.length,
    win_rate: closed.length ? wins.length / closed.length : 0,
    total_pnl: totalPnl,
    avg_win: wins.length ? grossWin / wins.length : 0,
    avg_loss: losses.length ? -grossLoss / losses.length : 0,
    profit_factor: grossLoss > 0 ? grossWin / grossLoss : (grossWin > 0 ? 999 : 0),
    max_drawdown: 0,
    filtered_count: filteredAll.length,
    bars_used: '—',
  };

  // Max drawdown
  let running = 0, peak = 0, maxDD = 0;
  for (const t of closed) {
    running += (t.pnl || 0);
    if (running > peak) peak = running;
    const dd = peak - running;
    if (dd > maxDD) maxDD = dd;
  }
  summary.max_drawdown = -maxDD;

  _renderStrategySummary(summary, showFiltered);
}

function _clearStrategySummary() {
  const el = document.getElementById('strategy-summary');
  if (el) el.style.display = 'none';
}

// ── Trade table rendering ─────────────────────────────────────────────────────

function _renderStrategyTrades(trades, showFiltered) {
  const tbody = document.getElementById('strat-trades-tbody');
  if (!tbody) return;

  // Filter visible trades based on showFiltered toggle
  const visible = showFiltered ? trades : (trades || []).filter(t => t.context_pass === 1);

  if (!visible || !visible.length) {
    tbody.innerHTML = '<tr><td colspan="12"><div class="empty-table">No trades</div></td></tr>';
    return;
  }

  tbody.innerHTML = visible.map((t, idx) => {
    const isFiltered = t.context_pass === 0;
    const rowClass   = isFiltered ? 'bt-filtered'
                     : t.outcome === 'win'  ? 'bt-win'
                     : t.outcome === 'loss' ? 'bt-loss'
                     : 'bt-open';

    const entryDt = t.entry_time ? new Date(t.entry_time * 1000).toLocaleString([], {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
    }) : '—';

    const dirArrow = t.direction === 'long'
      ? '<span style="color:#26a69a">↑ Long</span>'
      : '<span style="color:#ef5350">↓ Short</span>';

    const pnlStr = t.pnl != null
      ? `<span class="${t.pnl >= 0 ? 'up' : 'down'}">${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)}</span>`
      : '—';
    const outcomeStr = isFiltered ? '<span style="opacity:.5">filtered</span>'
      : t.outcome === 'win'  ? '<span class="up">Win</span>'
      : t.outcome === 'loss' ? '<span class="down">Loss</span>'
      : '<span style="color:#64b5f6">Open</span>';

    const ctxStr = isFiltered
      ? `<span style="color:#ff9800" title="${t.context_reason || ''}">⛔ ${t.context_reason || 'blocked'}</span>`
      : '<span style="color:#26a69a">✓</span>';

    const locateBtn = `<button class="mc-btn" onclick="stratLocateTrade(${t.entry_time})" title="Scroll chart to trade" style="font-size:11px;padding:1px 6px;background:none;border:1px solid var(--border);border-radius:3px;cursor:pointer">Locate</button>`;

    return `<tr class="${rowClass}" data-trade-idx="${idx}">
      <td>${entryDt}</td>
      <td>${dirArrow}</td>
      <td>${t.contracts ?? 1}</td>
      <td>${t.entry_price?.toFixed(2) ?? '—'}</td>
      <td>${t.exit_price != null ? t.exit_price.toFixed(2) : '—'}</td>
      <td>${t.stop_price?.toFixed(2) ?? '—'}</td>
      <td>${t.target_price?.toFixed(2) ?? '—'}</td>
      <td>${(t.signal_ibs * 100).toFixed(1)}%</td>
      <td>${outcomeStr}</td>
      <td>${pnlStr}</td>
      <td>${ctxStr}</td>
      <td>${locateBtn}</td>
    </tr>`;
  }).join('');
}

function _clearStrategyTrades() {
  const tbody = document.getElementById('strat-trades-tbody');
  if (tbody) tbody.innerHTML = '<tr><td colspan="12"><div class="empty-table">Run a backtest to see results</div></td></tr>';
}

// ── Locate trade on chart ─────────────────────────────────────────────────────

function stratLocateTrade(entry_time) {
  if (!_widget || !entry_time) return;
  try {
    const chart = _widget.activeChart();
    chart.setVisibleRange({
      from: entry_time - 60 * 30,    // 30min before entry
      to:   entry_time + 60 * 60,    // 1h after entry
    });
  } catch (e) {
    console.warn('[Strategy] stratLocateTrade error:', e);
  }
}

// ── Chart markers ─────────────────────────────────────────────────────────────

function _clearBacktestMarkers() {
  if (!_widget) return;
  try {
    const chart = _widget.activeChart();
    for (const id of _stratMarkerShapes) {
      try { chart.removeEntity(id); } catch (_) {}
    }
  } catch (_) {}
  _stratMarkerShapes = [];
}

function _drawBacktestMarkers(trades, showFiltered) {
  _clearBacktestMarkers();
  if (!_widget || !trades) return;

  // Filter visible trades based on showFiltered toggle
  const visible = showFiltered ? trades : trades.filter(t => t.context_pass === 1);

  try {
    const chart = _widget.activeChart();
    for (const t of visible) {
      if (!t.entry_time) continue;
      const isFiltered = t.context_pass === 0;
      const isLong = t.direction === 'long';

      // ── Colors ──
      // Long: green (#26a69a), Short: red (#ef5350), Filtered: gray (#888888)
      const longColor = '#26a69a';
      const shortColor = '#ef5350';
      const filteredColor = '#888888';

      const entryColor = isFiltered ? filteredColor : (isLong ? longColor : shortColor);

      // ── Entry marker ──
      // direction: 'buy' shows up-arrow, 'sell' shows down-arrow
      const entryLabel = isFiltered
        ? (isLong ? 'Entry▲ (filtered)' : 'Entry▼ (filtered)')
        : (isLong ? 'Entry▲ Stop' : 'Entry▼ Stop');

      try {
        const entryId = chart.createExecutionShape()
          .setTime(t.entry_time)
          .setDirection(isLong ? 'buy' : 'sell')
          .setPrice(t.entry_price)
          .setArrowColor(entryColor)
          .setArrowHeight(14)
          .setArrowSpacing(3)
          .setFont('bold 11px sans-serif')
          .setTextColor(entryColor)
          .setText(entryLabel)
          .getShapeId();
        if (entryId) _stratMarkerShapes.push(entryId);
      } catch (_) {}

      // ── Exit marker (only for non-filtered closed trades) ──
      if (!isFiltered && t.exit_time && t.exit_price != null) {
        const isWin = t.outcome === 'win';
        const isLoss = t.outcome === 'loss';
        const exitColor = isWin ? longColor
          : isLoss ? shortColor : '#64b5f6';

        // Exit arrow is opposite of entry direction
        const exitLabel = isWin
          ? 'Exit ✓ Target'
          : isLoss
            ? 'Exit ✗ Stop'
            : 'Exit (open)';

        try {
          const exitId = chart.createExecutionShape()
            .setTime(t.exit_time)
            .setDirection(isLong ? 'sell' : 'buy')
            .setPrice(t.exit_price)
            .setArrowColor(exitColor)
            .setArrowHeight(12)
            .setArrowSpacing(3)
            .setFont('bold 11px sans-serif')
            .setTextColor(exitColor)
            .setText(exitLabel)
            .getShapeId();
          if (exitId) _stratMarkerShapes.push(exitId);
        } catch (_) {}
      }
    }
  } catch (e) {
    console.warn('[Strategy] _drawBacktestMarkers error:', e);
  }
}
