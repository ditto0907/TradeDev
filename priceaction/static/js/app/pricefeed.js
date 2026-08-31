import { state } from './state.js';
import { setWsStatus } from './utils.js';
import { updateWatchlistPrice, updateBidAsk } from './watchlist.js';
import { updateWorkingOrderRow } from './orders-table.js';

let _openPrice = null;

export function connectPriceFeed(datafeed) {
  const origEnsure = datafeed._ensureWebSocket.bind(datafeed);
  datafeed._ensureWebSocket = function() {
    origEnsure();
    // Only wrap onmessage ONCE per socket instance.  _ensureWebSocket is
    // called on every subscribeBars (symbol/resolution change); without this
    // guard each call would stack another wrapper, causing handlePriceMessage
    // to run N+1 times per message and leaking closures.
    const ws = datafeed._ws;
    if (ws && !ws._priceFeedWrapped) {
      ws._priceFeedWrapped = true;
      const origOnMsg = ws.onmessage;
      ws.onmessage = function(event) {
        if (origOnMsg) origOnMsg.call(ws, event);
        handlePriceMessage(event);
      };
    }
  };
  datafeed._ensureWebSocket();
}

function handlePriceMessage(event) {
  let msg;
  try { msg = JSON.parse(event.data); } catch { return; }

  if (msg.type === 'bar' && msg.bar_size === '5min') {
    const barSymbol = msg.symbol || 'MES';
    // Update watchlist for all symbols
    updateWatchlistPrice(barSymbol, msg.bar.close);
    if (barSymbol === (state.currentSymbol || 'MES')) {
      updateTopbarOHLC(msg.bar);
      updateBidAsk(msg.bar.close);
      state.lastBar = msg.bar;
    }

  } else if (msg.type === 'snapshot' && msg.bars_5min?.length > 0) {
    const latest = msg.bars_5min[msg.bars_5min.length - 1];
    updateTopbarOHLC(latest);
    updateWatchlistPrice('MES', latest.close);
    updateBidAsk(latest.close);
    state.lastBar = latest;
    setWsStatus('live', 'Live');

  } else if (msg.type === 'order_update') {
    updateWorkingOrderRow(msg.order);
  }
  // cycle_analysis messages are handled by handleCycleAnalysisWS via datafeed callback
}

// ── Topbar OHLC (disabled - topbar simplified) ────────────────────────────────

function updateTopbarOHLC(bar) {
  // Topbar no longer displays symbol-specific data
  // Keeping function for compatibility but removing DOM updates
  if (_openPrice == null) _openPrice = bar.open;
}
