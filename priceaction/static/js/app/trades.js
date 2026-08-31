import { state, getChart, getResolution } from './state.js';
import { _escHtml, _escJsAttr, _fmtPnlByCcy } from './utils.js';

// Trade markers — per-file management
let _tradeFiles        = {};   // { filename: { trades: [], shown: false, expanded: true } }
let _tradeShapesByFile = {};   // { filename: [shapes] }
let _showTrades        = false;  // global chart visibility (legend toggle)

export async function loadTradeFileList() {
  try {
    // Source of truth: DB trade_logs (includes user annotations + stable ids),
    // grouped by source_file to preserve the per-file show/hide-on-chart UX.
    const rows = await fetch('/api/tradelogs').then(r => r.json());
    const byFile = {};
    (Array.isArray(rows) ? rows : []).forEach(t => {
      const fn = t.source_file || '(unknown)';
      (byFile[fn] = byFile[fn] || []).push(t);
    });
    // Display each file's trades ascending by entry_time.
    Object.values(byFile).forEach(list => list.sort((a, b) => (a.entry_time || 0) - (b.entry_time || 0)));
    // Rebuild _tradeFiles, preserving each file's shown/expanded UI state.
    const prevState = _tradeFiles;
    _tradeFiles = {};
    Object.keys(byFile).forEach(fn => {
      const prev = prevState[fn] || {};
      _tradeFiles[fn] = {
        trades:   byFile[fn],
        shown:    prev.shown || false,
        expanded: prev.expanded !== false,
      };
    });
    redrawShownTrades();
    updateTradeCount();
    renderTradeTable();
  } catch (e) {
    console.warn('Trade DB load error:', e);
  }
}

export function toggleFileExpand(filename) {
  const entry = _tradeFiles[filename];
  if (!entry) return;
  entry.expanded = !entry.expanded;
  renderTradeTable();
}

async function loadTradesForFile(filename) {
  // Trades are already loaded from the DB by loadTradeFileList(); return the
  // cached rows.  Kept as an async shim so existing callers stay unchanged.
  return _tradeFiles[filename]?.trades || [];
}

export async function toggleFileOnChart(filename) {
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
  updateTradeCount();
  renderTradeTable();
}

export async function deleteTradeFile(filename) {
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
    updateTradeCount();
    renderTradeTable();
    console.log(`[Trades] Deleted file: ${filename}`);
  } catch (e) {
    console.warn(`Trade file delete error (${filename}):`, e);
  }
}

export async function handleTradeCSVUpload(input) {
  const file = input.files?.[0];
  if (!file) return;
  // Client-side validation before uploading.
  if (!/\.csv$/i.test(file.name)) {
    alert('Please choose a .csv file.');
    input.value = '';
    return;
  }
  if (file.size > 10 * 1024 * 1024) {
    alert('File too large (max 10 MB).');
    input.value = '';
    return;
  }
  try {
    const formData = new FormData();
    formData.append('file', file);
    const res = await fetch('/api/trades/upload', { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok || !data.trades?.length) {
      alert(data.error || 'No filled trades found in CSV.');
      input.value = '';
      return;
    }
    const filename = data.filename;
    // The upload endpoint upserts into the DB (preserving annotations); reload
    // from the DB so rows carry stable ids + any existing annotations, then
    // show the newly-uploaded file on the chart.
    await loadTradeFileList();
    if (_tradeFiles[filename]) {
      _tradeFiles[filename].shown = true;
      _tradeFiles[filename].expanded = true;
      drawTradeMarkersForFile(filename, _tradeFiles[filename].trades || []);
      _showTrades = true;
      document.getElementById('leg-trades')?.classList.remove('sr-off');
    }
    updateTradeCount();
    renderTradeTable();
    console.log(`[Trades] Uploaded ${data.trades.length} trades from ${filename}`);
  } catch (e) {
    console.warn('Trade CSV upload error:', e);
    alert('Failed to parse CSV file.');
  }
  input.value = '';
}

// Persist a trade annotation (market_cycle / notes / …) to the DB and update
// the in-memory cache so re-renders keep the edit.
export async function saveTradeAnnotation(id, field, value) {
  if (id == null) return;
  try {
    await fetch(`/api/tradelogs/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [field]: value }),
    });
    Object.values(_tradeFiles).forEach(f => {
      (f.trades || []).forEach(t => { if (t.id === id) t[field] = value; });
    });
  } catch (e) {
    console.warn('saveTradeAnnotation failed:', e);
  }
}

function clearTradeShapesForFile(filename) {
  const shapes = _tradeShapesByFile[filename];
  if (!shapes) return;
  const chart = getChart();
  if (!chart) return;
  shapes.forEach(s => {
    try {
      if (s.type === 'exec') { s.obj.remove(); }
      else if (s.type === 'entity') { chart.removeEntity(s.id); }
    } catch {}
  });
  _tradeShapesByFile[filename] = [];
}

function drawTradeMarkersForFile(filename, trades) {
  const chart = getChart();
  if (!chart) return;

  // Clear existing shapes for this file
  clearTradeShapesForFile(filename);
  _tradeShapesByFile[filename] = [];

  if (!trades.length) return;

  // Only plot trades that belong to the instrument currently on the chart.
  // Execution shapes are placed by (time, price); plotting an MNQ/NK225 trade
  // on an MES chart would land the marker at a wrong price on the wrong
  // instrument, corrupting the replay.  A trade with no symbol is assumed MES.
  const curSym = state.currentSymbol || 'MES';
  const drawable = trades.filter(t => (t.symbol || 'MES') === curSym);
  if (!drawable.length) return;

  drawable.forEach(trade => {
    try {
      if (trade.entry_price == null || trade.entry_time == null) return;
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

export function toggleTrades() {
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
  updateTradeCount();
  renderTradeTable();
}

function updateTradeCount() {
  const countEl = document.getElementById('trade-count');
  if (!countEl) return;
  let total = 0;
  Object.values(_tradeFiles).forEach(f => {
    if (f.shown && f.trades) total += f.trades.length;
  });
  countEl.textContent = total ? `${total}` : '';
}

export function locateTradeOnChart(entryTime, exitTime, symbol) {
  const chart = getChart();
  if (!chart) return;
  const from = entryTime - 1800;   // 30min padding on each side
  const to = (exitTime || entryTime) + 1800;

  // If the trade belongs to a different instrument, switch the chart to it
  // first so the replay shows the correct bars, then set the range once the
  // new symbol's data has loaded.
  const tradeSym = symbol || 'MES';
  const curSym = state.currentSymbol || 'MES';
  if (tradeSym && tradeSym !== curSym) {
    const res = getResolution(chart);
    state.widget.setSymbol(tradeSym, res, () => {
      try { chart.setVisibleRange({ from, to }); } catch {}
    });
    return;
  }
  chart.setVisibleRange({ from, to });
}

// Redraw execution markers for every file currently toggled "shown".
// Called after a symbol change so markers for the newly-selected instrument
// appear (and markers for other instruments are dropped by the symbol filter
// inside drawTradeMarkersForFile).
export function redrawShownTrades() {
  if (!state.widget || !_showTrades) return;
  Object.keys(_tradeFiles).forEach(fn => {
    const entry = _tradeFiles[fn];
    if (entry && entry.shown && entry.trades) {
      drawTradeMarkersForFile(fn, entry.trades);
    }
  });
}

export function renderTradeTable() {
  const tbody = document.getElementById('history-tbody');
  if (!tbody) return;
  const info = document.getElementById('trade-panel-info');
  const filenames = Object.keys(_tradeFiles).sort();

  if (!filenames.length) {
    tbody.innerHTML = '<tr><td colspan="9"><div class="empty-table">No trade logs — upload a CSV to view</div></td></tr>';
    if (info) info.textContent = '';
    return;
  }

  // Compute global stats
  let wins = 0, losses = 0, totalCount = 0, totalWinAmt = 0, totalLossAmt = 0;
  const pnlByCcy = {};   // currency → summed P&L (never mix currencies)
  filenames.forEach(fn => {
    const f = _tradeFiles[fn];
    if (f.trades) {
      totalCount += f.trades.length;
      f.trades.forEach(t => {
        if (t.pnl != null) {
          const ccy = t.currency || 'USD';
          pnlByCcy[ccy] = (pnlByCcy[ccy] || 0) + t.pnl;
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
      const pnlStr = _fmtPnlByCcy(pnlByCcy);
      const anyPos = Object.values(pnlByCcy).reduce((a, b) => a + (b >= 0 ? 1 : -1), 0) >= 0;
      info.innerHTML = `<span style="color:var(--text-dim)">${totalCount} trades</span>` +
        ` &nbsp;|&nbsp; <span style="color:${anyPos ? 'var(--green)' : 'var(--red)'}">P&L: ${pnlStr}</span>` +
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
    let fWins = 0, fLosses = 0, fWinAmt = 0, fLossAmt = 0;
    const fPnlByCcy = {};
    if (f.trades) {
      f.trades.forEach(t => {
        if (t.pnl != null) {
          const ccy = t.currency || 'USD';
          fPnlByCcy[ccy] = (fPnlByCcy[ccy] || 0) + t.pnl;
          if (t.pnl >= 0) { fWins++; fWinAmt += t.pnl; } else { fLosses++; fLossAmt += Math.abs(t.pnl); }
        }
      });
    }
    const fWr = (fWins + fLosses) > 0 ? ((fWins / (fWins + fLosses)) * 100).toFixed(0) : '—';
    const fAvgWin = fWins > 0 ? fWinAmt / fWins : 0;
    const fAvgLoss = fLosses > 0 ? fLossAmt / fLosses : 0;
    const fRr = fAvgLoss > 0 ? (fAvgWin / fAvgLoss).toFixed(2) : '—';
    const fAnyPos = Object.values(fPnlByCcy).reduce((a, b) => a + (b >= 0 ? 1 : -1), 0) >= 0;
    const fStatsHtml = f.trades && f.trades.length
      ? `<span style="color:var(--text-dim)">${count} trades</span>` +
        ` &nbsp;|&nbsp; <span style="color:${fAnyPos ? 'var(--green)' : 'var(--red)'}">P&L: ${_fmtPnlByCcy(fPnlByCcy)}</span>` +
        ` &nbsp;|&nbsp; <span style="color:var(--text-dim)">WR: ${fWr}%</span>` +
        ` &nbsp;|&nbsp; <span style="color:var(--text-dim)">RR: ${fRr}</span>`
      : '';

    const eyeIcon = isShown
      ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>'
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
    const chevron = isExpanded ? '▾' : '▸';

    // File group header row
    html += `<tr class="trade-file-header" style="background:var(--panel);border-bottom:1px solid var(--border)">
      <td colspan="9" style="padding:5px 8px">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="cursor:pointer;font-size:12px;opacity:0.6;user-select:none;flex-shrink:0" onclick="toggleFileExpand('${_escJsAttr(fn)}')">${chevron}</span>
          <span style="cursor:pointer;display:inline-flex;align-items:center;opacity:0.7;flex-shrink:0" onclick="toggleFileOnChart('${_escJsAttr(fn)}')" title="${isShown ? 'Hide from chart' : 'Show on chart'}">${eyeIcon}</span>
          <span style="font-size:12px;font-weight:500;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:160px;max-width:280px">${_escHtml(fn)}</span>
          <span style="font-size:11px;white-space:nowrap;flex-shrink:0;margin-left:12px">${fStatsHtml}</span>
          <span style="margin-left:auto;cursor:pointer;display:inline-flex;align-items:center;opacity:0.5;flex-shrink:0" onclick="if(confirm('Delete ${_escJsAttr(fn)}?'))deleteTradeFile('${_escJsAttr(fn)}')" title="Delete file">
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
        const pnlStr = t.pnl != null
          ? _fmtPnlByCcy({ [t.currency || 'USD']: t.pnl })
          : '—';
        const pnlClass = t.pnl != null ? (t.pnl >= 0 ? 'up' : 'down') : '';
        const locateBtn = t.entry_time
          ? `<span style="cursor:pointer;opacity:0.5;margin-left:4px;display:inline-flex;vertical-align:middle" onclick="locateTradeOnChart(${t.entry_time},${t.exit_time || t.entry_time},'${_escJsAttr(t.symbol || 'MES')}')" title="Locate on chart"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/></svg></span>`
          : '';
        // Editable annotation cells (persist to DB via /api/tradelogs/{id}).
        const annDisabled = t.id == null ? 'disabled' : '';
        const cycleCell = `<input value="${_escHtml(t.market_cycle || '')}" ${annDisabled}
            style="width:100%;font-size:11px;background:transparent;color:var(--text);border:1px solid transparent"
            onfocus="this.style.borderColor='var(--border)'"
            onblur="this.style.borderColor='transparent';saveTradeAnnotation(${t.id},'market_cycle',this.value)">`;
        const notesCell = `<input value="${_escHtml(t.notes || '')}" ${annDisabled}
            style="width:100%;font-size:11px;background:transparent;color:var(--text);border:1px solid transparent"
            onfocus="this.style.borderColor='var(--border)'"
            onblur="this.style.borderColor='transparent';saveTradeAnnotation(${t.id},'notes',this.value)">`;
        html += `<tr>
          <td>${dateStr}${locateBtn}</td>
          <td>${_escHtml(t.symbol || 'MES')}</td>
          <td class="${sideClass}">${side}</td>
          <td>${t.qty || 1}</td>
          <td>${t.entry_price != null ? t.entry_price.toFixed(2) : '—'}</td>
          <td>${t.exit_price != null ? t.exit_price.toFixed(2) : '—'}</td>
          <td class="${pnlClass}">${pnlStr}</td>
          <td>${cycleCell}</td>
          <td>${notesCell}</td>
        </tr>`;
      });
    } else if (isExpanded && (!f.trades || !f.trades.length)) {
      html += `<tr><td colspan="9" style="text-align:center;color:var(--text-dim);font-size:11px;padding:4px">Loading...</td></tr>`;
    }
  });

  tbody.innerHTML = html;
}
