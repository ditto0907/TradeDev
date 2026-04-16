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
- **Data Valid**: 数据校验与维护

### 🔍 数据校验与维护 (Data Validation & Maintenance)
- **Data Query**: 按品种/时间框架/数据源/时间范围灵活查询K线数据，支持分页
- **Data Validate**: 对比 DB 数据与 IB 历史数据，检测差异并支持选择性修复
- **Integrity** (新增):
  - 数据完整性报告（数据源分布、OHLCV 完整性违规、重复检查）
  - 覆盖范围检查（所有品种/时间框架的数据范围和条数）
  - 交易日历感知的缺口检测（精确区分数据缺口 vs 周末/假日/维护停机）
  - 一键修复 OHLCV 违规（high/low 互换、无效价格删除）

### 🏗 数据架构 (Data Architecture)
- **交易日历 (Trading Calendar)**: 按交易所定义的交易时段（CME/COMEX: 美东时区, OSE: 日本时区），支持假日日历
- **数据入库验证**: 所有K线在入库前自动校验 OHLCV 完整性（high ≥ low, 价格 > 0, volume ≥ 0）
- **统一多品种架构**: 所有品种使用相同的 tick 处理和数据管理路径，无品种特定的定制逻辑
- **分层数据验证**: DB 完整性 + IB 对比校验 + 日历完整性检查

### ⚡ 启动优化
- 主品种(MES)数据同步在启动时完成
- 额外品种(MNQ/NK225MC/MGC)的历史数据同步改为后台异步加载，不阻塞服务启动
- 本地 DB 缓存增量同步，仅拉取缺失的新K线

### 📊 表格功能
- 所有底部面板表格支持列排序（点击表头切换升序/降序）
- 所有底部面板表格支持列筛选（表头内置过滤输入框）
- 表格列宽可拖拽调整

## 技术栈
- **后端**: Python / FastAPI / uvicorn
- **前端**: TradingView Charting Library / 原生 JS
- **行情**: Interactive Brokers TWS (ib_insync)
- **数据库**: SQLite
- **数据同步**: Google Sheets (可选)

## 项目结构
```
priceaction/
├── server.py            # FastAPI 主服务
├── config.py            # 配置（IB连接、品种、参数）
├── db.py                # SQLite 数据层（含入库验证）
├── ib_data_fetcher.py   # IB 历史数据拉取（统一多品种架构）
├── trading_calendar.py  # 交易日历（按交易所的交易时段与假日）
├── data_validator.py    # 数据校验与修复（三层验证）
├── analyzer.py          # 价格行为分析（S/R检测）
├── market_holidays.py   # 市场假日日历
├── refactor.md          # 数据架构重构设计文档
├── static/
│   ├── index.html       # 前端页面
│   ├── app.js           # 前端逻辑
│   ├── datafeed.js      # TradingView Datafeed 适配
│   └── datavalid.html   # 数据校验与维护页面
└── data/                # 交易日志 CSV 文件
```

## 最近更新

### 数据架构重构 (2026-04)
1. **交易日历 (Trading Calendar)**: 新增 `trading_calendar.py`，按交易所定义交易时段，支持 CME/COMEX (美东时区) 和 OSE (日本时区)，内置假日日历
2. **数据入库验证**: `db.insert_bars()` 和 `db.insert_ib_cache_bars()` 现在会自动校验 OHLCV 完整性，无效数据会被记录并跳过
3. **日历感知缺口检测**: `db.find_gaps()` 和 `server.py get_history()` 使用 TradingCalendar 精确分类缺口，不再依赖硬编码的时区/小时启发式规则
4. **统一多品种架构**: `ib_data_fetcher.py` 的 tick 处理已统一为单一处理路径，移除了所有 MES 专用代码
5. **三层数据验证**: `data_validator.py` 现在执行 IB 对比 + OHLCV 完整性 + 日历完整性三层验证
6. **数据维护工具**: 新增 Integrity 标签页和 7 个新 API 端点，支持覆盖范围检查、日历缺口检测、OHLCV 修复等
7. **详细设计文档**: 参见 `priceaction/refactor.md`

### 功能迭代 (2026-04)
1. **启动优化**: 额外品种数据加载改为后台异步执行，主服务启动不再被阻塞
2. **Data Valid 面板**: 新增数据校验前端功能
   - 支持按品种/时间框架/时间区间检查K线连续性（缺口检测）
   - 支持按 source 字段查询非 IB 拉取的K线（如 realtime 自动组装）
3. **Strategy 定位改进**: 策略交易 Locate 改为与 Trade History 一致的 setVisibleRange 方式（entry→exit 范围 + 30min padding）
4. **表格排序筛选**: 所有底部面板表格新增列排序（点击表头）和列筛选（表头过滤输入框）功能
