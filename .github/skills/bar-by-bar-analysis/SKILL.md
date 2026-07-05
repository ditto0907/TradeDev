---
name: bar-by-bar-analysis
description: "Teach and perform a structured, bar-by-bar price action read of an intraday session using Al Brooks methodology. Use when: 'bar by bar', '逐根分析', 'walk me through', 'K线逐根', 'analyze each bar', training/template requests for reading bars one at a time, or when the user wants a repeatable framework to read context + bull/bear strength for every bar. Reads K-line data from the trading terminal backend and produces a segmented, leg-by-leg narrative plus a reusable per-bar checklist."
argument-hint: "Symbol + date (+ optional time cutoff), e.g. 'MES 2026-05-29' or 'MES 2026-05-29 up to 11:00'"
---

# Bar-by-Bar Analysis — Al Brooks Price Action

Produce a disciplined, repeatable bar-by-bar read of an intraday session. The
output doubles as a training template the user can run independently. This skill
focuses on **reading each bar's context and how bull/bear strength is expressed**,
then grouping bars into legs/segments and naming the day type.

This skill shares data access and PA vocabulary with `market-cycle-analysis`.
Read that skill's glossary if an abbreviation is unfamiliar.

## When to Use

- User asks to "go bar by bar", "逐根分析", "walk me through the bars"
- User wants a reusable framework/template for independent analysis
- User wants to understand how strength shifts bar-to-bar, not just the final phase

## Data Access

- Backend is assumed running. Do NOT start it. Fetch via the skill API.
- Endpoint: `GET http://localhost:8000/api/skill/bars`
  - params: `symbol`, `resolution` (5/15/60/1D), `session` (RTH/ETH),
    `from_dt`, `to_dt` (`"YYYY-MM-DD HH:MM"` or `"YYYY-MM-DD"`, ET).
- **No hindsight bias**: when analyzing "up to HH:MM", set `to_dt` to that time only.
- **Pre-open context matters**: an RTH-only open often has just 1–3 bars. Always
  pull overnight/premarket ETH 5min AND prior-day levels so an open "spike" isn't
  misread — it may be a breakout of an overnight TR.
- **Daily levels**: read the 1D bar's `trade_date` field; never astimezone a 1D
  timestamp to ET. PDH/PDL = the daily bar whose `trade_date` is the PRIOR trading
  day (select by field, do not rely on `to_dt`). See `market-cycle-analysis` skill.
- **1H not stored**: DB holds only 5min + 1D. To read the 1H cycle, fetch 5min ETH
  and aggregate into clock-hour bars (group by the ET hour, O=first, H=max, L=min,
  C=last). Use this for the higher-timeframe context layer.

### Compute these per-bar metrics (do this for every bar before narrating)
For each 5min bar derive:
- `body = close - open`; `range = high - low`; `body% = |body|/range`
- `upper_tail = high - max(open,close)`; `lower_tail = min(open,close) - low`
- direction = Bull / Bear / Doji (|body%| < ~10% ≈ Doji)
- vs previous bar: HH/HL/LH/LL, overlap, body growing/shrinking, engulf?

## The Framework — 5 Questions Per Bar (never skip, keep the order)

```
① This bar — who won these 5 minutes?
   - direction; body% (>70% strong | 30–70% neutral | <30% weak/squeeze)
   - tails: long upper = rejection/sellers at high; long lower = buyers at low
   - close location (high/mid/low) = whose statement the close is

② Vs previous bar — strength increasing or fading?
   - HH+HL = bull push | LH+LL = bear push | large overlap = two-sided
   - bigger/smaller body than prior (momentum accel vs exhaustion)
   - engulfs prior? (reversal hint)

③ Position in structure — where is this bar?
   - current cycle: Spike / Channel / TR
   - which bar of the leg? (BO 1st bar vs trend Nth bar vs pullback)
   - near a key level? (PDH/PDL/OR/TR edge/round number)

④ Always In — if forced to hold right now, long or short?
   - resultant of ①②③ → AIL / AIS / unclear

⑤ Trapped / Signal — anyone trapped? any entry signal?
   - failed BO? reversal bar engulfing? signal bar (H1/H2/L1/L2/reversal)?
```

### Three quantities always running in the background
```
A. current leg length → MM projection target
B. this pullback depth vs the previous one → trend accelerating or weakening
C. distance to nearest magnet (PDH/PDL/round/MM)
```

### One-line spoken template (say it out loud per bar)
> "Bar N, [dir][body%], [tail note], vs prior [stronger/weaker], in [cycle] at
> [position], Always In [long/short], [no/has] trap or signal."

## Procedure

1. Fetch & print the session with per-bar metrics (table: #, time, OHLC, body%,
   tails, direction).
2. State the pre-open background line: PDH / PDL / PDC / overnight TR / gap.
3. Add the higher-TF layer: aggregated 1H cycle (TR/Channel/Spike + which leg).
4. Walk bars in **segments/legs**, not an undifferentiated list:
   - For each segment give a small table (#, time, the one-line read) and then a
     2-line "Segment verdict" + "How strength is expressed".
   - Inside long TR phases, summarize by leg rather than every bar (cite the
     defining extreme bars), but still name the edges and the failed BOs.
5. End with a whole-day summary in Brooks language:
   - Day type (Trend / Trading Range / Trend-from-open / etc.)
   - Structural narrative (Spike → Climax/DT → BO → reversal → TR …)
   - Key levels that proved to be magnets
   - The 1–2 extreme bars that defined the day's range
   - Where each side got trapped
6. Close with "reusable lessons" — the 3–4 transferable rules this day teaches.

## Output Format

Concise. Use tables for segments. Use PA abbreviations (TR, BO, FT, SBC/SBrC,
DT/DB, MTR, MM, AIL/AIS, HH/HL/LH/LL). No emoji unless requested.

Segment block shape:
```
## Segment K: <name> (#a–#b, HH:MM–HH:MM)
| #  | time  | read |
| …  | …     | direction + body% + tail + relative-strength + position |
Verdict: <cycle/pattern call>
Strength: <how bull/bear power is expressed: bodies, tails, FT, traps>
```

Whole-day summary shape:
```
• Day type:
• Structure:
• Key levels (magnets verified):
• Defining bars:
• Trapped:
• Reusable lessons:
```

## Rules (Al Brooks, enforced)
- An open spike + PDH breakout is NOT automatically a trend day — check for a
  Buy/Sell Climax and a failed BO (e.g. spike → climax → big reverse bar →
  failed BO ⇒ likely TR day; switch from "chase BO" to "fade edges").
- "No FT after a big bar" is the fingerprint of a TR — once seen, treat every BO
  as a probable failure and fade the TR edges.
- Find the two extreme-body bars (biggest push down and biggest buy-back); they
  usually define the day's TR edges = the day's framework.
- Never sell at TR bottom / buy at TR top unless consecutive large trend bars.
- Always cite concrete bar characteristics (body%, tails, overlap, gaps) — never
  "it looks weak". Measure it.
- Always identify magnets: PDH, PDL, PDC, overnight TR edges, round numbers, MM.

## Counter-Trend Entry Probability (first reversal vs second attempt)

When a bar looks like a reversal signal, ALWAYS qualify its win-rate by WHERE it
sits relative to a preceding strong Spike. The first counter-trend signal against
a strong Spike is one of the lowest-probability trades and a classic beginner loss.

```
Strong Bull Spike (e.g. 2+ consecutive SBC, body% >70%)
  → Buy Climax (long upper tail at the high)
  → FIRST bear signal bar (the "卖方夺权" bar)
```

| Entry style at the FIRST reversal bar | Win-rate (approx) | Why |
|---------------------------------------|-------------------|-----|
| Market-sell on close of 1st reversal  | ~40% or worse     | Spike buyers don't vanish on one bar; first dip usually bought |
| Wait for 2nd push down / LH (MTR 2nd attempt) | ~50–60%   | "Disappointment twice" confirms buyers exhausted |
| Wait for confirmed TR, then fade the upper edge | ~60%+    | No longer betting on reversal, only on the range |

Core rule — **a strong Spike expresses strong commitment; the first reversal
against it most often becomes a pullback or a TR, NOT a trend reversal.**

How to handle the first counter-trend signal (e.g. a big bear bar right after a
bull Spike top):
- Default assumption: it is the FIRST pullback that will be bought, not a reversal.
- If taken, **scalp only** — target the Spike origin or MA, exit at 1:1, stop above
  the climax high. Do NOT swing-hold a first counter-trend entry against a Spike.
- The high-probability short is the SECOND leg down / LH = MTR second attempt.
- Apply the "disappointment twice" rule: let bulls' first buy-back fail once; the
  second failure is your entry.

When narrating any reversal bar, state: (a) is this the 1st or 2nd attempt against
the prior Spike, (b) therefore scalp vs swing, (c) the invalidation level.
