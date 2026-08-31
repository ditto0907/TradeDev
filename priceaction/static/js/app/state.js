// ── Shared mutable application state ──────────────────────────────────────────
//
// ES module exports are read-only bindings for the importing module, so all
// cross-module mutable state lives on this single object.  Module-private state
// (shape caches, per-tab caches, …) stays inside its owning module.

export const state = {
  /** TradingView widget instance (set by chart.js once initialised). */
  widget: null,

  /** Active base symbol, without any @CONT_FRONT / @YYYYMM token. */
  currentSymbol: 'MES',

  /** Most recent 5min bar received over the WebSocket feed. */
  lastBar: null,

  /** Most recent S/R analysis payload. */
  lastAnalysis: null,

  /** Current IB position for the active instrument. */
  currentPosition: { symbol: 'MES', position: 0, avg_cost: 0, side: 'FLAT' },

  /** Order-entry side ('buy' | 'sell'). */
  orderSide: 'buy',

  /** Default bracket offsets, in ticks. */
  bracket: { tpTicks: 80, slTicks: 80 },
};

/**
 * Return the active chart, or null when the widget is not ready.
 * Callers should bail out on null instead of throwing.
 */
export function getChart() {
  if (!state.widget) return null;
  try { return state.widget.activeChart(); } catch { return null; }
}

/** Current chart resolution, falling back to '5' when unavailable. */
export function getResolution(chart) {
  try { return (chart || getChart()).resolution(); } catch { return '5'; }
}
