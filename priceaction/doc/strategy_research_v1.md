# Strategy Research System — Design v1

**目标**：提升交易策略研究效率。MCP 程序负责精确量化信号特征，LLM 负责背景与S/R分析，
人工最终评价并固化系统。

---

## 1. 角色与流程分工

| 阶段 | 执行者 | 输出 |
|------|--------|------|
| 1. 信号发现 + 量化 | **MCP Server (代码)** | 信号K列表，含方向、Bar Cnt、Gap、COH/L、Overlapping、FT |
| 2. 背景 / 形态 / S/R 分析 | **LLM (Skill)** | Pattern、Context、SR Detail、阻力/支撑文字描述 |
| 3. 主观评价 + 过滤规则 | **人工** | 背景支持(Y/N)、支持理由 |
| 4. 胜率统计 + 固化 | **MCP / LLM 协同** | 回测报告 |

输出全部写入 Google Sheet `2cc BO System` 的 `Orders` 工作表（gid=622455454）。

---

## 2. 信号K定义（Strict Spec）

> **信号K**：连续2根同向K线（"2 consecutive bars"），第2根即为"信号K"。

### 2.1 方向判定
- **Bull signal**：`bar[n].close > bar[n].open` AND `bar[n-1].close > bar[n-1].open`
- **Bear signal**：`bar[n].close < bar[n].open` AND `bar[n-1].close < bar[n-1].open`
- 中性（doji、十字星）打破连续性

### 2.2 字段量化定义

| Field | 类型 | 算法 |
|-------|------|------|
| `Date` | YYYYMMDD | 信号K所在交易日（ET） |
| `Bar Cnt` | `B<n>` | 9:30 ET 之后第 n 根 5min K线（含信号K自身），9:30→B1, 9:35→B2 ... |
| `Direction` | Bull / Bear | 见 §2.1 |
| `Gap` | None / GapUp / GapDown | 当日开盘 vs 前日收盘：`open > prev_close + 1tick` 为 GapUp，`<` 为 GapDown |
| `Signal Strength` | Strong / Weak | 信号K + 前一K两者 COH/L 都满足强K阈值则 Strong；否则 Weak |
| `COH/L` | float (0~1) | **Bull**: `(close - low) / (high - low)`；**Bear**: `(high - close) / (high - low)`。Bull bar > 0.69 即 Strong；Bear bar < 0.31 即 Strong（即从对侧算 > 0.69） |
| `Overlapping` | small / medium / large | `overlap = max(0, min(h1,h2) - max(l1,l2))`；`union = max(h1,h2) - min(l1,l2)`；`pct = overlap / union`。 < 0.33 → small, 0.33–0.66 → medium, > 0.66 → large |
| `PB Bars` | int | 信号K之前的回调腿K线数（同向 leg），仅供参考，缺省 0 |
| `PB Strength` | weak / strong | 回调腿是否含至少一根强反向K线（COH/L 阈值），缺省 weak |
| `FT` | N / Y / 2nd | 信号K之后立即同向跟进 → **Y**（要求至少十字星：`abs(close-open) > 0` 且方向同信号）；信号K后第2根才同向 → **2nd**；否则 **N** |

### 2.3 强K阈值
- 阈值 **0.69 / 0.31** 来自用户定义（69% bar range）
- Doji 容忍：`bar_range > 0` 才计算，`bar_range == 0` 时 COH/L = 0.5

---

## 3. Context / 形态 / S/R 由 LLM 填充

LLM 在收到 MCP 信号列表后，**对每个信号K**：
1. 取信号K前 N=80 根 5min RTH bars（≈一整个交易日）+ 前1日 1D bar
2. 严格遵循 Al Brooks PA 方法识别：
   - **Pattern**：Wedge / DB / DT / BO / Climax / 其他
   - **Minor/Major**：是否为主趋势反转
   - **Leg Cnt**：当前是趋势的第几条腿（1st/2nd/3rd）
   - **Context**：当时所处的市场周期文字描述（如 "空头通道底部", "TR上沿BO失败"）
   - **SR**：信号K上方 / 下方是否有近距离 S/R（Y/N）
   - **SR Detail**：具体 S/R 描述（"PDH @ 5210", "OR low @ 5180"）

填入对应 Google Sheet 列。

---

## 4. 模块设计

```
priceaction/
├── strategy/
│   ├── signal_detector.py      ← 纯函数，输入 bars 数组输出 SignalRecord 列表
│   ├── sheet_writer.py         ← Google Sheets 客户端封装（基于现有 google_sheets_sync 模式）
│   └── __init__.py
├── mcp_strategy_research.py    ← MCP server，stdio 模式
├── tests/
│   └── test_signal_detector.py ← 单元测试
└── doc/
    └── strategy_research_v1.md ← 本文档
```

### 4.1 `signal_detector.py` API

```python
@dataclass
class SignalRecord:
    date: str              # "20260301"
    bar_cnt: str           # "B4"
    bar_ts: int            # Unix ts
    direction: str         # "Bull" | "Bear"
    gap: str               # "None" | "GapUp" | "GapDown"
    signal_strength: str   # "Strong" | "Weak"
    coh_l: float
    overlapping: str       # "small" | "medium" | "large"
    overlapping_pct: float
    pb_bars: int
    pb_strength: str
    ft: str                # "Y" | "2nd" | "N"

def detect_signals(
    bars_5min: list[dict],     # [{time, open, high, low, close, volume}, ...] RTH only
    prev_day_close: float,
) -> list[SignalRecord]:
    ...
```

### 4.2 Google Sheet 列映射

参考实际 Sheet 第1-2行表头（已读取确认）：

| Col | Field |
|-----|-------|
| B | Date |
| C | Bar Cnt |
| D | Signal Strength |
| E | COH |
| F | Overlapping |
| G | PB Bars |
| H | PB Strength (Strength) |
| I | FT |
| J | Pattern |
| K | Minor/Major |
| L | Leg Cnt |
| M | Context |
| N | SR |
| O | SR Detail |
| P | 背景支持 |
| Q | 支持理由 |

> 注：A列空，第1行是大类标题，第2行是子标题，**数据从第3行开始**追加。

### 4.3 MCP Server Tools

| Tool | 用途 |
|------|------|
| `detect_signals` | 给定日期 / 日期范围 / 品种合约，调用 backend `/api/skill/bars`，跑 detector，返回信号 JSON |
| `write_signals_to_sheet` | 批量追加信号到 Sheet（数据列 B–I 由 detector 填，J–Q 留空给 LLM/人工） |
| `update_signal_analysis` | LLM 完成 Pattern/Context/SR 分析后，按行 ID 回写 J–O 列 |
| `list_signals_from_sheet` | 读取 Sheet 当前所有信号，便于 LLM 知道哪些已写入 |
| `compute_winrate` | (后续阶段) 给定过滤条件统计胜率 |

### 4.4 与现有系统的边界
- **复用** backend `/api/skill/bars` 取数据（已支持月份合约 token）
- **复用** `credentials/service_account.json` 做 Google 认证
- **不复用** 现有 `google_sheets_sync.py`（那是 OHLCV 流式写入，需求不同）

---

## 5. 工作流（Skill 引导 LLM）

```
用户："研究 MES 2026-04-01 至 2026-04-15 的信号"
↓
LLM 调用 detect_signals(symbol=MES@CONT_FRONT, from='2026-04-01', to='2026-04-15')
↓
得到 N 个信号 → 调用 write_signals_to_sheet(...) 批量写入
↓
对每个信号 LLM:
  1. 调用 get_bars 取信号K前 80 根 + D1
  2. 按 Al Brooks 方法分析 Pattern/Context/SR
  3. 调用 update_signal_analysis(row_id=..., pattern=..., context=..., sr_detail=...)
↓
人工 review Sheet，填写 P/Q 列（背景支持、支持理由）
↓
后续：compute_winrate 统计
```

---

## 6. 阶段划分

- **Phase 1 (本次实现)**：信号检测 + Sheet 写入 + LLM 分析回写 → 让流程跑通
- **Phase 2 (后续)**：胜率统计 + 过滤规则建议
- **Phase 3 (后续)**：自动回测引擎（基于 Sheet 的 Y/N 过滤规则）
