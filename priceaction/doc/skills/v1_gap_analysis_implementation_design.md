# Gap Analysis — Skill & MCP 实现设计（存档）

> 上游设计：[v1_gap_analysis_skill_design.md](v1_gap_analysis_skill_design.md)（定稿）。本文档是其落地实现方案（Skill / MCP / 测试）。
> 日期：2026-08-31
> 状态：实现方案定稿，编码进行中。测试部分先留方案，编码完成后再实现。

## 一、总体架构与分期

```
P1 (零后端改动)：SKILL/MCP instructions → LLM 调 /api/skill/bars
                → 进程内直调 gap_detector 跑检测+状态机+叙事 → POST /api/skill/analysis
P2 (机械化)：   gap_detector.py (纯函数, 仿 signal_detector.py)
                → mcp_gap_analysis.py (FastMCP, 仿 mcp_market_cycle.py)
                → 可选后端端点 GET /api/skill/gaps
P3 (信号化)：   缺口特征写入 Orders 表 → strategy-research 回测校准记分板权重
```

设计原则（从现有代码继承）：
- **纯函数检测层**：`gap_detector.py` 无 DB/IO，输入 bar dicts，输出 `@dataclass` + `to_dict()`（同 `signal_detector.py`）。
- **薄 MCP 封装层**：复用 `mcp_market_cycle.py` 的 `_client/_err/_request` 骨架 与 `mcp_strategy_research.py` 的 `_fetch_bars` / `trade_date` 防坑逻辑。
- **无后视偏差**：`to_dt` 硬约束；1D 按 `trade_date < date` 严格筛选前一交易日。

## 二、纯函数检测层 `strategy/gap_detector.py`

### 2.1 数据结构

```python
@dataclass
class Gap:
    gap_id: int            # 1..N
    gap_type: str          # "G3" | "G4" | "G5"
    direction: str         # "Bull" | "Bear"
    create_ts: int         # 创建棒(BO棒) open ts
    create_bar_cnt: str    # "B7" (复用 signal_detector._bar_cnt_for_ts)
    low: float             # 缺口区下沿
    high: float            # 缺口区上沿
    size_ticks: float      # 缺口大小 / tick
    origin_price: float    # 起源位 (REVERSED 判定基准)
    far_price: float       # 远端/突破位 L (CLOSED 判定基准)
    key_level_source: str  # G4 专用: "PDH"|"PDL"|"OR_high"|"OR_low"|"swing_high"|"swing_low"|""

@dataclass
class GapEvent:            # 状态机逐棒迁移事件
    gap_id: int
    ts: int
    bar_cnt: str
    from_state: str
    to_state: str          # CREATED/TESTED/PARTIAL/CLOSED/REVERSED/DEFENDED/HELD
    depth_pct: float       # 回撤进入缺口深度 0..1

@dataclass
class G5Heartbeat:
    direction: str
    current_streak: int
    max_streak_today: int
    ema_touch_events: list[int]   # 首次触碰 EMA 的 ts 列表
```

### 2.2 检测函数（文档 §2 表格）

```python
TICK_SIZE = {"MES": 0.25, "MNQ": 0.25, "MGC": 0.1, "NK225MC": 5}
G3_MIN_TICKS = 2
G3_ATR_PCT = 0.15
G5_STRONG_STREAK = 20
BAR_SECONDS = 300

def compute_ema20(bars) -> list[float]
def compute_atr(bars, n=20) -> list[float]

def detect_g3_micro_gaps(bars, tick, atr20) -> list[Gap]
    # bull: low(i+1) > high(i-1)；显著阈值 ≥2 ticks 或 ≥15% ATR(20)
    # 检测前必须校验 time(i) - time(i-1) == 300 （防数据缺口误判为价格缺口）

def detect_g4_breakout_gaps(bars, key_levels) -> list[Gap]
    # 突破关键位 L 后，后续回调极值始终不触及 L
    # key_levels: {"PDH":x,"PDL":x,"PDC":x,"OR_high":x,"OR_low":x, swing...}

def detect_g5_ema_gap_bars(bars, ema20) -> tuple[list[Gap], G5Heartbeat]
    # 连续 low>EMA20(bull)/high<EMA20(bear)，≥20 根 = 强趋势阈值
```

### 2.3 生命周期状态机（文档 §3 核心引擎）

```python
def track_gap_lifecycle(gap, bars_after, checkpoints=(6,12,18)) -> list[GapEvent]
```

双层回补判定（已确认）：
- 触及 `far_price` → `CLOSED`（磁铁效应，非反转）
- 收盘越过 `origin_price` → `REVERSED`（顺势方被套，failed BO）
- 长影线拒绝、收回缺口之外 → `DEFENDED`（逆势方被套）
- N 根无人测试 → `HELD`（检查点 B+6/B+12/B+18 = 30/60/90min）
- 回撤 > 50% → `PARTIAL`；进入缺口任意深度 → `TESTED`

状态机永远从 `CREATED` 全量重建，无"从中间开始"。

### 2.4 主编排

```python
def analyze_day(bars_5m, key_levels, symbol="MES", tick=None, cutoff_ts=None)
    -> dict  # {gaps, events, g5_heartbeat}
```

## 三、MCP 层 `mcp_gap_analysis.py`（仿 `mcp_market_cycle.py`）

Skill 名 `gap-analysis`。复用 `_client/_err/_request` 与 `trade_date` 防坑。

| Tool | 用途 |
|------|------|
| `analyze_gaps(symbol, date, cutoff, tick)` | 主入口：取 1D+隔夜+RTH → 检测 G3/G4/G5 → 跑状态机 → 返回 `{key_levels, prev_unfilled_gap, gaps[], events[], g5_heartbeat}` |
| `get_key_levels(symbol, date)` | 返回 PDH/PDL/PDC/OR/隔夜区间（G4 关键位来源） |
| `save_gap_analysis(...)` | 记分板结论 + 标注写回 `/api/skill/analysis`（**只标存活缺口**，REVERSED label 例外） |
| `list_gap_analyses / toggle_analysis / delete_analysis` | 复用 market-cycle 的三个管理工具签名 |

FastMCP `instructions` 固化：定性第二层（腿计数+EMA 拉伸 → Breakout/Measuring/Exhaustion；Measuring 投影 MM）、多空记分板（顺势/逆势证据 → AIL/AIS/中性 → 日型假设 → 被套方止损堆）、输出结构、标注色板。

无后视：`cutoff` → `to_dt` 硬约束；盘中调用也从 09:30 全量重建。

### 量化参数默认（文档 §6，可覆盖）

```python
DEFAULTS = {"g3_min_ticks": 2, "g3_atr_pct": 0.15,
            "held_checkpoints": [6, 12, 18],
            "g5_strong_streak": 20, "partial_threshold": 0.5}
```

## 四、可选后端端点（P2）`GET /api/skill/gaps`

对齐 `/api/skill/bars` 风格，参数 `symbol/date/cutoff/tick`，服务端跑 detector+状态机返回同 JSON。P1 先由 MCP 进程内直调 `gap_detector`，不建端点。

## 五、测试方案（编码完成后实现）

### 5.1 `tests/test_gap_detector.py`（单元，纯函数，无网络；仿 `test_signal_detector.py`）

| 测试类 | 覆盖点 |
|--------|--------|
| `TestG3Detection` | bull `low(i+1)>high(i-1)` 命中 / bear 对称 / 未达 2-tick 阈值不报 / **数据缺口 Δt≠300s 不误判** |
| `TestG4Detection` | 突破 PDH 后回调不触及=有效 / 回调触及 L=无效 / OR 高低作关键位 |
| `TestG5Detection` | 连续 20 根触发强趋势 / 计数中断重置 / 首次 EMA 触碰事件 |
| `TestLifecycleStateMachine` | CREATED→TESTED→PARTIAL / 触及远端=CLOSED / 收盘越起源=REVERSED / 长影线=DEFENDED / N根无测试=HELD 检查点 |
| `TestGapFillDoubleLayer` | CLOSED≠REVERSED：仅触及记 CLOSED，收盘越过才 REVERSED |
| `TestEdgeCases` | 无缺口日返回空+"TR 日证据" / 重叠缺口独立跟踪不合并 / 半日假日用时间戳非固定棒数 / 空输入 |
| `TestG5Heartbeat` | current/max streak / EMA 触碰列表 |
| `TestSerialization` | `to_dict()` 字段完整 |

### 5.2 `tests/test_gap_skill_api.py`（集成冒烟，需后端；仿 `test_skill_api.py`）

调 `/api/skill/bars` 取真实一天 → 跑 `analyze_gaps` → 断言缺口清单结构 → POST `/api/skill/analysis` 验证标注写回。

### 5.3 无后视偏差回归测试

- `test_cutoff_no_hindsight`：`analyze_gaps(cutoff=11:00)` 与全天重建到 11:00 的中间快照一致。
- `test_prev_day_close_trade_date`：PDC/PDH/PDL 按 `trade_date < date` 选前一交易日，不泄漏当日/次日 1D 棒。

## 六、落地文件清单

```
priceaction/
├── strategy/gap_detector.py      ← 新增：纯函数检测 + 状态机
├── strategy/__init__.py          ← 追加导出
├── mcp_gap_analysis.py           ← 新增：FastMCP server (stdio)
├── tests/test_gap_detector.py    ← 新增：单元测试（编码完成后）
├── tests/test_gap_skill_api.py   ← 新增：集成冒烟（编码完成后）
├── server.py                     ← (P2) 追加 GET /api/skill/gaps
└── v1_gap_analysis_skill_design.md  ← 上游设计
```

## 七、与现有约定的一致性

| 约定 | 来源 | 落实 |
|------|------|------|
| `/api/skill/bars` `from_dt`/`to_dt` ET 字符串 | `mcp_strategy_research._fetch_bars` | `analyze_gaps` 复用 |
| 1D `trade_date` 防坑选前一日 | `_prev_day_close` | `_prev_day_levels` 同模式取 PDC/PDH/PDL |
| `_request` 结构化错误 | `mcp_market_cycle` | MCP 层继承 |
| `_bar_cnt_for_ts` (B1/B2…) | `signal_detector` | import 复用于 `create_bar_cnt` |
| 标注 range/hline/label + 色板 | `mcp_market_cycle` instructions | `save_gap_analysis` 沿用 |
| `@dataclass` + `to_dict()` | `signal_detector.SignalRecord` | `Gap`/`GapEvent`/`G5Heartbeat` 同构 |
