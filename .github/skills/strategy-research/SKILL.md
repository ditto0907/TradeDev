---
name: strategy-research
description: Research trading strategies by detecting 2-consecutive-bar signals on 5min RTH bars, persisting them to a Google Sheet, and using Al Brooks Price Action methodology to analyze each signal's market context. Use when the user asks for "strategy research", "signal scanning", "back-test signal generation", "扫描信号", "策略研究", or wants to populate the Orders sheet with new signals over a date range.
argument-hint: <symbol> <from_date> <to_date>  e.g. "MES 2026-04-01 2026-04-15"
---

# Strategy Research Skill

Build a research dataset by combining mechanical signal detection (MCP tool) with
LLM context analysis (Al Brooks Price Action), then persist the results to a
Google Sheet for human evaluation.

## Pipeline

```
detect_signals_in_range  →  write_signals_to_sheet  →  per-signal LLM analysis
                                                      └→ update_signal_analysis
                                                      └→ human review (cols P, Q)
```

## Required MCP tools (server: `strategy-research`)
- `detect_signals_in_range(symbol, from_date, to_date)` — returns JSON `{count, signals:[…]}`
- `write_signals_to_sheet(signals_json)` — appends rows; returns `{rows:[…]}` (absolute sheet rows)
- `get_context_bars(symbol, signal_ts, lookback_bars=80, include_d1=true)` — returns `{"5min":[…], "1D":[…]}`
- `update_signal_analysis(row, pattern, minor_major, leg_cnt, context, sr, sr_detail)`
- `list_signals_from_sheet(only_unanalyzed=false)`

## Workflow

### Step 1 — Detect
Call `detect_signals_in_range(symbol=<S>, from_date=<F>, to_date=<T>)`.
The tool fetches 5min RTH bars and emits a `SignalRecord` for every
2-consecutive-bar pair with these mechanical fields:

| Field            | Values                                |
|------------------|---------------------------------------|
| direction        | Bull / Bear                           |
| gap              | None / GapUp / GapDown                |
| signal_strength  | Strong / Weak (both bars COH/L≥0.69)  |
| coh_l            | float 0..1                            |
| overlapping      | small / medium / large                |
| pb_bars          | # consecutive opposite bars before pair |
| pb_strength      | weak / strong                         |
| ft               | Y / 2nd / N (1st post-signal bar)     |

### Step 2 — Persist
Pass `signals_json = response["signals"]` to `write_signals_to_sheet`.
The detector-filled columns B–I are written. The returned `rows` list
maps signal index → absolute sheet row, used in Step 4.

### Step 3 — Analyze each signal (Al Brooks PA)
For each signal in order, call `get_context_bars(symbol, signal_ts)` and
classify the following per Al Brooks methodology:

- **Pattern** — concrete PA pattern at the signal point
  (e.g. `Bull BO`, `DB`, `DT`, `Wedge`, `BC`, `SC`, `Climax`, `MTR`)
- **Minor/Major** — `Minor` (intraday counter-trend) or `Major` (trend reversal)
- **Leg Cnt** — `1st` / `2nd` / `3rd` leg of the move
- **Context** — concise market cycle phrase, e.g.
  `空头通道底部 + 第二次MTR尝试`, `TR 上沿 BO 失败`
- **SR** — `Y` if there's a tight S/R level near the signal price; else `N`
- **SR Detail** — specific level(s) cited, e.g. `PDH @ 5210; OR low @ 5180`

### Step 4 — Write back
For each analyzed signal call:
```
update_signal_analysis(row=<row from step 2>,
                       pattern=…, minor_major=…, leg_cnt=…,
                       context=…, sr=…, sr_detail=…)
```

### Step 5 — Hand off
Tell the user: "✅ Wrote N signals to the Orders sheet. Please review
columns **P (背景支持)** and **Q (支持理由)** — those are the human
evaluation fields."

## Al Brooks PA Glossary (use these abbreviations)
TR = Trading Range · TTR = Tight TR · BO = Breakout · FT = Follow Through
BC = Buying Climax · SC = Selling Climax · MTR = Major Trend Reversal
MM = Measured Move · OR = Opening Range · HH/HL/LH/LL · DT/DB · ii/oo/ioi

## Rules (enforced)
- Never sell at TR bottom unless consecutive large bear bars
- Strong BO → enter on close or 1-2 bar pullback
- Trade channels in trend direction unless 2nd-attempt MTR
- Always identify magnets: PDH, PDL, MM targets, round numbers
- Cite bar characteristics: body size, tails, overlap, gaps

## Output format for the user
Concise bullets only. No long prose. No emoji unless the user requests them.

```
• Range: <from> → <to>, <symbol>
• Detected: N signals (B Bull / B Bear)
• Written to Sheet: rows R..R+N-1
• Analyzed: M signals (LLM PA classification + S/R)
• Next: review columns P, Q
```
