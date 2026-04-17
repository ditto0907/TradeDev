# `data_validator.py` — 模块走读

> 路径：`priceaction/data_validator.py`（~801 行）
> 角色：数据对账层。以 IB 历史数据为 **Source of Truth**，反向校验 SQLite `bars` 表，发现价格/成交量差异、非法 OHLCV、日历缺口；可选择修复（覆盖式写回）。

---

## 1. 定位：三类检查 × 两种模式

```
            ┌─────────────────────────────────────────────────┐
            │                  触发入口                         │
            │  · API  /api/data/validate   (单区间)            │
            │  · API  /api/data/fix        (单区间 + 修复)      │
            │  · API  /api/data/validate_all                  │
            │  · API  /api/data/bg_validate (后台扫全库)        │
            └──────────────────┬──────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                  data_validator.py                            │
│                                                               │
│  validate_bars(sym, tf, from, to, contract_month?)            │
│     ├─ DB:   db.get_bars(...)                                 │
│     ├─ IB:   get_ib_bars_with_cache(...)   ← 走 ib_fetch_cache │
│     ├─ _compare_bars  → mismatches / db_only / ib_only       │
│     ├─ OHLCV integrity (calendar.validate_bar)               │
│     └─ Completeness   (calendar.find_missing_bars)           │
│                                                               │
│  fix_bars(...)      = validate + 把差异写回 `bars` 表         │
│  validate_all(...)  = 枚举 (sym, tf, contract_month) 逐块跑   │
│  background_validate(...) = 常驻后台，跳过已校验区间           │
└──────────────────────────────────────────────────────────────┘
                               │
                               ▼
           ┌─────────────────────────────────────┐
           │   db.py (SQLite)                     │
           │    · bars               (被校验对象)  │
           │    · ib_fetch_cache     (IB 原始快照) │
           │    · validated_ranges   (已校验记录)  │
           └─────────────────────────────────────┘
```

### 检查维度（`validate_bars` 在每次调用里都做三件事）
1. **IB 对比**（`_compare_bars`）：逐 ts 比 DB.bar vs IB.bar
   - OHLC：差异 > `_PRICE_TOL = 0.5` 视为 mismatch
   - Volume：差异 > `_VOLUME_TOL = 1.0` 视为 mismatch
   - DB 有 / IB 无 → `db_only`；IB 有 / DB 无 → `ib_only`
2. **OHLCV 完整性**（`trading_calendar.validate_bar`）：`high < low`、价格非正、OHLC 关系违规等——这类是 **DB 内部自检**，不需要 IB。
3. **日历完整性**（`trading_calendar.find_missing_bars`）：用合约日历列出应有但缺失的 ts（跳过休市/节假日/维护窗）。

### 两种模式
- **Validate**：只读，返回 diff 报告；不改 DB。
- **Fix**：把 `mismatches` 用 IB 数据覆盖、把 `ib_only` 插入，`source="ib_validated"`（优先级高于 `realtime_completed` / `unknown`，低于……没了，也就是最高权威）。

---

## 2. 关键数据结构

### 2.1 与 `ib_data_fetcher` 共享的辅助

直接 import 复用，避免重复逻辑：
```python
from ib_data_fetcher import (
    RESOLUTION_MAP, _bar_to_dict, _key_to_ib,
    _contract_month_for_ts, _next_contract_month, _prev_contract_month,
    ib_duration,
)
```
合约月切换、bar 归一化、duration 字符串生成——全都复用 fetcher 的实现。

### 2.2 模块内缓存

```python
_qualified_cache:  Dict[str, Contract]  # "SYM_YYYYMM" → qualified Future
_failed_contracts: set                   # 记录 qualify 失败的，下次直接跳过
```
作用：`validate_all` 会把上百个 (sym, tf, contract_month) 组合跑一遍，避免每次再打一次 `qualifyContractsAsync`。

### 2.3 容差
```python
_PRICE_TOL  = 0.5   # 半个 tick，避开浮点/舍入抖动
_VOLUME_TOL = 1.0   # 成交量差异 1 手以下忽略
```

---

## 3. `get_ib_bars_with_cache` —— 本模块的"缓存发动机"

> 这是全文件最重要的一个函数——它决定一次 validate 是否真的打 IB。

```
请求 IB bars [from_ts, to_ts] for (symbol, tf)

 1. 时间对齐到 interval 网格
       aligned_from = (from // itv) * itv
       aligned_to   = ceil(to / itv) * itv

 2. cached = set( db.get_ib_cache_coverage(...) )   ← 已缓存的 ts
    expected = [aligned_from, aligned_from+itv, ..., aligned_to]

 3. 算出"缺失的连续子区间" missing_ranges

 4. merged = missing_ranges
             + 每段左右各 ±1 interval 作为 overlap buffer
             + 相邻段合并

 5. for sub_from, sub_to in merged:
        fetched = fetcher.fetch_range(tf, sub_from, sub_to, sym)
        db.insert_ib_cache_bars(sym, tf, fetched)   # 写 ib_fetch_cache
        sleep(2)                                    # IB pacing

 6. return db.get_ib_cache_bars(sym, tf, aligned_from, aligned_to)
```

### 为什么要有 `ib_fetch_cache` 表？
`validate` 和 `fix` 往往是同一区间被调用两次（先看报告、再点"修复"按钮），若没有这张缓存表，每次都重新打 IB，既慢又容易触发 pacing limit。缓存命中后 `fix_bars` 可以零 IB 请求直接下发。它也是 `validate_all` 跨 contract_month 切片扫描时的关键——每个 chunk 之间可能有重叠，缓存让重叠部分不用再拉。

### 注意
- `ib_fetch_cache` 与业务表 `bars` 是**完全独立**的——验证器永远不会直接读/写 `bars` 来满足"IB 源数据"需求；它读 IB → 先落 `ib_fetch_cache` → 比对。这保证了"源"与"业务数据"解耦。
- `fetcher` 可选传入：如果 server 已经有一个活跃的 `IBDataFetcher`，复用它的连接，避免重复开 IB client（`clientId = config.IB_CLIENT_ID + 80` 是验证器自起连接的偏移）。

---

## 4. 四个对外 API

### 4.1 `validate_bars(symbol, tf, from, to, *, contract_month?, skip_validated?)`

单区间校验——`/api/data/validate` 直接调用。

流程：
```
if skip_validated and db.is_range_validated(...):
    → 返回空报告 (already_validated=True)

db_bars = db.get_bars(..., contract_month=cm)
ib_bars = await get_ib_bars_with_cache(...)
if cm is not None:
    ib_bars = [b for b in ib_bars if b.contract_month == cm]

mismatches, db_only, ib_only = _compare_bars(db_bars, ib_bars)
ohlcv_violations   = calendar.validate_bar(each)
calendar_missing   = calendar.find_missing_bars(...)[:100]

return {mismatch_count, db_only_count, ib_only_count,
        ohlcv_violation_count, calendar_missing_count, ...}
```

**`contract_month` 参数的重要性**：期货在合约换月附近，同一个时间戳可能对应两份不同合约的 bar（前合约最后一日 + 次合约启动日），如果不按合约过滤会把"正常的换月差异"误判成 mismatch。因此 API / `validate_all` 会逐合约月独立跑。

### 4.2 `fix_bars(symbol, tf, from, to, *, timestamps?, contract_month?)`

校验 + 覆盖写回：
```
mismatches, db_only, ib_only = 同上
# 可选按 timestamps 过滤 (UI 勾选某几行修复)
fixed = [ib_bar for m in mismatches] + [ib_bar for b in ib_only]
db.insert_bars(sym, tf, fixed, source="ib_validated")   # 最高优先级
```

修复后的数据 `source="ib_validated"`——`db.insert_bars` 有冲突处理：同 (sym, tf, ts) 下，高优先级 source 会覆盖低的；但低优先级（例如后台实时回灌）不会反向覆盖已验证数据。

### 4.3 `validate_all(*, fix=False, chunk_seconds=86400)`

扫全库：
```
for (sym, tf) in SELECT DISTINCT symbol, timeframe FROM bars:
    effective_earliest = max(earliest, now - 365d)   # 跳过一年外（IB 可能不再提供过期合约）
    chunk = 30*86400 if tf=="1D" else chunk_seconds  # 默认每天一块
    contract_months = db.get_distinct_contract_months(sym, tf) or [None]

    for cm in contract_months:
        chunk_start = effective_earliest
        while chunk_start <= latest:
            chunk_end = chunk_start + chunk
            validate_bars(sym, tf, chunk_start, chunk_end, contract_month=cm)
            # 或 fix_bars(...)
            chunk_start = chunk_end + interval
            await asyncio.sleep(2)   # IB pacing
```

设计要点：
- **每个合约月独立一轮**：避免换月重叠期混合比较。
- **1D 用 30 天块**：减少请求数；5min 用 1 天块。
- **1 年截断**：IB 对过期合约的历史数据不稳定，不如直接跳过。
- **不写 `validated_ranges`**：这个函数的结果是给用户看的报告，后台巡检那一路才记录"已校验"。

### 4.4 `background_validate(*, fetcher?)`

常驻后台任务（`server.lifespan` 在 IB init 完成 + 30s stabilize 之后启动）。核心增量机制：`validated_ranges` 表。

```sql
CREATE TABLE validated_ranges (
    id, symbol, timeframe, from_ts, to_ts,
    checked_at TEXT, mismatches INTEGER, fixed INTEGER
);
```

流程：
```
for (sym, tf) in all pairs:
    unchecked = db.get_unchecked_ranges(sym, tf, earliest, latest)
       # 返回 "已校验区间" 的补集
    for uc in unchecked:
        chunk_end = uc.to
        while chunk_end >= uc.from:     # 从新到旧
            chunk_start = chunk_end - chunk
            result = await validate_bars(...)
            db.insert_validated_range(sym, tf, chunk_start, chunk_end,
                                       mismatches=result.mismatch_count)
            chunk_end = chunk_start - interval
            await asyncio.sleep(2)
```

特点：
- **可恢复**：每个 chunk 校验后立刻写 `validated_ranges`，下次启动跳过；所以是 at-least-once 增量推进。
- **从最近到最旧**：最近的数据最可能被实时流污染，优先保障；远端历史不容易出问题，放后面。
- **只 validate 不 fix**：后台默默扫，发现异常走日志，不动用户数据；是否修复由人在 UI 决定。

---

## 5. 请求路径：一次"点修复"发生了什么？

用户在前端校验页面点 `修复 [MES/5min, 2024-03-10 09:30 – 16:00]`：

```
POST /api/data/fix
   body={symbol:"MES", timeframe:"5min",
         from_ts, to_ts,
         contract_month:"202503",
         timestamps:[...可选...]}

server.py → data_validator.fix_bars(...)

  ┌─ db.get_bars(MES, 5min, from, to, contract_month="202503")
  │    → db_bars  (可能有价格/量偏差)
  │
  ├─ get_ib_bars_with_cache(...)
  │    ├─ ib_fetch_cache 命中检查 → 列出缺失子区间
  │    ├─ 缺失部分 → fetcher.fetch_range(...) → db.insert_ib_cache_bars(...)
  │    └─ return 完整 ib_bars (过滤 contract_month="202503")
  │
  ├─ _compare_bars(db_bars, ib_bars) → mismatches / ib_only
  │
  ├─ if timestamps 参数：只留用户勾选的几个
  │
  ├─ fixed = mismatches.map(ib) + ib_only.map(ib)
  │    (全部打上 source="ib_validated")
  │
  └─ db.insert_bars(MES, 5min, fixed, source="ib_validated")
         → REPLACE INTO bars (幂等，最高权威)

Response: {db_count, ib_count, mismatch_count, ib_only_inserted, fixed_count}
```

之后前端再刷一次图，`/api/history` 读 `bars` 表拿到的就是已修正版本。

---

## 6. 踩坑 / 设计 note

1. **`contract_month` 过滤是必须的** — 不加的话，换月当天会把两个合约的 bar 塞到同一时间戳做差，得到一堆假阳性 mismatch。前端 UI 也保留了这个选项。

2. **容差 vs 实际 tick size** — `_PRICE_TOL=0.5` 对 MES（tick=0.25）= 2 个 tick，对 MGC（tick=0.1）= 5 个 tick，对 NK225MC（tick=5）= 0.1 个 tick。如果要加新品种需要考虑相对容差而不是绝对值。

3. **`get_ib_bars_with_cache` 和 fetcher `fetch_range` 的关系** — 两者都会打 IB，但前者先查 `ib_fetch_cache` 表做去重；后者是"拉了就走，不缓存原始快照"。验证器统一走前者；`/api/history` 直接走后者。这是有意为之：业务路径要最新数据，校验路径要可重放快照。

4. **复用 fetcher 的 IB 连接** — 务必在能传 `fetcher=` 时传；`_connect_ib` 自起连接用的是 `clientId + 80`，容易和主连接抢资源造成事件循环死锁。

5. **`validated_ranges` 没 merge 相邻区间** — 长期跑会积攒很多小区间行，不过 `get_unchecked_ranges` 会聚合计算"补集"，功能不受影响；如果追求表面积干净可以加一个周期性 COALESCE 任务。

6. **OHLCV 完整性 fallback** — 当 `trading_calendar` 初始化失败时（加了新 symbol 但未配 calendar），代码走 except 分支做最基本的 `high<low` / 非正价检测；不会静默丢数据但检查更粗。

---

## 附：关键函数速查

| 函数 | 作用 |
|---|---|
| `_connect_ib(ib=None)` | 复用或新建 IB 连接；返回 `(ib, should_disconnect)` |
| `_fetch_ib_bars(...)` | 裸 IB 历史拉取（只在没有 fetcher 时兜底） |
| `get_ib_bars_with_cache(...)` | **核心缓存读取**；合并已缓存 + 补缺 + 落 `ib_fetch_cache` |
| `_compare_bars(db, ib)` | 返回 `(mismatches, db_only, ib_only)`，按容差比 OHLCV |
| `validate_bars(...)` | 单区间校验（含 OHLCV 完整性 + 日历完整性） |
| `fix_bars(...)` | validate + 按选择写回 `source=ib_validated` |
| `validate_all(fix?)` | 扫全库；按 (sym, tf, contract_month) × chunk 切片 |
| `background_validate(...)` | 常驻后台增量扫；按 `validated_ranges` 跳过已查区间 |

