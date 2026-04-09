/**
 * app.js — initialises the TradingView widget, wires up the custom datafeed,
 * and draws price-action annotations (S/R levels, market cycle highlights).
 */

// ── Constants ─────────────────────────────────────────────────────────────────

const CYCLE_COLORS = {
  markup:       'rgba(38, 166, 154, 0.12)',   // teal
  markdown:     'rgba(239, 83,  80,  0.12)',  // red
  accumulation: 'rgba(33,  150, 243, 0.12)', // blue
  distribution: 'rgba(255, 152,  0,  0.12)', // orange
};

const SR_COLORS = {
  support:    '#26a69a',
  resistance: '#ef5350',
};

// ── State ────────────────────────────────────────────────────────────────────

let _chart = null;         // TradingView IChartingLibraryWidget
let _activeShapes = [];    // IDs of drawn S/R lines
let _activeSeries = null;  // main candlestick series (unused directly — TV manages it)

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  const datafeed = new MESDatafeed();

  // Wire analysis updates to our annotation renderer
  datafeed.setAnalysisCallback((analysis) => {
    updateAnnotations(analysis);
    updateCycleBadge(analysis.market_cycle);
  });

  _chart = new TradingView.widget({
    container: 'tv-chart',

    // Data
    datafeed: datafeed,
    symbol:  'MES',
    interval: '5',         // default: 5-minute bars

    // Library path — where charting_library static assets are served
    library_path: '/charting_library/',

    // Locale
    locale: 'en',
    timezone: 'America/New_York',

    // Appearance
    theme: 'dark',
    toolbar_bg: '#1e222d',
    loading_screen: { backgroundColor: '#131722', foregroundColor: '#2962ff' },

    // Features
    enabled_features: [
      'use_localstorage_for_settings',
      'move_logo_to_main_pane',
    ],
    disabled_features: [
      'header_symbol_search',
      'header_compare',
      'display_market_status',
    ],

    // Full-window sizing
    autosize: true,

    studies_overrides: {},
  });

  _chart.onChartReady(() => {
    console.log('Chart ready');
    updateStatusBar('connected', 'Live');

    // After chart is ready, fetch and draw initial analysis
    fetch('/api/analysis')
      .then(r => r.json())
      .then(analysis => {
        updateAnnotations(analysis);
        updateCycleBadge(analysis.market_cycle);
      })
      .catch(err => console.error('Initial analysis fetch error:', err));
  });
});

// ── Status Bar ────────────────────────────────────────────────────────────────

function updateStatusBar(state, text) {
  const el = document.getElementById('status');
  if (!el) return;
  el.className = state;
  el.textContent = text;
}

// ── Market Cycle Badge ────────────────────────────────────────────────────────

function updateCycleBadge(cycle) {
  const el = document.getElementById('cycle-badge');
  if (!el) return;
  const labels = {
    markup:       'Markup (Uptrend)',
    markdown:     'Markdown (Downtrend)',
    accumulation: 'Accumulation',
    distribution: 'Distribution',
    unknown:      '—',
  };
  el.textContent = labels[cycle] || cycle || '—';
  el.className = cycle || '';
}

// ── Annotations ───────────────────────────────────────────────────────────────

/**
 * Redraw all S/R lines and market cycle background highlights.
 * Removes previous shapes before redrawing.
 */
function updateAnnotations(analysis) {
  if (!_chart) return;

  let activeChart;
  try {
    activeChart = _chart.activeChart();
  } catch (e) {
    return; // chart not ready yet
  }

  // Remove previous shapes
  for (const id of _activeShapes) {
    try { activeChart.removeEntity(id); } catch {}
  }
  _activeShapes = [];

  // Draw market cycle background ranges
  if (analysis.cycle_ranges && analysis.cycle_ranges.length > 0) {
    drawCycleRanges(activeChart, analysis.cycle_ranges);
  }

  // Draw S/R horizontal lines
  if (analysis.support_levels) {
    for (const level of analysis.support_levels) {
      drawHorizontalLine(activeChart, level.price, SR_COLORS.support, level.touches);
    }
  }
  if (analysis.resistance_levels) {
    for (const level of analysis.resistance_levels) {
      drawHorizontalLine(activeChart, level.price, SR_COLORS.resistance, level.touches);
    }
  }
}

/**
 * Draw a single horizontal S/R line.
 * Line width scales with the number of touches (strength).
 */
function drawHorizontalLine(chart, price, color, touches) {
  const lineWidth = Math.min(touches, 3);  // 1–3px
  try {
    const id = chart.createShape(
      { price: price, time: 0 },
      {
        shape: 'horizontal_line',
        lock: true,
        disableSelection: true,
        overrides: {
          linecolor: color,
          linewidth: lineWidth,
          linestyle: 0,           // solid
          showPrice: true,
          showLabel: true,
          text: `${price}`,
          textcolor: color,
          fontsize: 11,
        },
      }
    );
    if (id) _activeShapes.push(id);
  } catch (e) {
    console.warn('drawHorizontalLine error:', e);
  }
}

/**
 * Draw translucent background rectangles for each market cycle phase.
 * Only draws recent cycles (last 500 bars) to keep the chart uncluttered.
 */
function drawCycleRanges(chart, cycleRanges) {
  // Show at most the last 8 cycle segments
  const recent = cycleRanges.slice(-8);

  for (const range of recent) {
    const color = CYCLE_COLORS[range.type] || 'rgba(128,128,128,0.08)';
    try {
      const id = chart.createMultipointShape(
        [
          { time: range.start_time, price: 0 },
          { time: range.end_time,   price: 0 },
        ],
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
            textcolor: 'rgba(255,255,255,0.4)',
            fontsize: 10,
            bold: false,
          },
        }
      );
      if (id) _activeShapes.push(id);
    } catch (e) {
      console.warn('drawCycleRanges error:', e);
    }
  }
}
