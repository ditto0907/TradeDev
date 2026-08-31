import { state, getChart } from './state.js';
import { STRAT_TS_MAX } from './constants.js';

let _stratCurrentId     = null;   // currently loaded backtest id
let _stratMarkerShapes  = [];     // chart execution shapes for current backtest
let _stratShowMarkers   = false;
let _stratShowFiltered  = true;   // show SR-filtered trades on chart & in summary
let _stratBacktestList  = [];     // cached list from server
let _stratCurrentTrades = [];     // trades from the current backtest run

export function initStrategyTab() {
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

export async function runStrategyBacktest() {
  const btn = document.getElementById('strat-run-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Running…'; }

  try {
    const ibsPct = parseFloat(document.getElementById('strat-ibs')?.value || '70') / 100;
    const ctx    = document.getElementById('strat-ctx')?.checked ?? true;
    const maxStop = parseFloat(document.getElementById('strat-maxstop')?.value || '200');
    const fromEl = document.getElementById('strat-from');
    const toEl   = document.getElementById('strat-to');
    const session = document.getElementById('strat-session')?.value || 'all';
    const timeStart = document.getElementById('strat-time-start')?.value || '';
    const timeEnd   = document.getElementById('strat-time-end')?.value || '';
    const timeFilter = (timeStart && timeEnd) ? `${timeStart}-${timeEnd}` : '';

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

export async function loadBacktestHistory(backtest_id) {
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

export async function deleteCurrentBacktest() {
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

export function toggleBacktestMarkers(show) {
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

export function toggleFilteredDisplay(show) {
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

    const locateBtn = `<button class="mc-btn" onclick="stratLocateTrade(${t.entry_time},${t.exit_time || t.entry_time})" title="Scroll chart to trade" style="font-size:11px;padding:1px 6px;background:none;border:1px solid var(--border);border-radius:3px;cursor:pointer">Locate</button>`;

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

export function stratLocateTrade(entry_time, exit_time) {
  if (!entry_time) return;
  const chart = getChart();
  if (!chart) return;
  try {
    // Same behavior as Trade History's locateTradeOnChart: 30min padding on each side
    const from = entry_time - 1800;
    const to = (exit_time || entry_time) + 1800;
    chart.setVisibleRange({ from, to });
  } catch (e) {
    console.warn('[Strategy] stratLocateTrade error:', e);
  }
}

// ── Chart markers ─────────────────────────────────────────────────────────────

function _clearBacktestMarkers() {
  for (const obj of _stratMarkerShapes) {
    try { obj.remove(); } catch (_) {}
  }
  _stratMarkerShapes = [];
}

function _drawBacktestMarkers(trades, showFiltered) {
  _clearBacktestMarkers();
  if (!state.widget || !trades) return;

  // Filter visible trades based on showFiltered toggle
  const visible = showFiltered ? trades : trades.filter(t => t.context_pass === 1);

  try {
    const chart = state.widget.activeChart();
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
        const entryExec = chart.createExecutionShape()
          .setTime(t.entry_time)
          .setDirection(isLong ? 'buy' : 'sell')
          .setPrice(t.entry_price)
          .setArrowColor(entryColor)
          .setArrowHeight(14)
          .setArrowSpacing(3)
          .setFont('bold 11px sans-serif')
          .setTextColor(entryColor)
          .setText(entryLabel);
        _stratMarkerShapes.push(entryExec);
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
          const exitExec = chart.createExecutionShape()
            .setTime(t.exit_time)
            .setDirection(isLong ? 'sell' : 'buy')
            .setPrice(t.exit_price)
            .setArrowColor(exitColor)
            .setArrowHeight(12)
            .setArrowSpacing(3)
            .setFont('bold 11px sans-serif')
            .setTextColor(exitColor)
            .setText(exitLabel);
          _stratMarkerShapes.push(exitExec);
        } catch (_) {}
      }
    }
  } catch (e) {
    console.warn('[Strategy] _drawBacktestMarkers error:', e);
  }
}
