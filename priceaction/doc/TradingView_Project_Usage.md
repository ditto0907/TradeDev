# TradingView Charting Library 在本项目中的使用总结

## 项目配置

### 基本设置
```javascript
_widget = new TradingView.widget({
  container:    'tv-chart',               // HTML 容器 ID
  datafeed:     datafeed,                 // 自定义 Datafeed (见 datafeed.js)
  symbol:       'MES',                    // 默认品种
  interval:     '5',                      // 默认周期: 5分钟
  library_path: '/charting_library/',     // 库文件路径
  locale:       'en',                     // 语言
  timezone:     'America/New_York',       // 默认时区
  theme:        'dark',                   // 主题
  load_last_chart: true,                  // 加载上次保存的布局
  save_load_adapter: createSaveLoadAdapter(), // 自定义布局保存适配器
  autosize: true,                         // 自适应容器大小
});
```

### 启用的功能 (enabled_features)
- `use_localstorage_for_settings` — 使用 localStorage 保存设置
- `move_logo_to_main_pane` — 移动 TradingView logo 到主图
- `header_saveload` — 工具栏显示保存/加载布局按钮
- `show_exchange_logos` — 显示交易所 logo
- `pre_post_market_sessions` — 盘前盘后时段显示

### 禁用的功能 (disabled_features)
- `header_symbol_search` — 隐藏工具栏的品种搜索（使用自定义 Watchlist）
- `header_compare` — 禁用品种对比功能
- `display_market_status` — 隐藏市场状态指示器
- `create_volume_indicator_by_default` — 默认不创建成交量指标

### 样式覆盖 (overrides)
```javascript
overrides: {
  'paneProperties.background': '#131722',               // 背景色
  'paneProperties.backgroundType': 'solid',            // 纯色背景
  'paneProperties.vertGridProperties.color': '#1e222d', // 垂直网格线颜色
  'paneProperties.horzGridProperties.color': '#1e222d', // 水平网格线颜色
  'scalesProperties.textColor': '#787b86',             // 坐标轴文字颜色
}
```

---

## 自定义指标

### S-Bar Count (K线计数器)

**功能**: 在每根 K 线下方显示序号，用于 Al Brooks Price Action 分析

**配置**:
```javascript
custom_indicators_getter: function (PineJS) {
  return Promise.resolve([{
    name: 'S-Bar Count',
    metainfo: {
      id: 'SBarCount@tv-basicstudies-1',
      description: 'S-Bar Count',
      is_price_study: false,   // 独立窗口显示
      format: { type: 'price', precision: 0 },
      inputs: [
        { id: 'displayEvery', name: 'Display every X bars', type: 'integer', defval: 3 }
      ],
      // ...
    },
    constructor: function() {
      // 实现逻辑: 检测新一天，重置计数器
    }
  }]);
}
```

**逻辑**:
- **日线图**: 显示月份中的天数 (dayofmonth)
- **日内图**: 
  - 检测 `dayofweek` 变化判断新一天
  - 新一天时重置计数为 1
  - 在第 1 根 K 线和每隔 N 根 K 线显示计数
  - 其他 K 线返回 NaN (不显示)

**显示效果**:
- 柱状图 (columns)
- 颜色: 半透明深色 `rgba(20, 0, 0, 0.30)`
- baseline 为 0

---

## 实时数据集成

### Datafeed 实现
见 [priceaction/static/datafeed.js](../static/datafeed.js)

**核心方法**:
- `resolveSymbol()` — 品种信息解析
- `getBars()` — 历史 K 线数据获取
- `subscribeBars()` — 实时数据订阅
- `unsubscribeBars()` — 取消订阅
- `getQuotes()` — 报价数据（Watchlist 用）

**WebSocket 集成**:
```javascript
// app.js 中
wsClient.on('bar_update', (msg) => {
  if (msg.symbol && msg.timeframe) {
    datafeed.updateBar(msg.symbol, msg.timeframe, msg.bar);
  }
});
```

**数据来源**:
- 历史数据: `/api/history` (FastAPI)
- 实时更新: WebSocket (`/ws`) → `bar_update` 消息

---

## 使用的 API

### 图表操作
```javascript
const chart = _widget.activeChart();

// 1. 获取当前品种
const symbol = chart.symbol();

// 2. 获取当前周期
const resolution = chart.resolution();

// 3. 切换品种
_widget.setSymbol(token, resolution, callback);

// 4. 切换周期
chart.setResolution('1D', callback);

// 5. 十字光标跟踪
chart.crossHairMoved(({ price, time }) => {
  window._chartCursorPrice = price;
});
```

### 时区管理
```javascript
const tzApi = chart.getTimezoneApi();

// 获取当前时区
const tz = tzApi.getTimezone();

// 设置时区
tzApi.setTimezone('America/Chicago');

// 监听时区变化
tzApi.onTimezoneChanged().subscribe(null, (tz) => {
  console.log('时区已切换:', tz);
});
```

**双向绑定实现**:
- Chart → AppTZ: 用户从图表 UI 修改时区时更新顶部选择器
- AppTZ → Chart: 用户从顶部选择器修改时区时更新图表
- 使用 `_suppressChartEvent` 标志避免循环触发

### 右键菜单
```javascript
_widget.onContextMenu((unixTime, price) => {
  return [
    {
      position: 'top',
      text: `Buy @ ${price.toFixed(2)}`,
      click: () => placeOrder('buy', price)
    },
    {
      position: 'top',
      text: `Sell @ ${price.toFixed(2)}`,
      click: () => placeOrder('sell', price)
    }
  ];
});
```

### 品种切换回调
```javascript
chart.onSymbolChanged().subscribe(null, () => {
  const newSymbol = chart.symbol();
  
  // 重新加载 S/R 分析
  fetch(`/api/analysis?symbol=${newSymbol}`)
    .then(r => r.json())
    .then(updateAnnotations);
  
  // 重新加载市场周期分析
  loadCycleAnalyses();
});
```

### 保存/加载布局
```javascript
const saveLoadAdapter = {
  getAllCharts: () => {
    // 从 DB 加载所有保存的布局
    return fetch('/api/chart_layouts').then(r => r.json());
  },
  
  removeChart: (id) => {
    return fetch(`/api/chart_layouts/${id}`, { method: 'DELETE' });
  },
  
  saveChart: (chartData) => {
    return fetch('/api/chart_layouts', {
      method: 'POST',
      body: JSON.stringify(chartData)
    });
  },
  
  getChartContent: (id) => {
    return fetch(`/api/chart_layouts/${id}`).then(r => r.json());
  }
};
```

---

## 与后端集成

### FastAPI 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/history` | GET | 获取历史 K 线数据 |
| `/api/symbol_list` | GET | 获取可用品种列表（含连续合约和月份合约） |
| `/api/analysis` | GET | 获取 Al Brooks Price Action 分析结果 |
| `/api/chart_layouts` | GET/POST/DELETE | 图表布局 CRUD |
| `/ws` | WebSocket | 实时数据推送 |

### WebSocket 消息格式

**实时 K 线更新**:
```json
{
  "type": "bar_update",
  "symbol": "MES",
  "timeframe": "5min",
  "bar": {
    "time": 1714636800,
    "open": 5123.25,
    "high": 5125.50,
    "low": 5122.00,
    "close": 5124.75,
    "volume": 1234
  }
}
```

**市场周期分析**:
```json
{
  "type": "cycle_analysis",
  "symbol": "MES",
  "timeframe": "5min",
  "data": {
    "id": 123,
    "summary": "TR → BO (Bull)",
    "annotations": [...]
  }
}
```

---

## 自定义组件集成

### 1. Watchlist (左侧品种列表)
```javascript
// 点击品种时切换图表
document.querySelectorAll('.watch-item').forEach(item => {
  item.addEventListener('click', () => {
    const symbol = item.dataset.symbol;
    _widget.setSymbol(symbol, chart.resolution(), () => {
      loadContractOptions(symbol);  // 刷新合约选择器
      loadCycleAnalyses();          // 刷新分析
    });
  });
});
```

### 2. Contract Selector (品种下拉框)
- 位置: Watchlist 活跃品种下方
- 功能: 切换连续合约 / 月份合约
- 实现: 从 `/api/symbol_list` 获取可用合约，生成 `<select>` 选项

### 3. S/R Legend (阻力支撑图例)
- 位置: 图表左上角浮层
- 功能: 显示关键价位和描述
- 数据来源: `/api/analysis` 返回的 `sr_levels` 数组
- 可拖动: 通过 `#sr-legend-handle` 实现

### 4. Market Cycle Badge (市场周期标签)
- 位置: 图表右上角
- 功能: 显示当前市场周期阶段（TR/BO/BC/Channel/MTR）
- 数据来源: `/api/analysis` 返回的 `market_cycle` 字段

---

## 项目特色功能

### 1. 连续合约支持
- **MES@CONT_FRONT**: 前月合约（无调整）
- **MES@CONT_RATIO**: 比例调整连续合约
- **MES@CONT_DIFF**: 差价调整连续合约
- **MES@202606**: 单一月份合约（2026年6月）

**实现**: 
- 前端通过 `symbol_list` API 获取可用合约
- Datafeed 的 `resolveSymbol` 解析 `@` 后缀
- 后端通过 `continuous_view.py` 拼接月份合约数据

### 2. Al Brooks Price Action 注释
- **Opening Range**: 蓝色矩形标记开盘区间
- **Breakout**: 红色（Bear）/绿色（Bull）标记突破点
- **Trading Range**: 灰色矩形标记盘整区间
- **Measured Move**: 青色线段标记目标位

**数据流程**:
```
WebSocket (bar_update)
  → price_action_analyzer.py (后端分析)
  → /api/analysis (REST API)
  → app.js updateAnnotations() (前端渲染)
  → TradingView createShape() (图表绘制)
```

### 3. 时区同步
- 顶部时区选择器（AppTZ）
- TradingView 内置时区选择器
- 双向绑定，保持一致
- 影响：
  - K 线时间显示
  - S-Bar Count 的"新一天"检测
  - 分析结果的时间范围显示

---

## 性能优化

### 1. Datafeed 缓存
- 品种列表缓存: `_symbolListCache`
- 避免重复请求 `/api/symbol_list`

### 2. WebSocket 节流
- `bar_update` 消息去重
- 只更新活跃 subscriptions

### 3. 布局保存
- 使用 `load_last_chart: true`
- 用户关闭后下次自动恢复
- 布局存储在 SQLite (`chart_layouts` 表)

---

## 已知限制

### 1. 无原生日历组件
- TradingView 不提供独立的日期选择器 UI 组件
- 需要集成第三方库（如 Flatpickr）实现日期范围选择
- 通过 `setVisibleRange` API 设置可见时间范围

### 2. 自定义指标限制
- Pine Script 语法有限子集
- 不支持所有 TradingView 平台的高级特性
- 需要用 JavaScript 封装实现

### 3. 绘图工具同步
- TradingView 的绘图（画线工具）无法通过 API 直接操作
- 分析注释使用 `createShape()` API，不是原生绘图工具
- 用户手动绘图和程序注释是分离的

---

## 未来计划

### 短期
- [ ] 添加日期范围选择器（集成 Flatpickr）
- [ ] 实现"跳转到今天"快捷按钮
- [ ] 优化时区切换的用户体验

### 中期
- [ ] 支持更多自定义指标（ATR、IBS 等）
- [ ] 实现多图表布局（Multi-Chart Layout）
- [ ] 增强 S/R 分析的可视化效果

### 长期
- [ ] Trading Terminal 模式（订单管理界面）
- [ ] 实时报价 Watchlist
- [ ] Depth of Market (DOM) 深度图

---

## 相关文件

### 前端
- [static/app.js](../static/app.js) — 主应用逻辑
- [static/datafeed.js](../static/datafeed.js) — Datafeed 实现
- [static/index.html](../static/index.html) — 主页面
- [static/tz-selector.js](../static/tz-selector.js) — 时区选择器

### 后端
- [server.py](../server.py) — FastAPI 服务器
- [continuous_view.py](../continuous_view.py) — 连续合约拼接
- [price_action_analyzer.py](../price_action_analyzer.py) — Al Brooks 分析引擎
- [db.py](../db.py) — 数据库操作

### 文档
- [TradingView_ChartingLibrary_Components.md](./TradingView_ChartingLibrary_Components.md) — 组件功能说明
- [TradingView_Official_Docs_Index.md](./TradingView_Official_Docs_Index.md) — 官方文档索引

---

**维护**: TradeDev 项目组  
**最后更新**: 2026-05-04  
**TradingView 版本**: v28.5.0
