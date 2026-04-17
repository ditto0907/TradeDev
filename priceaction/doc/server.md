# `server.py` — 模块走读

> 路径：`priceaction/server.py`（~2193 行）
> 角色：FastAPI 服务入口。对外提供 TradingView UDF / WebSocket / 订单 / 数据校验 / 回测 / 策略分析 等全部 HTTP 接口；对内编排 `ib_data_fetcher` + `db` + `data_validator` + `order_manager` + `price_action_analyzer` 等模块的生命周期。

---

## 1. 系统架构：编排层的位置

```
       ┌─────────────────────────────────────────────────────────┐
       │  前端（TradingView Advanced Charts / Data Validation UI）│
       └────────────▲───────────────────▲──────────────────▲─────┘
                    │ HTTP /api/*       │ WebSocket        │ 静态资源
                    │ (UDF / 自定义)    │ /ws/realtime     │ /static, /charting_library
                    │                   │                  │
       ┌────────────┴───────────────────┴──────────────────┴─────┐
       │                      server.py (FastAPI)                  │
       │  ───────────────────────────────────────────────────────  │
       │  · lifespan()          启动/停止编排                        │
       │  · on_new_bar()        tick/bar 回调（持久化+广播+分析）    │
       │  · _ib_background_init / _ib_reconnect_loop                │
       │  · _fill_internal_gaps / _prefetch_extra_symbols           │
       │  · 全部 REST endpoints（50+ 个）                           │
       │  · /ws/realtime WebSocket                                 │
       │                                                           │
       │  全局实例：                                                 │
       │    fetcher  = IBDataFetcher()                             │
       │    sheets   = GoogleSheetsSync()                          │
       │    analyzer = PriceActionAnalyzer()                       │
       │    _order_mgr: IBOrderManager  (IB 连通后创建)             │
       │    _ws_clients: List[WebSocket]                           │
       │    _prev_completed_bar: {(sym, tf): bar}                  │
       │    _ib_fetch_cooldown:  {key: expiry_ts}                  │
       └────────────┬───────────────────┬──────────────────────────┘
                    │                   │
         ┌──────────┴─────────┐  ┌──────┴──────────┐  ┌──────────────┐
         │ ib_data_fetcher.py │  │ data_validator  │  │ order_manager │
         └──────────┬─────────┘  └──────┬──────────┘  └──────┬───────┘
                    │                   │                     │
                    └───────────┬───────┴─────────────────────┘
                                ▼
                      ┌──────────────────┐      ┌──────────────────┐
                      │  db.py (SQLite)  │      │  IB TWS / Gateway │
                      │  WAL + 连接池     │      │  (ib_insync)      │
                      └──────────────────┘      └──────────────────┘
```

关键概念：**server.py 是"粘合层"**——它不写业务逻辑，而是把各模块按生命周期 / 请求路径串起来。

---

## 2. 生命周期（`lifespan` 上下文）

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── 启动（前端已可访问，后台并行做重活） ──────────────────

    # Step 1  init DB + 从 bars 表加载所有 symbol 5min 最近 MAX_BARS_IN_MEMORY 根到 fetcher._symbol_bars
    db.init_db()
    for sym in [MES] + config.EXTRA_SYMBOLS:
        fetcher._symbol_bars[sym]["5min"] = db.get_bars(sym, "5min")[-MAX_BARS_IN_MEMORY:]
    fetcher._sync_legacy_bars()

    # Step 2  崩溃恢复：从 realtime_bars 表把未收盘 bar 回填到 _rt_current / _prev_completed_bar
    for rt in db.get_all_realtime_bars():
        if rt.time == current_bar_ts:
            _prev_completed_bar[(sym, tf)] = rt
            fetcher._rt_current[f"{sym}:{tf}"] = rt

    # Step 3  初始 price-action 分析（纯 DB 数据，不依赖 IB）
    _latest_analysis = analyzer.get_analysis(fetcher.get_bars("5min"))

    # Step 4  后台任务：IB 连接 + 历史增量 + 实时订阅
    _ib_init_task      = asyncio.create_task(_ib_background_init())

    # Step 5  后台任务：IB 断线重连（每 60s 检查）
    _ib_reconnect_task = asyncio.create_task(_ib_reconnect_loop())

    # Step 6  后台任务：等 IB 稳定 30s 后跑 data_validator.background_validate
    _bg_validate_task  = asyncio.create_task(_bg_validate_after_init())

    logger.info("Server ready (DB-only mode)")    # <-- ~100ms 后 FastAPI 就接客了
    yield

    # ── 关闭 ─────────────────────────────────────────────────
    _ib_reconnect_task.cancel(); _bg_validate_task.cancel()
    sheets.flush_buffer()
    fetcher.unsubscribe_realtime()
    fetcher.disconnect()
```

### 启动的分层设计（非常重要）

**快路径（同步 ~100ms）** — 只做 DB 读 + 内存恢复：
- 不等 IB → 即便 TWS 离线，服务也能立刻响应 `/api/history`（从 DB 回答）；
- `realtime_bars` 回填保证了"未收盘 bar"不会因为重启消失。

**慢路径（后台 task）**：
1. `_ib_background_init`：IB connect → qualify 合约 → `load_history(since_5min=db.get_latest_ts)` 增量拉 MES 历史 → `db.insert_bars(source="ib_historical")` → `_fill_internal_gaps(MES, "5min")` → `_prefetch_extra_symbols` → `subscribe_mktdata_all()` → 注册 `on_new_bar` 回调 → `_order_mgr = IBOrderManager(...)` → 订阅 `orderStatusEvent`。
2. `_ib_reconnect_loop`：每 60s 检查 `fetcher.ib.isConnected()`，断了就走一遍完整恢复（clean up → connect → load_history → subscribe → order_mgr 重建）；**所有失败路径都走这里**，意味着 server 可以在 IB 完全离线启动，等 TWS 上线后自动接入。
3. `_bg_validate_after_init`：等 `_ib_init_task` 完成 + 30s → 调 `data_validator.background_validate(fetcher=fetcher)`。用的是 fetcher 已经建立的 IB 连接，不会重新起 client。

---

## 3. 实时数据回调 `on_new_bar`

`fetcher.add_new_bar_callback(on_new_bar)` 注册。每个 tick 和每根完结 bar 都会触发（unified tick handler 在节流后调用一次）。做 4 件事：

```python
def on_new_bar(bar_size_key, bar, symbol=None):
    prev = _prev_completed_bar.get((symbol, bar_size_key))
    # ① 上一根"刚收盘的"写进 bars 表（source="realtime_completed"）
    if prev and bar.time > prev.time:
        db.insert_bars(symbol, bar_size_key, [prev], source="realtime_completed")
        if symbol == "MES":
            sheets.buffer_bar(bar_size_key, prev)   # Google Sheets 缓冲
    _prev_completed_bar[(symbol, bar_size_key)] = dict(bar)

    # ② 当前 in-progress bar 写 realtime_bars 表（崩溃恢复用）
    db.upsert_realtime_bar(symbol, bar_size_key, bar)

    # ③ 5min 新 bar 触发重新分析（只 MES）
    if symbol == "MES" and bar_size_key == "5min" and bar.time > _last_analysis_bar_ts:
        _latest_analysis = analyzer.get_analysis(fetcher.get_bars("5min"))

    # ④ WebSocket 广播 bar + (若有) analysis
    asyncio.create_task(broadcast({"type": "bar", "bar": bar, "symbol": symbol}))
    if analysis_updated:
        asyncio.create_task(broadcast({"type": "analysis", "data": _latest_analysis}))
```

**数据源优先级** —— `db.insert_bars` 按 `source` 有冲突覆盖策略：

| source | 优先级 | 场景 |
|---|---|---|
| `ib_validated` | 最高 | `data_validator.fix_bars` 写入 |
| `ib_historical` | 高 | `load_history` / `fetch_range` 写入 |
| `realtime_completed` | 中 | `on_new_bar` 把完结 bar 写入 |
| `unknown` | 低 | 兜底 |

**为什么 realtime 完结 bar 要立刻入 `bars`？** 如果只在 `realtime_bars` 里，下一次 `/api/history` 查询时 DB 会有一个 5min 的"近端缺口"，触发 IB 冷启动拉取。先落一份低优先级临时 bar，能让查询直接返回；当后续 IB 历史拉取到达时，高优先级 `ib_historical` 覆盖它，最终收敛到权威数据。

---

## 4. `/api/history` 的完整组装流程（~315 行，本 server 最核心接口）

> 这个端点集成了 **L2(DB) + L1(fetcher 内存) + L3(IB 实时拉取) + _prev_completed_bar(实时柱子)** 四层数据，并且用 `trading_calendar` 做休市识别。与 `ib_data_fetcher.md` 里的请求流程一一对应。

```
GET /api/history?symbol=&resolution=&from=&to=&countback=

Step 1  DB 查询
   bars = db.get_bars(sym, tf, from, to)
   earliest_db, latest_db = db.get_earliest_ts / get_latest_ts

Step 2  诊断缺口，生成 fetch_ranges[]
   Case 1 earliest_db 为空                 → fetch [from, now]
   Case 2 from < earliest_db (左缺口)       → fetch [from, earliest_db]
   Case 3 latest_db 落后请求末端 > 2*interval → fetch [latest_db, min(to,now)]
   Case 4 bars 为空但在 DB 覆盖内 (中间空洞)  → fetch [from, min(to,now)]
   
   每种情形都有独立冷却键：
     _ib_fetch_cooldown[(sym, tf)]                  右缺口
     _ib_fetch_cooldown[f"left_{sym}_{tf}"]         左缺口
     _ib_fetch_cooldown[f"mid_{sym}_{tf}_{from}"]   中间空洞
     _ib_fetch_cooldown[f"internal_{sym}_{tf}"]     内部空洞

Step 3  循环 fetch_ranges
   for (f, t) in fetch_ranges:
     fetched = await fetcher.fetch_range(tf, f, t, symbol=sym)
     if fetched:
         db.insert_bars(sym, tf, fetched, source="ib_historical")
         any_fetched = True
         冷却键 pop
     elif 返回 0 bars:
         冷却键 = now + 300s  (_IB_COOLDOWN_NO_DATA)
     except:
         冷却键 = now + 60s   (_IB_COOLDOWN_ERROR)
   
   if any_fetched:
       bars = db.get_bars(...)   重新查

Step 4  内部空洞（calendar-aware）
   gaps = trading_calendar.find_gaps(bars, interval) filter gap_type=="data_gap"
   for (g_from, g_to) in gaps:
     按 7 天切片循环 fetch_range + insert_bars
   if filled_any:
       bars = db.get_bars(...)   重新查

Step 5  剥离"无法修补的大缺口"之前的数据
   遍历 bars，若 i 到 i+1 gap ≥ max(interval*8, 4h) 且非 weekend/holiday/maintenance：
       last_big_gap_idx = i
   bars = bars[last_big_gap_idx:]   # 只保留最后一段连续段

Step 6  兜底 & 实时 bar 拼接
   if not bars and sym == "MES":
       bars = fetcher.get_bars(tf, from, to)   # L1 兜底

   if countback:
       bars = bars[-max(countback, countback*4)-1:]

   rt_bar = _prev_completed_bar[(sym, tf)]
   if rt_bar in [from, to]:
       替换 bars[-1] 或 append

返回 UDF 列格式：{"s":"ok","t":[],"o":[],"h":[],"l":[],"c":[],"v":[]}
         或   {"s":"no_data","nextTime":db.get_latest_ts_before(from)}
```

### 冷却机制为什么这么细？
4 类缺口独立冷却，是为了**不同失败原因互不阻塞**：
- 右缺口是常见"市场已关"的场景，给 5 分钟；
- 左缺口是"合约太老，IB 无源"，也是 5 分钟；
- 中间空洞是"真实数据丢失但不在继续扩展的那头"，单独按起始 ts 分键；
- 内部空洞是前后都有数据但中间断了，填一次不成功也放 5 分钟。

如果只用一个全局锁，右缺口失败会把左缺口也锁住，前端滚动历史就会卡。

---

## 5. API 端点分布

按功能分区：

| 分区 | 路由 | 说明 |
|---|---|---|
| 静态页 | `GET /`、`GET /datavalid` | TradingView / 数据校验 UI |
| TradingView UDF | `GET /api/config`、`/api/symbols`、`/api/history`、`/api/time` | 标准 UDF 协议 |
| Watchlist | `GET /api/watchlist_prices` | 多标的最新价 + 日涨幅 |
| 价格分析 | `GET /api/analysis`、`GET /api/skill/bars`、`POST /api/skill/analysis`、`GET /api/skill/analyses` | MCP 分析 + 人工标注 |
| 数据校验 | `GET /api/data/validate`、`POST /api/data/fix`、`POST /api/data/validate_all`、`GET /api/data/validated_ranges`、`POST /api/data/bg_validate` | 对接 `data_validator.py` |
| 数据运维 | `GET /api/data/{gaps,bars_by_source,integrity,coverage,bar,query,calendar_gaps}`、`POST /api/data/{delete_by_source,delete_range,delete_bars,fix_ohlcv}` | DB 运维工具 |
| 交易 | `POST /api/order`、`POST /api/order/bracket`、`GET /api/orders`、`POST /api/flatten`、`GET /api/position` | `IBOrderManager` 封装 |
| Chart 持久化 | `{GET,POST,GET} /api/charts[/{id}]`、`study_templates`、`drawing_templates`、`chart_templates` | TradingView save/load |
| 交易日志 | `GET /api/trades`、`/api/trades/files`、`/api/trades/file/{filename}`、`POST /api/trades/upload` | 人工交易记录导入 |
| 回测 | `POST /api/strategy/backtest`、`GET /api/strategy/backtests`、`/api/strategy/backtests/{id}/trades` | `strategy_backtest.py` |
| 实时 | `WS /ws/realtime` | 前端首连推 snapshot（最近 200 bar + analysis），之后广播 `bar` / `analysis` / `order_update` |

---

## 6. WebSocket `/ws/realtime`

```python
@app.websocket("/ws/realtime")
async def websocket_endpoint(websocket):
    await websocket.accept()
    _ws_clients.append(websocket)

    # 首连推 snapshot
    await websocket.send_text({"type":"snapshot",
                               "bars_5min": fetcher.get_bars("5min")[-200:],
                               "analysis": _latest_analysis})
    try:
        while True:
            await websocket.receive_text()   # 只做 keepalive
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.remove(websocket)
```

广播由 `broadcast(message)` 逐客户端发；发送失败的 WS 直接从 `_ws_clients` 删除（没有重试）。这是一个**典型的 fan-out 模型**，业界常见实现（pub/sub 如果跨进程，会把 `_ws_clients` 替换为 Redis pub/sub channel）。

广播消息类型：`bar`（新/更新的 bar）、`analysis`（price-action 分析结果）、`order_update`（订单状态变化）、`analysis_annotation_*`（人工标注事件）。

---

## 7. 中间件 & 其他基础设施

### 日志
- 控制台 + `priceaction/log/server.log` 每小时轮转，保留 7 天（168 份）。
- 环境变量 `DATAFEED_DEBUG=1` 打开 `datafeed_debug_middleware`：记录 /api/* 的方法、路径、状态、耗时、响应 body 摘要（list > 6 会压缩为 `[first…last](n)`）。layout 类大 payload 路径自动跳过 body 记录。

### 静态
- `/charting_library` → TradingView 库
- `/static` → 自定义前端资源（validation UI 等）

### 线程/事件循环
- 整个 server 是单进程 asyncio；`uvicorn` 默认单 worker；所有 IB、DB、WS、HTTP 共享同一 event loop。
- `ib_insync` 要求和你的 asyncio loop 绑定——启动顺序是 FastAPI loop 就绪 → `fetcher.connect()` 在后台 task 里 `asyncio.set_event_loop(asyncio.get_running_loop())`。

---

## 8. 关键集成点 & 设计权衡

1. **启动快 vs IB 慢的解耦** — lifespan 把 DB 恢复和 IB 连接分开；即使 IB 不通也能启动响应历史请求。前端不会因 TWS 离线而白屏。

2. **4 类 cooldown** — `_ib_fetch_cooldown` 按缺口类型分键；避免一类故障阻塞所有数据刷新路径。

3. **`_prev_completed_bar` vs `fetcher._rt_current`** — 两者都持有"最后一根 bar"，但语义不同：
   - `fetcher._rt_current[f"{sym}:{tf}"]` 由 tick 实时更新，可能在被写入 `bars` 前被覆盖；
   - `_prev_completed_bar[(sym, tf)]` 由 `on_new_bar` 维护，用于：(a) 检测"上一根刚收盘"、(b) `/api/history` 尾部拼接 in-progress bar。
   重启时两处都从 `realtime_bars` 表回填。

4. **3 个后台 task 的解耦** — init / reconnect / validate 独立运行；reconnect 失败不影响已跑起来的 validate（拿到的是 `fetcher=None` 分支，只走 DB）。

5. **DB 是"唯一权威数据源"** — 所有 `/api/history` 查询都最终命中 DB；L1 只作为极端场景（sym=MES 且 DB 空）的兜底。这避免了"前端看到的和 DB 里的不一致"的常见 bug。

6. **实时 bar 走单独表** — `realtime_bars` 独立于 `bars`，不会污染历史查询；同时提供 1-row-per-(sym,tf) upsert，性能足以支撑每 tick 一次写。

7. **没有消息队列** — WS 广播 + DB 写都在同一 loop；对于单机 / 1 个 TWS / 少量 WS client 足够。如要扩规模，自然演进路径是把广播改成 Redis pub/sub、DB 写改成异步队列，或把实时/历史/交易拆为独立微服务。

---

## 9. 常见路径排查表

| 症状 | 看哪里 |
|---|---|
| 前端图看不到最新 bar | `on_new_bar` 有无触发；`fetcher._ib_ready`；`_ws_clients` 列表；tick 节流 `_TICK_BROADCAST_INTERVAL` |
| `/api/history` 返回 no_data | DB 是否真的没数据（`db.get_bars` 直查）；`_ib_fetch_cooldown` 是否锁住；IB 是否 connected |
| 重启后缺最后半根柱子 | `realtime_bars` 表是否写入；lifespan Step 2 的 `_current_bar_ts` 判断 |
| 换月后出现价格跳变 | `contract_month` 列是否被 `fetch_range` 正确标注；验证器是否跑过 |
| IB 频繁打满 pacing | 各冷却键是否过早被清；`fetch_range` 的 7 天 chunk 是否生效 |
| 后台 validate 不跑 | `_bg_validate_after_init` 是否到达；`validated_ranges` 是否记录；日志 `[BG VALIDATE]` |

---

## 附：关键函数速查

| 函数/对象 | 作用 |
|---|---|
| `lifespan(app)` | 启动/停止编排：DB init + realtime 恢复 + 3 个后台 task |
| `broadcast(message)` | WS 广播（死连接自动剔除） |
| `on_new_bar(tf, bar, symbol)` | tick/bar 回调：完结 bar 落 `bars`、in-progress 落 `realtime_bars`、触发分析 + WS |
| `_fill_internal_gaps(sym, tf, fetcher)` | 扫 DB 内部空洞，用 `fetcher.fetch_range` 补，跳过 weekend/holiday/maintenance |
| `_prefetch_extra_symbols(fetcher, ib_ok)` | 按 `config.EXTRA_SYMBOLS` 后台拉历史（非阻塞） |
| `_ib_background_init()` | 启动后台：IB connect → 历史 → 实时订阅 → 订单管理器 → Google Sheets |
| `_ib_reconnect_loop()` | 每 60s 检查并重连 IB；恢复所有订阅 |
| `get_history(symbol, resolution, from, to, countback)` | **本 server 核心端点**——组装 DB + IB fetch + calendar + realtime bar |
| `websocket_endpoint(ws)` | 首连推 snapshot + 加入 `_ws_clients` |

