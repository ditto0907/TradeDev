# TradeDev 数据流文档 (Data Flow Documentation)

> 本文档描述 TradeDev 系统中数据从 IB TWS 到前端 chart 的完整流转过程。
> 所有 diagram 使用 Mermaid 格式。

---

## 目录

1. [概览 (Overview)](#1-概览-overview)
2. [启动流程 (Startup)](#2-启动流程-startup)
3. [数据加载与服务 (Data Loading & Serving)](#3-数据加载与服务-data-loading--serving)
4. [IB 数据拉取触发条件 (IB Fetch Triggers)](#4-ib-数据拉取触发条件-ib-fetch-triggers)
5. [实时数据流 (Real-time Data Flow)](#5-实时数据流-real-time-data-flow)
6. [DB 与 IB 数据协作 (DB + IB Collaboration)](#6-db-与-ib-数据协作-db--ib-collaboration)
7. [数据校验流程 (Data Validation)](#7-数据校验流程-data-validation)
8. [前端数据获取 (Frontend Data Flow)](#8-前端数据获取-frontend-data-flow)

---

## 1. 概览 (Overview)

系统由四个核心层组成：**数据源层** (IB TWS)、**服务层** (FastAPI server)、**存储层** (SQLite) 和 **展示层** (TradingView chart)。数据通过 IB API 获取后存入 SQLite，由 server 统一对前端提供 REST + WebSocket 服务。

```mermaid
graph TB
    subgraph 数据源层 ["数据源层 (Data Source)"]
        IB["IB TWS Gateway<br/>127.0.0.1:7497"]
    end

    subgraph 服务层 ["服务层 (Application Server)"]
        SRV["FastAPI server.py"]
        FET["ib_data_fetcher.py<br/>(IBDataFetcher)"]
        PA["price_action_analyzer.py<br/>(S/R Analysis)"]
        OM["order_manager.py<br/>(OrderManager)"]
        GS["google_sheets_sync.py"]
        VAL["data_validator.py"]
        MEM["In-Memory Cache<br/>fetcher.bars[5min]"]
    end

    subgraph 存储层 ["存储层 (Storage)"]
        DB["SQLite WAL<br/>data/tradedev.db"]
    end

    subgraph 展示层 ["展示层 (Frontend)"]
        TV["TradingView Chart<br/>datafeed.js"]
        WS["WebSocket Client<br/>/ws/realtime"]
    end

    IB -- "reqHistoricalData<br/>reqMktData" --> FET
    FET -- "insert_bars" --> DB
    FET -- "bars cache" --> MEM
    DB -- "get_bars" --> SRV
    MEM -- "fallback" --> SRV
    SRV -- "GET /api/history" --> TV
    SRV -- "push updates" --> WS
    PA -- "S/R levels" --> SRV
    OM -- "order status" --> SRV
    SRV -- "upload" --> GS
    VAL -- "validate/fix" --> DB

    style IB fill:#e1f5fe,stroke:#0288d1
    style DB fill:#fff3e0,stroke:#f57c00
    style SRV fill:#e8f5e9,stroke:#388e3c
    style TV fill:#fce4ec,stroke:#c62828
    style WS fill:#fce4ec,stroke:#c62828
```

---

## 2. 启动流程 (Startup)

### 启动的时候默认如何加载数据？

Server 启动时通过 `lifespan` context manager 执行以下步骤。数据加载分为 **同步阶段**（阻塞式，server 启动前完成）和 **异步阶段**（后台任务，server 已开始接受请求）。

```mermaid
sequenceDiagram
    autonumber
    participant Main as server.py<br/>lifespan
    participant DB as db.py<br/>SQLite
    participant Cache as In-Memory<br/>Cache
    participant PA as PriceAction<br/>Analyzer
    participant BG as Background<br/>Task
    participant IB as IB TWS<br/>Gateway
    participant GS as Google<br/>Sheets

    Note over Main: === 同步阶段 (Blocking) ===

    Main->>DB: db.init_db()
    Note right of DB: CREATE TABLE IF NOT EXISTS<br/>~5ms

    Main->>DB: load MES 5min bars
    DB-->>Cache: bars → fetcher.bars["5min"]
    Note right of Cache: 全量加载到内存<br/>~50ms

    Main->>PA: run initial S/R analysis
    Note right of PA: 基于 DB 中已有数据<br/>计算 Support/Resistance

    Main->>BG: start _db_coverage_loop
    Note right of BG: 后台定期检查<br/>数据覆盖率

    Note over Main: === yield: SERVER READY ===
    Note over Main: 此时 server 已可接受请求<br/>但 IB 尚未连接

    Note over Main: === 异步阶段 (Background) ===

    BG->>IB: _ib_background_init()
    Note right of IB: Connect 127.0.0.1:7497<br/>retry 3 attempts

    IB-->>BG: connected

    BG->>IB: qualifyContractsAsync<br/>(MES ContFuture)
    IB-->>BG: contract qualified

    BG->>DB: db.get_latest_ts()
    DB-->>BG: latest timestamp

    alt DB 有历史数据
        BG->>IB: Incremental fetch<br/>只拉取 latest_ts 之后的新 bars
        IB-->>BG: new bars
        BG->>DB: insert_bars(source="ib_historical")
    else DB 无数据 且 IB 不可用
        BG->>Cache: generate synthetic bars<br/>(GBM model, source="synthetic")
    end

    BG->>IB: subscribe_mktdata()<br/>开始接收实时 ticks
    BG->>BG: Create OrderManager
    BG->>GS: Google Sheets upload

    Note over BG: _prefetch_extra_symbols()
    BG->>IB: fetch MNQ bars
    BG->>IB: fetch NK225MC bars
    BG->>IB: fetch MGC bars
```

### 关键设计决策

| 阶段 | 耗时 | 说明 |
|------|------|------|
| `db.init_db()` | ~5ms | 建表（如不存在） |
| Load DB → Memory | ~50ms | MES 5min bars 全量加载 |
| S/R Analysis | ~100ms | 基于已有数据初始分析 |
| **Server Ready** | **~200ms** | **yield 后开始接受请求** |
| IB Connect | 1-10s | 后台异步，含 retry |
| Incremental Fetch | 2-30s | 仅拉新数据，非全量 |
| Extra Symbols | 5-60s | MNQ, NK225MC, MGC |

---

## 3. 数据加载与服务 (Data Loading & Serving)

### 运行过程中 DB 存储的数据是如何和 IB fetch 组合对前端提供服务？

当前端请求 `GET /api/history?symbol=MES&resolution=5&from=T1&to=T2` 时，server 执行以下流程：

```mermaid
flowchart TD
    REQ["GET /api/history<br/>symbol, key, from_ts, to_ts"]
    Q1["Step 1: Query DB<br/>db.get_bars(sym, key, from_ts, to_ts)"]
    CHK{"检查 coverage gaps"}

    CASE1{"Case 1:<br/>完全无数据？"}
    CASE2{"Case 2:<br/>Left gap?<br/>chart 滚动到 oldest bar 之前"}
    CASE3{"Case 3:<br/>Right gap?<br/>数据陈旧 + cooldown 已过"}

    FETCH1["IB fetch full range<br/>[from_ts → to_ts]"]
    FETCH2["IB fetch left gap<br/>[from_ts → earliest_db]"]
    FETCH3["IB fetch right gap<br/>[latest_db → to_ts]"]

    IB_READY{"IB connected<br/>& ready?"}
    FETCH_EXEC["fetcher.fetch_range()<br/>1. 尝试 month-specific Future<br/>2. 处理 rollover<br/>3. fallback → ContFuture"]
    SAVE["db.insert_bars<br/>source='ib_historical'"]
    REQUERY["Re-query DB<br/>获取完整数据"]

    FALLBACK{"MES symbol?"}
    MEM_CACHE["Fallback: In-Memory Cache<br/>fetcher.bars['5min']"]
    EMPTY["Return empty"]

    LIMIT["Apply countback limit"]
    RESP["Return JSON<br/>{s:'ok', t:[], o:[], h:[], l:[], c:[], v:[]}"]

    REQ --> Q1
    Q1 --> CHK

    CHK --> CASE1
    CASE1 -- "是" --> IB_READY
    CASE1 -- "否" --> CASE2

    CASE2 -- "是" --> IB_READY
    CASE2 -- "否" --> CASE3

    CASE3 -- "是" --> IB_READY
    CASE3 -- "否" --> LIMIT

    IB_READY -- "是" --> FETCH_EXEC
    IB_READY -- "否" --> FALLBACK

    FETCH_EXEC --> SAVE
    SAVE --> REQUERY
    REQUERY --> LIMIT

    FALLBACK -- "是 (MES)" --> MEM_CACHE
    FALLBACK -- "否" --> EMPTY
    MEM_CACHE --> LIMIT
    EMPTY --> LIMIT

    LIMIT --> RESP

    style REQ fill:#e3f2fd,stroke:#1565c0
    style RESP fill:#e8f5e9,stroke:#2e7d32
    style FETCH_EXEC fill:#fff3e0,stroke:#ef6c00
    style SAVE fill:#fff3e0,stroke:#ef6c00
    style MEM_CACHE fill:#f3e5f5,stroke:#7b1fa2
```

### Gap 检测逻辑详解

```mermaid
flowchart LR
    subgraph timeline ["时间轴示意"]
        direction LR
        FROM["from_ts<br/>(请求起点)"]
        EDB["earliest_db<br/>(DB最早记录)"]
        LDB["latest_db<br/>(DB最新记录)"]
        TO["to_ts<br/>(请求终点)"]
    end

    subgraph gaps ["Gap 类型"]
        LG["Left Gap<br/>from_ts < earliest_db<br/>→ fetch [from_ts, earliest_db]"]
        RG["Right Gap<br/>latest_db < to_ts<br/>→ fetch [latest_db, to_ts]<br/>(需 cooldown 检查)"]
        FG["Full Gap<br/>DB 无数据<br/>→ fetch [from_ts, to_ts]"]
    end

    FROM -.->|"gap"| EDB
    LDB -.->|"gap"| TO

    style LG fill:#ffecb3,stroke:#ff8f00
    style RG fill:#ffecb3,stroke:#ff8f00
    style FG fill:#ffcdd2,stroke:#c62828
```

---

## 4. IB 数据拉取触发条件 (IB Fetch Triggers)

### 什么时候会触发 IB fetch？

| # | 触发场景 | 触发位置 | 拉取范围 | 备注 |
|---|---------|---------|---------|------|
| 1 | **Startup** | `_ib_background_init()` | `latest_ts → now` | Incremental，仅拉新 bars |
| 2 | **Startup Prefetch** | `_prefetch_extra_symbols()` | 完整历史 | MNQ, NK225MC, MGC |
| 3 | **On-demand** | `GET /api/history` gap 检测 | `gap_start → gap_end` | 按需拉取缺失区间 |
| 4 | **Validation** | `data_validator.py` | chunk-based | 校验时独立获取 IB 数据 |

### Cooldown 机制

```mermaid
flowchart TD
    TRIGGER["IB Fetch 请求"]
    CD_CHK{"Cooldown 检查<br/>per (symbol, key)"}

    CD_ZERO{"上次 IB 返回<br/>0 bars?"}
    CD_ERR{"上次 IB<br/>出错?"}

    WAIT5["等待 5 分钟 cooldown<br/>避免重复请求空数据"]
    WAIT1["等待 1 分钟 cooldown<br/>避免频繁重试"]
    EXEC["执行 IB fetch<br/>fetcher.fetch_range()"]

    RESULT{"返回结果"}
    R_OK["有数据 → 存入 DB"]
    R_EMPTY["0 bars → 设置 5min cooldown"]
    R_ERR["Error → 设置 1min cooldown"]

    TRIGGER --> CD_CHK
    CD_CHK -- "在 cooldown 中" --> CD_ZERO
    CD_CHK -- "不在 cooldown 中" --> EXEC

    CD_ZERO -- "是" --> WAIT5
    CD_ZERO -- "否" --> CD_ERR
    CD_ERR -- "是" --> WAIT1
    CD_ERR -- "否" --> EXEC

    EXEC --> RESULT
    RESULT --> R_OK
    RESULT --> R_EMPTY
    RESULT --> R_ERR

    style WAIT5 fill:#ffcdd2,stroke:#c62828
    style WAIT1 fill:#fff9c4,stroke:#f9a825
    style EXEC fill:#c8e6c9,stroke:#2e7d32
```

### IB Fetch 决策流程

```mermaid
flowchart TD
    START["需要获取 bars"]
    TRY_MONTH["尝试 month-specific<br/>Future contract"]
    MONTH_OK{"成功?"}
    ROLLOVER["处理 rollover<br/>尝试相邻月份合约"]
    ROLL_OK{"成功?"}
    CONT["Fallback:<br/>ContFuture 连续合约"]
    CONT_OK{"成功?"}
    DONE["返回 bars"]
    FAIL["返回空 + 设置 cooldown"]

    START --> TRY_MONTH
    TRY_MONTH --> MONTH_OK
    MONTH_OK -- "是" --> DONE
    MONTH_OK -- "否" --> ROLLOVER
    ROLLOVER --> ROLL_OK
    ROLL_OK -- "是" --> DONE
    ROLL_OK -- "否" --> CONT
    CONT --> CONT_OK
    CONT_OK -- "是" --> DONE
    CONT_OK -- "否" --> FAIL

    style DONE fill:#c8e6c9,stroke:#2e7d32
    style FAIL fill:#ffcdd2,stroke:#c62828
```

---

## 5. 实时数据流 (Real-time Data Flow)

实时数据从 IB TWS 的 market data subscription 开始，经过 tick aggregation 组装成 5min bar，最终通过 WebSocket 推送到前端。

```mermaid
sequenceDiagram
    autonumber
    participant TWS as IB TWS
    participant FET as IBDataFetcher<br/>_on_tick()
    participant AGG as Tick Aggregator<br/>(5min bar assembly)
    participant DB as SQLite DB
    participant SRV as server.py<br/>on_new_bar()
    participant PA as PriceAction<br/>Analyzer
    participant GS as Google Sheets
    participant WS as WebSocket<br/>Clients

    TWS->>FET: ticker.updateEvent<br/>(reqMktData callback)
    Note right of FET: 收到 tick:<br/>last price, volume, etc.

    FET->>AGG: 聚合 tick 到当前 bar
    Note right of AGG: bar_ts = wall_ts // 300 * 300<br/>(对齐到 5min 边界)

    AGG->>AGG: 更新 OHLCV:<br/>O=first, H=max, L=min,<br/>C=last, V=sum

    alt 检测到新 bar_ts (跨越 5min 边界)
        Note over AGG: 当前 bar_ts ≠ 上一 bar_ts<br/>→ 上一 bar 已完成

        AGG->>DB: 保存 completed bar<br/>source="realtime"

        AGG->>SRV: on_new_bar callback<br/>dispatch completed bar

        SRV->>DB: insert completed bar<br/>db.insert_bars()

        SRV->>GS: buffer bar 到<br/>Google Sheets 队列

        SRV->>PA: re-run S/R analysis<br/>基于新 bar 重新计算

        PA-->>SRV: updated S/R levels

        SRV->>WS: broadcast bar update
        SRV->>WS: broadcast analysis update
    end

    Note over FET,WS: Throttle: 最多每 250ms broadcast 一次

    alt Throttle 未到 250ms
        FET--xWS: 跳过本次 broadcast
    end
```

### Tick → Bar 聚合细节

```mermaid
flowchart LR
    subgraph tick_stream ["Tick Stream (连续)"]
        T1["tick @14:01:03<br/>price=4520.25"]
        T2["tick @14:01:15<br/>price=4520.50"]
        T3["tick @14:03:42<br/>price=4519.75"]
        T4["tick @14:05:01<br/>price=4521.00"]
    end

    subgraph bar_assembly ["Bar Assembly"]
        B1["Bar 14:00:00<br/>bar_ts = 14:01:03 // 300 * 300<br/>O=4520.25 H=4520.50<br/>L=4519.75 C=4519.75"]
        B2["Bar 14:05:00 (新 bar)<br/>→ 触发 B1 完成事件<br/>O=4521.00 ..."]
    end

    T1 --> B1
    T2 --> B1
    T3 --> B1
    T4 -->|"新 bar_ts 检测"| B2

    B1 -->|"completed"| SAVE["保存到 DB + Broadcast"]

    style SAVE fill:#c8e6c9,stroke:#2e7d32
    style B2 fill:#fff9c4,stroke:#f9a825
```

---

## 6. DB 与 IB 数据协作 (DB + IB Collaboration)

DB 作为持久化层，IB 作为数据源。两者协作模式如下：

```mermaid
graph TB
    subgraph 数据生命周期 ["数据生命周期 (Data Lifecycle)"]
        direction TB

        subgraph sources ["数据来源"]
            IB_HIST["IB Historical API<br/>source='ib_historical'"]
            IB_RT["IB Real-time Ticks<br/>source='realtime'"]
            SYNTH["Synthetic Generator<br/>source='synthetic'"]
            IB_VAL["Data Validator<br/>source='ib_validated'"]
        end

        subgraph storage ["持久化存储"]
            SQLITE["SQLite WAL<br/>data/tradedev.db"]
            TABLE["bars table<br/>PK: (symbol, timeframe, ts)<br/>fields: OHLCV, source"]
        end

        subgraph serving ["数据服务"]
            REST["REST API<br/>GET /api/history"]
            WSOCK["WebSocket<br/>/ws/realtime"]
            MEMCACHE["In-Memory Cache<br/>(MES 5min fallback)"]
        end
    end

    IB_HIST --> SQLITE
    IB_RT --> SQLITE
    SYNTH --> SQLITE
    IB_VAL --> SQLITE
    SQLITE --> TABLE

    TABLE --> REST
    TABLE --> WSOCK
    MEMCACHE -.->|"fallback only"| REST

    style SQLITE fill:#fff3e0,stroke:#ef6c00
    style REST fill:#e8f5e9,stroke:#2e7d32
    style WSOCK fill:#e8f5e9,stroke:#2e7d32
```

### 请求处理中 DB 与 IB 的协作序列

```mermaid
sequenceDiagram
    autonumber
    participant FE as Frontend
    participant SRV as Server
    participant DB as SQLite
    participant IB as IB TWS
    participant Cache as Memory Cache

    FE->>SRV: GET /api/history<br/>(symbol, from_ts, to_ts)

    SRV->>DB: Step 1: get_bars(sym, key, from_ts, to_ts)
    DB-->>SRV: DB bars (可能不完整)

    SRV->>SRV: Step 2: 分析 coverage gaps

    alt 存在 gap 且 IB ready
        SRV->>IB: Step 3: fetch_range(gap_start, gap_end)
        IB-->>SRV: fetched bars

        SRV->>DB: Step 3b: insert_bars(source="ib_historical")
        Note right of DB: UPSERT: 相同 PK 会覆盖

        SRV->>DB: Step 4: Re-query get_bars()
        DB-->>SRV: 完整数据
    else 无 gap
        Note over SRV: DB 数据已完整，无需 IB
    else 有 gap 但 IB 不可用
        alt MES symbol
            SRV->>Cache: Step 5: fallback in-memory
            Cache-->>SRV: cached bars
        else 其他 symbol
            Note over SRV: 返回已有数据或空
        end
    end

    SRV->>SRV: Step 6: apply countback limit

    SRV-->>FE: Step 7: {s:"ok", t:[], o:[], h:[], l:[], c:[], v:[]}
```

### DB Source 标签说明

| Source Tag | 产生方式 | 优先级 | 说明 |
|-----------|---------|--------|------|
| `ib_historical` | IB reqHistoricalData | 高 | 标准历史数据 |
| `realtime` | Tick aggregation | 高 | 实时组装的 bar |
| `ib_validated` | Data validator fix | 最高 | 校验后修正的数据 |
| `synthetic` | GBM model | 低 | 测试用合成数据 |
| `unknown` | Legacy import | 低 | 历史遗留数据 |

> **注意**: 所有 source 的 bars 共享同一张表，PRIMARY KEY `(symbol, timeframe, ts)` 保证同一时间点只有一条记录。后写入的数据会覆盖先前的数据（UPSERT 语义）。

---

## 7. 数据校验流程 (Data Validation)

数据校验确保 DB 中存储的 bars 与 IB 提供的历史数据一致。

```mermaid
flowchart TD
    subgraph validate ["validate_bars(symbol, timeframe, from_ts, to_ts)"]
        V_START["开始校验"]
        V_DB["Fetch DB bars<br/>db.get_bars()"]
        V_IB["Fetch IB bars<br/>fetcher.fetch_range()<br/>或 standalone IB connection"]
        V_CMP["_compare_bars()<br/>逐 bar 比较 OHLC"]
        V_TOL{"差异 ≤ 0.5 tick?"}
        V_MATCH["✅ Match"]
        V_MISMATCH["❌ Mismatch"]
        V_RESULT["返回结果:<br/>mismatches, db_only, ib_only"]
    end

    V_START --> V_DB
    V_START --> V_IB
    V_DB --> V_CMP
    V_IB --> V_CMP
    V_CMP --> V_TOL
    V_TOL -- "是" --> V_MATCH
    V_TOL -- "否" --> V_MISMATCH
    V_MATCH --> V_RESULT
    V_MISMATCH --> V_RESULT

    subgraph fix ["fix_bars() — 修复模式"]
        F_START["与 validate 相同流程"]
        F_OVERWRITE["用 IB 数据覆盖 DB<br/>source='ib_validated'"]
    end

    V_MISMATCH -.->|"fix mode"| F_OVERWRITE

    style V_MATCH fill:#c8e6c9,stroke:#2e7d32
    style V_MISMATCH fill:#ffcdd2,stroke:#c62828
    style F_OVERWRITE fill:#fff9c4,stroke:#f9a825
```

### validate_all() 全量校验流程

```mermaid
flowchart TD
    VA_START["validate_all() 启动"]
    VA_SCAN["扫描 DB 中所有<br/>(symbol, timeframe) pairs"]
    VA_FILTER{"数据 > 1 year old?"}
    VA_SKIP["跳过"]
    VA_CHUNK["按 chunk 分割:<br/>intraday → 1 day/chunk<br/>daily → 30 days/chunk"]
    VA_LOOP["遍历每个 chunk"]
    VA_VALIDATE["validate_bars()<br/>校验当前 chunk"]
    VA_PAUSE["Rate limit:<br/>暂停 2 秒<br/>避免 IB 限流"]
    VA_NEXT{"还有更多<br/>chunk?"}
    VA_REPORT["输出校验报告:<br/>total mismatches,<br/>db_only, ib_only"]

    VA_START --> VA_SCAN
    VA_SCAN --> VA_FILTER
    VA_FILTER -- "是" --> VA_SKIP
    VA_FILTER -- "否" --> VA_CHUNK
    VA_CHUNK --> VA_LOOP
    VA_LOOP --> VA_VALIDATE
    VA_VALIDATE --> VA_PAUSE
    VA_PAUSE --> VA_NEXT
    VA_NEXT -- "是" --> VA_LOOP
    VA_NEXT -- "否" --> VA_REPORT

    style VA_SKIP fill:#eeeeee,stroke:#9e9e9e
    style VA_REPORT fill:#e8f5e9,stroke:#2e7d32
    style VA_PAUSE fill:#fff9c4,stroke:#f9a825
```

### 校验容差规则

```
OHLC 比较: |db_value - ib_value| ≤ 0.5 * tick_size
  - MES tick_size = 0.25 → tolerance = 0.125
  - 超过 tolerance 视为 mismatch

Volume: 不参与比较 (IB historical volume 可能与 realtime 不同)

Timestamp: 精确匹配 (unix epoch seconds)
  - db_only: DB 有但 IB 没有 → 可能是非交易时段的 bar
  - ib_only: IB 有但 DB 没有 → 数据缺失，需要补录
```

---

## 8. 前端数据获取 (Frontend Data Flow)

### 前端初始化 → 历史数据加载 → 实时更新全流程

```mermaid
sequenceDiagram
    autonumber
    participant Browser as Browser<br/>index.html
    participant App as app.js<br/>initChart()
    participant DF as datafeed.js<br/>MESDatafeed
    participant REST as Server<br/>GET /api/history
    participant WS_C as WebSocket<br/>Client
    participant WS_S as Server<br/>/ws/realtime
    participant TV as TradingView<br/>Widget

    Browser->>App: 页面加载
    App->>TV: new TradingView.widget({<br/>  datafeed: MESDatafeed,<br/>  symbol: "MES", ...})

    Note over TV: TradingView 初始化

    TV->>DF: resolveSymbol("MES")
    DF-->>TV: symbol info<br/>(name, exchange, timezone, etc.)

    TV->>DF: getBars(symbolInfo,<br/>resolution, periodParams)
    DF->>REST: GET /api/history?<br/>symbol=MES&resolution=5<br/>&from=T1&to=T2
    REST-->>DF: {s:"ok", t:[...], o:[...],<br/>h:[...], l:[...], c:[...], v:[...]}
    DF-->>TV: bars[] for chart rendering

    Note over TV: Chart 渲染完成<br/>显示历史 K 线

    TV->>DF: subscribeBars(symbolInfo,<br/>resolution, onRealtimeCallback)

    DF->>WS_C: new WebSocket<br/>("ws://host/ws/realtime")
    WS_C->>WS_S: WebSocket 连接建立

    WS_S-->>WS_C: Snapshot 推送:<br/>200 bars + S/R analysis
    WS_C-->>DF: onSnapshot()
    DF-->>TV: 更新 chart 数据

    loop 实时更新循环
        WS_S-->>WS_C: bar update<br/>{type:"bar", data:{...}}
        WS_C-->>DF: onBarUpdate()
        DF-->>TV: onRealtimeCallback(bar)
        Note right of TV: 实时更新最新 K 线

        WS_S-->>WS_C: analysis update<br/>{type:"analysis", data:{...}}
        WS_C-->>DF: onAnalysis()
        DF-->>TV: 更新 S/R lines,<br/>annotations

        WS_S-->>WS_C: order status<br/>{type:"order", data:{...}}
        WS_C-->>App: 更新订单状态 UI

        WS_S-->>WS_C: cycle analysis<br/>{type:"cycle", data:{...}}
        WS_C-->>TV: 添加 cycle annotations
    end
```

### Chart 滚动触发历史数据加载

```mermaid
sequenceDiagram
    participant User as 用户
    participant TV as TradingView<br/>Widget
    participant DF as datafeed.js
    participant SRV as Server

    User->>TV: 向左滚动 chart<br/>(查看更早数据)

    TV->>DF: getBars(symbol, resolution,<br/>{from: earlier_ts, to: current_oldest})
    Note right of DF: TradingView 自动请求<br/>更早时间段的数据

    DF->>SRV: GET /api/history?<br/>from=earlier_ts&to=current_oldest

    Note over SRV: 触发 Left Gap 检测<br/>→ 可能触发 IB fetch

    SRV-->>DF: older bars
    DF-->>TV: append older bars
    Note right of TV: Chart 无缝显示<br/>更多历史数据
```

### WebSocket 消息类型汇总

| 消息类型 | 方向 | 格式 | 说明 |
|---------|------|------|------|
| `snapshot` | Server → Client | `{type:"snapshot", bars:[...], analysis:{...}}` | 连接时推送 200 bars + 分析 |
| `bar` | Server → Client | `{type:"bar", data:{t,o,h,l,c,v}}` | 实时 bar 更新 (≤250ms throttle) |
| `analysis` | Server → Client | `{type:"analysis", data:{sr_levels, ...}}` | S/R 分析更新 |
| `order` | Server → Client | `{type:"order", data:{status, ...}}` | 订单状态变更 |
| `cycle` | Server → Client | `{type:"cycle", data:{annotations, ...}}` | Market cycle 分析标注 |

---

## 附录: 数据库 Schema

```sql
-- SQLite WAL mode
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS bars (
    symbol    TEXT    NOT NULL,
    timeframe TEXT    NOT NULL,
    ts        INTEGER NOT NULL,  -- Unix epoch seconds
    open      REAL    NOT NULL,
    high      REAL    NOT NULL,
    low       REAL    NOT NULL,
    close     REAL    NOT NULL,
    volume    INTEGER DEFAULT 0,
    source    TEXT    DEFAULT 'unknown',
    PRIMARY KEY (symbol, timeframe, ts)
);

-- Source values:
--   'ib_historical'  — fetched via IB reqHistoricalData
--   'realtime'       — assembled from live market ticks
--   'synthetic'      — generated test data (GBM model)
--   'ib_validated'   — corrected by data_validator.py
--   'unknown'        — legacy/imported data
```
