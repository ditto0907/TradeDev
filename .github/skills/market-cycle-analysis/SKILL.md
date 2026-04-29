---
name: market-cycle-analysis
description: "Analyze market cycles using Al Brooks price action methodology. Use when: market analysis, price action analysis, market cycle, S/R levels, opening range, trend analysis, breakout analysis, trading range, measured move. Reads K-line data from the trading terminal backend and writes annotated analysis results back to the chart."
argument-hint: "Symbol and timeframe to analyze, e.g. 'MES 5min RTH today' or 'MES 1D last 20 bars'"
---

# Market Cycle Analysis — Al Brooks Price Action

## Abbreviation Glossary

### Price Action Phases & Patterns
| 缩写 | 全称 | 说明 |
|------|------|------|
| TR | Trading Range | 交易区间：价格在支撑/阻力之间震荡，无明确方向 |
| TTR | Tight Trading Range | 紧缩交易区间：极窄的TR，通常是BO前的蓄力 |
| BO | Breakout | 突破：价格强力离开TR或形态 |
| FT | Follow-Through | 跟进：BO后的后续确认K线 |
| BLCH | Bull Channel | 牛市通道：一系列更高高点(HH)和更高低点(HL)构成的上升通道 |
| BRCH | Bear Channel | 熊市通道：一系列更底高点(LH)和更低低点(LL)构成的下降通道 |
| TC | Tight Channel | 紧密通道：K线几乎无重叠、回调极浅，最强势的通道形态 |
| BrC | Broad Channel | 宽松通道：K线大量overlap、回调深，弱通道，常预示反转 |
| Spike | Spike | 推进段：BO之后的强力大实体连续推进，趋势第一阶段 |
| SC | Sell Climax | 卖出高潮：极度恐慌性下跌，通常预示反转；也缩写为Bear Climax |
| BuC | Buy Climax | 买入高潮：极度追涨，通常预示反转 |
| MTR | Major Trend Reversal | 主趋势反转：趋势方向改变，需要第二次测试确认 |
| MM | Measured Move | 量度涨跌幅：用第一腿长度预测第二腿目标位 |
| OR | Opening Range | 开盘区间：RTH开盘后前1-6根K线（通常前5-30分钟）形成的高低范围 |
| PB | Pullback | 回调：趋势中的短暂反向移动 |
| 2L PB | Two-Legged Pullback | 两腿回调：由两个连续回调腿组成的复杂回调 |
| ioi | Inside-Outside-Inside | 内包-外包-内包K线形态 |
| ii | Inside-Inside | 连续内包K线，TTR信号 |
| oo | Outside-Outside | 连续外包K线 |
| DB | Double Bottom | 双底：两个相近低点，潜在牛市反转信号 |
| DT | Double Top | 双顶：两个相近高点，潜在熊市反转信号 |
| H1/H2 | High 1 / High 2 | 第1/2次更高高点，牛市回调入场信号 |
| L1/L2 | Low 1 / Low 2 | 第1/2次更低低点，熊市回调入场信号 |

### Price Level Abbreviations
| 缩写 | 全称 | 说明 |
|------|------|------|
| PDH | Prior Day High | 前一交易日最高价，作为关键阻力/磁铁 |
| PDL | Prior Day Low | 前一交易日最低价，作为关键支撑/磁铁 |
| PDC | Prior Day Close | 前一交易日收盘价，作为关键参考位 |
| PDO | Prior Day Open | 前一交易日开盘价 |
| HOD | High of Day | 当日最高价 |
| LOD | Low of Day | 当日最低价 |
| S/R | Support / Resistance | 支撑/阻力 |
| EMA | Exponential Moving Average | 指数移动平均线（通常指20EMA） |

### Bar & Candle Characteristics
| 缩写 | 全称 | 说明 |
|------|------|------|
| HH | Higher High | 更高高点：当前K线高点高于前一K线高点 |
| HL | Higher Low | 更高低点：当前K线低点高于前一K线低点 |
| LH | Lower High | 更低高点：当前K线高点低于前一K线高点，熊市信号 |
| LL | Lower Low | 更低低点：当前K线低点低于前一K线低点 |
| WBC | Weak Bull Candle | 弱多头K线：实体小、影线长或收盘靠近低点 |
| SBC | Strong Bull Candle | 强多头K线：大实体、收盘靠近高点、影线小 |
| WBrC | Weak Bear Candle | 弱空头K线：实体小、影线长或收盘靠近高点 |
| SBrC | Strong Bear Candle | 强空头K线：大实体、收盘靠近低点、影线小 |
| Doji | Doji | 十字星：开收盘接近，多空均衡，方向不明 |

### Sessions & Timeframes
| 缩写 | 全称 | 说明 |
|------|------|------|
| RTH | Regular Trading Hours | 正式交易时段：09:30-16:00 ET |
| ETH | Extended Trading Hours | 延长交易时段：含盘前盘后，全天 |
| D1 | Daily | 日线时间框架 |
| H1 | 1-Hour | 1小时时间框架 |
| M15 | 15-Minute | 15分钟时间框架 |
| M5 | 5-Minute | 5分钟时间框架 |
| MTF | Multi-TimeFrame | 多时间框架分析 |
| HTF | Higher TimeFrame | 更高时间框架（如分析M5时，M15/H1/D1均为HTF） |

### Trading Actions & Concepts
| 缩写 | 全称 | 说明 |
|------|------|------|
| PA | Price Action | 价格行为：直接依据裸K线分析市场 |
| LE | Long Entry | 多头入场 |
| SE | Short Entry | 空头入场 |
| SL | Stop Loss | 止损 |
| PT | Profit Target | 利润目标 |
| Magnet | — | 磁铁：价格倾向被吸引的目标位，如PDH/PDL/MM目标/整数关口 |
| Gap | — | 跳空：K线与前一K线之间无价格重叠 |

---

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

#### 4.1 BO vs Climax — 关键区分

**核心原则**：BO 与 Climax 在 K 线形态上极相似（大实体、强 urgency、单向连续推进），区分关键在**位置/上下文**与**FT 行为**。BO 是"出生"，Climax 是"死亡"。

**判断步骤：**

| 维度 | **BO**（突破） | **SC / BuC**（卖出/买入高潮） |
|------|----------------|------------------------------|
| 起点位置 | 从 TR 内部 / TTR / 形态突破 | 已在单边趋势末端，离起点很远 |
| 价格已走距离 | 短，未拉伸 | 长，连续若干腿，超过 2×ATR |
| 与 S/R 关系 | 突破 S/R，**离开**它 | 已远离最近 S/R，伸向极端区 |
| 前置背景 | 横盘酝酿、ii/oo、收缩 | 已有明显趋势腿，第 3 腿 / 第 3 次推进 |
| 大棒计数 | ≤ 3 根连续大棒 | ≥ 3 根加速大棒，实体逐根放大（escalation） |
| EMA20 距离 | 靠近 EMA20 | 极度拉伸，远离 EMA20 |

**FT（Follow-Through）观察 — 1-3 根后续 K 线：**
- 同向继续大实体推进 → **BO 成功**（进入 Spike → Channel）
- 立刻出现反向大实体 K（吞没/引擎反向）→ **Climax 确认**，启动反转
- 紧缩 / Doji / 小 K 线（停滞）→ 可能 Climax 或 Failed BO，等 2nd entry
- 立即跌回 BO 起点之内 → **Failed BO**（TR 延续）

**决策树：**
```
强势单向推进 K 线出现
  ├─ 来自横盘 / TR 内部？
  │    ├─ 是 → BO 假设
  │    │     └─ FT 同向？ → BO 成功；FT 反向？ → Failed BO
  │    └─ 否 → Climax 假设
  │          └─ 已连续 ≥3 根加速大棒？
  │                ├─ 是 → SC/BuC 概率高，等反向信号棒
  │                └─ 否 → 趋势中段 Spike（不是 Climax，继续顺势）
```

**交易禁忌：**
- **永远不要在 SC 底部直接做多**，等 2nd entry（DB 或反向 H2/L2）
- **永远不要在 BuC 顶部直接做空**，等 2nd entry（DT 或反向 L2/H2）
- 不确定是 BO 还是 Climax 时，**等 FT** —— Al Brooks 黄金原则

#### 4.2 Channel（通道）识别

**通道 = 趋势第二阶段**，紧跟 Spike/BO 之后，斜率较缓但持续推进。**通道不会凭空出现，前面必须有 Spike**（或 Climax 反转后的 reversal spike）。无 Spike 前置 → 不是通道，只是 TR 震荡。

**通道三种类型：**

| 类型 | 形态特征 | 力量 |
|------|---------|------|
| **Tight Channel** (TC) | K 线几乎无重叠、HH/HL 严密、几乎无回调 | 最强，最难反转 |
| **Normal Channel** | 有少量 overlap，回调 1-2 根 K 线 | 中等，可顺势 |
| **Broad Channel** (BrC) | 大量 overlap、深回调、bar size 不一 | 弱，接近 TR；常预示反转 |

**5 个判断标准（必须同时满足）：**

1. **HH/HL（牛通道）或 LH/LL（熊通道）序列**：至少 2 组；序列被破坏 → 通道结束
2. **可绘出两条平行斜线**：上轨连 swing highs，下轨连 swing lows，夹角 < 15°；若收敛 → wedge
3. **斜率缓于前置 Spike**：通道斜率必然缓于 Spike；若一致或更陡 → 仍在 Spike，不是通道
4. **回调深度**：
   - Tight Channel：回调 < 上一推进腿的 50%
   - Broad Channel：回调可达 80%+，frequent overlap
5. **EMA20 关系**：
   - Bull Channel：价格保持在 EMA20 之上，回调测试 EMA 后反弹
   - Bear Channel：价格保持在 EMA20 之下
   - 若价格反复穿越 EMA20 → 不是通道，是 TR

**通道结束信号（转 TR 或反转）：**
- 收盘**强力穿越通道线**（trendline break）
- 出现 **Climax** 式加速（最后冲刺常预示衰竭）
- 通道线第二次测试失败（failed BO 反方向）= MTR 1st leg
- 价格回到通道中轴后无法继续推进

**通道决策树：**
```
价格连续单向推进
  ├─ 前面有 Spike？
  │    ├─ 有 → 通道假设成立
  │    │     ├─ 紧密无 overlap → Tight Channel（强势顺势）
  │    │     ├─ 中度 overlap → Normal Channel（顺势 H2/L2 入场）
  │    │     └─ 大量 overlap + 深回调 → Broad Channel（接近 TR，警惕反转）
  │    └─ 无 → 不是通道，是 TR 震荡
```

**通道交易规则：**
- **顺势**：每次回调到通道下轨 / EMA20 时入场（H1/H2 或 L1/L2）
- **逆势禁忌**：通道中**不要逆势交易**，除非已是 MTR 第二次尝试
- **目标**：通道延续 → 跟到 MM 目标；通道结束 → 平仓等 TR

#### 4.3 综合判断流程

```
强势单向推进 K 线出现
  │
  ├─ 1. 位置：来自 TR/形态？还是已远离起点？
  ├─ 2. 大棒计数：≤3 根 vs ≥3 根加速？
  ├─ 3. EMA 距离：靠近 EMA 还是已极度拉伸？
  ├─ 4. S/R 关系：刚突破 S/R 还是远离 S/R？
  ├─ 5. FT 观察：1-3 根后续 K 线行为
  │
  └─ 综合判定：
        BO  → 接 Spike → 进入 Channel → 抵达 MM/S/R → 转 TR 或 Climax
        Climax → 反向信号棒 → MTR 1st leg → 等 2nd attempt 确认
```

#### 4.4 常见误判规避

| 误判 | 正确做法 |
|------|---------|
| 把 Spike 当 Climax 提前反向 | 等 FT；Spike 只 3 根内不算 Climax |
| 把通道末端 Climax 当作"再涨一段"追入 | 远离 EMA + 加速大棒 = 警惕，至少缩仓 |
| 把 Broad Channel 当作 TR 反复刷震荡 | 仍有 HH/HL 序列时，方向偏好顺势 H2/L2 |
| 把 Failed BO 当作有效 BO 追入 | 必看 FT；BO 起点回吞 = 失败 |

> **Al Brooks 黄金原则**：当不确定时，**永远等 2nd signal**。第一次永远可能错，第二次成功概率是第一次的两倍。

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
