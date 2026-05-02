/**
 * Custom TradingView DataFeed adapter.
 *
 * Symbol uses TradingView's built-in extended hours feature.
 * The chart will show extended hours toggle in the toolbar.
 */

class MESDatafeed {
  constructor() {
    this._ws = null;
    this._wsReady = false;
    this._subscriptions = {};   // listenerGuid → { resolution, onTick }
    this._onAnalysis = null;
  }

  // ── onReady ────────────────────────────────────────────────────────────────

  onReady(callback) {
    fetch('/api/config')
      .then(r => r.json())
      .then(cfg => setTimeout(() => callback(cfg), 0))
      .catch(err => console.error('DataFeed onReady error:', err));
  }

  // ── searchSymbols ──────────────────────────────────────────────────────────

  searchSymbols(userInput, exchange, symbolType, onResult) {
    fetch('/api/symbol_list')
      .then(r => r.json())
      .then(data => {
        const q = (userInput || '').toUpperCase();
        const results = (data.symbols || [])
          .filter(item => !q || item.token.toUpperCase().includes(q) || item.label.toUpperCase().includes(q))
          .map(item => ({
            symbol:      item.token,
            full_name:   item.token,
            description: item.label,
            exchange:    'CME',
            type:        'futures',
          }));
        onResult(results);
      })
      .catch(() => onResult([]));
  }

  // ── resolveSymbol ──────────────────────────────────────────────────────────
  //
  // Supports: MES, MNQ, NK225MC, MGC
  // Fetches metadata from /api/symbols with has_extended_hours=true.
  // TradingView's built-in extended hours toggle handles session filtering.

  resolveSymbol(symbolName, onResolve, onError, extension) {
    console.log('[DataFeed] resolveSymbol called:', symbolName, 'extension:', extension);

    // Token form: "MES@CONT_FRONT", "MES@202506", or bare "MES"
    // Use base symbol for metadata lookup but keep full token as chart name.
    const atIdx = symbolName.indexOf('@');
    const baseSym = atIdx !== -1 ? symbolName.slice(0, atIdx) : symbolName;
    const isToken = atIdx !== -1;

    fetch(`/api/symbols?symbol=${encodeURIComponent(baseSym)}`)
      .then(r => r.json())
      .then(info => {
        // Override name with the full token so getBars/realtime use it
        if (isToken) {
          info.name = symbolName;
          info.full_name = symbolName;
          // Build a human-readable description for the token
          const suffix = symbolName.slice(atIdx + 1);
          const suffixLabel = {
            CONT_FRONT: 'Continuous (Front)',
            CONT_RATIO: 'Continuous (Ratio Adj)',
            CONT_DIFF:  'Continuous (Diff Adj)',
          }[suffix] || suffix;
          info.description = `${baseSym} – ${suffixLabel}`;
          // Continuous and monthly contracts have no realtime subscription
          info.has_intraday = true;
        }
        // When TradingView's subsession selector triggers a session change,
        // extension.session indicates the new subsession id ("regular" or "extended").
        if (extension && extension.session) {
          info.subsession_id = extension.session;
          const match = (info.subsessions || []).find(s => s.id === extension.session);
          if (match) info.session = match.session;
        } else {
          const match = (info.subsessions || []).find(s => s.id === info.subsession_id);
          if (match) info.session = match.session;
        }
        console.log('[DataFeed] resolveSymbol result:', info.name, 'session:', info.session);
        setTimeout(() => onResolve(info), 0);
      })
      .catch(err => {
        console.error('[DataFeed] resolveSymbol error:', err);
        onError('SYMBOL_NOT_FOUND');
      });
  }


  // ── getBars ────────────────────────────────────────────────────────────────
  //
  // Query backend for bars. TradingView handles session filtering based on
  // the extended hours toggle state.

  getBars(symbolInfo, resolution, periodParams, onResult, onError) {
    const { from, to, countBack } = periodParams;
    let url = `/api/history?symbol=${encodeURIComponent(symbolInfo.name)}&resolution=${resolution}&from=${from}&to=${to}`;
    if (countBack) {
      url += `&countback=${countBack}`;
    }
    console.log('[DataFeed] getBars:', symbolInfo.name, 'res:', resolution, 'from:', from, 'to:', to, 'countBack:', countBack);

    fetch(url)
      .then(r => r.json())
      .then(data => {
        if (data.s === 'no_data') {
          const meta = { noData: true };
          // nextTime is in SECONDS — same unit as periodParams.from/to.
          // Do NOT multiply by 1000; bar timestamps use ms but nextTime does not.
          if (data.nextTime != null) meta.nextTime = data.nextTime;
          onResult([], meta);
          return;
        }
        if (data.s !== 'ok') {
          onError('HISTORY_ERROR');
          return;
        }
        const bars = data.t.map((t, i) => ({
          time:   t * 1000,   // seconds → milliseconds for TradingView
          open:   data.o[i],
          high:   data.h[i],
          low:    data.l[i],
          close:  data.c[i],
          volume: data.v[i],
        }));
        onResult(bars, { noData: false });
      })
      .catch(err => {
        console.error('getBars error:', err);
        onError('FETCH_ERROR');
      });
  }

  // ── subscribeBars ──────────────────────────────────────────────────────────

  subscribeBars(symbolInfo, resolution, onTick, listenerGuid, onResetCacheNeededCallback) {
    // `onResetCacheNeededCallback` is invoked by TradingView to tell the
    // chart to drop its internal bar cache — we store it so that when a
    // background history batch completes we can call it and refresh the
    // on-screen widget.
    this._subscriptions[listenerGuid] = {
      resolution,
      onTick,
      symbol: symbolInfo.name,
      onResetCacheNeededCallback: onResetCacheNeededCallback || null,
    };
    this._ensureWebSocket();
  }

  // ── unsubscribeBars ────────────────────────────────────────────────────────

  unsubscribeBars(listenerGuid) {
    delete this._subscriptions[listenerGuid];
  }

  // ── getServerTime ──────────────────────────────────────────────────────────

  getServerTime(callback) {
    fetch('/api/time')
      .then(r => r.json())
      .then(t => callback(t))
      .catch(() => callback(Math.floor(Date.now() / 1000)));
  }

  // ── Analysis callback ──────────────────────────────────────────────────────

  setAnalysisCallback(cb) { this._onAnalysis = cb; }
  setCycleAnalysisCallback(cb) { this._onCycleAnalysis = cb; }

  // ── WebSocket ──────────────────────────────────────────────────────────────

  _ensureWebSocket() {
    if (this._ws && (this._ws.readyState === WebSocket.OPEN ||
                     this._ws.readyState === WebSocket.CONNECTING)) return;

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    this._ws = new WebSocket(`${proto}://${location.host}/ws/realtime`);

    this._ws.onopen = () => {
      console.log('DataFeed WebSocket connected');
      this._wsReady = true;
    };

    this._ws.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }

      if (msg.type === 'bar') {
        this._handleBarUpdate(msg);
      } else if (msg.type === 'history_ready') {
        this._handleHistoryReady(msg);
      } else if (msg.type === 'analysis') {
        if (this._onAnalysis) this._onAnalysis(msg.data);
      } else if (msg.type === 'snapshot') {
        if (this._onAnalysis && msg.analysis) this._onAnalysis(msg.analysis);
      } else if (msg.type === 'cycle_analysis' || msg.type === 'cycle_analysis_toggle' || msg.type === 'cycle_analysis_delete') {
        if (this._onCycleAnalysis) this._onCycleAnalysis(msg);
      }
    };

    this._ws.onclose = () => {
      console.log('DataFeed WebSocket closed, reconnecting in 3s…');
      this._wsReady = false;
      setTimeout(() => this._ensureWebSocket(), 3000);
    };

    this._ws.onerror = err => console.error('DataFeed WebSocket error:', err);
  }

  _handleBarUpdate(msg) {
    const resMap = { '5min': '5', '15min': '15', '60min': '60', '1D': '1D' };
    const barRes = resMap[msg.bar_size];
    if (!barRes) return;

    const msgSymbol = msg.symbol || 'MES';
    const tvBar = {
      time:   msg.bar.time * 1000,
      open:   msg.bar.open,
      high:   msg.bar.high,
      low:    msg.bar.low,
      close:  msg.bar.close,
      volume: msg.bar.volume,
    };
    for (const sub of Object.values(this._subscriptions)) {
      if (sub.resolution === barRes && sub.symbol === msgSymbol) sub.onTick(tvBar);
    }
  }

  // ── _handleHistoryReady ──────────────────────────────────────────────────
  //
  // Server-side dataManager broadcasts a `history_ready` WS message after a
  // background batch fetch completes for a (symbol, timeframe) range.  We
  // invoke the TradingView-supplied `onResetCacheNeededCallback` for every
  // matching active subscription so the chart re-queries `/api/history` and
  // displays the freshly-populated bars without requiring a manual reload.
  _handleHistoryReady(msg) {
    const resMap = { '5min': '5', '15min': '15', '60min': '60', '1D': '1D' };
    const tfRes = resMap[msg.timeframe];
    if (!tfRes) return;
    const msgSymbol = msg.symbol || 'MES';
    console.log(`DataFeed: history_ready for ${msgSymbol}/${msg.timeframe} — refreshing widgets (+${msg.added_bars || 0} bars)`);
    for (const sub of Object.values(this._subscriptions)) {
      if (sub.resolution === tfRes && sub.symbol === msgSymbol && sub.onResetCacheNeededCallback) {
        try {
          sub.onResetCacheNeededCallback();
        } catch (err) {
          console.warn('onResetCacheNeededCallback failed:', err);
        }
      }
    }
  }
}
