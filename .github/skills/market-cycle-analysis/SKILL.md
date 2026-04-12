---
name: market-cycle-analysis
description: "Analyze market cycles using Al Brooks price action methodology. Use when: market analysis, price action analysis, market cycle, S/R levels, opening range, trend analysis, breakout analysis, trading range, measured move. Reads K-line data from the trading terminal backend and writes annotated analysis results back to the chart."
argument-hint: "Symbol and timeframe to analyze, e.g. 'MES 5min RTH today' or 'MES 1D last 20 bars'"
---

# Market Cycle Analysis — Al Brooks Price Action

## Purpose

Analyze market structure and cycles strictly following Al Brooks' price action methodology. Read K-line data from the TradeDev backend, identify the current market phase, key S/R levels, and annotate the chart with structured findings.

## When to Use

- User asks for market cycle analysis, price action analysis, or market structure reading
- User wants to identify the current trading range, breakout, trend, or reversal
- User needs Opening Range, legs, measured moves, or key S/R marked on chart
- User says "analyze", "what phase", "market cycle", "where are we"

## Data Access

### Read K-line Data

```
GET http://localhost:8000/api/skill/bars?symbol={SYM}&resolution={RES}&session={SESSION}&from={FROM_TS}&to={TO_TS}
```

Parameters:
- `symbol`: MES (default), MNQ, NK225MC, MGC
- `resolution`: 5 (5min), 15, 30, 60, 1D
- `session`: RTH (default, 09:30-16:00 ET) or ETH (all hours)
- `from`, `to`: Unix seconds timestamp range

Response: `{ symbol, resolution, session, count, bars: [{time, open, high, low, close, volume}, ...] }`

### Write Analysis Results

```
POST http://localhost:8000/api/skill/analysis
Content-Type: application/json

{
  "symbol": "MES",
  "timeframe": "5",
  "session": "RTH",
  "bar_from": 1744200000,
  "bar_to":   1744220000,
  "summary": "Concise Al Brooks analysis summary using PA terminology",
  "annotations": [
    {
      "label": "Opening Range",
      "type": "range",
      "start_time": 1744200600,
      "end_time": 1744203000,
      "price_high": 5430.50,
      "price_low": 5418.25,
      "color": "rgba(33,150,243,0.12)"
    },
    {
      "label": "S1 5402.50",
      "type": "hline",
      "start_time": 1744200000,
      "price": 5402.50,
      "style": "dashed"
    },
    {
      "label": "Bull BO",
      "type": "label",
      "start_time": 1744210000,
      "price": 5435.00
    }
  ]
}
```

### Annotation Types

| Type | Required Fields | Description |
|------|----------------|-------------|
| `range` | `start_time`, `end_time`, `price_high`, `price_low` | Rectangle on chart (Opening Range, TR, legs) |
| `hline` | `price`, `start_time` | Horizontal line (S/R levels, measured move targets) |
| `label` | `start_time`, `price` | Text label at specific bar/price (BO point, reversal) |

### Built-in Color Palette (auto-applied by label name)

| Label | Color |
|-------|-------|
| Opening Range | Blue |
| Bear Leg | Red |
| Bull Leg / Bull Breakout | Green |
| Bear Breakout | Red |
| Reversal / Double Bottom | Orange |
| Reversal / Double Top | Orange |
| Trading Range / Tight Trading Range | Gray |
| Channel | Purple |
| Measured Move | Cyan |
| Climax | Dark Red |

Custom colors can be set via the `color` field (CSS rgba string).

## Analysis Procedure

### Step 1: Fetch Data

Determine the appropriate time range based on the user's request:
- **Intraday today**: Use today's RTH session (from 09:30 ET to current time)
- **Multi-day**: Use appropriate `from`/`to` range
- **Daily chart**: Use resolution=1D with wider range

Fetch bars using the skill API endpoint. Verify you received sufficient bars.

### Step 2: Multi-Timeframe Context (if needed)

For intraday analysis, optionally fetch the daily chart (1D) for context:
- Identify the daily chart's current phase (TR, trend, BO)
- Note prior day's high, low, close as magnets

### Step 3: Identify Market Structure

Apply Al Brooks methodology strictly:

1. **Opening Range (OR)**: First 1-6 bars of RTH (first 5-30 minutes). Mark as range annotation.
2. **Legs**: Identify consecutive bars moving in one direction (Bull Leg / Bear Leg). Look for:
   - Bar size (body relative to range)
   - Consecutive closes in same direction
   - Gaps between bars
3. **Trading Range (TR)**: Price oscillating between S/R without clear trend. Characteristics:
   - Overlapping bars
   - Doji bars, bars with prominent tails
   - Failed breakouts on both sides
4. **Breakout (BO)**: Strong move out of TR or pattern. Assess:
   - Bar size and close location
   - Follow-through (FT) bars
   - Gap from prior bar
5. **Measured Move (MM)**: Project leg length for targets
6. **Reversals**: MTR (Major Trend Reversal) requires at least 2 attempts

### Step 4: Classify Current Phase

Using the PA vocabulary:
- **TR** (Trading Range): No clear direction, price bound between S/R
- **TTR** (Tight Trading Range): Very narrow TR, often precedes BO
- **BO** (Breakout): Strong move from TR/pattern
- **BC** (Bull Channel): Series of HH/HL
- **SC** (Sell Climax) / **BC** (Buy Climax): Extreme exhaustion move
- **MTR** (Major Trend Reversal): Trend change, needs 2nd attempt confirmation

### Step 5: Write Summary

Output format — concise bullet points using Al Brooks abbreviations:

```
• Phase: [TR / BO / BC / Bear Channel / MTR]
• Context: [D1 context if available]
• OR: [H/L of opening range]
• Key levels: [S/R with price]
• Magnets: [prior H/L, MM targets]
• Bias: [Bull/Bear/Neutral] — [reasoning citing bar characteristics]
```

### Step 6: Create Annotations

Build the annotations array with appropriate types:
- Use `range` for: Opening Range, Trading Ranges, legs, channels
- Use `hline` for: Key S/R levels, MM targets, prior day H/L
- Use `label` for: BO points, reversal signals, climax bars

### Step 7: Submit to Backend

POST the analysis to `/api/skill/analysis`. The chart will update automatically via WebSocket.

## Rules (Al Brooks Methodology)

- **Never sell at TR bottom** unless consecutive large bear bars appear
- **Strong BO**: Enter on close or small pullback (1-2 bars)
- **Trade channels in trend direction**, unless MTR on 2nd attempt
- **Always identify magnets**: Prior day H/L, MM targets, round numbers
- **Bar characteristics matter**: Cite body size, tails, overlap, gaps
- **Context is king**: Higher timeframe structure overrides lower
- Use PA abbreviations: TR, TTR, BO, FT, BC, SC, MTR, MM, OR, HH, HL, LH, LL, DT, DB, ii, oo, ioi

## Analysis Log Management

Users can manage analyses via the "Analysis Log" tab in the bottom panel:
- **Eye icon**: Toggle visibility on/off (shows/hides annotations on chart)
- **✕ button**: Delete analysis permanently
- All analyses are persisted in the database across sessions

### Query Existing Analyses

```
GET http://localhost:8000/api/skill/analyses?symbol=MES&active_only=true
```

Returns array of analysis records with full annotations.
