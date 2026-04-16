# Price Action Trading System - Implementation Plan

## Context

Build a real-time MES (Micro E-mini S&P 500) futures trading visualization system in `/home/user/TradeDev/priceaction/`. The system connects to a local Interactive Brokers TWS/Gateway (127.0.0.1:7497), fetches 1min/5min OHLC data, stores it in Google Sheets, displays 5min K-lines via TradingView charting library, and annotates support/resistance levels and market cycles using price action analysis.

The user already has IB TWS running locally and existing IB integration code in `virtualenv/PriceGetter.ipynb` and `virtualenv/RiskControlV2.ipynb` using both `ibapi` and `ib_insync`.

---

## File Structure

```
priceaction/
├── requirements.txt              # Python dependencies
├── config.py                     # Configuration (IB connection, Google Sheets, etc.)
├── ib_data_fetcher.py            # IB data fetching module (ib_insync)
├── google_sheets_sync.py         # Google Sheets read/write module (gspread)
├── price_action_analyzer.py      # S/R levels & market cycle detection
├── server.py                     # FastAPI backend (REST + WebSocket)
├── static/
│   ├── index.html                # Main frontend page
│   ├── datafeed.js               # Custom TradingView DataFeed adapter
│   └── app.js                    # Frontend app logic (chart init, S/R overlay)
├── charting_library/             # Symlink → ../../charting_library-master-v28.5/charting_library
└── credentials/
    └── .gitkeep                  # Google service account JSON goes here (gitignored)
```

---

## Implementation Steps

### Step 1: Project Setup & Dependencies

**File: `requirements.txt`**
```
ib_insync>=0.9.86
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
websockets>=12.0
gspread>=5.12.0
google-auth>=2.23.0
pandas>=2.1.0
```

- Create symlink from `priceaction/charting_library/` → `../charting_library-master-v28.5/charting_library/`
- Add `credentials/` to `.gitignore`
- Install dependencies via pip

### Step 2: Configuration Module

**File: `config.py`**
- IB connection settings: host, port, clientId
- MES contract definition (symbol="MES", secType="FUT", exchange="CME", currency="USD", auto-detect front month)
- Google Sheets settings: spreadsheet name/ID, worksheet names for 1min/5min data
- Server settings: host, port

### Step 3: IB Data Fetcher

**File: `ib_data_fetcher.py`**

Reuses patterns from `virtualenv/RiskControlV2.ipynb` (ib_insync async pattern).

Key class: `IBDataFetcher`
- `connect()` — Connect to IB TWS at 127.0.0.1:7497
- `get_mes_contract()` — Build MES continuous futures contract (using `ContFuture` or auto-detect front month via `qualifyContracts`)
- `fetch_historical_bars(bar_size, duration)` — Fetch historical 1min or 5min OHLCV data, return as list of dicts
- `subscribe_realtime_bars(bar_size, callback)` — Subscribe to real-time bar updates using `reqHistoricalData` with `keepUpToDate=True`
- `get_bars_dataframe()` — Return bars as pandas DataFrame
- Internal bar storage: in-memory list/dict for fast access by the API server

Data flow:
1. On startup → fetch last 5 days of 1min + 5min historical data
2. Subscribe to real-time updates → append new bars to in-memory store
3. Notify WebSocket clients when new bars arrive

### Step 4: Google Sheets Sync

**File: `google_sheets_sync.py`**

Key class: `GoogleSheetsSync`
- `authenticate(credentials_path)` — Auth via service account JSON
- `sync_bars(bars, worksheet_name)` — Batch write OHLCV bars to specified worksheet
- `initial_upload(bars_1min, bars_5min)` — Upload historical data on startup
- `append_new_bar(bar, worksheet_name)` — Append single new bar (called on real-time update)

Rate limiting: Batch writes (max 100 rows per API call), throttle to avoid Google's 300 req/min limit.

**Google Cloud Setup Guide** (included in README/comments):
1. Go to Google Cloud Console → Create project
2. Enable Google Sheets API & Google Drive API
3. Create Service Account → Download JSON key → place in `credentials/`
4. Create a Google Sheet → Share with service account email (Editor role)

### Step 5: Price Action Analyzer

**File: `price_action_analyzer.py`**

Key class: `PriceActionAnalyzer`

**Support/Resistance Detection:**
- `find_swing_points(bars, lookback=5)` — Identify swing highs (local maxima) and swing lows (local minima) using N-bar lookback
  - Swing High: bar.high > all N bars before AND after
  - Swing Low: bar.low < all N bars before AND after
- `cluster_levels(swing_points, threshold_pct=0.15)` — Group nearby swing points into S/R zones (merge levels within 0.15% of each other)
- `rank_levels(levels)` — Rank S/R by number of touches and recency

**Market Cycle Detection:**
- `detect_market_structure(bars)` — Analyze sequence of swing highs/lows:
  - **Uptrend (Markup)**: Higher Highs (HH) + Higher Lows (HL)
  - **Downtrend (Markdown)**: Lower Highs (LH) + Lower Lows (LL)
  - **Range/Accumulation**: Swing points within a horizontal band
  - **Distribution**: After uptrend, swing points form a horizontal range at highs
- `get_analysis(bars)` — Return complete analysis: S/R levels + current market cycle + cycle ranges

Output format:
```python
{
    "support_levels": [{"price": 5820.5, "strength": 3, "touches": 4}, ...],
    "resistance_levels": [{"price": 5875.0, "strength": 2, "touches": 3}, ...],
    "market_cycle": "markup",  # markup | markdown | accumulation | distribution
    "cycle_ranges": [{"start_time": ..., "end_time": ..., "type": "markup"}, ...]
}
```

### Step 6: FastAPI Backend Server

**File: `server.py`**

**REST Endpoints** (for TradingView DataFeed):
- `GET /api/config` — Return chart configuration (supported_resolutions, exchanges, etc.)
- `GET /api/symbols?symbol=MES` — Return symbol info (name, timezone, session times, etc.)
- `GET /api/history?symbol=MES&resolution=5&from=X&to=Y` — Return OHLCV bars in TradingView UDF format
- `GET /api/analysis` — Return current S/R levels and market cycle data

**WebSocket Endpoint**:
- `WS /ws/realtime` — Push real-time bar updates + analysis updates to connected clients

**Static File Serving**:
- Serve `static/` at `/`
- Serve `charting_library/` at `/charting_library/` (TradingView assets)

**Startup Flow**:
1. Connect to IB → fetch historical data
2. Authenticate with Google Sheets → initial data upload
3. Subscribe to real-time IB data
4. On each new bar: update in-memory store → run price action analysis → push to WebSocket → append to Google Sheets
5. Start FastAPI server on configured port

### Step 7: Frontend - TradingView Integration

**File: `static/index.html`**
- Load TradingView charting library from `/charting_library/charting_library.standalone.js`
- Load custom `datafeed.js` and `app.js`
- Container div for the TradingView widget

**File: `static/datafeed.js`**
Custom DataFeed class implementing TradingView's JS API:
- `onReady(callback)` — Return config with supported_resolutions: ["1", "5"]
- `resolveSymbol(symbolName, onResolve)` — Fetch from `/api/symbols`
- `getBars(symbolInfo, resolution, periodParams, onResult)` — Fetch from `/api/history`
- `subscribeBars(symbolInfo, resolution, onTick, listenerGuid)` — Connect to WebSocket `/ws/realtime`, call onTick with each new bar
- `unsubscribeBars(listenerGuid)` — Close WebSocket subscription

**File: `static/app.js`**
- Initialize TradingView widget with custom DataFeed, default to MES 5min
- Fetch S/R levels from `/api/analysis`
- Draw horizontal lines for support (green) and resistance (red) using `chart.createMultipointShape()` or `chart.createShape()`
- Draw background highlights for market cycle phases using `chart.createMultipointShape()` with rectangle shapes
- Listen to WebSocket for analysis updates → redraw annotations when S/R levels change
- Legend/labels for market cycle phases

---

## Data Flow Summary

```
IB TWS (127.0.0.1:7497)
    │
    ▼
ib_data_fetcher.py (ib_insync)
    │
    ├──▶ In-memory bar store (1min + 5min)
    │       │
    │       ├──▶ FastAPI REST API ──▶ TradingView DataFeed (getBars)
    │       │
    │       └──▶ price_action_analyzer.py
    │               │
    │               └──▶ S/R levels + Market cycles
    │                       │
    │                       └──▶ WebSocket ──▶ Frontend (annotations)
    │
    └──▶ google_sheets_sync.py ──▶ Google Sheets (1min + 5min worksheets)
    
    Real-time flow:
    IB new bar ──▶ update store ──▶ analyze ──▶ WebSocket push ──▶ Frontend update
                                             ──▶ Google Sheets append
```

---

## Key Files to Reference

| Existing File | Reuse Pattern |
|---|---|
| `virtualenv/RiskControlV2.ipynb` | ib_insync async connection, event binding, Contract definition |
| `virtualenv/PriceGetter.ipynb` | Historical data request pattern, MES contract setup, DataFrame conversion |
| `charting_library-master-v28.5/charting_library/` | TradingView library assets (symlink into priceaction/) |

---

## Verification Plan

1. **IB Connection**: Run `ib_data_fetcher.py` standalone → verify it connects and fetches MES 5min bars, prints DataFrame
2. **Google Sheets**: Run `google_sheets_sync.py` standalone → verify it authenticates and writes sample data to a test sheet
3. **Backend API**: Start `server.py` → hit `GET /api/history?symbol=MES&resolution=5&from=0&to=9999999999` → verify JSON response with OHLCV data
4. **Frontend Chart**: Open browser to `http://localhost:8000` → verify TradingView chart renders with MES 5min candles
5. **Real-time Updates**: Watch chart → verify new candles appear as IB streams data
6. **Price Action**: Verify S/R horizontal lines and market cycle shading appear on chart
7. **Google Sheets Live**: Check Google Sheet updates with new bars as they arrive

---

## Important Notes

- **TradingView Library**: User confirmed they have the full distribution package. Need to ensure the `charting_library/` directory contains the full distribution (with `static/` subdirectory, bundles, CSS). If the current directory only has JS wrappers, user needs to replace with the full package.
- **MES Contract Rolling**: MES futures roll quarterly (Mar/Jun/Sep/Dec). Use `ContFuture` or auto-detect front month to avoid hardcoding expiry.
- **IB Client ID**: Use a unique clientId (e.g., 10) to avoid conflicts with other running IB scripts (RiskControl uses clientId=0).
- **Google Sheets Rate Limits**: Batch writes, throttle real-time appends (e.g., buffer and write every 30 seconds).
