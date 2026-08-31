import { state, getChart } from './state.js';
import { CYCLE_COLORS } from './constants.js';

// S/R shape tracking
let _supportShapes    = [];
let _resistanceShapes = [];
let _cycleShapes      = [];
let _showSupport      = false;
let _showResistance   = false;

// ── Cycle Badge (disabled - topbar simplified) ────────────────────────────────

export function updateCycleBadge(cycle) {
  // Cycle badge removed from topbar
  // Keeping function for compatibility
}

// ── S/R Panel ─────────────────────────────────────────────────────────────────

export function updateSRPanel(analysis) {
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

export function updateAnnotations(analysis) {
  const chart = getChart();
  if (!chart) return;

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

export function toggleSR(type) {
  const chart = getChart();
  if (!chart) return;

  if (type === 'support') {
    _showSupport = !_showSupport;
    if (!_showSupport) {
      _supportShapes.forEach(id => { try { chart.removeEntity(id); } catch {} });
      _supportShapes = [];
    } else if (state.lastAnalysis) {
      (state.lastAnalysis.support_levels || []).forEach(l =>
        drawHLine(chart, l.price, '#26a69a', Math.min(l.touches, 3), _supportShapes));
    }
    document.getElementById('leg-support')?.classList.toggle('sr-off', !_showSupport);
  } else {
    _showResistance = !_showResistance;
    if (!_showResistance) {
      _resistanceShapes.forEach(id => { try { chart.removeEntity(id); } catch {} });
      _resistanceShapes = [];
    } else if (state.lastAnalysis) {
      (state.lastAnalysis.resistance_levels || []).forEach(l =>
        drawHLine(chart, l.price, '#ef5350', Math.min(l.touches, 3), _resistanceShapes));
    }
    document.getElementById('leg-resistance')?.classList.toggle('sr-off', !_showResistance);
  }
}

// ── Market Cycle Annotations (skill payload rendered inline) ──────────────────

export function renderCycleAnnotations(analysis) {
  const chart = getChart();
  if (!chart) return;

  // Check if analysis is for current symbol
  const currentSym = state.widget.symbolInterval().symbol;
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
