# Data Management Redesign — v3

> 状态：方案稿（pending review）
> 范围：bars 存储、IB fetch、IB cache、连续合约视图、前端 symbol 选择
> 前提：**清空所有历史数据**，不做兼容/回补；冷启动重建

---

## 0. 目标与原则

### 设计原则（与业界 CQG / Barchart / IQFeed / NinjaTrader 对齐）

1. **Per-contract bars 是唯一事实（fact）**：底层只存"某个具体到期月在某个时间点的真实成交"，永久 immutable。
2. **连续合约是 derived view**：由 per-contract bars + rollover policy + adjustment policy 在**读时**合成，**不落地为 fact**。
3. **来源可追溯、不可降级**：source 有 rank，低 rank 不能覆盖高 rank；任何修订留痕。
4. **IB cache 是 IB 的镜像**：与业务表 `bars` 解耦；用于减少 IB 请求次数，本身不参与对外服务。
5. **前端显式选择**：dropdown 同时列出连续合约（`MES`、`MES_CONT_RATIO` 等）与具体到期月（`MES 202606`、`MES 202609`），用户自主选；不再隐式拼接。

### 非目标
- 不实现历史数据迁移、不做老数据回补
- 不改 trading_calendar / contract_calendar 的语义，只改其调用方式
- 不引入新的数据源（仅 IB）

---

## 1. 存储模型

### 1.1 `bars` 表（per-contract，唯一事实表）

```sql
CREATE TABLE bars (
    symbol         TEXT NOT NULL,        -- 'MES', 'MNQ', 'NK225M', 'MGC'
    contract_month TEXT NOT NULL,        -- 'YYYYMM'，永远非空
    timeframe      TEXT NOT NULL,        -- '5min','15min','60min','1D'
    ts             INTEGER NOT NULL,     -- bar open, Unix UTC seconds
    open           REAL NOT NULL,
    high           REAL NOT NULL,
    low            REAL NOT NULL,
    close          REAL NOT NULL,
    volume         REAL NOT NULL,
    source         TEXT NOT NULL,        -- 见 §1.4
    source_rank    INTEGER NOT NULL,     -- 冗余写入，避免读时 lookup
    fetched_at     INTEGER NOT NULL,     -- 此行最近一次写入时刻
    PRIMARY KEY (symbol, contract_month, timeframe, ts)
);
CREATE INDEX bars_lookup ON bars (symbol, timeframe, ts, contract_month);
```

**关键变化**：
- `contract_month` 进入主键，**强制非空** —— 同 ts 多合约可共存（rollover overlap 完整保留）
- 新增 `source_rank`、`fetched_at`
- 取消 `realtime_completed` 落入 `bars` 表的路径 —— 见 §3

### 1.2 `realtime_bars` 表（仅 in-progress 当前 bar）

```sql
CREATE TABLE realtime_bars (
    symbol         TEXT NOT NULL,
    contract_month TEXT NOT NULL,
    timeframe      TEXT NOT NULL,
    ts             INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (symbol, contract_month, timeframe)
);
```

**用途**：单条最新未完成 bar 的临时快照；bar 收盘后**不直接 promote 到 `bars` 表**，而是触发 IB 拉取覆盖（见 §3）。

### 1.3 `ib_fetch_cache` 表（IB 镜像，请求去重）

```sql
CREATE TABLE ib_fetch_cache (
    symbol         TEXT NOT NULL,
    contract_token TEXT NOT NULL,        -- 'MONTH:202606' | 'CONT'
    timeframe      TEXT NOT NULL,
    ts             INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    fetched_at     INTEGER NOT NULL,
    PRIMARY KEY (symbol, contract_token, timeframe, ts)
);
CREATE TABLE ib_fetch_log (
    symbol TEXT, contract_token TEXT, timeframe TEXT,
    from_ts INTEGER, to_ts INTEGER, fetched_at INTEGER,
    bar_count INTEGER, PRIMARY KEY (symbol, contract_token, timeframe, from_ts, to_ts)
);
```

**关键变化**：
- `contract_token` 取代 `contract_month`：可以是 `'MONTH:202606'` 或 `'CONT'`
- ContFuture 数据**只**入 `ib_fetch_cache`，**永远不入 `bars`** —— 因为 IB 会在 rollover 时对 ContFuture 全历史 back-adjust，数据非 immutable
- `ib_fetch_log` 记录每次实际打 IB 的 range，避免反复 query "expected vs cached" 计算（轻量优化）

### 1.4 source 枚举与 rank 表

| source             | rank | 含义                                       | 入 `bars`？ |
|--------------------|------|--------------------------------------------|-------------|
| `ib_validated`     | 100  | 经 fix/promote 流程比对 IB 一致后落地      | ✅          |
| `ib_monthly`       | 80   | 来自 `Future(symbol, YYYYMM)` 的初始拉取  | ✅          |
| `ib_historical`    | 60   | 启动批量加载（≡ ib_monthly，仅来源标识）  | ✅          |
| `realtime_pending` | 20   | （仅在 realtime_bars 表）                  | ❌          |
| `ib_continuous`    |  0   | ContFuture 数据                            | ❌（仅 cache）|

**rank 护栏**：`db.insert_bars` 在 REPLACE 前查旧行 rank，`new_rank < old_rank` 直接拒写并 log warning。

### 1.5 `bar_revisions` 表（审计）

```sql
CREATE TABLE bar_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, contract_month TEXT, timeframe TEXT, ts INTEGER,
    prev_source TEXT, prev_rank INTEGER,
    prev_open REAL, prev_high REAL, prev_low REAL, prev_close REAL, prev_volume REAL,
    new_source TEXT, new_rank INTEGER,
    diff_summary TEXT,            -- JSON
    revised_at INTEGER NOT NULL,
    reason TEXT                   -- 'fix_bars' | 'bg_validate' | 'recover_realtime'
);
CREATE INDEX bar_revisions_lookup ON bar_revisions (symbol, contract_month, timeframe, ts);
```

每次 `insert_bars` 检测到与现有行不同 → 自动追加一行。便于事后诊断"这根 bar 为什么变成现在这样"。

### 1.6 `validated_ranges` 表（保留，加 contract_month 维度）

```sql
CREATE TABLE validated_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, contract_month TEXT, timeframe TEXT,
    from_ts INTEGER, to_ts INTEGER,
    checked_at INTEGER, mismatches INTEGER, fixed INTEGER,
    UNIQUE (symbol, contract_month, timeframe, from_ts, to_ts)
);
```

---

## 2. 连续合约（continuous）—— derived view，读时合成

### 2.1 三种 series 类型（业界标准）

| series                  | 拼接方式                                                                 | 价格调整 |
|-------------------------|--------------------------------------------------------------------------|----------|
| `front`（不调整）       | 在每个 rollover_date 切到下一合约。价格保留**真实**值，rollover 处可能跳空 | 无       |
| `cont_ratio`（比率）    | 同上切换，但用 ratio = front_close / new_close 反向乘到所有更早 bar      | 比率     |
| `cont_difference`（差值）| 同上切换，但用 diff = front_close - new_close 反向加到所有更早 bar       | 差值     |

### 2.2 Rollover 规则

由 `contract_calendar.active_contract(ts, symbol)` 决定。已有实现，无需改。

### 2.3 实现位置：`continuous_view.py`（新文件）

```python
def assemble_continuous(
    symbol: str, timeframe: str,
    from_ts: int, to_ts: int,
    method: Literal['front','cont_ratio','cont_difference'] = 'front',
) -> List[dict]:
    """读 per-contract bars + 按 rollover 切片 + 应用 adjustment policy。
    永远不写库。"""
```

**关键**：每次请求即时计算。对 5min × 一年的 chart，per-contract bars ≈ 几千行，内存合成开销 < 50ms。无需缓存。

### 2.4 ContFuture 数据的位置

`fetch_range(..., contract_token='CONT')` 仍可能被调用（用于：①历史超出 monthly 可服务窗口的回填；②比对参考），但结果**只**入 `ib_fetch_cache`（contract_token='CONT'），**永远不参与对外 `/api/history` 响应**。对外的连续合约响应一律走 `assemble_continuous()`。

---

## 3. Realtime 数据流（重大简化）

### 3.1 旧流程的问题
现状：tick → 累积成 5min bar → 收盘瞬间 `persist_completed_bar` 写入 `bars` 表（source=`realtime_completed`）→ 3 分钟后 `revalidate_realtime_bar` 拉 IB 覆盖。

问题：
- 收盘瞬间和 IB 端真正完整 bar 之间有几秒到几分钟的窗口期，DB 里就是错的
- `realtime_completed` 的 bar 没法精确表达 contract（早期实现 cm 为空）
- 与 `ib_validated` 在时间线上交错

### 3.2 新流程

```
tick → 累积 in-progress bar → upsert 到 realtime_bars 表（仅当前 bar）
bar 收盘 → 不写 bars 表
       → 立即调度 fetch_range(MONTH:cm, [ts, ts]) （延迟 60s 以等 IB ready）
       → 拿到 IB 数据后 insert_bars（source='ib_monthly', rank=80）
       → realtime_bars 该行清除（或被下一 ts 的 in-progress 替换）
```

**对前端的影响**：
- `/api/history` 永远只返 `bars` 表（IB 验证过的）+ 当前 in-progress 一根（来自 `realtime_bars`）
- 用户刷新页面：除了最新一根（in-progress），其余全部 `ib_monthly` 或更高 rank
- 不再需要 `recover_realtime_bars` sweep（因为 `bars` 表压根不存 realtime 来源数据）

### 3.3 IB 不可用时的降级
若 60s 后 IB 仍不通：把 `realtime_bars` 里这一根 promote 到 `bars` 表，source=`realtime_completed`、rank=20。下次 IB 通时由 `bg_validate` 自动覆盖。该路径仅作为兜底，正常情况下走不到。

---

## 4. IB Fetch & Cache 改造

### 4.1 fetch 入口统一为 `(contract_token, timeframe, from_ts, to_ts)`

```python
async def fetch_range(
    contract_token: str,   # 'MONTH:202606' | 'CONT'
    timeframe: str, from_ts: int, to_ts: int,
    symbol: str,
) -> List[dict]:
    """单一职责：从 IB 拉一个 contract 的一段数据，并写入 ib_fetch_cache。
    不写 bars 表。"""
```

调用方负责决定要哪个 contract，fetcher 不再做 monthly→cont fallback "智能"决策。

### 4.2 调用方策略

- **chart on-demand（`/api/history`）**：根据用户选择的 series 类型，分解为一个或多个 contract 区间，分别调 `fetch_range`
- **bg_validate / fix_bars**：按 (sym, cm) 分组调用
- **realtime promote**：调 `fetch_range('MONTH:cm', tf, ts, ts)`

### 4.3 cache 命中策略
保留现有"按 expected_ts 集合算 missing sub-ranges"的逻辑，contract_token 进入 cache key。命中即 0 IB 请求 0 sleep（已实现，与 v3 兼容）。

### 4.4 `get_ib_bars_for_validation(sym, cm, tf, from_ts, to_ts)` —— 校验专用读
读 `ib_fetch_cache` 的 `MONTH:cm`（**不含 CONT**），用于 `data_validator._compare_bars`。彻底消除 ContFuture back-adjust 漂移污染校验结果。

---

## 5. 前端：symbol 选择 dropdown

### 5.1 新 API

```
GET /api/symbols
→ [
    {label: "MES (Continuous, ratio-adjusted)", token: "MES@CONT_RATIO"},
    {label: "MES (Continuous, no adjustment)",  token: "MES@CONT_FRONT"},
    {label: "MES Jun 2026 (MESM6)",              token: "MES@202606"},
    {label: "MES Sep 2026 (MESU6)",              token: "MES@202609"},
    ...
]
```

后端读 `bars` 表里实际有数据的 `(symbol, contract_month)`，加上每个 symbol 的两个 continuous 选项。

### 5.2 `/api/history` 接收 `symbol` 参数为 token

- `MES@202606` → 直接 `db.get_bars(symbol='MES', contract_month='202606', ...)`
- `MES@CONT_RATIO` → `continuous_view.assemble_continuous('MES', tf, from, to, method='cont_ratio')`
- `MES@CONT_FRONT` → 同上 `method='front'`

### 5.3 datafeed.js 的 symbol 解析

`resolveSymbol` 把 token 拆给 `/api/symbols/info?token=...`，返回 ticker 显示名、tick size、price scale 等。Chart 上方 symbol selector 显式列所有 token。

### 5.4 切换 symbol 时
TradingView chart 的 `setSymbol()` 会重新走 `getBars` —— 后端按 token 路由到正确数据源，无需任何前端额外逻辑。

---

## 6. 模块/文件改动地图

| 文件                       | 改动                                                         |
|----------------------------|--------------------------------------------------------------|
| `db.py`                    | schema 重建（`bars`/`realtime_bars`/`ib_fetch_cache`/`bar_revisions` 改 PK 与列）；`insert_bars` 加 rank 护栏 + 写 `bar_revisions` |
| `ib_data_fetcher.py`       | `fetch_range` 简化为单 contract；删除 ContFuture fallback 写 `bars` 路径 |
| `continuous_view.py`       | **新建** —— 实现 `assemble_continuous()` 三种 method        |
| `realtime_builder.py`      | `persist_completed_bar` 改为：upsert realtime_bars + 调度 IB pull；删除 `revalidate_realtime_bar` 调度 |
| `data_validator.py`        | `_compare_bars`/`fix_bars` 全部改为 per-(sym,cm) 单维比对；删除 ContFuture 跳过分支；删 `recover_realtime_bars`（不再需要） |
| `data_manager.py`          | 路由层加 token 解析；`/api/symbols`、`/api/history` 改造    |
| `server.py`                | `/api/history` 接 token；`/api/symbols` 新增；启动不再调 recover sweep |
| `static/datafeed.js`       | `resolveSymbol` 走新 token 模式                              |
| `static/index.html`        | symbol selector dropdown UI（或用 TradingView 自带 search） |
| `static/datavalid.html`    | issues view 加 contract_month 列、bar_revisions 查询面板    |
| `config.py`                | 新增 `SOURCE_RANK` dict、`REALTIME_PROMOTE_DELAY=60`         |

---

## 7. 实施步骤

### Phase 1 — schema 与底层（1–2 天）
1. 写 `db.py` 新 schema，启动时若表不存在则创建；**清空 `data/tradedev.db` 旧数据**
2. 实现 source rank 护栏 + `bar_revisions` 触发
3. 单元测试 `insert_bars` 各 rank 场景

### Phase 2 — fetch & realtime 重写（2 天）
4. `fetch_range` 单合约化；`ib_fetch_cache` 改 contract_token
5. 改 `realtime_builder` 走 IB-pull-after-close 流程
6. 删除 `recover_realtime_bars` 与 `revalidate_realtime_bar`

### Phase 3 — 连续合约视图（1 天）
7. `continuous_view.py` + 单元测试（构造已知 per-contract 数据，验证 ratio/diff 拼接结果）

### Phase 4 — API & 前端（1–2 天）
8. `/api/symbols`、`/api/history` token 化
9. datafeed.js + index.html dropdown
10. datavalid.html 显示 contract_month 与 revision history

### Phase 5 — 文档与验证（半天）
11. 更新 `README.md`、`doc/dataflow.md`、`doc/ib_data_fetcher.md`
12. 端到端：启动 → 拉 MES 202606 5min 一周 → 切到 MES@CONT_RATIO → 切到 MES@202609 → 各场景手工 check

---

## 8. 风险与权衡

| 风险                                                       | 缓解                                                            |
|------------------------------------------------------------|-----------------------------------------------------------------|
| 前端 chart 的 widget cache 在 token 切换时可能保留老数据   | 切 token 时调 `widget.activeChart().resetData()`               |
| `assemble_continuous` 每次请求都计算，长 range 1D 可能慢   | 加 in-process LRU cache（key = (symbol, tf, method, from, to)） |
| Per-contract bars 占用空间增加（rollover overlap 期间）    | 5min × 全合约一年 ≈ 几 MB，SQLite 完全可承受                    |
| `bar_revisions` 表无限增长                                 | 加定期 archive（>180 天搬到 `bar_revisions_archive`）           |

---

## 9. 与业界对标小结

| 维度          | priceaction v3 | TradingView | CQG/Barchart | NinjaTrader |
|---------------|----------------|-------------|--------------|-------------|
| Per-contract storage | ✅            | provider 决定 | ✅            | ✅           |
| Continuous as derived view | ✅       | ✅           | ✅            | ✅           |
| Multi adjustment policies | ✅ (front/ratio/diff) | ✅ | ✅            | ✅           |
| Source rank / immutability | ✅      | n/a          | ✅            | n/a          |
| Bar revision audit | ✅              | n/a          | partial       | n/a          |
| Symbol selector dropdown   | ✅       | ✅           | ✅            | ✅           |

---

*— end of v3 plan —*
