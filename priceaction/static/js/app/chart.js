import { state, getResolution } from './state.js';
import { setWsStatus } from './utils.js';
import { createSaveLoadAdapter } from './saveload.js';
import { buildCustomIndicators } from './indicators.js';
import { loadContractOptions, syncContractSelector } from './contracts.js';
import { updateAnnotations, updateCycleBadge, updateSRPanel } from './annotations.js';
import { handleCycleAnalysisWS, loadCycleAnalyses, drawAllActiveAnalyses } from './marketcycle.js';
import { loadTradeFileList, redrawShownTrades } from './trades.js';
import { loadWorkingOrders } from './orders.js';
import { buildContextMenuItems } from './contextmenu.js';
import { connectPriceFeed } from './pricefeed.js';

let _volumeStudyId = null;

export function initChart() {
  const datafeed = new MESDatafeed();

  datafeed.setAnalysisCallback((analysis) => {
    state.lastAnalysis = analysis;
    updateAnnotations(analysis);
    updateCycleBadge(analysis.market_cycle);
    updateSRPanel(analysis);
  });

  datafeed.setCycleAnalysisCallback(handleCycleAnalysisWS);

  state.widget = new TradingView.widget({
    container:    'tv-chart',
    datafeed:     datafeed,
    symbol:       'MES',
    interval:     '5',
    library_path: '/charting_library/',
    locale:       'en',
    timezone:     (window.AppTZ ? AppTZ.get() : 'America/New_York'),
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

    custom_indicators_getter: (PineJS) => Promise.resolve(buildCustomIndicators(PineJS)),
  });

  state.widget.onChartReady(() => {
    setWsStatus('live', 'Live');
    const chart = state.widget.activeChart();

    // ── Two-way bind chart timezone with AppTZ (top-bar selector) ──────
    try {
      if (window.AppTZ && chart.getTimezoneApi) {
        const tzApi = chart.getTimezoneApi();
        // 1) Push current AppTZ into the chart in case load_last_chart restored
        //    a different value than the configured one.
        try {
          const cur = tzApi.getTimezone();
          const wanted = AppTZ.get();
          if (cur && cur.id !== wanted) tzApi.setTimezone(wanted);
        } catch (e) { /* ignore */ }
        // 2) Chart → AppTZ: when the user changes tz from the chart UI.
        let _suppressChartEvent = false;
        tzApi.onTimezoneChanged().subscribe(null, (tz) => {
          if (_suppressChartEvent) return;
          AppTZ.set(String(tz), { source: 'chart' });
        });
        // 3) AppTZ → chart: when the user picks tz from the top-bar.
        AppTZ.onChange((tz, ev) => {
          if (ev && ev.source === 'chart') return;
          try {
            _suppressChartEvent = true;
            tzApi.setTimezone(tz);
          } catch (e) { console.warn('chart setTimezone failed', e); }
          finally { _suppressChartEvent = false; }
        });
        // 4) Refresh tz-bearing side panels (cycle analyses periods, etc.)
        AppTZ.onChange(() => {
          try { loadCycleAnalyses(); } catch (e) {}
        });
      }
    } catch (e) {
      console.warn('Timezone bind error:', e);
    }

    // ── Right-click context menu for quick order placement ─────────────
    state.widget.onContextMenu(function(unixTime, price) {
      return buildContextMenuItems(price);
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

    // ── Default EMA20 indicator on main pane ───────────────────────────
    // NOTE: getAllStudies() returns {id, name} only — there is NO metaInfo
    // field, so the previous metaInfo-based detection always returned false
    // and stacked a new EMA20 on every chart-ready (saved-layout reload,
    // symbol switch, etc.).  Match by study name instead.
    try {
      const hasEma = existingStudies.some(s =>
        (s.name || '').toLowerCase() === 'moving average exponential');
      if (!hasEma) {
        chart.createStudy(
          'Moving Average Exponential', false, false,
          { length: 20 },
          { 'plot.color': '#FFA726', 'plot.linewidth': 2 }
        ).catch(() => {
          // Fallback: name-only invocation
          try { chart.createStudy('Moving Average Exponential', false, false, [20]); } catch {}
        });
      }
    } catch (e) {
      console.debug('EMA20 default add skipped:', e);
    }

    // Load S/R analysis
    fetch(`/api/analysis?symbol=${state.currentSymbol || 'MES'}`)
      .then(r => r.json())
      .then(analysis => {
        state.lastAnalysis = analysis;
        updateAnnotations(analysis);
        updateCycleBadge(analysis.market_cycle);
        updateSRPanel(analysis);
      })
      .catch(e => console.warn('Analysis fetch error:', e));

    // Load trade file list (don't show on chart by default)
    loadTradeFileList();

    // Load existing working orders
    loadWorkingOrders();

    // Sync currentSymbol with the chart's actual symbol (may differ from default
    // when load_last_chart restores a previous session).  Always store the BASE
    // symbol (strip any @CONT_FRONT / @YYYYMM token) so downstream comparisons
    // against WS messages (which carry the bare symbol) and ?symbol= queries work.
    try { state.currentSymbol = chart.symbol().split('@')[0]; } catch {}

    // Load initial contract options and switch to front month
    loadContractOptions(state.currentSymbol.split('@')[0]).then(frontToken => {
      if (frontToken && state.widget) {
        const res = getResolution(chart);
        state.widget.setSymbol(frontToken, res, () => {
          console.log('[Chart] init: switched to front month', frontToken);
        });
      } else {
        syncContractSelector();
      }
    });

    // Reload analyses (and redraw active ones) whenever the chart symbol changes
    chart.onSymbolChanged().subscribe(null, () => {
      try { state.currentSymbol = chart.symbol().split('@')[0]; } catch {}
      const baseSym = state.currentSymbol;
      loadContractOptions(baseSym).then(() => syncContractSelector());
      loadCycleAnalyses();
      // Session switch (RTH ↔ ETH) triggers onSymbolChanged BEFORE bar data reloads.
      // The bar reload wipes all ephemeral (disableSave:true) shapes, so we must
      // redraw once after onDataLoaded fires for the new session.
      chart.onDataLoaded().subscribe(null, () => {
        drawAllActiveAnalyses();
        redrawShownTrades();
      }, true);
    });

    // Load market cycle analyses for the current symbol
    loadCycleAnalyses();
  });

  connectPriceFeed(datafeed);
}
