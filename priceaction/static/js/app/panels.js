import { state } from './state.js';

// ── SR Legend Drag ────────────────────────────────────────────────────────────

export function initSRLegendDrag() {
  const legend = document.getElementById('sr-legend');
  const handle = document.getElementById('sr-legend-handle');
  if (!legend || !handle) return;

  const saved = localStorage.getItem('srLegendPos');
  if (saved) {
    try {
      const { left, top } = JSON.parse(saved);
      legend.style.left = left;
      legend.style.top  = top;
    } catch {}
  }

  let dragging = false, startX = 0, startY = 0, startLeft = 0, startTop = 0;

  handle.addEventListener('mousedown', e => {
    dragging  = true;
    startX    = e.clientX;
    startY    = e.clientY;
    startLeft = parseInt(legend.style.left) || legend.offsetLeft;
    startTop  = parseInt(legend.style.top)  || legend.offsetTop;
    document.body.style.cursor = 'grabbing';
    e.preventDefault();
  });

  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const maxW = window.innerWidth  - legend.offsetWidth;
    const maxH = window.innerHeight - legend.offsetHeight;
    legend.style.left = Math.max(0, Math.min(maxW, startLeft + (e.clientX - startX))) + 'px';
    legend.style.top  = Math.max(0, Math.min(maxH, startTop  + (e.clientY - startY))) + 'px';
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = '';
    localStorage.setItem('srLegendPos', JSON.stringify({
      left: legend.style.left, top: legend.style.top,
    }));
  });
}

// ── Bottom Tabs ───────────────────────────────────────────────────────────────

export function initBottomTabs() {
  const main = document.getElementById('main');
  const minBtn = document.getElementById('bottom-minimize');

  document.querySelectorAll('.btab').forEach(tab => {
    tab.addEventListener('click', () => {
      const pane = tab.dataset.pane;
      // If minimized, restore on tab click
      if (main.classList.contains('bottom-minimized')) {
        main.classList.remove('bottom-minimized');
        if (minBtn) { minBtn.textContent = '▼'; minBtn.title = 'Minimize'; }
        setTimeout(() => { if (state.widget) state.widget.resize(); }, 50);
      }
      document.querySelectorAll('.btab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.btab-pane').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById(`pane-${pane}`)?.classList.add('active');
    });
  });

  if (minBtn) {
    minBtn.addEventListener('click', () => {
      const minimized = main.classList.toggle('bottom-minimized');
      minBtn.textContent = minimized ? '▲' : '▼';
      minBtn.title = minimized ? 'Restore' : 'Minimize';
      if (minimized) {
        // Save current inline grid size and clear it so CSS class takes effect
        main.dataset.bottomGrid = main.style.gridTemplateRows || '';
        main.style.gridTemplateRows = '';
      } else {
        // Restore previously saved inline grid size
        if (main.dataset.bottomGrid) {
          main.style.gridTemplateRows = main.dataset.bottomGrid;
        }
      }
      setTimeout(() => { if (state.widget) state.widget.resize(); }, 50);
    });
  }

  // Default to minimized on initial load.
  main.classList.add('bottom-minimized');
  if (minBtn) { minBtn.textContent = '▲'; minBtn.title = 'Restore'; }
}

// ── Bottom Panel Resize ───────────────────────────────────────────────────────

export function initBottomResize() {
  const handle = document.getElementById('bottom-resize');
  const main   = document.getElementById('main');
  if (!handle || !main) return;
  let dragging = false, startY = 0, startH = 0;
  handle.addEventListener('mousedown', e => {
    dragging = true; startY = e.clientY;
    startH   = document.getElementById('bottom')?.offsetHeight || 180;
    document.body.style.cursor = 'row-resize';
    document.body.style.userSelect = 'none';
    // Block pointer events on iframes so mousemove isn't swallowed
    document.querySelectorAll('iframe').forEach(f => f.style.pointerEvents = 'none');
    e.preventDefault();
  });
  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const newH = Math.max(100, Math.min(500, startH + (startY - e.clientY)));
    main.style.gridTemplateRows = `1fr ${newH}px`;
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    document.querySelectorAll('iframe').forEach(f => f.style.pointerEvents = '');
  });
}

// ── Panel Toggle (Collapsible Watchlist & Order Entry) ────────────────────────

export function initPanelState() {
  const wlState = localStorage.getItem('panel_watchlist');
  const orderState = localStorage.getItem('panel_order');
  if (wlState === 'hidden') {
    document.getElementById('main').classList.add('watchlist-hidden');
    const btn = document.getElementById('toggle-watchlist');
    if (btn) btn.classList.remove('active');
  }
  if (orderState === 'hidden') {
    document.getElementById('main').classList.add('order-hidden');
    const btn = document.getElementById('toggle-order');
    if (btn) btn.classList.remove('active');
  }
}

export function togglePanel(panelName) {
  const main = document.getElementById('main');
  if (!main) return;
  const cls = panelName === 'watchlist' ? 'watchlist-hidden' : 'order-hidden';
  const btnId = panelName === 'watchlist' ? 'toggle-watchlist' : 'toggle-order';
  const storageKey = 'panel_' + panelName;
  const btn = document.getElementById(btnId);

  main.classList.toggle(cls);
  const isHidden = main.classList.contains(cls);
  localStorage.setItem(storageKey, isHidden ? 'hidden' : 'visible');
  if (btn) {
    btn.classList.toggle('active', !isHidden);
  }

  // Trigger chart resize after panel toggle
  if (state.widget) {
    setTimeout(() => {
      try { state.widget.activeChart(); } catch (_) {}
    }, 100);
  }
}
