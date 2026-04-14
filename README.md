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
- **Data Valid**: 数据校验
  - K线连续性检查（检测非休市时间的数据缺口）
  - 按数据源查询（区分 IB 拉取 vs realtime 自动组装的K线）

### 🔍 数据校验 (Data Validation)
- 支持选择品种、时间框架、时间周期检查K线连续性
- 自动识别工作日缺口（⚠ 异常）、周末缺口、假日缺口
- 按 source 字段查询数据库中的K线来源（realtime / ib_historical / ib_validated / synthetic）
- 支持对比 DB 数据与 IB 历史数据，检测并修复差异

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
├── server.py           # FastAPI 主服务
├── config.py           # 配置（IB连接、品种、参数）
├── db.py               # SQLite 数据层
├── ib_data_fetcher.py  # IB 历史数据拉取
├── data_validator.py   # 数据校验与修复
├── analyzer.py         # 价格行为分析（S/R检测）
├── market_holidays.py  # 美国市场假日
├── static/
│   ├── index.html      # 前端页面
│   ├── app.js          # 前端逻辑
│   └── datafeed.js     # TradingView Datafeed 适配
└── data/               # 交易日志 CSV 文件
```

## 最近更新

### 功能迭代 (2026-04)
1. **启动优化**: 额外品种数据加载改为后台异步执行，主服务启动不再被阻塞
2. **Data Valid 面板**: 新增数据校验前端功能
   - 支持按品种/时间框架/时间区间检查K线连续性（缺口检测）
   - 支持按 source 字段查询非 IB 拉取的K线（如 realtime 自动组装）
3. **Strategy 定位改进**: 策略交易 Locate 改为与 Trade History 一致的 setVisibleRange 方式（entry→exit 范围 + 30min padding）
4. **表格排序筛选**: 所有底部面板表格新增列排序（点击表头）和列筛选（表头过滤输入框）功能
