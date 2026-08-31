// ── Shared constants ──────────────────────────────────────────────────────────

export const CYCLE_COLORS = {
  markup:       'rgba(38,166,154,0.10)',
  markdown:     'rgba(239,83,80,0.10)',
  accumulation: 'rgba(33,150,243,0.10)',
  distribution: 'rgba(255,152,0,0.10)',
};

export const CYCLE_LABELS = {
  markup:       'Markup (Uptrend)',
  markdown:     'Markdown (Downtrend)',
  accumulation: 'Accumulation',
  distribution: 'Distribution',
  unknown:      '—',
};

export const MES_TICK   = 0.25;
export const MES_TICK_$ = 1.25;
export const MES_MARGIN = 1650;
export const MES_MULTIPLIER = 5;  // Contract multiplier ($5 per point)

// Position polling interval
export const POSITION_POLL_MS = 5000;

// Order statuses that take an order out of the working-orders table
export const TERMINAL_STATUSES = ['Filled', 'Cancelled', 'Inactive', 'ApiCancelled'];

// ── Market cycle annotation palette ───────────────────────────────────────────

export const MC_COLORS = {
  'Opening Range':          { bg: 'rgba(33,150,243,0.12)',  border: 'rgba(33,150,243,0.4)',  text: '#2196F3' },
  'Bear Leg':               { bg: 'rgba(239,83,80,0.12)',   border: 'rgba(239,83,80,0.4)',   text: '#ef5350' },
  'Bull Leg':               { bg: 'rgba(38,166,154,0.12)',  border: 'rgba(38,166,154,0.4)',  text: '#26a69a' },
  'Bull Breakout':          { bg: 'rgba(38,166,154,0.18)',  border: 'rgba(38,166,154,0.5)',  text: '#26a69a' },
  'Bear Breakout':          { bg: 'rgba(239,83,80,0.18)',   border: 'rgba(239,83,80,0.5)',   text: '#ef5350' },
  'Reversal / Double Bottom':{ bg: 'rgba(255,152,0,0.12)', border: 'rgba(255,152,0,0.4)',   text: '#ff9800' },
  'Reversal / Double Top':  { bg: 'rgba(255,152,0,0.12)',  border: 'rgba(255,152,0,0.4)',   text: '#ff9800' },
  'Trading Range':          { bg: 'rgba(128,128,128,0.08)', border: 'rgba(128,128,128,0.3)', text: '#9e9e9e' },
  'Tight Trading Range':    { bg: 'rgba(128,128,128,0.06)', border: 'rgba(128,128,128,0.2)', text: '#9e9e9e' },
  'Channel':                { bg: 'rgba(156,39,176,0.10)',  border: 'rgba(156,39,176,0.3)',  text: '#9c27b0' },
  'Measured Move':          { bg: 'rgba(0,188,212,0.10)',   border: 'rgba(0,188,212,0.3)',   text: '#00bcd4' },
  'Climax':                 { bg: 'rgba(244,67,54,0.15)',   border: 'rgba(244,67,54,0.5)',   text: '#f44336' },
};

export const MC_DEFAULTS = { bg: 'rgba(100,181,246,0.10)', border: 'rgba(100,181,246,0.3)', text: '#64b5f6' };

// Sentinel: no upper timestamp bound for backtest queries
export const STRAT_TS_MAX = 9999999999;
