/**
 * Custom TradingView DataFeed adapter.
 *
 * REST endpoints:
 *   GET /api/config      → onReady
 *   GET /api/symbols     → resolveSymbol  (uses window._rthMode for session)
 *   GET /api/history     → getBars
 *   GET /api/time        → getServerTime
 *
 * WebSocket:
 *   WS /ws/realtime      → subscribeBars
 */

class MESDatafeed {
  constructor() {
    this._ws = null;
    this._wsReady = false;
    this._subscriptions = {};   // listenerGuid → { resolution, onTick }
    this._onAnalysis = null;    // callback for analysis updates
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

  resolveSymbol(symbolName, onResolve, onError) {
    fetch(`/api/symbols?symbol=${encodeURIComponent(symbolName)}`)
      .then(r => r.json())
      .then(info => {
        // Apply RTH / ETH session override
        if (window._rthMode) {
          // RTH: 9:30 AM – 4:00 PM ET, Mon–Fri
          info.session          = '0930-1600:12345';
          info.session_holidays = '';
        } else {
          // ETH (Globex): virtually 24h Sun–Fri
          info.session = '0000-2359:23456';
        }
        setTimeout(() => onResolve(info), 0);
      })
      .catch(err => {
        console.error('resolveSymbol error:', err);
        onError('SYMBOL_NOT_FOUND');
      });
  }

  // ── getBars ────────────────────────────────────────────────────────────────

  getBars(symbolInfo, resolution, periodParams, onResult, onError) {
    const { from, to, countBack } = periodParams;
    let url = `/api/history?symbol=${symbolInfo.name}&resolution=${resolution}&from=${from}&to=${to}`;
    if (countBack) url += `&countback=${countBack}`;

    fetch(url)
      .then(r => r.json())
      .then(data => {
        if (data.s === 'no_data') {
          const meta = { noData: true };
          // nextTime is in SECONDS (same unit as periodParams.from/to).
          // Do NOT convert to ms — bar timestamps use ms, but nextTime does not.
          if (data.nextTime != null) meta.nextTime = data.nextTime;
          onResult([], meta);
          return;
        }
        if (data.s !== 'ok') {
          onError('HISTORY_ERROR');
          return;
        }
        const bars = data.t.map((t, i) => ({
          time:   t * 1000,           // TradingView uses milliseconds
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

  // ── WebSocket Management ───────────────────────────────────────────────────

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

    this._ws.onerror = (err) => console.error('DataFeed WebSocket error:', err);
  }

  _handleBarUpdate(msg) {
    const resMap = { '1min': '1', '5min': '5' };
    const barResolution = resMap[msg.bar_size];
    if (!barResolution) return;

    const tvBar = {
      time:   msg.bar.time * 1000,
      open:   msg.bar.open,
      high:   msg.bar.high,
      low:    msg.bar.low,
      close:  msg.bar.close,
      volume: msg.bar.volume,
    };

    for (const sub of Object.values(this._subscriptions)) {
      if (sub.resolution === barResolution) sub.onTick(tvBar);
    }
  }
}
