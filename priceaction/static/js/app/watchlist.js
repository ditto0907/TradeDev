import { state } from './state.js';
import { MES_TICK } from './constants.js';
import { setText } from './utils.js';
import { loadContractOptions, mountContractSelectorInActiveItem } from './contracts.js';
import { updateAnnotations, updateCycleBadge, updateSRPanel } from './annotations.js';
import { loadCycleAnalyses } from './marketcycle.js';
import { updateSummary } from './orderform.js';

// Per-symbol open prices for watchlist change calculation
const _symbolOpenPrices = {};

export function initWatchlistClick() {
  document.querySelectorAll('.watch-item').forEach(item => {
    item.addEventListener('click', async () => {
      const sym = item.dataset.symbol;
      if (!sym || !state.widget) return;
      // Update active state
      document.querySelectorAll('.watch-item').forEach(i => i.classList.remove('active'));
      item.classList.add('active');
      state.currentSymbol = sym;
      // Move contract dropdown into the newly-active watch-item
      mountContractSelectorInActiveItem();
      // Populate selector and get front-month token
      const frontToken = await loadContractOptions(sym);
      const token = frontToken || sym;
      if (frontToken) {
        const sel = document.getElementById('contract-selector');
        if (sel) sel.value = frontToken;
      }
      try {
        const res = state.widget.activeChart().resolution();
        state.widget.setSymbol(token, res, () => {
          console.log('[Watchlist] switched to', token);
          // Reload S/R analysis for new symbol
          fetch(`/api/analysis?symbol=${sym}`)
            .then(r => r.json())
            .then(analysis => {
              state.lastAnalysis = analysis;
              updateAnnotations(analysis);
              updateCycleBadge(analysis.market_cycle);
              updateSRPanel(analysis);
            })
            .catch(e => console.warn('Analysis fetch error:', e));
          // Reload market cycle analyses for new symbol
          loadCycleAnalyses();
        });
      } catch (e) {
        console.warn('[Watchlist] setSymbol error:', e);
      }
    });
  });
}

export function updateWatchlistPrice(symbol, price) {
  const key = symbol.toLowerCase();
  const priceEl = document.getElementById(`wl-${key}-price`);
  const chgEl   = document.getElementById(`wl-${key}-chg`);
  if (priceEl) priceEl.textContent = price != null ? price.toFixed(2) : '—';
  if (chgEl) {
    const openPrice = _symbolOpenPrices[symbol];
    if (openPrice && price != null) {
      const chg    = price - openPrice;
      const chgPct = (chg / openPrice * 100).toFixed(2);
      chgEl.textContent = `${chg >= 0 ? '+' : ''}${chgPct}%`;
      chgEl.className   = `watch-change ${chg >= 0 ? 'up' : 'down'}`;
    }
  }
  // Track open price
  if (!_symbolOpenPrices[symbol] && price != null) {
    _symbolOpenPrices[symbol] = price;
  }
}

export async function fetchWatchlistPrices() {
  try {
    const res = await fetch('/api/watchlist_prices');
    const data = await res.json();
    for (const [sym, info] of Object.entries(data)) {
      if (sym === 'MES') continue; // MES updated via WebSocket
      const key = sym.toLowerCase();
      const priceEl = document.getElementById(`wl-${key}-price`);
      const chgEl   = document.getElementById(`wl-${key}-chg`);
      if (priceEl) priceEl.textContent = info.close != null ? info.close.toFixed(2) : '—';
      if (chgEl && info.change_pct != null) {
        chgEl.textContent = `${info.change_pct >= 0 ? '+' : ''}${info.change_pct.toFixed(2)}%`;
        chgEl.className   = `watch-change ${info.change_pct >= 0 ? 'up' : 'down'}`;
      }
    }
  } catch (e) {
    console.warn('fetchWatchlistPrices error:', e);
  }
}

export async function fetchWatchlistContractInfo() {
  const symbols = ['MES', 'MNQ', 'NK225M', 'NK225MC', 'MGC'];
  for (const sym of symbols) {
    try {
      const res = await fetch(`/api/symbols?symbol=${sym}`);
      const info = await res.json();
      const key = sym.toLowerCase();
      const exchEl = document.getElementById(`wl-${key}-exch`);
      if (exchEl) {
        const ibSym = info.ib_symbol || sym;
        const exch  = info.exchange || info.listed_exchange || '';
        // Show: "CME · MESM6" style but we only have the root symbol, use ib_symbol
        exchEl.textContent = ibSym !== sym ? `${exch} · ${ibSym}` : exch;
      }
    } catch (e) {
      console.warn('fetchWatchlistContractInfo error for', sym, e);
    }
  }
}

// ── Bid / Ask ─────────────────────────────────────────────────────────────────

export function updateBidAsk(lastPrice) {
  if (lastPrice == null) return;
  const bid = (lastPrice - MES_TICK).toFixed(2);
  const ask = (lastPrice + MES_TICK).toFixed(2);
  setText('bid-price', bid);
  setText('ask-price', ask);
  setText('bid-size', '—');
  setText('ask-size', '—');
  const priceInput = document.getElementById('order-price');
  if (priceInput && !priceInput.value) {
    priceInput.value = state.orderSide === 'buy' ? bid : ask;
    updateSummary();
  }
}
