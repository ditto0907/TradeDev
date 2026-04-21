# K-Line Data Architecture Refactoring — Design Document

> **Date**: 2026-04-16  
> **Scope**: `priceaction/` directory — K-line data fetch, storage, validation, and display  
> **Status**: Implemented

---

## 1. Problem Statement

The trading terminal suffered from recurring K-line (candlestick) display issues:
- Missing bars during weekends, holidays, and maintenance breaks
- Gaps after IB data fetches due to incorrect gap classification
- Duplicate/inconsistent bars from MES-specific legacy code running alongside multi-symbol code
- No OHLCV integrity validation — invalid bars (high < low, negative prices) could enter the DB
- Ad-hoc gap detection heuristics scattered across multiple modules

Despite multiple fix attempts, the root causes persisted because each fix addressed symptoms (specific gap patterns) rather than the architectural deficiency.

## 2. Root Cause Analysis

### 2.1 No Trading Session Calendar
Gap classification used hardcoded heuristics (e.g., "if weekday==4 and hour>=16, it's a weekend") scattered across `db.py`, `server.py`, and `ib_data_fetcher.py`. These heuristics:
- Were US-timezone-specific (broke for NK225MC / JST sessions)
- Didn't account for holidays properly
- Created inconsistencies — each module had slightly different gap thresholds

### 2.2 Dual MES Code Paths
`IBDataFetcher` had:
- `_on_tick()` — MES-only tick handler with legacy state (`_prev_tick_price`, `_last_tick_broadcast`)
- `_on_tick_multi()` — separate handler for non-MES symbols with different state management
- `self.bars["5min"]` — MES-only in-memory store alongside `self._symbol_bars` for all symbols

This duplication meant bugs fixed in one path didn't apply to the other.

### 2.3 No Data Validation on Ingest
`db.insert_bars()` performed raw `INSERT OR REPLACE` without checking:
- OHLCV integrity (high ≥ low, open/close within [low, high])
- Non-positive prices
- Negative volumes

Invalid data from IB glitches or network issues was silently persisted.

### 2.4 Monolithic get_history()
The `/api/history` endpoint (300+ lines) performed gap detection, IB fetching, internal gap filling, gap stripping, and response formatting all in one function, making each concern's logic fragile and hard to reason about.

## 3. Industry Standard Architecture (TradingView UDF Reference)

Professional trading terminals like TradingView follow these principles:

| Concept | Standard Approach | Our Previous Approach |
|---------|------------------|----------------------|
| **Session Calendar** | Per-instrument schedule with holiday overrides | Hardcoded US-only weekday/hour checks |
| **Expected vs Actual Bars** | Generate expected bar grid from calendar, compare against actual | Direct timestamp-gap detection |
| **Gap Classification** | Calendar-based: is this gap during trading hours? | Heuristic: "is gap > 56 hours? probably weekend" |
| **Bar Validation** | Validate OHLCV on ingest, reject invalid | No validation — raw INSERT |
| **Data Source** | DB as single source of truth, in-memory as cache | DB + in-memory with complex sync logic |
| **Multi-Symbol** | Single unified data path for all instruments | Separate MES path + multi-symbol path |

## 4. Refactoring Design

### 4.1 New Module: `trading_calendar.py`

Central trading session calendar for all instruments.

```
TradingCalendar(symbol)
  ├── is_trading_time(ts) → bool
  ├── is_rth(ts) → bool  
  ├── classify_gap(start_ts, end_ts) → "weekend"|"holiday"|"maintenance"|"data_gap"|"normal"
  ├── expected_bars(from_ts, to_ts, interval) → [timestamps]
  ├── find_missing_bars(actual_ts, from_ts, to_ts, interval) → [missing_ts]
  ├── find_gaps(bars, interval) → [gap_records]
  └── validate_bar(bar) → [violation_descriptions]
```

**Session Definitions**:
- CME/COMEX (MES, MNQ, MGC): Sun 18:00 – Fri 17:00 ET, daily 17:00–18:00 maintenance
- OSE (NK225MC): Mon-Fri 08:45–15:45 + 17:00–06:00 JST

**Holiday Support**:
- US holidays: via existing `market_holidays.py`
- Japanese holidays: built-in simplified calendar

### 4.2 Refactored `db.py`

**Bar Validation on Insert**:
```
insert_bars() now validates:
  ✓ high ≥ low
  ✓ open/close within [low, high] 
  ✓ all prices > 0
  ✓ volume ≥ 0
  → Invalid bars are logged and SKIPPED (never persisted)
```

**Calendar-Aware Gap Detection**:
```
find_gaps() now uses TradingCalendar to classify gaps instead of
hardcoded weekday/hour checks. Falls back to legacy heuristics
only when the calendar is unavailable.
```

**New Data Maintenance Functions**:
- `delete_bars_range()` — delete bars in a time range
- `delete_bars_by_timestamps()` — delete specific bars
- `get_bar_at()` — point-inspect a single bar
- `get_integrity_report()` — source breakdown, OHLCV violations, duplicate check
- `fix_ohlcv_violations()` — auto-fix high/low swaps, clamp open/close

### 4.3 Refactored `ib_data_fetcher.py`

**Unified Tick Handler**:
```
BEFORE:                           AFTER:
_on_tick()        ← MES only      _on_tick_unified()  ← ALL symbols
_on_tick_multi()  ← non-MES       _process_tick()     ← shared aggregation
```

**Unified State**:
```
BEFORE:                           AFTER:
self.bars["5min"]       ← MES     self._symbol_bars[sym][key]  ← ALL
self._symbol_bars       ← others  self.bars["5min"]            ← alias → MES
self._prev_tick_price   ← MES     self._tick_state[sym]        ← ALL
self._tick_state        ← others
```

**Key Methods**:
- `_ensure_symbol_state(symbol)` — initializes all per-symbol structures
- `_sync_legacy_bars()` — keeps `self.bars["5min"]` as alias to `_symbol_bars["MES"]["5min"]`
- `_seed_rt_current(symbol)` — unified startup seeding for any symbol

### 4.4 Refactored `server.py`

**get_history() Steps**:
1. Query DB for existing bars
2. Determine missing ranges (left/right/middle gaps)  
3. Fetch from IB (with cooldown protection)
4. **NEW**: Fill internal gaps using `TradingCalendar.find_gaps()` — only fills `data_gap` type
5. **NEW**: Strip unfillable gaps using `TradingCalendar.classify_gap()` — preserves weekends/holidays
6. Append realtime in-progress bar

**_fill_internal_gaps() (background)**:
- Now uses `TradingCalendar.find_gaps()` instead of simple interval-gap detection
- Only attempts to fill gaps classified as `data_gap`
- Maintenance/weekend/holiday gaps are correctly skipped for ALL symbols

**Unified Lifespan**:
```python
# BEFORE: Separate MES + EXTRA_SYMBOLS loading loops
# AFTER: Single loop over all_symbols = [MES_SYM] + extras
for sym in all_symbols:
    fetcher._ensure_symbol_state(sym)
    bars = db.get_bars(sym, "5min")
    if bars:
        fetcher._symbol_bars[sym]["5min"] = bars[-MAX:]
```

### 4.5 Refactored `data_validator.py`

**validate_bars()** now performs three-level validation:
1. **IB Comparison**: DB bars vs IB source-of-truth (unchanged)
2. **OHLCV Integrity**: Check all bars for high < low, non-positive prices, etc.
3. **Completeness**: Use `TradingCalendar.find_missing_bars()` to check for expected-but-missing bars

### 4.6 New API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/data/integrity` | GET | Data integrity report (source breakdown, OHLCV violations) |
| `/api/data/coverage` | GET | Coverage summary for all symbol/timeframe pairs |
| `/api/data/bar` | GET | Point-inspect a single bar |
| `/api/data/delete_range` | POST | Delete bars in a time range |
| `/api/data/delete_bars` | POST | Delete specific bars by timestamps |
| `/api/data/fix_ohlcv` | POST | Auto-fix OHLCV violations |
| `/api/data/calendar_gaps` | GET | Detect gaps using trading session calendar |

### 4.7 Enhanced Data Validation UI

New **Integrity** tab in `datavalid.html`:
- **Run Report**: Shows source breakdown, OHLCV violations, duplicate count
- **Coverage**: Lists all symbol/timeframe pairs with date ranges and bar counts
- **Calendar Gaps**: Session-calendar-aware gap detection (shows only real data gaps)
- **Fix OHLCV**: One-click fix for high/low swaps and invalid prices

## 5. Data Flow Architecture

```
                    ┌─────────────┐
                    │  IB TWS/GW  │
                    └──────┬──────┘
                           │ tick / 5s bar
                    ┌──────▼──────┐
                    │ IBDataFetcher│
                    │ (unified    │
                    │  tick handler│
                    │  for ALL    │
                    │  symbols)   │
                    └──────┬──────┘
                           │ _process_tick()
                    ┌──────▼──────┐      ┌─────────────────┐
                    │ In-Memory   │      │ TradingCalendar  │
                    │ _symbol_bars│      │ (session-aware   │
                    └──────┬──────┘      │  gap classify)   │
                           │             └────────┬────────┘
                    ┌──────▼──────┐               │
                    │  SQLite DB  │◄──────────────┘
                    │ (validated  │  find_gaps()
                    │  on insert) │  classify_gap()
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
       ┌──────▼──────┐ ┌──▼────┐ ┌─────▼─────┐
       │ /api/history │ │  WS   │ │ /api/data │
       │ (TradingView │ │  bar  │ │ validate/ │
       │  UDF compat) │ │ push  │ │ fix/query │
       └──────────────┘ └───────┘ └───────────┘
```

## 6. Key Design Principles

1. **Single source of truth**: SQLite DB is authoritative; in-memory is a read-cache
2. **Validate on ingest**: Every bar is checked before persistence (db.insert_bars)
3. **Calendar-driven gap classification**: No ad-hoc heuristics — use TradingCalendar
4. **Symbol-agnostic code paths**: No `if symbol == "MES"` special cases
5. **Type-based customization**: Different session schedules are per-exchange (CME, OSE), not per-symbol
6. **Layered validation**: DB integrity + IB comparison + calendar completeness

## 7. Files Changed

| File | Changes |
|------|---------|
| `trading_calendar.py` | **NEW** — Session calendar with holiday awareness |
| `db.py` | OHLCV validation on insert, calendar-aware gap detection, data maintenance tools |
| `ib_data_fetcher.py` | Unified tick handler, removed MES-specific code, unified state management |
| `server.py` | Calendar-aware get_history, unified lifespan, new maintenance APIs |
| `data_validator.py` | Three-level validation (IB + OHLCV + calendar completeness) |
| `static/datavalid.html` | New Integrity tab (coverage, calendar gaps, OHLCV fix) |
| `refactor.md` | This design document |
| `README.md` | Updated architecture documentation |

## 8. Migration Notes

- **Database**: No schema changes required. The existing `bars` and `ib_fetch_cache` tables are unchanged.
- **DB rebuild**: Not required, but running `/api/data/fix_ohlcv` on all pairs after deployment is recommended to clean any pre-existing invalid bars.
- **Config**: No changes to `config.py`. The `INSTRUMENTS` and `EXTRA_SYMBOLS` configurations are used as-is.
- **Breaking changes**: None. All existing API contracts are preserved. New APIs are additive.

---

## 9. v2 Addendum — Service-Oriented Refactor (2026-04)

The v1 refactor delivered calendar-driven gap detection and OHLCV
validation.  The v2 refactor takes the next step: **one clear owner per
responsibility**, so future work doesn't have to thread through `server.py`.

### 9.1 New service boundaries

| Module | Role (v2) |
|---|---|
| `server.py` | Routing / WS orchestration **only**. Does not call `db.insert_bars`. |
| `data_manager.py` | **NEW** — read-only `get_bars`, WS broadcaster registration, `notify_history_ready` after batch fetches, `record_validated` façade. |
| `realtime_builder.py` | **NEW** — `persist_completed_bar` (validate+write) and `persist_inprogress_bar`. Wraps `fetcher.persist_bars`. |
| `data_validator.py` | Single validation entry point: adds `validate_bar`, `classify_gaps`, `data_gaps_only`. |
| `contract_calendar.py` | **NEW** — official rollover dates per instrument (`config.INSTRUMENTS[*].rollover_rule`). Replaces the day-10 heuristic. |
| `ib_data_fetcher.py` | Adds `persist_bars()` — the **sole** `db.insert_bars` wrapper used by the rest of the codebase. |
| `scripts/migrate_v2.py` | **NEW** — idempotent migration using `PRAGMA user_version`; rewrites any `bars.contract_month` rows affected by the new calendar. |
| `static/datafeed.js` | Subscribes to the `history_ready` WS message and calls TradingView's `onResetCacheNeededCallback` to refresh open chart widgets. |

### 9.2 Write-path invariant

```
ANYTHING that writes bars  →  IBDataFetcher.persist_bars()  →  db.insert_bars
```

`grep 'db\.insert_bars' priceaction/server.py` must return **zero**
matches.  This is the primary acceptance test for the v2 refactor.

### 9.3 Contract-month rule

Previously:

```python
# Legacy heuristic — REMOVED in v2
if m < qm or (m == qm and d <= 10):
    return f"{y}{qm:02d}"
```

Now:

```python
# config.INSTRUMENTS[symbol]["rollover_rule"] drives contract_calendar:
#   CME MES / MNQ   → nth_business_day, n=8   (official CME Quarterly Roll)
#   COMEX MGC       → n_bdays_before_ltd, n=1
#   OSE NK225MC     → second_friday, offset_bdays=-1  (day before SQ)
from contract_calendar import active_contract
month = active_contract(ts, symbol)
```

`contract_calendar` never consults the system clock; it is a pure
function of the instrument, year, and month — safe for back-fills.

### 9.4 `history_ready` WS protocol

```
{
  "type": "history_ready",
  "symbol": "MES",
  "timeframe": "5min",
  "from": 1712345678,
  "to":   1712432078,
  "added_bars": 288
}
```

Emitted by `data_manager.notify_history_ready` whenever background
prefetch or internal-gap fill adds bars.  The TradingView datafeed
invokes `onResetCacheNeededCallback()` on every subscription whose
`(symbol, resolution)` matches.

### 9.5 Migration

See `doc/migration_v2.md`.  No schema changes; only existing
`contract_month` column values near rollovers may be rewritten.

### 9.6 Tests

* `tests/test_contract_calendar.py` — 13 tests covering CME/COMEX/OSE
  rollover dates, active-contract lookup, and the day-10 regression.

