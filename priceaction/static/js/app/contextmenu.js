import { state } from './state.js';
import { snapToTick } from './utils.js';
import {
  showOrderConfirm, showBracketConfirm, showFlattenConfirm, showCancelAllConfirm,
} from './dialogs.js';

/**
 * Build the right-click context-menu entries for a chart price.
 * Returns [] when the price is unusable so the library falls back to its
 * default menu.
 */
export function buildContextMenuItems(rawPrice) {
  if (rawPrice == null || isNaN(rawPrice)) return [];
  console.log('buildContextMenuItems price:', rawPrice);

  const price     = snapToTick(rawPrice);
  const lastPrice = state.lastBar ? state.lastBar.close : price;
  const isAbove   = price >= lastPrice;
  const pStr      = price.toFixed(2);
  const qty       = parseInt(document.getElementById('order-qty')?.value) || 1;

  const items = [];
  try {
    items.push({ text: '-', position: 'top' });   // separator

    // ── Conditional orders based on position relative to last price ──────
    if (isAbove) {
      items.push({
        position: 'top',
        text: `Buy Stop  @ ${pStr}  (${qty} ct)`,
        click: () => showOrderConfirm('BUY', 'stop', null, price, qty),
      });
      items.push({
        position: 'top',
        text: `Sell Limit @ ${pStr}  (${qty} ct)`,
        click: () => showOrderConfirm('SELL', 'limit', price, null, qty),
      });
    } else {
      items.push({
        position: 'top',
        text: `Buy Limit  @ ${pStr}  (${qty} ct)`,
        click: () => showOrderConfirm('BUY', 'limit', price, null, qty),
      });
      items.push({
        position: 'top',
        text: `Sell Stop  @ ${pStr}  (${qty} ct)`,
        click: () => showOrderConfirm('SELL', 'stop', null, price, qty),
      });
    }

    items.push({ text: '-', position: 'top' });   // separator

    // ── Bracket orders (entry + TP + SL) ────────────────────────────────
    if (isAbove) {
      items.push({
        position: 'top',
        text: `Bracket Buy Stop  @ ${pStr}  (TP+SL)`,
        click: () => showBracketConfirm('BUY', 'stop', null, price, qty),
      });
    } else {
      items.push({
        position: 'top',
        text: `Bracket Buy Limit @ ${pStr}  (TP+SL)`,
        click: () => showBracketConfirm('BUY', 'limit', price, null, qty),
      });
    }
    if (!isAbove) {
      items.push({
        position: 'top',
        text: `Bracket Sell Stop  @ ${pStr}  (TP+SL)`,
        click: () => showBracketConfirm('SELL', 'stop', null, price, qty),
      });
    } else {
      items.push({
        position: 'top',
        text: `Bracket Sell Limit @ ${pStr}  (TP+SL)`,
        click: () => showBracketConfirm('SELL', 'limit', price, null, qty),
      });
    }

    items.push({ text: '-', position: 'top' });   // separator

    // ── Market orders always available ──────────────────────────────────
    items.push({
      position: 'top',
      text: `Market Buy  (${qty} ct)`,
      click: () => showOrderConfirm('BUY', 'market', null, null, qty),
    });
    items.push({
      position: 'top',
      text: `Market Sell  (${qty} ct)`,
      click: () => showOrderConfirm('SELL', 'market', null, null, qty),
    });

    items.push({ text: '-', position: 'top' });   // separator

    // ── Position management ─────────────────────────────────────────────
    if (state.currentPosition.position !== 0) {
      const posLabel = `${state.currentPosition.side} ${Math.abs(state.currentPosition.position)}`;
      items.push({
        position: 'top',
        text: `⚡ Flatten Position (${posLabel})`,
        click: () => showFlattenConfirm(),
      });
    }

    // ── Cancel all ──────────────────────────────────────────────────────
    items.push({
      position: 'top',
      text: '✕ Cancel All Orders',
      click: () => showCancelAllConfirm(),
    });
  } catch (e) {
    console.warn('Context menu build error:', e);
    return [];
  }

  return items;
}
