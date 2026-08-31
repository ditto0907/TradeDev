import { state, getChart } from './state.js';
import { MC_COLORS, MC_DEFAULTS } from './constants.js';
import { _escHtml } from './utils.js';

// Market cycle analysis
let _mcAnalyses = [];   // full records from backend
let _mcShapes   = {};   // analysis_id → [entity_id, ...]

function _mcColor(label) {
  return MC_COLORS[label] || MC_DEFAULTS;
}

export async function loadCycleAnalyses() {
  try {
    const sym = state.currentSymbol || 'MES';
    const res = await fetch(`/api/skill/analyses?active_only=false&symbol=${encodeURIComponent(sym)}`);
    _mcAnalyses = await res.json();
    renderAnalysisTable();
    drawAllActiveAnalyses();
  } catch (e) {
    console.warn('loadCycleAnalyses error:', e);
  }
}

export function drawAllActiveAnalyses() {
  const chart = getChart();
  if (!chart) return;

  // Clear all existing analysis shapes
  for (const shapes of Object.values(_mcShapes)) {
    shapes.forEach(sid => { try { chart.removeEntity(sid); } catch {} });
  }
  _mcShapes = {};

  // Draw active analyses for the current symbol only
  const sym = state.currentSymbol || 'MES';
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

export function handleCycleAnalysisWS(msg) {
  if (msg.type === 'cycle_analysis') {
    // New analysis arrived
    _mcAnalyses.unshift(msg.analysis);
    renderAnalysisTable();
    // Only draw on the chart when the analysis belongs to the symbol we're
    // currently viewing — otherwise another contract's annotations would be
    // painted onto this chart.
    const curSym = state.currentSymbol || 'MES';
    const sameSym = !msg.analysis.symbol || msg.analysis.symbol === curSym;
    if (msg.analysis.active && sameSym && state.widget) {
      try {
        const chart = state.widget.activeChart();
        drawOneAnalysis(chart, msg.analysis);
        chart.selectLineTool('cursor');
      } catch {}
    }
  } else if (msg.type === 'cycle_analysis_toggle') {
    const rec = _mcAnalyses.find(a => a.id === msg.id);
    if (rec) {
      rec.active = msg.active ? 1 : 0;
      renderAnalysisTable();
      const chart = getChart();
      if (!chart) return;
      const curSym = state.currentSymbol || 'MES';
      const sameSym = !rec.symbol || rec.symbol === curSym;
      if (rec.active && sameSym) {
        drawOneAnalysis(chart, rec);
      } else {
        removeOneAnalysis(chart, rec.id);
      }
    }
  } else if (msg.type === 'cycle_analysis_delete') {
    const chart = getChart();
    if (chart) {
      try { removeOneAnalysis(chart, msg.id); } catch {}
    }
    _mcAnalyses = _mcAnalyses.filter(a => a.id !== msg.id);
    renderAnalysisTable();
  }
}

export async function toggleAnalysisActive(id) {
  const rec = _mcAnalyses.find(a => a.id === id);
  if (!rec) return;
  const newActive = !rec.active;
  try {
    await fetch(`/api/skill/analyses/${id}/active?active=${newActive}`, { method: 'PUT' });
  } catch (e) {
    console.warn('toggleAnalysisActive error:', e);
  }
}

export async function deleteAnalysis(id) {
  try {
    await fetch(`/api/skill/analyses/${id}`, { method: 'DELETE' });
  } catch (e) {
    console.warn('deleteAnalysis error:', e);
  }
}

// Format analysis time period (bar_from - bar_to) to readable string
function _formatAnalysisPeriod(bar_from, bar_to) {
  if (!bar_from || !bar_to) return '';

  const tz = (window.AppTZ ? AppTZ.get() : 'America/New_York');
  const dateOpts = { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' };
  const timeOpts = { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false };

  const fromDate = new Date(bar_from * 1000);
  const toDate = new Date(bar_to * 1000);

  const fromDateStr = new Intl.DateTimeFormat('sv-SE', dateOpts).format(fromDate);
  const toDateStr   = new Intl.DateTimeFormat('sv-SE', dateOpts).format(toDate);
  const fromTimeStr = new Intl.DateTimeFormat('sv-SE', timeOpts).format(fromDate);
  const toTimeStr   = new Intl.DateTimeFormat('sv-SE', timeOpts).format(toDate);

  // Same day: "2026-04-08 09:30-11:00"
  if (fromDateStr === toDateStr) {
    return `${fromDateStr} ${fromTimeStr}-${toTimeStr}`;
  }

  // Different days: "2026-04-08 09:30 - 2026-04-09 11:00"
  return `${fromDateStr} ${fromTimeStr} - ${toDateStr} ${toTimeStr}`;
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

export function showSummaryModal(id) {
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

export function closeSummaryModal() {
  document.getElementById('mc-modal-overlay')?.classList.remove('open');
}

// ── Modal Drag ────────────────────────────────────────────────────────────────

export function initSummaryModal() {
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

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeSummaryModal();
  });
}

export function renderAnalysisTable() {
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
      ? _escHtml(a.summary.substring(0, 60)) + '…'
      : _escHtml(a.summary || '—');

    return `<tr class="${activeClass}">
      <td>${created}</td>
      <td>${_escHtml(a.symbol || '')}</td>
      <td>${period}</td>
      <td>${_escHtml(a.timeframe || '')}</td>
      <td>${_escHtml(a.session || '')}</td>
      <td class="mc-summary-cell" onclick="showSummaryModal(${a.id})" title="Click to view full summary">${summary}</td>
      <td>${annCount}</td>
      <td class="mc-actions">
        <span class="mc-btn" onclick="toggleAnalysisActive(${a.id})" title="${toggleTitle}">${toggleIcon}</span>
        <span class="mc-btn mc-del" onclick="deleteAnalysis(${a.id})" title="Delete">✕</span>
      </td>
    </tr>`;
  }).join('');
}
