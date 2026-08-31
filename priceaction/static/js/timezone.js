/* ────────────────────────────────────────────────────────────────────────────
 * Shared application-timezone module.
 *
 * Provides:
 *   - window.AppTZ.get()                 → IANA tz id (string)
 *   - window.AppTZ.set(tz, opts)         → persist + notify (opts.silent skips event)
 *   - window.AppTZ.onChange(cb)          → returns disposer
 *   - window.AppTZ.formatTs(ts, opts)    → format unix-seconds in active tz
 *   - window.AppTZ.mountSwitcher(container)
 *
 * Persistence: localStorage['app:timezone'] (default 'America/New_York').
 * The Trade page additionally two-way binds with the TradingView chart's
 * timezone API (see app.js).
 * ──────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  var STORAGE_KEY = 'app:timezone';
  var DEFAULT_TZ  = 'America/New_York';

  // Curated list of timezones relevant to US futures + common user locales.
  // value = IANA id; label = display.
  var PRESETS = [
    { value: 'America/New_York',    label: 'New York (ET)' },
    { value: 'America/Chicago',     label: 'Chicago (CT)' },
    { value: 'America/Los_Angeles', label: 'Los Angeles (PT)' },
    { value: 'Etc/UTC',             label: 'UTC' },
    { value: 'Europe/London',       label: 'London' },
    { value: 'Asia/Shanghai',       label: 'Shanghai (CN)' },
    { value: 'Asia/Tokyo',          label: 'Tokyo' },
    { value: 'Asia/Hong_Kong',      label: 'Hong Kong' },
  ];

  // Inject into PRESETS the browser-detected tz if not already present.
  try {
    var localTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (localTz && !PRESETS.some(function (p) { return p.value === localTz; })) {
      PRESETS.push({ value: localTz, label: localTz + ' (Local)' });
    }
  } catch (e) { /* ignore */ }

  var listeners = [];
  var current = null;

  function _read() {
    try {
      var v = localStorage.getItem(STORAGE_KEY);
      if (v) return v;
    } catch (e) { /* ignore */ }
    return DEFAULT_TZ;
  }

  function _write(tz) {
    try { localStorage.setItem(STORAGE_KEY, tz); } catch (e) { /* ignore */ }
  }

  function get() {
    if (current == null) current = _read();
    return current;
  }

  function set(tz, opts) {
    if (!tz || typeof tz !== 'string') return;
    if (tz === get()) return;
    current = tz;
    _write(tz);
    if (opts && opts.silent) return;
    var ev = { tz: tz, source: (opts && opts.source) || 'api' };
    // notify direct subscribers
    for (var i = 0; i < listeners.length; i++) {
      try { listeners[i](tz, ev); } catch (e) { console.warn('AppTZ listener error', e); }
    }
    // also broadcast as DOM event for pages that prefer it
    try {
      window.dispatchEvent(new CustomEvent('apptz:change', { detail: ev }));
    } catch (e) { /* ignore */ }
    // sync any other switchers on the page
    document.querySelectorAll('select.app-tz-switcher').forEach(function (el) {
      if (el.value !== tz) el.value = tz;
    });
  }

  function onChange(cb) {
    if (typeof cb !== 'function') return function () {};
    listeners.push(cb);
    return function () {
      var i = listeners.indexOf(cb);
      if (i >= 0) listeners.splice(i, 1);
    };
  }

  /**
   * Format a unix-seconds timestamp in the active timezone.
   * @param {number} ts unix seconds (or ms if >= 1e12)
   * @param {object} [opts]
   *   - style: 'datetime' (default) | 'date' | 'time' | 'short'
   *   - withSeconds: boolean — include seconds for datetime/time
   *   - hour12: boolean
   */
  function formatTs(ts, opts) {
    if (ts == null || ts === '' || isNaN(ts)) return '—';
    var n = Number(ts);
    var ms = n >= 1e12 ? n : n * 1000;
    var d = new Date(ms);
    if (isNaN(d.getTime())) return '—';

    opts = opts || {};
    var tz = get();
    var style = opts.style || 'datetime';

    var fopts = { timeZone: tz, hour12: opts.hour12 === true };
    if (style === 'date') {
      fopts.year = 'numeric'; fopts.month = '2-digit'; fopts.day = '2-digit';
    } else if (style === 'time') {
      fopts.hour = '2-digit'; fopts.minute = '2-digit';
      if (opts.withSeconds) fopts.second = '2-digit';
    } else if (style === 'short') {
      fopts.year = '2-digit'; fopts.month = '2-digit'; fopts.day = '2-digit';
      fopts.hour = '2-digit'; fopts.minute = '2-digit';
    } else { // datetime
      fopts.year = 'numeric'; fopts.month = '2-digit'; fopts.day = '2-digit';
      fopts.hour = '2-digit'; fopts.minute = '2-digit';
      if (opts.withSeconds) fopts.second = '2-digit';
    }

    try {
      // Use sv-SE for stable YYYY-MM-DD HH:mm style; fall back on en-CA.
      return new Intl.DateTimeFormat('sv-SE', fopts).format(d);
    } catch (e) {
      return d.toLocaleString([], fopts);
    }
  }

  function _shortLabel(tz) {
    var p = PRESETS.find(function (x) { return x.value === tz; });
    return p ? p.label : tz;
  }

  function _abbr(tz) {
    // Try to get short tz abbreviation (e.g. "EST", "CST") for the pill display.
    try {
      var parts = new Intl.DateTimeFormat('en-US', {
        timeZone: tz, timeZoneName: 'short', hour: '2-digit',
      }).formatToParts(new Date());
      var part = parts.find(function (p) { return p.type === 'timeZoneName'; });
      if (part) return part.value;
    } catch (e) { /* ignore */ }
    return tz;
  }

  /**
   * Mount a timezone selector into a container element.
   * Renders as a compact pill: "🌐 America/New_York (EST) ▾"
   */
  function mountSwitcher(container) {
    if (!container) return;
    if (container.querySelector('select.app-tz-switcher')) return; // already mounted

    var wrap = document.createElement('label');
    wrap.className = 'app-tz-pill';
    wrap.title = 'System timezone — affects all time displays';
    wrap.style.cssText = [
      'display:inline-flex',
      'align-items:center',
      'gap:6px',
      'padding:3px 8px',
      'border:1px solid var(--border, #2a2e39)',
      'border-radius:4px',
      'background:var(--panel, #1e222d)',
      'font-size:11px',
      'color:var(--text-dim, #b2b5be)',
      'cursor:pointer',
      'user-select:none',
    ].join(';');

    var icon = document.createElement('span');
    icon.textContent = '🕒';
    icon.style.fontSize = '12px';

    var sel = document.createElement('select');
    sel.className = 'app-tz-switcher';
    sel.style.cssText = [
      'background:transparent',
      'border:none',
      'outline:none',
      'color:var(--text, #d1d4dc)',
      'font-size:11px',
      'cursor:pointer',
      'appearance:none',
      '-webkit-appearance:none',
      'padding-right:14px',
    ].join(';');

    PRESETS.forEach(function (p) {
      var o = document.createElement('option');
      o.value = p.value;
      o.textContent = p.label + '  (' + _abbr(p.value) + ')';
      sel.appendChild(o);
    });

    var cur = get();
    if (!PRESETS.some(function (p) { return p.value === cur; })) {
      var o = document.createElement('option');
      o.value = cur; o.textContent = cur + '  (' + _abbr(cur) + ')';
      sel.appendChild(o);
    }
    sel.value = cur;

    sel.addEventListener('change', function () {
      set(sel.value, { source: 'switcher' });
    });

    wrap.appendChild(icon);
    wrap.appendChild(sel);
    container.appendChild(wrap);
  }

  // Re-render existing switchers if abbr changes when tz changes.
  onChange(function () {
    document.querySelectorAll('select.app-tz-switcher').forEach(function (sel) {
      // Update the labels' tz abbreviations
      Array.prototype.forEach.call(sel.options, function (opt) {
        var p = PRESETS.find(function (x) { return x.value === opt.value; });
        var lbl = p ? p.label : opt.value;
        opt.textContent = lbl + '  (' + _abbr(opt.value) + ')';
      });
    });
  });

  window.AppTZ = {
    get: get,
    set: set,
    onChange: onChange,
    formatTs: formatTs,
    mountSwitcher: mountSwitcher,
    PRESETS: PRESETS,
    DEFAULT: DEFAULT_TZ,
  };
})();
