import { state } from './state.js';
import { MES_TICK } from './constants.js';
import { showToast, snapToTick } from './utils.js';
import { placeQuickOrder, placeBracketOrder, cancelAllOrders, flattenPosition } from './orders.js';

export function showOrderConfirm(action, orderType, limitPrice, stopPrice, qty) {
  const price     = limitPrice || stopPrice;
  const typeLabel = orderType === 'market' ? 'MARKET'
                  : orderType === 'limit'  ? `LIMIT @ ${price?.toFixed(2)}`
                  : orderType === 'stop'   ? `STOP @ ${price?.toFixed(2)}`
                  : 'STP LMT';
  const side      = action === 'BUY' ? 'buy' : 'sell';

  showConfirmDialog({
    title: `Confirm ${action} Order`,
    body:  `<div class="confirm-order-details">
              <div class="confirm-row"><span>Action</span><strong class="${side}">${action}</strong></div>
              <div class="confirm-row"><span>Type</span><strong>${typeLabel}</strong></div>
              <div class="confirm-row"><span>Quantity</span><strong>${qty} ct</strong></div>
              <div class="confirm-row"><span>Symbol</span><strong>MES</strong></div>
            </div>`,
    confirmClass: side,
    confirmText:  `${action} ${qty} MES`,
    onConfirm:    () => placeQuickOrder(action, orderType, limitPrice, stopPrice),
  });
}

export function showBracketConfirm(action, orderType, limitPrice, stopPrice, qty) {
  const entryPrice = limitPrice || stopPrice;
  const isBuy      = action === 'BUY';
  const tpOffset   = state.bracket.tpTicks * MES_TICK;
  const slOffset   = state.bracket.slTicks * MES_TICK;
  const tpDefault  = snapToTick(isBuy ? entryPrice + tpOffset : entryPrice - tpOffset);
  const slDefault  = snapToTick(isBuy ? entryPrice - slOffset : entryPrice + slOffset);
  const side       = isBuy ? 'buy' : 'sell';
  const typeLabel  = orderType === 'limit' ? `LIMIT @ ${entryPrice?.toFixed(2)}`
                   : `STOP @ ${entryPrice?.toFixed(2)}`;

  showConfirmDialog({
    title: `Confirm Bracket ${action}`,
    body:  `<div class="confirm-order-details">
              <div class="confirm-row"><span>Entry</span><strong class="${side}">${action} ${typeLabel}</strong></div>
              <div class="confirm-row"><span>Quantity</span><strong>${qty} ct</strong></div>
              <div class="confirm-row">
                <span>Take Profit</span>
                <input type="number" id="bracket-tp" class="confirm-input" value="${tpDefault.toFixed(2)}" step="0.25" />
              </div>
              <div class="confirm-row">
                <span>Stop Loss</span>
                <input type="number" id="bracket-sl" class="confirm-input" value="${slDefault.toFixed(2)}" step="0.25" />
              </div>
            </div>`,
    confirmClass: side,
    confirmText:  `${action} Bracket`,
    onConfirm:    () => {
      const tpRaw = parseFloat(document.getElementById('bracket-tp')?.value);
      const slRaw = parseFloat(document.getElementById('bracket-sl')?.value);
      if (isNaN(tpRaw) || tpRaw <= 0) {
        showToast('Invalid take-profit price', 'error');
        return;
      }
      if (isNaN(slRaw) || slRaw <= 0) {
        showToast('Invalid stop-loss price', 'error');
        return;
      }
      const tp = snapToTick(tpRaw);
      const sl = snapToTick(slRaw);
      // Directional sanity: for a BUY the TP must be above and SL below the
      // entry (reverse for a SELL).  Catches transposed TP/SL before it hits IB.
      const entry = entryPrice;
      if (entry != null && !isNaN(entry)) {
        if (isBuy && (tp <= entry || sl >= entry)) {
          showToast('For a BUY: take-profit must be above and stop-loss below the entry', 'error');
          return;
        }
        if (!isBuy && (tp >= entry || sl <= entry)) {
          showToast('For a SELL: take-profit must be below and stop-loss above the entry', 'error');
          return;
        }
      }
      placeBracketOrder(action, orderType, limitPrice, stopPrice, tp, sl);
    },
  });
}

export function showFlattenConfirm() {
  const side = state.currentPosition.side;
  const qty  = Math.abs(state.currentPosition.position);
  showConfirmDialog({
    title: 'Flatten Position',
    body:  `<div class="confirm-order-details">
              <div class="confirm-row"><span>Current Position</span><strong>${side} ${qty} MES</strong></div>
              <div class="confirm-row"><span>Action</span><strong>Market Close All</strong></div>
            </div>
            <p style="color:var(--orange);font-size:11px;margin-top:8px">⚠ This will close your entire position at market.</p>`,
    confirmClass: 'sell',
    confirmText:  'Flatten Now',
    onConfirm:    () => flattenPosition(),
  });
}

export function showCancelAllConfirm() {
  showConfirmDialog({
    title: 'Cancel All Orders',
    body:  `<p style="margin:12px 0">Cancel <strong>all</strong> working orders?</p>`,
    confirmClass: 'sell',
    confirmText:  'Cancel All',
    onConfirm:    () => cancelAllOrders(),
  });
}

/**
 * Generic confirmation dialog.
 * Options: { title, body (HTML), confirmClass, confirmText, onConfirm, onCancel? }
 */
export function showConfirmDialog({ title, body, confirmClass, confirmText, onConfirm, onCancel }) {
  // Remove any existing dialog
  document.getElementById('order-confirm-overlay')?.remove();

  const overlay = document.createElement('div');
  overlay.id = 'order-confirm-overlay';
  overlay.innerHTML = `
    <div class="confirm-dialog">
      <div class="confirm-title">${title}</div>
      <div class="confirm-body">${body}</div>
      <div class="confirm-actions">
        <button class="confirm-btn cancel" id="confirm-cancel">Cancel</button>
        <button class="confirm-btn ${confirmClass}" id="confirm-ok">${confirmText}</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  // Focus the confirm button
  const okBtn     = document.getElementById('confirm-ok');
  const cancelBtn = document.getElementById('confirm-cancel');
  okBtn.focus();

  // Guard so the confirm action can only ever run once, no matter how the
  // dialog is dismissed (button click, Enter key, or both firing together).
  let settled = false;
  const cleanup = () => {
    document.removeEventListener('keydown', onKey);
    overlay.remove();
  };
  const confirm = () => {
    if (settled) return;
    settled = true;
    cleanup();
    onConfirm();
  };
  const dismiss = () => {
    if (settled) return;
    settled = true;
    cleanup();
    if (onCancel) onCancel();
  };

  cancelBtn.addEventListener('click', dismiss);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) dismiss();
  });
  okBtn.addEventListener('click', confirm);

  // Keyboard: ESC to dismiss, Enter to confirm. preventDefault stops the
  // focused OK button from also firing a native click on the same Enter.
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); dismiss(); }
    else if (e.key === 'Enter') { e.preventDefault(); confirm(); }
  };
  document.addEventListener('keydown', onKey);
}
