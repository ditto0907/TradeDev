/**
 * Custom TradingView DataFeed adapter.
 *
 * Symbol name convention:
 *   'MES'     → ETH session (0000-2359, Sun–Fri) — default
 *   'MES_RTH' → RTH session (0930-1600 ET, Mon–Fri)
 *
 * Both symbols query the same backend history endpoint with symbol=MES.
 * Using distinct names forces TradingView to call resolveSymbol again
 * on each RTH/ETH toggle (same name → TV skips re-resolution).
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
    onResult([{
      symbol: 'MES',
      full_name: 'CME:MES',
      description: 'Micro E-mini S&P 500 Futures',
      exchange: 'CME',
      type: 'futures',
    }]);
  }

  // ── resolveSymbol ──────────────────────────────────────────────────────────
  //
  // 'MES'     → ETH (full Globex session)
  // 'MES_RTH' → RTH only (9:30–16:00 ET Mon–Fri)
  //
  // Both fetch their metadata from /api/symbols?symbol=MES; we override
  // the session and name fields so TV treats them as independent symbols.

  resolveSymbol(symbolName, onResolve, onError) {
    const isRTH = symbolName === 'MES_RTH';
    console.log('[DataFeed] resolveSymbol called:', symbolName, ' isRTH:', isRTH);

    fetch('/api/symbols?symbol=MES')
      .then(r => r.json())
      .then(info => {
        if (isRTH) {
          info.name        = 'MES_RTH';
          info.full_name   = 'CME:MES_RTH';
          info.description = 'Micro E-mini S&P 500 Futures (RTH)';
          info.session     = '0930-1600:12345';   // 9:30–16:00 ET, Mon–Fri
        } else {
          info.name        = 'MES';
          info.full_name   = 'CME:MES';
          info.session     = '0000-2359:23456';   // full Globex (Sun–Fri 24 h)
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
  // Always query the backend with symbol=MES regardless of whether the chart
  // is in RTH ('MES_RTH') or ETH ('MES') mode.

  getBars(symbolInfo, resolution, periodParams, onResult, onError) {
    const { from, to, countBack } = periodParams;
    // Strip _RTH suffix — backend only knows 'MES'
    const backendSymbol = 'MES';
    let url = `/api/history?symbol=${backendSymbol}&resolution=${resolution}&from=${from}&to=${to}`;
    if (countBack) url += `&countback=${countBack}`;
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

  subscribeBars(symbolInfo, resolution, onTick, listenerGuid) {
    this._subscriptions[listenerGuid] = { resolution, onTick };
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
      } else if (msg.type === 'analysis') {
        if (this._onAnalysis) this._onAnalysis(msg.data);
      } else if (msg.type === 'snapshot') {
        if (this._onAnalysis && msg.analysis) this._onAnalysis(msg.analysis);
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
    const resMap = { '1min': '1', '5min': '5' };
    const barRes = resMap[msg.bar_size];
    if (!barRes) return;

    const tvBar = {
      time:   msg.bar.time * 1000,
      open:   msg.bar.open,
      high:   msg.bar.high,
      low:    msg.bar.low,
      close:  msg.bar.close,
      volume: msg.bar.volume,
    };
    for (const sub of Object.values(this._subscriptions)) {
      if (sub.resolution === barRes) sub.onTick(tvBar);
    }
  }
}
