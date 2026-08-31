# `ib_data_fetcher.py` — 模块走读

> 路径：`priceaction/ib_data_fetcher.py`（~845 行）
> 角色：IB TWS/Gateway 数据接入层；封装历史数据拉取、实时 tick/bar 聚合、合约月切换，以及"内存缓存"层。

本文档回答四个问题：
1. 存储 / 缓存架构、DB 模型设计、流程设计
2. 业界类似交易后端的通用方案
3. 每一次请求如何"读缓存 + 拉新数据"组装完整结果
4. 以及后续改造留意的坑

---

## 1. 全局定位：三层存储 + 一个"行情状态机"

```
            ┌─────────────────────────────────────────────────────┐
            │           TradingView 前端 / WebSocket client        │
            └───────────────▲──────────────────▲──────────────────┘
                            │ /api/history     │ /ws/realtime (bar/analysis)
            ┌───────────────┴──────────────────┴──────────────────┐
            │                     server.py (FastAPI)              │
            │  ─────────────────────────────────────────────────── │
            │   L1  内存 in-memory cache       ← fetcher._symbol_bars
            │   L2  SQLite 持久层 (bars 表)     ← db.py
            │   L3  外部 Source-of-Truth        ← IB TWS (ib_insync)
            └───────────────▲──────────────────▲──────────────────┘
                            │ get_bars_for_symbol / fetch_range
                            │ add_new_bar_callback(on_new_bar)
            ┌───────────────┴──────────────────┴──────────────────┐
            │                 ib_data_fetcher.py                   │
            │  - IB 连接 / 合约 qualify / 月份合约缓存              │
            │  - 历史 reqHistoricalData                            │
            │  - 实时 reqMktData / reqRealTimeBars → 统一 tick 处理 │
            │  - _symbol_bars (多标的内存 OHLCV)                   │
            │  - _rt_current  (正在聚合中的当前 bar)               │
            └──────────────────────────────────────────────────────┘
```

`ib_data_fetcher.py` **自身只负责 L1（内存）和 L3（对外 IB 通讯）**。L2 持久化是由 `server.py` 在收到 fetcher 返回数据 / 新 bar 回调后，调用 `db.insert_bars(...)` 完成的。这是一个重要的分层——fetcher 本身无状态地对外吐数据，DB 只由 server 层决定何时写。

### 1.1 内存结构（L1 缓存）

`IBDataFetcher` 维护的核心状态：

| 字段 | 类型 | 语义 |
|---|---|---|
| `self.ib` | `ib_insync.IB` | 单例 IB 客户端 |
| `self._contract` | `ContFuture` | MES 的主连续合约（qualified） |
| `self._contract_cache` | `Dict[f"{symbol}_{YYYYMM}", Future]` | 月份合约 qualify 结果缓存（避免重复 reqContractDetails） |
| `self._ib_ready` | `bool` | 合约 qualify 完成后才置 True；标志 IB 是否可发请求 |
| `self._symbol_bars` | `Dict[symbol, Dict[tf_key, List[bar_dict]]]` | **L1 OHLCV 缓存**，按 symbol × timeframe 切 |
| `self._rt_current` | `Dict[f"{symbol}:{tf_key}", bar_dict]` | "正在聚合中"的当前 bar——未收盘就位于此处 |
| `self._tick_state` | `Dict[symbol, {prev_price, prev_size, last_broadcast}]` | 每标的 tick 增量 / 节流状态 |
| `self._realtime_subscriptions` | `Dict[key, Ticker\|RealTimeBars]` | IB 订阅句柄，用于取消 |
| `self._new_bar_callbacks` | `List[Callable]` | 订阅"新 bar 产生"事件的回调（server 注入 `on_new_bar`） |
| `self.bars["5min"]` | `List[bar_dict]` | **Legacy 兼容**：只是 `_symbol_bars["MES"]["5min"]` 的别名，由 `_sync_legacy_bars()` 维护 |

一个 bar 的标准结构：

```python
{"time": int_unix_utc_sec, "open": float, "high": float,
 "low": float, "close": float, "volume": float,
 "contract_month": "YYYYMM"   # 只在 fetch_range 返回时出现
}
```

### 1.2 DB 模型（L2，在 `db.py` 中定义，但 fetcher 直接/间接使用）

与 fetcher 最相关的 3 张表：

**`bars`** —— 真正的历史 OHLCV 持久层
```sql
CREATE TABLE bars (
    symbol         TEXT    NOT NULL,
    timeframe      TEXT    NOT NULL,     -- "5min" / "15min" / "60min" / "1D"
    ts             INTEGER NOT NULL,     -- UTC seconds, 对齐到 interval 边界
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    source         TEXT    NOT NULL DEFAULT 'unknown',
        -- 'ib_historical' / 'ib_validated' / 'realtime_completed' / 'unknown'
    contract_month TEXT    NOT NULL DEFAULT '',  -- 'YYYYMM'，标注来自哪份合约
    PRIMARY KEY (symbol, timeframe, ts)
);
CREATE INDEX idx_bars_sym_tf_ts ON bars (symbol, timeframe, ts);
```
设计要点：
- **(symbol, timeframe, ts) 三元主键** → `INSERT OR REPLACE` 天然幂等，重复拉取的数据自动去重覆盖。
- **source 列**：区分"来自历史"/"来自实时收盘"/"经校验修正"；数据校验器用它做"优先级覆盖"（`ib_validated` 可覆盖 `realtime_completed`）。
- **contract_month 列**：期货关键——同一时间戳可能属于不同月份合约，没有这列会在 rollover 附近产生"脏对比"（验证器会依赖）。
- SQLite WAL + 连接池 → 支持一个 writer + 多个 reader 并发。

**`realtime_bars`** —— 正在聚合的"当前 bar"持久化
```sql
CREATE TABLE realtime_bars (
    symbol, timeframe, ts, open, high, low, close, volume, updated_at,
    PRIMARY KEY (symbol, timeframe)
);
```
- **一个 (symbol, timeframe) 只存一行**：upsert 覆盖。
- 目的：崩溃恢复——重启后，`server.lifespan` 会把未收盘的 bar 恢复到 `_prev_completed_bar` 和 `_rt_current`，避免丢失"最后半根柱子"。

**`ib_fetch_cache`** —— IB 原始数据快照（验证器用）
```sql
CREATE TABLE ib_fetch_cache (
    symbol, timeframe, ts, open, high, low, close, volume,
    fetched_at INTEGER, contract_month TEXT,
    PRIMARY KEY (symbol, timeframe, ts)
);
```
- `fetch_range` 本身**不写**这张表；写入它的是 `data_validator.get_ib_bars_with_cache`。
- 目的：避免验证/修复循环中重复打 IB（IB 有 pacing 限制：~6 req / 10 s）。

### 1.3 流程图：三个主要流程

```
① 启动增量同步 (load_history)
   DB.get_latest_ts ──► since_ts
   IB.reqHistoricalData(duration = now - since_ts)
   ──► merge in-memory (保序 + 去重 by ts)
   ──► 写回 _symbol_bars["MES"]["5min"]
   (持久化由 server 层另外 db.insert_bars)

② 前端翻页到冷区 (fetch_range(tf, from, to, symbol))
   策略 1：按 timestamp 推算目标合约月 YYYYMM
            - 尝试 [target, next, prev] 三个月份
            - 第一个返回 ≥1 bar 的合约即采纳，并在每根 bar 上打 contract_month 标签
   策略 2：全失败 → 退化到 ContFuture 连续合约 + 按时间戳反推 contract_month

③ 实时 tick 聚合 (subscribe_mktdata_all → _on_tick_unified)
   reqMktData(ticker) ──► _on_tick_unified(ticker, symbol)
       ├─ 过滤非法价 / NaN
       ├─ 相对 prev_price/prev_size 算 vol_delta（去重累加）
       └─ _process_tick(symbol, "5min", 300, now_ts, price, ...)
              ├─ bar_ts = (ts // 300) * 300
              ├─ 若 bar_ts > cur.time：
              │     _append_bar_multi(cur)   # 上一根入内存列表
              │     _dispatch_multi(cur)     # 触发 on_new_bar 回调 (server 侧持久化 + WS 广播)
              │     开新 bar 以 (tick_open..close, vol_delta)
              └─ 否则：更新 cur.high/low/close/volume（增量）
       └─ 节流广播：_dispatch_multi 每 250 ms 最多一次（不论几百个 tick）
```

---

## 2. 业界交易后端"行情存储与派发"通用方案

把这里的做法和业界常见方案做个对照，方便你看本仓库选型的合理性：

| 能力 | 业界主流方案 | 本项目对应做法 |
|---|---|---|
| 原始 tick 存储 | Kafka / Chronicle Queue / kdb+ / ClickHouse | **不存 raw tick**，直接在 fetcher 侧聚合成 5s/5min bar |
| OHLCV 时序持久化 | InfluxDB / TimescaleDB / ClickHouse / kdb+ / Parquet | **SQLite (`bars` 表)** + PK=(symbol, tf, ts) |
| 热数据缓存 | Redis（sorted set by ts）/ 进程内 LRU | `_symbol_bars` 进程内 dict（MAX_BARS_IN_MEMORY 限制） |
| 实时当前 bar | Redis / 内存 + 周期性 snapshot 到持久层 | `_rt_current` + `realtime_bars` 表（每 tick upsert） |
| 数据源→前端的推送 | WebSocket + pub/sub（Redis/Kafka） | FastAPI WebSocket + `_new_bar_callbacks` fan-out |
| 回补/对账 | 定时 job 扫 S3/历史库 vs 实时落盘 | `data_validator.py` + `ib_fetch_cache` |
| 合约切换（期货） | Roll table + adjusted/continuous series | `_contract_month_for_ts` + ContFuture fallback |
| Rate limit | Token bucket + 队列调度 | 手动 `await asyncio.sleep(2)` + `_ib_fetch_cooldown` 冷却字典 |
| 崩溃恢复 | WAL / 重放 Kafka offset | `realtime_bars` 表重启时回填 `_rt_current` |

**选型评价：**
- SQLite 适合 dev / 单机 / 中频策略；如果要扩展到多标的 × 多客户端 × 秒级数据，通常会迁移到 TimescaleDB 或 ClickHouse（按时间分区 + 压缩列存）。
- "只存 bar、不存 tick" 是策略型系统常见选择（节省 10×+ 存储）；做 TCA/执行分析的系统通常会保留 tick。
- 把"L1 内存 / L2 持久 / L3 外部权威源" 分清楚，是本模块最重要的设计思想——业界也是同一套范式。

---

## 3. 一次请求的完整"组装"流程

前端 TradingView 调 `GET /api/history?symbol=MES&resolution=5&from=...&to=...&countback=N`。`server.get_history` 与 `ib_data_fetcher` 协同工作，分 5 步：

### Step 1 — 读 L2 (DB)
```python
bars = db.get_bars(sym, key, from_ts, to_ts)
earliest_db = db.get_earliest_ts(sym, key)
latest_db   = db.get_latest_ts(sym, key)
```
拿到当前 DB 在请求区间内已有的柱子，以及 DB 覆盖范围边界。

### Step 2 — 诊断"缺口"，决定要不要打 IB

区分 4 种情形（每种独立判断，可叠加到 `fetch_ranges`）：

| 情形 | 条件 | 处理 |
|---|---|---|
| Case 1 整区无数据 | `earliest_db is None` | 拉 `[from, min(to, now)]` |
| Case 2 左缺口 | `from_ts < earliest_db` 且不在 cooldown | 拉 `[from_ts, earliest_db]` |
| Case 3 右缺口 | `latest_db < to` 且 gap > 2 个 interval 且不在 cooldown | 拉 `[latest_db, capped_to]`，受 `max_gap` 限制 |
| Case 4 中间空洞 | 区间在 DB 覆盖内但 `bars == []` | 拉 `[from, min(to, now)]`，加 5 分钟 cooldown |

**Cooldown 设计**（`_ib_fetch_cooldown` 字典）：IB 不一定每次都有数据（如市场休市），无脑重试会触发 pacing 限制。若 IB 返回 0 bar → 冷却 `_IB_COOLDOWN_NO_DATA = 300s`，异常 → 60s，成功则立即 pop。

### Step 3 — 逐缺口调 `fetcher.fetch_range`

```python
fetched = await fetcher.fetch_range(key, f_from, f_to, symbol=sym)
if fetched:
    db.insert_bars(sym, key, fetched, source="ib_historical")
```

`fetch_range` 内部（期货特殊性）：
1. 按 `from_ts / to_ts` 对齐到 interval 网格；
2. `_contract_month_for_ts(end_ts)` 推算目标合约月（月份 day ≤ 10 用当前，否则下一月）；
3. 尝试 `[target, next, prev]` 三个月份合约，依次 `reqHistoricalDataAsync(contract, endDateTime, durationStr, barSizeSetting="5 mins"|...)`，第一个返回非空的即采纳；
4. 全失败 → `ContFuture` 连续合约兜底（注意 IB 不允许 ContFuture 带 `endDateTime`，只能取最近 N 根再过滤）。
5. 每根 bar 打上 `contract_month` 标签后返回。

### Step 4 — 内部空洞填补（calendar-aware）

拿完缺口数据后，用 `trading_calendar.find_gaps()` 对现有 bars 序列再扫一遍，区分：
- `weekend` / `holiday` / `maintenance`（交易所规则性休市）→ 不填
- `data_gap` → 逐个按 7 天切片再次 `fetcher.fetch_range` + `db.insert_bars`

这一步让"真实缺失"和"正常闭市"不再混淆——是 futures 场景下避免无限重试的关键。

### Step 5 — 拼接"正在聚合的实时 bar"

```python
rt_bar = _prev_completed_bar.get((sym, key))   # server 侧维护
if rt_bar and from_ts <= rt_bar["time"] <= to_ts:
    bars[-1] = rt_bar or append
```
这样前端拿到的最后一根总是"最新截止到此刻的 in-progress bar"，不会在整点卡死等 IB 下发。

### 返回给前端的载体
TradingView UDF 格式，把 list[dict] 转成列模式：
```json
{"s":"ok","t":[...],"o":[...],"h":[...],"l":[...],"c":[...],"v":[...]}
```

### 图示：一次 /history 请求的数据组装
```
Request (symbol, tf, from, to)
        │
        ▼
   db.get_bars  ──► bars_existing
        │
        ├── earliest_db None?   ─► fetch [from, now]
        ├── from < earliest_db? ─► fetch [from, earliest_db]     (left gap)
        ├── to   > latest_db?   ─► fetch [latest_db, min(to,now)] (right gap)
        └── 中间空洞?            ─► fetch [from, to]              (middle hole)
              │  每个缺口：
              │  fetcher.fetch_range(tf, f, t, symbol)
              │     ├─ try target_month Future
              │     ├─ try next_month / prev_month
              │     └─ fallback ContFuture
              ▼
   db.insert_bars(source='ib_historical')
        │
        ▼
   bars = db.get_bars(...)  (重新查询，包含刚写入的)
        │
        ▼
   calendar.find_gaps + 逐 7d chunk 再补一次   (内部空洞)
        │
        ▼
   strip 无法修补的 gap 之前的数据
        │
        ▼
   append _prev_completed_bar[(sym,tf)]  (in-progress 实时 bar)
        │
        ▼
   转 UDF 列格式返回
```

---

## 4. 容易踩坑 / 后续改造注意

1. **Legacy `self.bars["5min"]` 别名** — 只是 `_symbol_bars["MES"]["5min"]` 的引用；不要再加"只写 self.bars 不写 _symbol_bars"的代码，否则会和 `_sync_legacy_bars()` 打架。新代码全部走 `get_bars_for_symbol` / `_append_bar_multi`。

2. **实时 bar 不进 `bars` 表** — `on_new_bar` 把已收盘的前一根以 `source="realtime_completed"` 写入 `bars`，in-progress 的只写 `realtime_bars`；验证器允许 `ib_historical` / `ib_validated` 覆盖 `realtime_completed`，所以 IB 迟到的历史数据也能修正。

3. **`qualifyContractsAsync` 会卡死** — 代码用 `asyncio.wait_for(..., 30s)` 超时；如果你在 offline / 缺订阅的环境测试，`_ib_ready` 永远 False，server 层会绕过 IB 分支只读 DB。

4. **`ib_duration` 上限 30 天** — 避免一次拉太大区间触发 pacing。若前端一次性滚动到 1 年前，gap 会被切到 30 天 → `fetch_range` 只补 30 天窗口；内部空洞填补那一步才是真正的长距离补齐。

5. **合约月 rollover 的 day 10 假设** — `_contract_month_for_ts` 里写死了"月 10 号之前用当月合约，之后用下月"。这是 MES 常见近似，但不同标的真实 roll 日期不同；精确做法应查 IB contract expiration。

6. **Tick 节流 250ms** — `_TICK_BROADCAST_INTERVAL = 0.25` 是 WS 广播节流，不是聚合节流；bar 的 OHLCV 仍是每个 tick 实时更新，只是"告诉前端"最多 4 Hz。

7. **连接池 vs asyncio** — `db.py` 用线程安全连接池，fetcher/server 都在同一 event loop 写 SQLite；WAL 允许 1 writer + N reader，但高并发 `insert_bars` 会串行。长期应考虑把落盘移到后台线程队列或升级到 TimescaleDB。

---

## 附：关键函数速查

| 函数 | 作用 |
|---|---|
| `IBDataFetcher.connect()` | 连 TWS，3 次重试 |
| `_get_contract()` | qualify MES 主合约（单例缓存） |
| `_get_future_for_month(yyyymm, sym)` | qualify 指定月份的 `Future`（多标的通用，缓存） |
| `load_history(since_5min)` | 启动时增量拉历史 + 合并去重 |
| `fetch_range(tf_key, from, to, symbol)` | 按区间补数据；月份合约 + ContFuture 兜底 |
| `subscribe_mktdata_all()` | 给 MES + EXTRA_SYMBOLS 全部订阅 tick |
| `_on_tick_unified(ticker, symbol)` | **统一 tick 处理**——所有标的走同一路径 |
| `_process_tick(...)` | 核心 bar 聚合：对齐时间、滚动 OHLC、dispatch 完结 bar |
| `_append_bar_multi` / `_dispatch_multi` | 内存写入 + 回调 fan-out |
| `get_bars_for_symbol(sym, tf, from, to)` | 读 L1 内存缓存（server 在 DB 查空且 sym=MES 时才会回退到这里） |

