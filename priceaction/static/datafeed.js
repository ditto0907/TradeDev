/**
 * Custom TradingView DataFeed adapter.
 *
 * Implements the TradingView JS DataFeed interface to connect the charting
 * library to our FastAPI backend.
 *
 * REST endpoints used:
 *   GET /api/config      → onReady configuration
 *   GET /api/symbols     → resolveSymbol
 *   GET /api/history     → getBars (historical OHLCV)
 *   GET /api/time        → server time
 *
 * WebSocket used:
 *   WS /ws/realtime      → subscribeBars (real-time updates)
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
      .then(cfg => {
        setTimeout(() => callback(cfg), 0);
      })
      .catch(err => console.error('DataFeed onReady error:', err));
  }

  // ── searchSymbols ──────────────────────────────────────────────────────────

  searchSymbols(userInput, exchange, symbolType, onResult) {
    // Only MES is available
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
      .then(info => setTimeout(() => onResolve(info), 0))
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
          onResult([], { noData: true });
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

  // ── WebSocket Management ───────────────────────────────────────────────────

  /**
   * Register a callback to receive analysis updates (S/R levels, market cycle).
   * Called by app.js to wire up annotations.
   */
  setAnalysisCallback(cb) {
    this._onAnalysis = cb;
  }

  _ensureWebSocket() {
    if (this._ws && (this._ws.readyState === WebSocket.OPEN || this._ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${proto}://${location.host}/ws/realtime`;
    this._ws = new WebSocket(wsUrl);

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
        // Initial snapshot: just trigger analysis overlay
        if (this._onAnalysis && msg.analysis) this._onAnalysis(msg.analysis);
      }
    };

    this._ws.onclose = () => {
      console.log('DataFeed WebSocket closed, reconnecting in 3s…');
      this._wsReady = false;
      setTimeout(() => this._ensureWebSocket(), 3000);
    };

    this._ws.onerror = (err) => {
      console.error('DataFeed WebSocket error:', err);
    };
  }

  _handleBarUpdate(msg) {
    // Map bar_size key to TradingView resolution string
    const resMap = { '1min': '1', '5min': '5' };
    const barResolution = resMap[msg.bar_size];
    if (!barResolution) return;

    const bar = msg.bar;
    const tvBar = {
      time:   bar.time * 1000,    // seconds → milliseconds
      open:   bar.open,
      high:   bar.high,
      low:    bar.low,
      close:  bar.close,
      volume: bar.volume,
    };

    // Notify all matching subscriptions
    for (const sub of Object.values(this._subscriptions)) {
      if (sub.resolution === barResolution) {
        sub.onTick(tvBar);
      }
    }
  }
}
