# 2-Bar Close-at-Extreme Backtest Report

- **Symbol**: MES | **Timeframe**: M5 | **Session**: RTH
- **Period**: 2026-04-15 → 2026-04-28
- **Entry window**: 09:30-12:00 ET (first 2.5h)
- **R:R**: 1:1 | **Stop**: 2-bar swing ± 1 tick

## Overall

| Metric | Value |
|--------|-------|
| Total trades | 7 |
| Wins | 2 |
| Losses | 5 |
| Flats (no hit) | 0 |
| **Win Rate (decided)** | **28.6%** |
| Net points | -39.00 |
| Expectancy / trade | -5.57 pts

## Breakdown by Context

### By Direction

| Bucket | N | Win | Loss | Flat | Win% | Net pts | Exp/trade |
|--------|---|-----|------|------|------|---------|-----------|
| LONG | 4 | 1 | 3 | 0 | 25.0% | -22.25 | -5.56 |
| SHORT | 3 | 1 | 2 | 0 | 33.3% | -16.75 | -5.58 |

### By D1 Bias Alignment

| Bucket | N | Win | Loss | Flat | Win% | Net pts | Exp/trade |
|--------|---|-----|------|------|------|---------|-----------|
| COUNTER | 4 | 1 | 3 | 0 | 25.0% | -28.75 | -7.19 |
| NEUTRAL | 1 | 1 | 0 | 0 | 100.0% | +14.75 | +14.75 |
| WITH | 2 | 0 | 2 | 0 | 0.0% | -25.00 | -12.50 |

### By Time Bucket

| Bucket | N | Win | Loss | Flat | Win% | Net pts | Exp/trade |
|--------|---|-----|------|------|------|---------|-----------|
| EARLY | 6 | 1 | 5 | 0 | 16.7% | -43.50 | -7.25 |
| MID | 1 | 1 | 0 | 0 | 100.0% | +4.50 | +4.50 |

### By Signal Strength (Pre-bar overlap)

| Bucket | N | Win | Loss | Flat | Win% | Net pts | Exp/trade |
|--------|---|-----|------|------|------|---------|-----------|
| CHOP | 6 | 2 | 4 | 0 | 33.3% | -26.50 | -4.42 |
| LEG | 1 | 0 | 1 | 0 | 0.0% | -12.50 | -12.50 |

### By Open Gap Classification

| Bucket | N | Win | Loss | Flat | Win% | Net pts | Exp/trade |
|--------|---|-----|------|------|------|---------|-----------|
| IN_PDR | 6 | 1 | 5 | 0 | 16.7% | -53.75 | -8.96 |
| UNKNOWN | 1 | 1 | 0 | 0 | 100.0% | +14.75 | +14.75 |

### By D1 Bias

| Bucket | N | Win | Loss | Flat | Win% | Net pts | Exp/trade |
|--------|---|-----|------|------|------|---------|-----------|
| BEAR | 1 | 0 | 1 | 0 | 0.0% | -12.00 | -12.00 |
| BULL | 5 | 1 | 4 | 0 | 20.0% | -41.75 | -8.35 |
| NEUTRAL | 1 | 1 | 0 | 0 | 100.0% | +14.75 | +14.75 |

## All Trades

| Date | Signal | Entry | Dir | Entry$ | Stop$ | Target$ | Risk | Outcome | PnL | Bias | Align | Bucket | Strength | Gap |
|------|--------|-------|-----|--------|-------|---------|------|---------|-----|------|-------|--------|----------|-----|
| 2026-04-15 | 09:35 | 09:40 | LONG | 7024.25 | 7011.75 | 7036.75 | 12.5 | LOSS | -12.50 | BULL | WITH | EARLY | LEG | IN_PDR |
| 2026-04-16 | 10:15 | 10:20 | SHORT | 7050.5 | 7062.75 | 7038.25 | 12.25 | LOSS | -12.25 | BULL | COUNTER | EARLY | CHOP | IN_PDR |
| 2026-04-17 | 09:55 | 10:00 | LONG | 7143.5 | 7128.75 | 7158.25 | 14.75 | WIN | +14.75 | NEUTRAL | NEUTRAL | EARLY | CHOP | UNKNOWN |
| 2026-04-21 | 09:45 | 09:50 | LONG | 7161.75 | 7149.25 | 7174.25 | 12.5 | LOSS | -12.50 | BULL | WITH | EARLY | CHOP | IN_PDR |
| 2026-04-22 | 09:45 | 09:50 | SHORT | 7145.0 | 7154.0 | 7136.0 | 9.0 | LOSS | -9.00 | BULL | COUNTER | EARLY | CHOP | IN_PDR |
| 2026-04-23 | 09:40 | 09:45 | LONG | 7163.0 | 7151.0 | 7175.0 | 12.0 | LOSS | -12.00 | BEAR | COUNTER | EARLY | CHOP | IN_PDR |
| 2026-04-27 | 11:15 | 11:20 | SHORT | 7182.75 | 7187.25 | 7178.25 | 4.5 | WIN | +4.50 | BULL | COUNTER | MID | CHOP | IN_PDR |
