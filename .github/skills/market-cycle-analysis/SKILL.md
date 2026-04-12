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

- 默认后端服务已经启动，不要在本skill中启动后端服务。
- 直接使用下面的API接口获取K线数据进行分析即可。获取不到数据就报错，提示用户检查后端服务是否正常运行。
- 严禁自行创建任何环境以及启动服务，这不是你该干的事情。

### Read K-line Data

```
GET http://localhost:8000/api/skill/bars?symbol={SYM}&resolution={RES}&session={SESSION}&from_dt={FROM_DT}&to_dt={TO_DT}
```

Parameters:
- `symbol`: MES (default), MNQ, NK225MC, MGC
- `resolution`: 5 (5min), 15, 30, 60, 1D
- `session`: RTH (default, 09:30-16:00 ET) or ETH (all hours)
- `from_dt`, `to_dt`: Human-readable datetime strings
  - Format: `"YYYY-MM-DD HH:MM"` (e.g. `"2026-04-08 09:30"`)
  - Or: `"YYYY-MM-DD"` (e.g. `"2026-04-08"` for full day)
  - Timezone: America/New_York (ET) for all trading times
- `from`, `to`: Unix seconds timestamp (legacy, for backward compatibility)

Response: `{ symbol, resolution, session, count, bars: [{time, open, high, low, close, volume}, ...] }`

Example:
```
GET /api/skill/bars?symbol=MNQ&resolution=5&session=RTH&from_dt=2026-04-08%2009:30&to_dt=2026-04-08%2011:00
```

**Quick Start Example:**
```bash
# 1. Fetch bars using datetime strings (no timestamp conversion needed!)
curl "http://localhost:8000/api/skill/bars?symbol=MNQ&resolution=5&session=RTH&from_dt=2026-04-08%2009:30&to_dt=2026-04-08%2011:00"

# 2. Analyze the data
# 3. POST results back to /api/skill/analysis
```

The API automatically converts datetime strings to Unix timestamps internally. No Python scripting required for date conversion!

**⚠️ No Hindsight Bias — Critical for Historical Analysis:**
```bash
# ✓ CORRECT: Analyzing market at 11:00 → fetch bars UP TO 11:00 only
curl "http://localhost:8000/api/skill/bars?symbol=MNQ&from_dt=2026-04-08 09:30&to_dt=2026-04-08 11:00"

# ✗ WRONG: Analyzing market at 11:00 but fetching bars beyond that time
curl "http://localhost:8000/api/skill/bars?symbol=MNQ&from_dt=2026-04-08 09:30&to_dt=2026-04-08 16:00"
# ↑ This would include bars from 11:00-16:00 = hindsight bias!
```

When the user asks "market cycle at 11:00", they want to know what YOU COULD SEE at 11:00, not what happened after.

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
      // Optional: "end_time": 1744220000 (if omitted, uses bar_to from analysis period)
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

| Type | Required Fields | Optional Fields | Description |
|------|----------------|----------------|-------------|
| `range` | `start_time`, `end_time`, `price_high`, `price_low` | `color` | Rectangle on chart (Opening Range, TR, legs) |
| `hline` | `price`, `start_time` | `end_time`, `style` | Horizontal S/R level (rendered as trend_line). If `end_time` omitted, defaults to `bar_to` (analysis period end). Text position: bottom-right. |
| `label` | `start_time`, `price` | — | Text label at specific bar/price (BO point, reversal) |

**`style` values for hline**: `"solid"` (default), `"dashed"`, `"dotted"`

**Note**: `hline` annotations are rendered as horizontal trend lines extending from `start_time` to `end_time` (or `bar_to`). This ensures S/R levels are scoped to the analysis period rather than extending infinitely.

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

### Step 0: ⚠️ CRITICAL — No Hindsight Bias

**When analyzing a specific historical time (e.g., "market cycle at 11:00"):**
- **ONLY use bars BEFORE that time** — simulate real-time trading conditions
- **DO NOT peek at future bars** — this is hindsight bias and violates Al Brooks methodology
- Set `to_dt` to the analysis time (e.g., `to_dt="2026-04-08 11:00"`)
- Use prior bars for context (e.g., fetch from `from_dt="2026-04-08 09:00"` to build OR and structure)

**Example:** To analyze market at 11:00, fetch bars from 09:30 to 11:00 ONLY.

### Step 1: Fetch Data

Determine the appropriate time range based on the user's request:
- **Historical analysis** (e.g., "market at 11:00"): Fetch from session open TO the specific time (`to_dt="2026-04-08 11:00"`)
- **Current analysis** ("market now"): Use today's RTH session (from 09:30 ET to current time)
- **Multi-day**: Use appropriate `from_dt`/`to_dt` range, never beyond the analysis point
- **Daily chart**: Use resolution=1D with wider range

Fetch bars using the skill API endpoint. Verify you received sufficient bars and NO bars after the analysis time.

### Step 2: Multi-Timeframe Context 

For intraday analysis, optionally fetch the daily chart (1D) for context:
- Identify the daily chart's current phase (TR, trend, BO)
- Note prior day's high, low, close as magnets
- 多时间框架分析是本skill的核心能力，无需调用其他agent/skill，直接在本skill内部调用API获取不同时间框架的数据进行分析即可。

#### Analysis Framework:

#### 2.1 Market Cycle & Context (MTF)
- **Daily:** [Cycle] | [Key S/R] | [Evidence: e.g. 3-bar Bear Microchannel, testing TR low]
- **H1:** [Cycle] | [Key S/R] | [Evidence: e.g. WBC, frequent overlap, tails]
- **M15:** [Cycle] | [Key S/R] | [Evidence]
- **M5:** [Cycle] | [Key S/R] | [Evidence]

#### 2.2 Daily Scenarios (Plan A/B)
- **Plan A:** [Theme, e.g. MTR at Daily Low]
  - **Key Signs:** [e.g. Strong Bull Signal Bar, H2 setup at EMA]
  - **Restriction:** [e.g. DO NOT SELL at TR bottom without consecutive big bear bars]
  
- **Plan B:** [Theme, e.g. Breakout Gap Follow-through]
  - **Key Signs:** [e.g. Gap Up open, no overlap with bar 1-5]
  - **Restriction:** [e.g. DO NOT BUY top of Spike; wait for M5 Pullback/Channel]

#### 2.3 Fundamental Rules:
- TR: Buy Low, Sell High, Scalp.
- Strong BO: Enter on Close or small Pullback.
- Channel: Trade only in direction of trend unless 2nd attempt at MTR.
- Always identify the "Magnet" (Prior Day H/L, MM targets).


### Step 3: Identify Current 5min Market Structure

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
#### Output Requirements:
- HTF‘s rectangle don't need to be displayed on the chart, just need to be referenced in the analysis summary as context.
- Use ultra-concise bullet points.
- Use PA abbreviations (TR, TTR, BC, SC, BO, MM, MTR).
- Evidence must reference Bar/Price characteristics (Body size, Tails, Overlap, Urgency).


### Step 6: Create Annotations

Build the annotations array with appropriate types:
- Use `range` for: Opening Range, Trading Ranges,
- Use `trend line` for: legs, channels can be identified by trend line and label
- Use `hline` for: Key S/R levels, MM targets(specify whose MM) , prior day H/L, when multiple leveles are at same price, merger them and display as one hline with multiple labels.
- Use `label` for: BO points, reversal signals, climax bars

### Step 7: Submit to Backend

POST the analysis to `/api/skill/analysis`. The chart will update automatically via WebSocket.

## Rules (Al Brooks Methodology)

### Core Principle: No Hindsight Bias
- **⚠️ NEVER use future bars** when analyzing a historical time point
- **Only analyze with bars available at that moment** — simulate real-time trading
- Example: Analyzing "market at 11:00" → use bars from 09:30 to 11:00 ONLY

### Trading Rules
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
