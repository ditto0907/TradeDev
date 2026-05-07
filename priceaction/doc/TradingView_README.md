# TradingView Charting Library 文档中心

本目录包含 TradingView Charting Library v28.5 的完整使用文档。

---

## 📚 文档索引

### 🎯 快速开始

1. **[官方文档索引](./TradingView_Official_Docs_Index.md)**  
   - TradingView 官方文档的完整链接索引
   - API 参考、教程、最佳实践
   - 适合初次接触 TradingView 的开发者

2. **[项目使用总结](./TradingView_Project_Usage.md)**  
   - 本项目中如何使用 TradingView
   - 配置详解、自定义指标、实时数据集成
   - 与后端 FastAPI 的集成方式
   - **推荐先阅读此文档了解项目实际使用**

3. **[组件功能说明](./TradingView_ChartingLibrary_Components.md)**  
   - 日历/日期相关组件详解
   - 时间导航 API (setVisibleRange, TimeScale API)
   - 日期选择器集成方案
   - 常见问题解答

---

## 🔍 组件查询

### ❓ 我想找...

#### 日历/日期选择组件
→ **没有原生日历组件**，但有日期导航 API：
- 查看 [组件功能说明 - 日历/日期相关组件](./TradingView_ChartingLibrary_Components.md#日历日期相关组件)
- 使用 `setVisibleRange()` 设置时间范围
- 集成第三方日历库（Flatpickr、react-datepicker）
- 内置 "Go To Date" 对话框

#### 时区管理
→ 查看 [项目使用总结 - 时区管理](./TradingView_Project_Usage.md#时区管理)
- Timezone API 使用方法
- 双向绑定实现（Chart ↔ 顶部选择器）
- 时区变化事件监听

#### 自定义指标
→ 查看 [项目使用总结 - 自定义指标](./TradingView_Project_Usage.md#自定义指标)
- S-Bar Count 指标实现
- Pine Script 子集语法
- `custom_indicators_getter` 配置

#### 实时数据推送
→ 查看 [项目使用总结 - 实时数据集成](./TradingView_Project_Usage.md#实时数据集成)
- Datafeed 实现 (`datafeed.js`)
- WebSocket 集成
- `subscribeBars()` / `updateBar()` API

#### 连续合约拼接
→ 查看 [项目使用总结 - 连续合约支持](./TradingView_Project_Usage.md#1-连续合约支持)
- `MES@CONT_FRONT` / `MES@202606` 语法
- 后端 `continuous_view.py` 实现
- 前月、比例调整、差价调整三种模式

#### 右键菜单定制
→ 查看 [项目使用总结 - 右键菜单](./TradingView_Project_Usage.md#右键菜单)
- `onContextMenu()` API
- 快速下单菜单实现

#### 保存/加载布局
→ 查看 [项目使用总结 - 保存/加载布局](./TradingView_Project_Usage.md#保存加载布局)
- `save_load_adapter` 实现
- 与 SQLite 数据库集成
- `chart_layouts` 表结构

---

## 📖 官方资源

### 在线文档
- **官方文档**: https://www.tradingview.com/charting-library-docs/
- **在线演示**: https://charting-library.tradingview.com/
- **GitHub**: https://github.com/tradingview/charting_library
- **Discord**: https://discord.gg/UC7cGkvn4U

### 本地文件
- **README**: [../charting_library-master-v28.5/README.md](../../charting_library-master-v28.5/README.md)
- **Changelog**: [../charting_library-master-v28.5/changelog.md](../../charting_library-master-v28.5/changelog.md)
- **类型定义**: [../charting_library-master-v28.5/charting_library.d.ts](../../charting_library-master-v28.5/charting_library.d.ts)
- **UDF Datafeed**: [../charting_library-master-v28.5/datafeeds/udf/README.md](../../charting_library-master-v28.5/datafeeds/udf/README.md)

---

## 🎨 项目文件结构

```
TradeDev/
├── charting_library-master-v28.5/     # TradingView 库文件
│   ├── charting_library/              # 核心库
│   ├── charting_library.d.ts          # TypeScript 类型定义
│   ├── changelog.md                   # 版本更新日志
│   └── README.md                      # 官方说明
│
├── priceaction/
│   ├── static/
│   │   ├── app.js                     # 主应用逻辑 (TradingView 初始化)
│   │   ├── datafeed.js                # 自定义 Datafeed 实现
│   │   ├── index.html                 # 主页面 (包含 tv-chart 容器)
│   │   └── tz-selector.js             # 时区选择器
│   │
│   ├── doc/                           # 📂 文档目录 (你在这里)
│   │   ├── TradingView_README.md      # ← 本文档
│   │   ├── TradingView_Official_Docs_Index.md
│   │   ├── TradingView_ChartingLibrary_Components.md
│   │   └── TradingView_Project_Usage.md
│   │
│   ├── server.py                      # FastAPI 后端
│   ├── continuous_view.py             # 连续合约拼接逻辑
│   └── db.py                          # 数据库操作
│
└── ...
```

---

## 🚀 快速参考

### 初始化图表
```javascript
const widget = new TradingView.widget({
  container: 'tv-chart',
  datafeed: datafeed,
  symbol: 'MES',
  interval: '5',
  library_path: '/charting_library/',
  timezone: 'America/New_York',
  theme: 'dark',
  load_last_chart: true,
  autosize: true
});
```

### 获取图表实例
```javascript
widget.onChartReady(() => {
  const chart = widget.activeChart();
  
  // 获取当前品种
  const symbol = chart.symbol();
  
  // 切换品种
  widget.setSymbol('MNQ', '5', callback);
  
  // 设置可见时间范围
  chart.setVisibleRange({ from: 1735689600, to: 1767225600 });
});
```

### 实时数据更新
```javascript
// Datafeed 中
this.onRealtimeCallback({
  time: timestamp * 1000,
  open: bar.open,
  high: bar.high,
  low: bar.low,
  close: bar.close,
  volume: bar.volume
});
```

### 监听事件
```javascript
// 品种切换
chart.onSymbolChanged().subscribe(null, () => {
  console.log('新品种:', chart.symbol());
});

// 十字光标移动
chart.crossHairMoved().subscribe(null, ({ time, price }) => {
  console.log('时间:', time, '价格:', price);
});

// 时区变化
chart.getTimezoneApi().onTimezoneChanged().subscribe(null, (tz) => {
  console.log('新时区:', tz);
});
```

---

## ⚠️ 常见问题

### Q1: 为什么找不到日历组件？
**A**: TradingView 没有提供独立的日历选择器 UI 组件。需要：
1. 使用第三方日历库（如 Flatpickr）
2. 通过 `setVisibleRange()` API 设置时间范围
3. 或使用内置的 "Go To Date" 对话框 (`executeActionById('ChartDialogsShowGoToDate')`)

详见 [组件功能说明](./TradingView_ChartingLibrary_Components.md#常见问题)

### Q2: 时间戳单位是秒还是毫秒？
**A**: **取决于 API**：
- `setVisibleRange({ from, to })` 使用**秒**
- Datafeed 的 `onRealtimeCallback({ time })` 使用**毫秒**
- WebSocket `bar_update` 消息使用**秒** (后端 UNIX 时间戳)

### Q3: 如何切换连续合约和月份合约？
**A**: 使用 `@` 后缀：
- `MES@CONT_FRONT` — 前月连续合约
- `MES@202606` — 2026年6月合约
- 前端通过 Contract Selector 下拉框切换
- 后端通过 `continuous_view.py` 拼接数据

### Q4: 自定义指标如何调试？
**A**: 
1. 启用 Debug 模式: https://www.tradingview.com/charting-library-docs/latest/tutorials/enable-debug-mode
2. 在 `constructor.main()` 中使用 `console.log()`
3. 检查浏览器控制台的错误信息
4. 参考 `static/app.js` 中的 S-Bar Count 实现

### Q5: 如何保存用户的图表布局？
**A**: 实现 `save_load_adapter`：
```javascript
save_load_adapter: {
  getAllCharts: () => fetch('/api/chart_layouts').then(r => r.json()),
  saveChart: (data) => fetch('/api/chart_layouts', { method: 'POST', body: JSON.stringify(data) }),
  removeChart: (id) => fetch(`/api/chart_layouts/${id}`, { method: 'DELETE' }),
  getChartContent: (id) => fetch(`/api/chart_layouts/${id}`).then(r => r.json())
}
```

详见 [项目使用总结 - 保存/加载布局](./TradingView_Project_Usage.md#保存加载布局)

---

## 📝 更新记录

| 日期 | 版本 | 说明 |
|------|------|------|
| 2026-05-04 | 1.0.0 | 创建 TradingView 文档中心 |

---

## 🤝 贡献

如发现文档错误或需要补充内容，请：
1. 更新对应的 Markdown 文件
2. 在本文档中更新索引
3. 提交 commit 时注明 `docs: update TradingView documentation`

---

**维护**: TradeDev 项目组  
**TradingView 版本**: v28.5.0 (2024-12-18)  
**文档版本**: 1.0.0
