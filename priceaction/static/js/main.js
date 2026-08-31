'use strict';

// ── Application entry point ───────────────────────────────────────────────────
//
// Loaded as an ES module (`<script type="module">`).  All feature code lives in
// ./app/*.js; this file only bootstraps the UI and re-exports the handful of
// functions that inline `onclick=` attributes in index.html (and in dynamically
// generated table rows) still resolve through `window`.

import { initChart } from './app/chart.js';
import { initContractSelector } from './app/contracts.js';
import {
  initBottomTabs, initBottomResize, initSRLegendDrag, initPanelState, togglePanel,
} from './app/panels.js';
import { initPositionPolling } from './app/position.js';
import {
  initOrderForm, initBracketConfig, setBracketMode, setOrderSide,
  onOrderTypeChange, adjustQty, updateSummary, placeOrder,
} from './app/orderform.js';
import {
  initWatchlistClick, fetchWatchlistPrices, fetchWatchlistContractInfo,
} from './app/watchlist.js';
import {
  initStrategyTab, runStrategyBacktest, loadBacktestHistory, deleteCurrentBacktest,
  toggleBacktestMarkers, toggleFilteredDisplay, stratLocateTrade,
} from './app/strategy.js';
import { toggleSR } from './app/annotations.js';
import { cancelOrder } from './app/orders.js';
import {
  toggleTrades, toggleFileExpand, toggleFileOnChart, deleteTradeFile,
  handleTradeCSVUpload, saveTradeAnnotation, locateTradeOnChart,
} from './app/trades.js';
import {
  initSummaryModal, showSummaryModal, closeSummaryModal,
  toggleAnalysisActive, deleteAnalysis,
} from './app/marketcycle.js';

// ── Bootstrap ─────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  initChart();
  initBottomTabs();
  initBottomResize();
  initOrderForm();
  initBracketConfig();
  initSRLegendDrag();
  initPositionPolling();
  initWatchlistClick();
  initContractSelector();
  initSummaryModal();
  fetchWatchlistPrices();
  fetchWatchlistContractInfo();
  // Refresh watchlist prices every 60s
  setInterval(fetchWatchlistPrices, 60000);
  initStrategyTab();
  initPanelState();
});

// ── Inline-handler bridge ─────────────────────────────────────────────────────
// index.html and generated table markup use `onclick="fn(...)"`, which resolves
// against the global scope; module scope is not global, so expose them here.

Object.assign(window, {
  // order entry
  adjustQty,
  cancelOrder,
  onOrderTypeChange,
  placeOrder,
  setBracketMode,
  setOrderSide,
  updateSummary,
  // chart annotations / panels
  toggleSR,
  togglePanel,
  // trade logs
  toggleTrades,
  toggleFileExpand,
  toggleFileOnChart,
  deleteTradeFile,
  handleTradeCSVUpload,
  saveTradeAnnotation,
  locateTradeOnChart,
  // market cycle analyses
  showSummaryModal,
  closeSummaryModal,
  toggleAnalysisActive,
  deleteAnalysis,
  // strategy backtest
  runStrategyBacktest,
  loadBacktestHistory,
  deleteCurrentBacktest,
  toggleBacktestMarkers,
  toggleFilteredDisplay,
  stratLocateTrade,
});
