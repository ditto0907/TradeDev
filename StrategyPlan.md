# IBS 2-Bar Strategy — Design & Implementation Plan

## Overview

A backtesting engine for a 2-bar Internal Bar Strength (IBS) momentum strategy on MES (Micro E-mini S&P 500) 5-min bars, with market context filters, 1:1 Risk:Reward, results persisted to SQLite, and a Strategy tab in the TradingView-based UI.

---

## Signal Definition

**IBS** = (Close − Low) / (High − Low)

### Long Signal (Momentum Continuation)
- Bar 1: Closes **bullish** (Close ≥ Open)
- Bar 2: IBS ≥ **threshold** (default 70%) — close near bar's high

### Short Signal (Momentum Continuation)
- Bar 1: Closes **bearish** (Close ≤ Open)
- Bar 2: IBS ≤ **(1 − threshold)** (default 30%) — close near bar's low

The threshold is configurable via `config.IBS_THRESHOLD`.

---

## Trade Management

### Entry
- Entry price = Bar 2's **close** (market-on-close simulation)

### Stop & Target (1:1 R:R)
- **Stop distance** = max(Bar1.high, Bar2.high) − min(Bar1.low, Bar2.low) (2-bar range)
- **Long**: Stop = Entry − stop_distance ; Target = Entry + stop_distance
- **Short**: Stop = Entry + stop_distance ; Target = Entry − stop_distance
- PnL in USD: price_diff × 5 (MES: $5 per point)

### Position Management
- One position at a time — skip new signals while a trade is open

---

## Market Context Filter

Uses existing `PriceActionAnalyzer` (S/R levels + market cycle) as a proxy for channel/range position.

| Condition | Action |
|---|---|
| Price near **support** (within 0.3%) — any cycle | **Block short** (near bottom of channel/range) |
| Price near **resistance** (within 0.3%) — any cycle | **Block long** (near top of channel/range) |
| Price NOT near any S/R level | **Allow** (mid-channel / breakout area) |

Rolling lookback window (default 200 bars) prevents look-ahead bias.

---

## Files Changed

| File | Change |
|---|---|
| `priceaction/config.py` | Add IBS strategy config constants |
| `priceaction/db.py` | Add `strategy_backtests` + `strategy_trades` tables and CRUD |
| `priceaction/strategy_backtest.py` | **New** — complete backtest engine |
| `priceaction/server.py` | Add `/api/strategy/*` endpoints |
| `priceaction/static/index.html` | Add Strategy tab + pane + legend toggle |
| `priceaction/static/app.js` | Add backtest UI, table rendering, chart markers |

---

## Database Schema

### `strategy_backtests`
```sql
id           TEXT PRIMARY KEY        -- UUID
symbol       TEXT                    -- "MES"
timeframe    TEXT                    -- "5min"
from_ts      INTEGER                 -- Unix epoch seconds
to_ts        INTEGER
created_at   TEXT                    -- ISO timestamp
params_json  TEXT                    -- JSON of BacktestParams
summary_json TEXT                    -- JSON of BacktestSummary
trade_count  INTEGER
```

### `strategy_trades`
```sql
id             INTEGER PK AUTOINCREMENT
backtest_id    TEXT                   -- FK → strategy_backtests.id
symbol         TEXT
timeframe      TEXT
direction      TEXT                   -- "long" | "short"
entry_time     INTEGER
entry_price    REAL
exit_time      INTEGER                -- NULL if still open
exit_price     REAL
stop_price     REAL
target_price   REAL
pnl            REAL                   -- USD
outcome        TEXT                   -- "win" | "loss" | "open"
bars_held      INTEGER
signal_ibs     REAL                   -- IBS value of Bar 2
context_pass   INTEGER                -- 1=trade taken, 0=filtered out
context_reason TEXT                   -- reason if filtered
created_at     TEXT
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/api/strategy/backtest` | Run backtest, persist results, return summary + trades |
| GET | `/api/strategy/backtests` | List all backtest runs (with summary) |
| GET | `/api/strategy/backtests/{id}/trades` | Get trades for a specific run |
| DELETE | `/api/strategy/backtests/{id}` | Delete run and its trades |

### POST `/api/strategy/backtest` Request Body
```json
{
  "symbol": "MES",
  "timeframe": "5min",
  "from_ts": 1740787200,
  "to_ts": 1746057600,
  "ibs_threshold": 0.70,
  "rr_ratio": 1.0,
  "use_context_filter": true
}
```

---

## Backtest Summary Output

```json
{
  "total": 42,
  "wins": 23,
  "losses": 19,
  "win_rate": 0.548,
  "total_pnl": 315.0,
  "avg_win": 62.5,
  "avg_loss": -45.0,
  "profit_factor": 1.61,
  "max_drawdown": -125.0,
  "filtered_count": 8,
  "bars_used": 4800,
  "data_source": "db"
}
```

---

## Frontend: Strategy Tab

Located in the bottom panel (alongside Positions, Trade History, etc.).

### Controls
- **Run Backtest** button
- IBS% threshold input (default 70)
- Context Filter checkbox (default on)
- Backtest history dropdown (select previous runs)
- Summary stats: total trades / win rate / P&L / profit factor

### Trade Table
Columns: Entry Time | Direction | Entry | Exit | Stop | Target | IBS | Outcome | P&L | Context

- **Green rows**: winning trades
- **Red rows**: losing trades
- **Dimmed rows**: filtered-out signals (with reason)
- **Locate button**: scrolls TradingView chart to the trade's time

### Chart Markers
- Entry arrow (teal ↑ for long, red ↓ for short) via `createExecutionShape()`
- Exit arrow colored by outcome (green=win, red=loss)
- Filtered-out signals shown as ghost markers

---

## Verification

```bash
# 1. Unit test IBS calculation
python3 -c "
from priceaction.strategy_backtest import compute_ibs
bar = {'open':100,'high':110,'low':90,'close':104,'time':0,'volume':0}
assert abs(compute_ibs(bar) - 0.7) < 1e-9, 'IBS should be 0.7'
print('IBS test passed:', compute_ibs(bar))
"

# 2. Run a backtest via CLI
python3 -c "
import sys; sys.path.insert(0, 'priceaction')
from strategy_backtest import run_backtest
result = run_backtest('MES', '5min')
print('Trades:', result['summary']['total'])
print('Win rate:', result['summary']['win_rate'])
"

# 3. Test via API (server must be running)
curl -X POST http://localhost:8000/api/strategy/backtest \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"MES","timeframe":"5min","ibs_threshold":0.70}'
```
