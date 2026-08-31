import { state, getResolution } from './state.js';

// Cache of symbol_list API response, keyed by base symbol
let _symbolListCache = {};

/**
 * Populate the contract selector dropdown for *baseSym*.
 * Returns the front-month token (newest monthly contract) if one exists,
 * or null when no monthly data is in the DB yet.
 * Callers are responsible for calling widget.setSymbol() with the returned token.
 */
export async function loadContractOptions(baseSym) {
  const sel = document.getElementById('contract-selector');
  if (!sel) return null;

  try {
    // Fetch once per base symbol, then cache
    if (!_symbolListCache[baseSym]) {
      const res = await fetch('/api/symbol_list');
      const data = await res.json();
      // Group all tokens by base symbol
      const grouped = {};
      for (const item of (data.symbols || [])) {
        const base = item.token.split('@')[0];
        if (!grouped[base]) grouped[base] = [];
        grouped[base].push(item);
      }
      // Cache all groups at once
      Object.assign(_symbolListCache, grouped);
    }

    const items = _symbolListCache[baseSym] || [];
    sel.innerHTML = '';

    const continuous = items.filter(i => i.kind === 'continuous');
    const monthly    = items.filter(i => i.kind === 'month');

    if (continuous.length) {
      const grp = document.createElement('optgroup');
      grp.label = 'Continuous';
      for (const item of continuous) {
        const opt = document.createElement('option');
        opt.value = item.token;
        const methodLabel = {
          front: 'Front (no adj)',
          cont_ratio: 'Ratio Adj',
          cont_difference: 'Diff Adj',
        }[item.method] || item.method;
        opt.textContent = methodLabel;
        grp.appendChild(opt);
      }
      sel.appendChild(grp);
    }

    // Monthly contracts — newest (front month) first
    const sortedMonthly = [...monthly].reverse();
    if (sortedMonthly.length) {
      const grp = document.createElement('optgroup');
      grp.label = 'Monthly Contracts';
      for (const item of sortedMonthly) {
        const opt = document.createElement('option');
        opt.value = item.token;
        // Show "YYYY-MM" extracted from token suffix (e.g. "MES@202506" → "2026-06")
        const cm = item.token.split('@')[1] || item.token;
        const cmLabel = cm.length === 6 ? `${cm.slice(0, 4)}-${cm.slice(4)}` : cm;
        opt.textContent = cmLabel;
        grp.appendChild(opt);
      }
      sel.appendChild(grp);
    }

    // Return front-month token so the caller can setSymbol with it
    return sortedMonthly.length ? sortedMonthly[0].token : null;

  } catch (e) {
    console.warn('[ContractSelector] loadContractOptions error:', e);
    return null;
  }
}

export function syncContractSelector() {
  const sel = document.getElementById('contract-selector');
  if (!sel || !state.widget) return;
  let sym;
  try { sym = state.widget.activeChart().symbol(); } catch { return; }
  // If the chart is using a bare symbol (e.g. 'MES'), treat as CONT_FRONT
  const token = sym.includes('@') ? sym : `${sym}@CONT_FRONT`;
  if ([...sel.options].some(o => o.value === token)) {
    sel.value = token;
  }
}

// Move the contract-selector wrapper into the currently-active watch-item.
export function mountContractSelectorInActiveItem() {
  const wrap = document.getElementById('contract-selector-wrap');
  if (!wrap) return;
  const active = document.querySelector('.watch-item.active');
  if (!active) return;
  if (wrap.parentElement !== active) active.appendChild(wrap);
}

export function initContractSelector() {
  const sel = document.getElementById('contract-selector');
  if (!sel) return;
  // Don't let clicking the dropdown trigger the watch-item click
  const wrap = document.getElementById('contract-selector-wrap');
  if (wrap) {
    wrap.addEventListener('click', (e) => e.stopPropagation());
    wrap.addEventListener('mousedown', (e) => e.stopPropagation());
  }
  // Mount under the currently-active watch-item
  mountContractSelectorInActiveItem();
  sel.addEventListener('change', () => {
    const token = sel.value;
    if (!token || !state.widget) return;
    state.currentSymbol = token.split('@')[0];
    const res = getResolution();
    state.widget.setSymbol(token, res, () => {
      console.log('[ContractSelector] switched to', token);
    });
  });
}
