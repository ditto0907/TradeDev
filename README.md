# TradeDev — 期货交易终端

一个基于 Web 的期货交易终端，集成 Interactive Brokers (IB) TWS，支持实时行情、下单交易、策略回测和市场分析。

## 功能概览

### 📊 图表与行情
- TradingView 高级图表（支持多品种：MES、MNQ、NK225MC、MGC）
- 实时 WebSocket 价格推送（5min K线）
- 自定义指标（S-Bar Count）
- 支持/阻力位自动检测与图表标注
- 市场周期分析（Market Cycle Analysis）
- 图表布局持久化（自动保存/加载）

### 💹 交易功能
- 右键快捷下单（限价/止损/市价单）
- 一键 Bracket Order（带止盈止损）
- 图表拖拽改单
- 实时持仓监控
- 工作订单管理
- 一键平仓

### 📋 底部面板 (Bottom Tabs)
- **Positions**: 持仓展示
- **Working Orders**: 活跃订单
- **Filled Orders**: 成交订单
- **Order History**: 订单历史
- **Trade History**: 交易日志（支持 CSV 导入，图表定位）
- **Analysis Log**: 市场周期分析记录
- **Strategy**: IBS 策略回测
- **Data Ops**: 数据运维（探查、校验、修复）

### 🔧 数据运维 (Data Operations)
- **数据探查**：按品种、时间框架、时间范围、数据源条件查询K线数据
- **单点修复**：从 IB 重新拉取并替换单条 bar，手动编辑，删除异常数据
- **批量校验**：对比 DB 数据与 IB 历史数据，生成差异报告和缺口报告
- **批量修复**：一键修复不匹配的 bar（用 IB 数据替换），一键填充数据缺口
- **数据质量报告**：连续性评分、价格一致性评分、成交量一致性评分
- **修复审计日志**：所有修复操作记录审计日志

### ⚡ 数据架构 (v2 — 重构后)
- **DB 为唯一数据源**：`/api/history` 纯 DB 读取，无 IB 等待，P99 < 100ms
- **后台数据同步**：BarManager 持续后台检测和填充数据缺口
- **写入前校验**：所有写入 DB 的数据经过时间戳对齐、OHLC 一致性、价格合理性校验
- **统一交易日历**：TradingCalendar 统一管理 session、假日、维护窗口，消除 gap 误判
- **多品种通用**：所有逻辑以 (symbol, timeframe) 为 key，品种差异通过配置驱动

### 📊 表格功能
- 所有底部面板表格支持列排序（点击表头切换升序/降序）
- 所有底部面板表格支持列筛选（表头内置过滤输入框）
- 表格列宽可拖拽调整

## 技术栈
- **后端**: Python / FastAPI / uvicorn
- **前端**: TradingView Charting Library / 原生 JS
- **行情**: Interactive Brokers TWS (ib_insync)
- **数据库**: SQLite (WAL mode)
- **数据同步**: Google Sheets (可选)

## 项目结构
```
priceaction/
├── server.py               # FastAPI 主服务（API 层，纯读取 + WebSocket）
├── bar_manager.py          # 🆕 统一 Bar 数据管理器（获取/组装/校验/存储）
├── trading_calendar.py     # 🆕 统一交易日历（session/假日/维护窗口/gap分类）
├── data_ops.py             # 🆕 数据运维工具（探查/单点修复/批量校验/批量修复）
├── config.py               # 配置（IB连接、品种、阈值参数）
├── db.py                   # SQLite 纯数据存取层（CRUD + 索引）
├── ib_data_fetcher.py      # IB API 纯封装（历史数据拉取/实时 tick 订阅）
├── data_validator.py       # 数据校验（IB 对比校验，集成到 DataOps）
├── price_action_analyzer.py # 价格行为分析（S/R 检测、Market Cycle）
├── market_holidays.py      # 美国市场假日（→ 迁移到 trading_calendar.py）
├── static/
│   ├── index.html          # 前端页面（含 Data Ops 面板）
│   ├── app.js              # 前端逻辑
│   └── datafeed.js         # TradingView Datafeed 适配（简化版）
├── data/                   # 交易日志 CSV 文件
└── refactor.md             # 🆕 K线数据重构方案文档
```

## 数据流架构 (v2)

```
IB TWS ──→ IBDataFetcher (pure API) ──→ BarManager ──→ DB (bars table)
                                            │                  │
                                            │ RT bar assembly  │
                                            ▼                  ▼
                                      WebSocket push    /api/history (pure read)
                                            │                  │
                                            ▼                  ▼
                                      TradingView Chart (datafeed.js)

Background: BarManager ──→ gap detect ──→ IB fetch ──→ validate ──→ DB
Data Ops:   DataOps API ──→ query/validate/fix ──→ DB + IB
```

**关键设计原则：**
1. DB 是唯一数据源（Single Source of Truth）
2. API 层纯读取，无副作用（数据补全是后台任务）
3. 写入前校验（时间戳对齐、OHLC 一致性）
4. 多品种通用（配置驱动，无品种硬编码）

详细重构方案请参考 [refactor.md](priceaction/refactor.md)

## 最近更新

### 数据架构重构 (2026-04)
1. **数据层重构**：新增 BarManager 统一管理 bar 数据的获取、组装、校验、存储
2. **统一交易日历**：TradingCalendar 合并 session/假日/维护窗口逻辑，消除 gap 误判
3. **API 纯读取**：`/api/history` 去除 IB 同步拉取，改为纯 DB 查询，响应时间 P99 < 100ms
4. **数据运维工具**：新增 Data Ops 面板（探查、单点修复、批量校验、批量修复、质量报告）
5. **写入前校验**：所有 bar 写入 DB 前经过时间戳对齐、OHLC 一致性、价格合理性校验
6. **修复审计日志**：所有数据修复操作记录到 `data_fix_log` 表

### 功能迭代 (2026-04 早期)
1. **启动优化**: 额外品种数据加载改为后台异步执行，主服务启动不再被阻塞
2. **Data Valid 面板**: 数据校验前端功能（已升级为 Data Ops）
3. **Strategy 定位改进**: 策略交易 Locate 改为与 Trade History 一致的 setVisibleRange 方式
4. **表格排序筛选**: 所有底部面板表格新增列排序和列筛选功能
