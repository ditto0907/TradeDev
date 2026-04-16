# K线数据逻辑重构方案

## 一、问题现状分析

### 1.1 反复修复的K线问题统计

通过 review 全部 123 次提交记录，其中 **55+ 次 (45%) 与K线数据/图表显示相关**，反复出现以下问题：

| 问题类别 | 出现次数 | 典型表现 |
|---------|---------|---------|
| **数据缺口 (Gap)** | 15+ | 启动后缺口、跨品种缺口、内部缺口、滚动加载缺口 |
| **数据获取/翻页** | 10+ | 合约解析错误、翻页加载失败、历史数据拉取超时 |
| **实时更新Bug** | 8+ | 事件循环崩溃、价格线错乱、Bar组装丢数据 |
| **数据质量** | 8+ | OHLCV不匹配、Source追踪、缺口分类错误 |

### 1.2 根因分析 — 与行业标准方案的差距

经过对比 TradingView 标准 UDF Datafeed 实现方案和专业交易终端的数据架构，我们的实现存在以下核心差距：

#### 差距1：数据源职责不清 — Historical vs Realtime 混合写入

**标准方案：**
- DB 是唯一的 Historical 数据持久化层，只接受经过校验的IB历史数据
- Realtime bar 是纯内存态，仅用于当前K线的实时展示
- 两者有清晰的边界：当 realtime bar 完成（下一个 bar 的第一个 tick 到达），completed bar 才写入 DB

**我们的问题：**
- `realtime_completed` 和 `ib_historical` 共存于同一张 bars 表，source 字段区分
- `on_new_bar()` 在每个 tick 都 upsert `realtime_bars` 表（crashRecovery 用途），但逻辑复杂、写入频繁
- 完成的 realtime bar 直接写入 bars 表，可能与后续 IB historical 数据冲突
- **结果：** 多次出现 DB 数据与 IB 源数据不一致的 bug

#### 差距2：API 层缺少数据完整性保障

**标准方案：**
- `/api/history` 应该是一个简单的 DB 查询 + 格式化返回
- 数据补全是后台任务，不应在 API 请求路径上同步执行
- TradingView 通过 `noData: true` + `nextTime` 实现分页加载，前端控制加载节奏

**我们的问题：**
- `/api/history` 内部嵌套了 5 步 gap 检测 + IB 实时拉取逻辑
- API 请求可能因 IB 超时而 hang（30秒+），影响图表加载体验
- cooldown map 无限增长，内存泄漏风险
- gap 检测逻辑在多个地方重复实现（startup、API request、internal gap fill）
- **结果：** 请求延迟不稳定，逻辑分散难维护，频繁出现翻页加载问题

#### 差距3：时间戳对齐和 Session 处理分散

**标准方案：**
- 所有时间戳对齐通过统一工具函数 `align_to_interval(ts, interval)` 处理
- Session（交易时段）信息配置化，gap 检测/数据过滤统一使用 session calendar
- 假日、周末、维护窗口的判断集中在 trading calendar 模块

**我们的问题：**
- `bar_ts = (ts // interval) * interval` 散落在 tick handler、API endpoint、gap detector 等多处
- Session 判断逻辑在 `db.find_gaps()`、`server.py` 的 gap 检测、`market_holidays.py` 之间分散
- 阈值（2×interval、4×interval、56小时、72小时等）硬编码在代码中
- **结果：** DRY 违反，不同位置的对齐/判断逻辑不一致导致 bug

#### 差距4：多品种状态管理不完善

**标准方案：**
- 所有状态必须以 `(symbol, timeframe)` 为 key
- 品种配置完全驱动行为，无硬编码的品种特定逻辑

**我们的问题：**
- `_prev_completed_bar` 曾经只以 `bar_size_key` 为 key，导致 MES tick 覆盖其他品种的 chart
- 启动时 MES 同步阻塞，其他品种异步 — 逻辑不统一
- **结果：** 新增品种时需要逐个排查状态 key 是否完整

#### 差距5：缺少系统化的数据生命周期管理

**标准方案：**
- 数据有明确的生命周期：**Fetch → Validate → Store → Serve → Archive**
- 每个阶段有独立的校验规则和错误处理
- 提供完善的数据运维工具（探查、修复、批量校验）

**我们的问题：**
- 数据写入后没有统一的校验机制（IB fetch 后直接 INSERT OR REPLACE）
- `data_validator.py` 是后期追加的，与主流程耦合不紧密
- 缺少按条件探查数据的 web 工具
- **结果：** 数据问题需要手动排查和修复，效率低

---

## 二、参考标准：TradingView Web 交易终端数据架构

### 2.1 标准数据流架构

```
┌─────────────────────────────────────────────────────────────┐
│                    Data Source Layer                         │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │ IB Historical│  │ IB Realtime  │  │ Other Data Sources│  │
│  │ API          │  │ Tick Stream  │  │ (future)          │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────────────┘   │
└─────────┼─────────────────┼─────────────────────────────────┘
          │                 │
          ▼                 ▼
┌─────────────────────────────────────────────────────────────┐
│                  Data Ingestion Layer                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │              Bar Manager (BarManager)                   │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐  │  │
│  │  │ Historical   │  │ Realtime Bar │  │ Data         │  │  │
│  │  │ Fetcher      │  │ Assembler    │  │ Validator    │  │  │
│  │  │ (IB→DB)      │  │ (Tick→OHLCV) │  │ (IB vs DB)  │  │  │
│  │  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  │  │
│  │         │                 │                  │          │  │
│  │         ▼                 ▼                  ▼          │  │
│  │  ┌────────────────────────────────────────────────┐     │  │
│  │  │         Write-through Validation               │     │  │
│  │  │  (OHLC range check, timestamp alignment,       │     │  │
│  │  │   duplicate detection, session awareness)      │     │  │
│  │  └──────────────────┬─────────────────────────────┘     │  │
│  └─────────────────────┼──────────────────────────────────┘  │
└────────────────────────┼────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                  Persistence Layer                           │
│  ┌────────────────────────────────────────────────────────┐  │
│  │              SQLite Database                            │  │
│  │  ┌──────────┐  ┌──────────────┐  ┌─────────────────┐  │  │
│  │  │ bars     │  │ ib_fetch_    │  │ bar_metadata    │  │  │
│  │  │ (OHLCV)  │  │ cache        │  │ (coverage/gaps) │  │  │
│  │  └──────────┘  └──────────────┘  └─────────────────┘  │  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                   API Serving Layer                          │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  GET /api/history  →  DB Query + Format (Pure Read)    │  │
│  │  WS  /ws/realtime  →  Broadcast In-Memory RT Bar       │  │
│  │  GET /api/data-ops →  Data Operations (探查/修复)       │  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                 Frontend Display Layer                       │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  TradingView Widget ← datafeed.js (getBars/subscribeBars)│
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 关键设计原则

1. **DB 是唯一数据源 (Single Source of Truth)**
   - `/api/history` 只做 DB 查询 + 格式化返回，不执行任何 IB 数据拉取
   - 数据补全是独立的后台任务，定时执行，不阻塞 API 请求

2. **写入前校验 (Write-through Validation)**
   - 所有写入 DB 的数据必须通过校验管道：时间戳对齐、OHLC 合理性、重复检测
   - Realtime completed bar 写入时与 DB 已有数据做一致性检查

3. **关注点分离 (Separation of Concerns)**
   - `BarManager`：统一管理所有 bar 数据的获取、组装、校验、写入
   - `TradingCalendar`：统一管理交易时段、假日、维护窗口
   - `DataFeedAPI`：纯数据查询和格式化，无副作用
   - `DataOpsAPI`：数据运维操作（探查、修复、校验）

4. **多品种通用 (Multi-Instrument Generality)**
   - 所有逻辑以 `(symbol, timeframe)` 为 key
   - 品种特性通过 `config.INSTRUMENTS` 配置驱动
   - 无硬编码的品种特定逻辑

---

## 三、重构方案

### 3.1 新模块架构

```
priceaction/
├── bar_manager.py          # 🆕 统一 Bar 数据管理器（核心模块）
├── trading_calendar.py     # 🆕 交易日历（session、假日、维护窗口）
├── data_ops.py             # 🆕 数据运维工具（探查、修复、批量校验）
├── db.py                   # 🔄 重构：纯数据存取层，移除业务逻辑
├── ib_data_fetcher.py      # 🔄 重构：纯 IB API 封装，移除 bar 组装
├── data_validator.py       # 🔄 重构：集成到 bar_manager 的校验管道
├── server.py               # 🔄 重构：API 纯读取，移除 gap 检测
├── config.py               # 🔄 增强：集中阈值配置
├── market_holidays.py      # → 合并到 trading_calendar.py
├── static/
│   ├── datafeed.js          # 🔄 重构：简化 getBars，移除 hack
│   ├── app.js               # 基本不变
│   └── index.html           # 增加 Data Ops 面板
└── ...
```

### 3.2 核心模块设计

#### 3.2.1 `bar_manager.py` — 统一 Bar 数据管理器

**职责：** 作为所有 bar 数据操作的单一入口，管理数据的获取、组装、校验、存储和查询。

```python
class BarManager:
    """
    Unified bar data manager.
    
    Responsibilities:
    - Historical data fetching (from IB → validate → store to DB)
    - Realtime bar assembly (ticks → OHLCV → store completed bars)
    - Data serving (DB query → format for frontend)
    - Background data sync (gap detection → fill)
    - Data validation pipeline (write-through checks)
    """
    
    def __init__(self, db, fetcher, calendar, config):
        self._db = db
        self._fetcher = fetcher      # IB API wrapper
        self._calendar = calendar    # Trading calendar
        self._config = config
        
        # In-memory state per (symbol, timeframe)
        self._rt_current = {}        # Current forming bar
        self._rt_completed = {}      # Last completed bar (for serving)
        self._memory_cache = {}      # Recent bars cache for fast API reads
        
        # Background sync state
        self._sync_tasks = {}        # Active sync tasks per (symbol, timeframe)
        self._cooldowns = {}         # Fetch cooldowns with auto-expiry
    
    # ── Data Serving (Pure Read) ───────────────────────────
    
    async def get_bars(self, symbol, timeframe, from_ts, to_ts, count_back=None):
        """
        Get bars for API response. Pure DB read + memory RT bar.
        NO IB fetching or gap filling — that's done by background sync.
        """
        bars = self._db.get_bars(symbol, timeframe, from_ts, to_ts)
        
        # Append current in-memory RT bar if within range
        rt_key = (symbol, timeframe)
        if rt_key in self._rt_current:
            rt_bar = self._rt_current[rt_key]
            if from_ts <= rt_bar['time'] <= to_ts:
                # Replace or append RT bar
                bars = self._merge_rt_bar(bars, rt_bar)
        
        # If no data, provide nextTime for TradingView pagination
        if not bars:
            next_time = self._db.get_nearest_bar_before(symbol, timeframe, from_ts)
            return {'bars': [], 'no_data': True, 'next_time': next_time}
        
        return {'bars': bars, 'no_data': False}
    
    # ── Realtime Bar Assembly ──────────────────────────────
    
    def on_tick(self, symbol, price, size, wall_ts):
        """
        Process a new tick. Assemble into OHLCV bars.
        When a bar completes, validate and write to DB.
        """
        for timeframe, interval in self._get_active_timeframes():
            bar_ts = self._align_ts(wall_ts, interval)
            key = (symbol, timeframe)
            
            current = self._rt_current.get(key)
            
            if current and current['time'] < bar_ts:
                # Previous bar completed — validate and persist
                completed = current.copy()
                self._validate_and_store(symbol, timeframe, completed)
                self._rt_completed[key] = completed
                self._rt_current[key] = None
            
            # Update or create current bar
            if self._rt_current.get(key) is None:
                self._rt_current[key] = {
                    'time': bar_ts, 'open': price, 'high': price,
                    'low': price, 'close': price, 'volume': size
                }
            else:
                bar = self._rt_current[key]
                bar['high'] = max(bar['high'], price)
                bar['low'] = min(bar['low'], price)
                bar['close'] = price
                bar['volume'] += size
    
    # ── Write-through Validation ───────────────────────────
    
    def _validate_and_store(self, symbol, timeframe, bar):
        """
        Validate a bar before writing to DB.
        Checks: timestamp alignment, OHLC consistency, duplicate detection.
        """
        issues = self._validate_bar(bar, symbol, timeframe)
        if issues:
            logger.warning("Bar validation issues for %s/%s at %d: %s",
                          symbol, timeframe, bar['time'], issues)
            # Auto-fix fixable issues (e.g., timestamp alignment)
            bar = self._auto_fix_bar(bar, issues)
        
        self._db.upsert_bar(symbol, timeframe, bar, source='realtime_completed')
    
    def _validate_bar(self, bar, symbol, timeframe):
        """
        Validate a single bar's integrity.
        Returns list of issues (empty = valid).
        """
        issues = []
        interval = self._get_interval(timeframe)
        
        # 1. Timestamp alignment
        expected_ts = self._align_ts(bar['time'], interval)
        if bar['time'] != expected_ts:
            issues.append(('ts_misaligned', bar['time'], expected_ts))
        
        # 2. OHLC consistency: H >= O,C >= L, H >= L
        if bar['high'] < bar['open'] or bar['high'] < bar['close']:
            issues.append(('high_below_oc', bar['high'], max(bar['open'], bar['close'])))
        if bar['low'] > bar['open'] or bar['low'] > bar['close']:
            issues.append(('low_above_oc', bar['low'], min(bar['open'], bar['close'])))
        if bar['high'] < bar['low']:
            issues.append(('high_below_low', bar['high'], bar['low']))
        
        # 3. Price reasonableness (> 0)
        for field in ('open', 'high', 'low', 'close'):
            if bar[field] <= 0:
                issues.append(('non_positive_price', field, bar[field]))
        
        # 4. Volume non-negative
        if bar.get('volume', 0) < 0:
            issues.append(('negative_volume', bar.get('volume')))
        
        return issues
    
    # ── Background Data Sync ───────────────────────────────
    
    async def start_background_sync(self):
        """
        Start background tasks for all instruments:
        1. Initial historical data sync (startup gap fill)
        2. Periodic gap detection and fill (every N minutes)
        3. Periodic data validation against IB (every N hours)
        """
        for symbol in self._config.INSTRUMENTS:
            for timeframe in ['5min', '15min', '60min', '1D']:
                asyncio.create_task(self._sync_loop(symbol, timeframe))
    
    async def _sync_loop(self, symbol, timeframe):
        """
        Background sync loop for one (symbol, timeframe) pair.
        Runs continuously, detecting and filling gaps.
        """
        # Phase 1: Initial sync (fill gaps since last known bar)
        await self._initial_sync(symbol, timeframe)
        
        # Phase 2: Periodic sync (check for gaps every 5 minutes)
        while True:
            await asyncio.sleep(300)  # 5 minutes
            await self._periodic_sync(symbol, timeframe)
    
    async def _initial_sync(self, symbol, timeframe):
        """
        On startup: fetch bars from IB to fill gap between
        last DB bar and current time.
        """
        latest_ts = self._db.get_latest_ts(symbol, timeframe)
        if latest_ts is None:
            # No data — fetch default duration
            duration = self._config.get_default_duration(timeframe)
            bars = await self._fetcher.fetch_historical(symbol, timeframe, duration)
        else:
            # Incremental fetch
            now_ts = int(time.time())
            gap_seconds = now_ts - latest_ts
            if gap_seconds > self._get_interval(timeframe) * 2:
                bars = await self._fetcher.fetch_range(symbol, timeframe, latest_ts, now_ts)
            else:
                bars = []
        
        if bars:
            validated = self._validate_bars_batch(bars, symbol, timeframe)
            self._db.insert_bars(symbol, timeframe, validated, source='ib_historical')
    
    # ── Utility ────────────────────────────────────────────
    
    @staticmethod
    def _align_ts(ts, interval):
        """Align timestamp to interval boundary."""
        return (ts // interval) * interval
```

#### 3.2.2 `trading_calendar.py` — 统一交易日历

**职责：** 统一管理所有与交易时段相关的逻辑。

```python
class TradingCalendar:
    """
    Unified trading calendar for all instruments.
    
    Handles:
    - Market session detection (RTH/ETH/closed)
    - Holiday detection (per exchange)
    - Gap classification (weekend/holiday/maintenance/data_gap)
    - Expected bar timestamps generation
    """
    
    def __init__(self, instruments_config):
        self._instruments = instruments_config
    
    def is_market_open(self, symbol, ts):
        """Check if market is open at given timestamp."""
        ...
    
    def classify_gap(self, symbol, timeframe, gap_start_ts, gap_end_ts):
        """
        Classify a data gap.
        Returns: 'weekend' | 'holiday' | 'maintenance' | 'data_gap'
        """
        ...
    
    def get_expected_timestamps(self, symbol, timeframe, from_ts, to_ts):
        """
        Generate all expected bar timestamps in a range,
        excluding known market closures.
        """
        ...
    
    def find_gaps(self, symbol, timeframe, actual_timestamps, from_ts, to_ts):
        """
        Compare actual timestamps against expected to find true data gaps.
        Only returns gaps that should have data (market was open).
        """
        expected = self.get_expected_timestamps(symbol, timeframe, from_ts, to_ts)
        actual_set = set(actual_timestamps)
        
        gaps = []
        gap_start = None
        for ts in expected:
            if ts not in actual_set:
                if gap_start is None:
                    gap_start = ts
            else:
                if gap_start is not None:
                    gaps.append({
                        'start': gap_start,
                        'end': ts,
                        'type': 'data_gap',  # Market was open but no data
                        'missing_bars': len([t for t in expected 
                                           if gap_start <= t < ts and t not in actual_set])
                    })
                    gap_start = None
        
        return gaps
```

#### 3.2.3 `data_ops.py` — 数据运维工具

**职责：** 提供标准的数据运维能力。

```python
class DataOps:
    """
    Data operations tools for maintenance and debugging.
    
    Provides:
    - Data exploration (query by conditions)
    - Single bar fix
    - Batch validation
    - Batch repair
    - Coverage analysis
    - Data quality reports
    """
    
    # ── 探查 (Exploration) ─────────────────────────────────
    
    async def query_bars(self, symbol, timeframe, from_ts, to_ts,
                        source_filter=None, price_range=None):
        """Query bars with flexible conditions."""
        ...
    
    async def get_coverage(self, symbol, timeframe):
        """Get data coverage info: earliest/latest ts, total bars, gaps."""
        ...
    
    async def get_gap_report(self, symbol, timeframe, from_ts, to_ts):
        """
        Generate detailed gap report:
        - Expected bars vs actual bars
        - Gap locations and classifications
        - Coverage percentage
        """
        ...
    
    # ── 单点修复 (Single Bar Fix) ──────────────────────────
    
    async def fix_bar(self, symbol, timeframe, ts, source='manual_fix'):
        """
        Fix a single bar by fetching from IB and replacing DB data.
        Returns: {old_bar, new_bar, changes}
        """
        ...
    
    async def delete_bar(self, symbol, timeframe, ts):
        """Delete a specific bar from DB."""
        ...
    
    async def insert_bar(self, symbol, timeframe, bar_data, source='manual_insert'):
        """Manually insert a bar (with validation)."""
        ...
    
    # ── 批量校验 (Batch Validation) ─────────────────────────
    
    async def validate_range(self, symbol, timeframe, from_ts, to_ts):
        """
        Validate a range of bars against IB source.
        Returns: {
            total_bars, matched, mismatched, missing_in_db,
            missing_in_ib, details: [...]
        }
        """
        ...
    
    async def validate_all(self, symbols=None, timeframes=None):
        """Validate all data for specified symbols/timeframes."""
        ...
    
    # ── 批量修复 (Batch Repair) ─────────────────────────────
    
    async def fix_range(self, symbol, timeframe, from_ts, to_ts,
                       fix_mismatches=True, fix_missing=True):
        """
        Fix all issues in a range:
        - Replace mismatched bars with IB data
        - Insert missing bars from IB
        """
        ...
    
    async def fix_gaps(self, symbol, timeframe, from_ts, to_ts):
        """Find and fill all data gaps in a range."""
        ...
    
    # ── 数据质量报告 ────────────────────────────────────────
    
    async def quality_report(self, symbol, timeframe, from_ts, to_ts):
        """
        Generate comprehensive data quality report:
        - Continuity score (% of expected bars present)
        - Price consistency score (% matching IB)
        - Volume consistency score
        - Gap summary
        - Anomaly detection (price spikes, zero volumes)
        """
        ...
```

#### 3.2.4 `db.py` 重构 — 纯数据存取层

**变更：** 移除业务逻辑（gap 检测、分类），只保留 CRUD 操作和索引。

```python
# 新增/修改的 DB 函数

def upsert_bar(symbol, timeframe, bar, source='unknown'):
    """Insert or update a single bar. Returns True if inserted, False if updated."""
    ...

def upsert_bars(symbol, timeframe, bars, source='unknown'):
    """Batch upsert bars with transaction."""
    ...

def get_bars(symbol, timeframe, from_ts=None, to_ts=None, limit=None, source=None):
    """Query bars with optional filters. Pure read, no side effects."""
    ...

def get_nearest_bar_before(symbol, timeframe, ts):
    """Get timestamp of the nearest bar before given ts (for nextTime)."""
    ...

def get_bar_timestamps(symbol, timeframe, from_ts, to_ts):
    """Get only timestamps (for gap analysis by TradingCalendar)."""
    ...

def get_coverage(symbol, timeframe):
    """Get {earliest_ts, latest_ts, total_count}."""
    ...

def delete_bars(symbol, timeframe, from_ts=None, to_ts=None, timestamps=None):
    """Delete bars by range or specific timestamps."""
    ...

# 移除的函数（迁移到 TradingCalendar/BarManager）
# - find_gaps()          → TradingCalendar.find_gaps()
# - _fill_internal_gaps() → BarManager._initial_sync()
# - _classify_gap()      → TradingCalendar.classify_gap()
```

**新增表结构：**

```sql
-- 保持原有 bars 表结构不变（兼容现有数据）
-- bars(symbol, timeframe, ts, open, high, low, close, volume, source)

-- 新增：数据同步状态追踪
CREATE TABLE IF NOT EXISTS sync_state (
    symbol       TEXT NOT NULL,
    timeframe    TEXT NOT NULL,
    last_sync_ts INTEGER,          -- 最后一次成功同步的时间
    last_gap_check_ts INTEGER,     -- 最后一次 gap 检查的时间
    status       TEXT DEFAULT 'idle',  -- idle | syncing | error
    error_msg    TEXT,
    PRIMARY KEY (symbol, timeframe)
);

-- 新增：数据修复记录（审计用）
CREATE TABLE IF NOT EXISTS data_fix_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    timeframe    TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    fix_type     TEXT NOT NULL,     -- 'insert' | 'update' | 'delete'
    old_data     TEXT,              -- JSON of old bar (if update/delete)
    new_data     TEXT,              -- JSON of new bar (if insert/update)
    source       TEXT NOT NULL,     -- 'ib_sync' | 'manual_fix' | 'batch_repair'
    fixed_at     INTEGER NOT NULL
);
```

#### 3.2.5 `ib_data_fetcher.py` 重构 — 纯 IB API 封装

**变更：** 移除 bar 组装、内存存储、回调分发逻辑，只保留 IB API 调用封装。

```python
class IBDataFetcher:
    """
    Pure IB API wrapper. No bar assembly or storage logic.
    
    Responsibilities:
    - Connect/reconnect to IB TWS
    - Fetch historical bars from IB
    - Subscribe to real-time ticks (raw ticks, no bar assembly)
    - Contract resolution (month-specific, continuous)
    """
    
    async def fetch_historical(self, symbol, timeframe, duration, end_dt=None):
        """Fetch historical bars from IB. Returns list of bar dicts."""
        ...
    
    async def fetch_range(self, symbol, timeframe, from_ts, to_ts):
        """Fetch bars for a specific time range. Handles contract rollover."""
        ...
    
    async def subscribe_ticks(self, symbol, callback):
        """
        Subscribe to real-time ticks for a symbol.
        callback receives: (symbol, price, size, wall_ts)
        """
        ...
    
    async def unsubscribe_ticks(self, symbol):
        """Unsubscribe from real-time ticks."""
        ...
```

#### 3.2.6 `server.py` 重构 — API 层简化

**变更：** `/api/history` 变成纯 DB 读取，移除 gap 检测和 IB 拉取逻辑。

```python
# 重构后的 /api/history
@app.get("/api/history")
async def get_history(symbol, resolution, from_ts, to_ts, countback=None):
    """
    TradingView UDF history endpoint.
    Pure DB read — no IB fetching or gap filling.
    Background sync ensures data freshness.
    """
    result = await bar_manager.get_bars(symbol, timeframe, from_ts, to_ts)
    
    if result['no_data']:
        resp = {"s": "no_data"}
        if result.get('next_time'):
            resp["nextTime"] = result['next_time']
        return resp
    
    bars = result['bars']
    return {
        "s": "ok",
        "t": [b['time'] for b in bars],
        "o": [b['open'] for b in bars],
        "h": [b['high'] for b in bars],
        "l": [b['low'] for b in bars],
        "c": [b['close'] for b in bars],
        "v": [b.get('volume', 0) for b in bars],
    }

# 新增：数据运维 API
@app.get("/api/data-ops/coverage")
async def data_coverage(symbol, timeframe):
    """Get data coverage info."""
    return await data_ops.get_coverage(symbol, timeframe)

@app.get("/api/data-ops/gaps")
async def data_gaps(symbol, timeframe, from_ts, to_ts):
    """Get gap report."""
    return await data_ops.get_gap_report(symbol, timeframe, from_ts, to_ts)

@app.post("/api/data-ops/validate")
async def validate_data(symbol, timeframe, from_ts, to_ts):
    """Validate data against IB source."""
    return await data_ops.validate_range(symbol, timeframe, from_ts, to_ts)

@app.post("/api/data-ops/fix")
async def fix_data(symbol, timeframe, from_ts, to_ts, timestamps=None):
    """Fix data issues."""
    return await data_ops.fix_range(symbol, timeframe, from_ts, to_ts)

@app.post("/api/data-ops/fix-bar")
async def fix_single_bar(symbol, timeframe, ts):
    """Fix a single bar."""
    return await data_ops.fix_bar(symbol, timeframe, ts)

@app.get("/api/data-ops/quality-report")
async def quality_report(symbol, timeframe, from_ts, to_ts):
    """Get data quality report."""
    return await data_ops.quality_report(symbol, timeframe, from_ts, to_ts)
```

#### 3.2.7 `datafeed.js` 重构 — 简化 getBars

**变更：** 移除 hack 逻辑，依赖后端数据完整性。

```javascript
getBars(symbolInfo, resolution, periodParams, onResult, onError) {
    const { from, to, countBack } = periodParams;
    let url = `/api/history?symbol=${encodeURIComponent(symbolInfo.name)}&resolution=${resolution}&from=${from}&to=${to}`;
    if (countBack) url += `&countback=${countBack}`;

    fetch(url)
      .then(r => r.json())
      .then(data => {
        if (data.s === 'no_data') {
          const meta = { noData: true };
          if (data.nextTime != null) meta.nextTime = data.nextTime;
          onResult([], meta);
          return;
        }
        if (data.s !== 'ok') {
          onError('HISTORY_ERROR');
          return;
        }
        const bars = data.t.map((t, i) => ({
          time:   t * 1000,
          open:   data.o[i],
          high:   data.h[i],
          low:    data.l[i],
          close:  data.c[i],
          volume: data.v[i],
        }));
        onResult(bars, { noData: false });
      })
      .catch(err => onError('FETCH_ERROR'));
}
```

---

## 四、重构步骤（执行计划）

### Phase 1: 基础设施 (不影响现有功能)

| Step | 描述 | 文件 | 影响 |
|------|------|------|------|
| 1.1 | 创建 `trading_calendar.py` | 新建 | 无 |
| 1.2 | 将 `market_holidays.py` 逻辑迁移到 `trading_calendar.py`（迁移完成后 `market_holidays.py` 保留为空 wrapper，标记 deprecated，后续版本移除） | 修改 | 无 |
| 1.3 | 在 `config.py` 中集中配置阈值 | 修改 | 无 |
| 1.4 | 在 `db.py` 中添加新表和新函数（保留旧函数） | 修改 | 无 |

### Phase 2: 核心模块 (并行开发)

| Step | 描述 | 文件 | 影响 |
|------|------|------|------|
| 2.1 | 创建 `bar_manager.py` — 数据获取/组装/校验/写入 | 新建 | 无 |
| 2.2 | 创建 `data_ops.py` — 数据运维工具 | 新建 | 无 |
| 2.3 | 重构 `ib_data_fetcher.py` — 纯 API 封装 | 修改 | 需测试 |

### Phase 3: 集成切换

| Step | 描述 | 文件 | 影响 |
|------|------|------|------|
| 3.1 | 重构 `server.py` — 切换到 BarManager | 修改 | **关键** |
| 3.2 | 重构 `datafeed.js` — 简化 getBars | 修改 | 前端 |
| 3.3 | 更新 `data_validator.py` — 集成 DataOps | 修改 | 工具 |

### Phase 4: 前端增强

| Step | 描述 | 文件 | 影响 |
|------|------|------|------|
| 4.1 | 增加 Data Ops 前端面板 | index.html | 前端 |
| 4.2 | 添加数据探查、修复、校验 UI | index.html/app.js | 前端 |

### Phase 5: 验证与文档

| Step | 描述 | 文件 | 影响 |
|------|------|------|------|
| 5.1 | 全功能回归测试 | 所有 | 验证 |
| 5.2 | 更新 README.md | 文档 | 文档 |
| 5.3 | 更新 dataflow.md | 文档 | 文档 |
| 5.4 | 更新 PLAN.md | 文档 | 文档 |

---

## 五、数据校验与运维工具

### 5.1 校验能力矩阵

| 校验类型 | 触发时机 | 描述 |
|---------|---------|------|
| **写入前校验** | 每次 bar 写入 DB | 时间戳对齐、OHLC 一致性、价格合理性、Volume 非负 |
| **IB 对比校验** | 后台定时 / 手动触发 | 对比 DB 数据与 IB Historical，检测 OHLCV 差异 |
| **连续性校验** | 后台定时 / 手动触发 | 检测数据缺口（基于 TradingCalendar 排除合法空档） |
| **源可信度校验** | 数据查询时 | 标记数据来源（IB historical / realtime / synthetic / manual） |

### 5.2 IB Fetch Cache 保留策略

- 保留 `ib_fetch_cache` 表，用于校验和修复时减少 IB API 调用
- 新增 `fetched_at` 索引，支持按时间清理过期缓存
- IB fetch cache 的数据也要经过 `_validate_bar()` 校验

### 5.3 数据运维 Web 工具

前端 Data Ops 面板提供以下功能：

1. **数据探查**
   - 按品种、时间框架、时间范围查询 bar 数据
   - 按 source 过滤（ib_historical / realtime_completed / ib_validated / manual）
   - 展示每条 bar 的详细信息（OHLCV + source + 与 IB 差异）

2. **单点修复**
   - 选择一条 bar，从 IB 重新拉取并替换
   - 手动编辑 bar 数据（管理员功能）
   - 删除异常 bar

3. **批量校验**
   - 选择品种、时间框架、时间范围
   - 运行 IB 对比校验，展示差异报告
   - 运行连续性校验，展示缺口报告

4. **批量修复**
   - 一键修复所有不匹配的 bar（用 IB 数据替换）
   - 一键填充所有数据缺口（从 IB 拉取）
   - 修复历史记录（审计日志）

---

## 六、现有功能兼容性检查

### 6.1 影响评估

| 现有功能 | 影响 | 兼容方案 |
|---------|------|---------|
| 图表展示 | ✅ API 接口不变 | `/api/history` 返回格式不变 |
| 实时K线 | ✅ WebSocket 格式不变 | `bar` message 格式不变 |
| S/R 分析 | ✅ 不受影响 | 分析逻辑不变 |
| Market Cycle | ✅ 不受影响 | 分析逻辑不变 |
| 策略回测 | ✅ 不受影响 | 数据查询接口不变 |
| 下单交易 | ✅ 不受影响 | 与数据层无关 |
| Google Sheets | ✅ 不受影响 | 数据同步逻辑不变 |
| Data Valid 页面 | 🔄 需要适配 | 迁移到 Data Ops 新 API |
| 扩展时段 | ✅ 不受影响 | session 处理不变 |

### 6.2 迁移策略

1. **渐进式迁移**：新模块先并行运行，验证通过后再切换
2. **数据库兼容**：`bars` 表结构不变，新增表不影响旧代码
3. **API 向后兼容**：所有 API 返回格式保持不变
4. **回滚方案**：保留旧代码文件（`server.py` → `server_legacy.py`、`ib_data_fetcher.py` → `ib_data_fetcher_legacy.py`、`db.py` → `db_legacy.py`），可快速回滚。回滚触发条件：重构后出现 API 响应异常（非 200）、实时 bar 推送中断超过 5 分钟、或图表无法加载数据

---

## 七、多品种通用设计

### 7.1 设计原则

- 所有逻辑以 `(symbol, timeframe)` 为 key，无品种硬编码
- 品种差异通过 `config.INSTRUMENTS` 配置驱动
- 允许按品种**类型**定制（如 `contract_type: quarterly` vs `monthly`）
- 不允许按品种**名称**定制（如无 `if symbol == "MGC"` 这样的逻辑）

### 7.2 品种类型差异化配置

```python
# config.py 增强
INSTRUMENTS = {
    "MES": {
        # ... existing fields ...
        "data_config": {
            "default_duration_intraday": "5 D",
            "default_duration_daily": "2 Y",
            "max_gap_fetch_days": 3,        # 单次 gap 拉取最大天数
            "price_tolerance": 0.5,          # 校验价格容差
            "volume_tolerance": 1.0,         # 校验成交量容差
            "gap_threshold_multiplier": 2,   # gap 检测阈值倍数
        },
        "session_config": {
            "eth_start": (18, 0),  # ETH 开始时间
            "eth_end": (17, 0),    # ETH 结束时间（次日）
            "maintenance_start": (16, 0),
            "maintenance_end": (17, 0),
            "maintenance_duration_max_hours": 4,
        },
    },
    # ... 其他品种使用相同结构，不同值 ...
}
```

---

## 八、风险与注意事项

1. **IB API 限流**：后台 sync 需要严格遵守 IB 的 6 req/10s 限制
2. **SQLite 并发**：WAL 模式下支持并发读 + 单写，后台 sync 写入需要控制频率
3. **内存使用**：`_memory_cache` 需要有 LRU 或容量限制
4. **时区处理**：统一使用 UTC 存储，展示时按品种 timezone 转换
5. **合约换月**：保持现有的 3-month fallback + ContFuture 降级策略
6. **crashRecovery**：`realtime_bars` 表保留，BarManager 在启动时恢复状态

---

## 九、成功标准

1. **K线连续性**：Data Ops 连续性校验在 RTH 时段达到 99.9%+ 覆盖率
2. **数据一致性**：DB 数据与 IB Historical 的 OHLCV 差异率 < 0.1%
3. **API 响应时间**：`/api/history` P99 < 100ms（纯 DB 读取，无 IB 等待）
4. **零回退**：所有现有功能（图表、交易、策略、分析）正常工作
5. **多品种通用**：新增品种只需在 `config.INSTRUMENTS` 添加配置
