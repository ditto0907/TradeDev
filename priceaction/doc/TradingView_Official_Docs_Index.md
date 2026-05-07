# TradingView Charting Library 官方文档索引

## 官方资源

### 主要文档
- **官方文档首页**: https://www.tradingview.com/charting-library-docs/
- **在线演示**: https://charting-library.tradingview.com/
- **GitHub 仓库**: https://github.com/tradingview/charting_library
- **Discord 社区**: https://discord.gg/UC7cGkvn4U
- **Twitter**: https://twitter.com/tv_charts

### 快速开始
- **Getting Started**: https://www.tradingview.com/charting-library-docs/latest/getting_started
- **Tutorial**: https://github.com/tradingview/charting-library-tutorial
- **Best Practices**: https://www.tradingview.com/charting-library-docs/latest/getting_started/Best-Practices
- **NPM 集成**: https://www.tradingview.com/charting-library-docs/latest/getting_started/NPM

---

## API 文档

### 核心 API
- **Widget API**: https://www.tradingview.com/charting-library-docs/latest/api/interfaces/Charting_Library.IChartingLibraryWidget
- **Chart API**: https://www.tradingview.com/charting-library-docs/latest/api/interfaces/Charting_Library.IChartWidgetApi
- **Datafeed API**: https://www.tradingview.com/charting-library-docs/latest/api/interfaces/Datafeed.IDatafeedChartApi
- **完整 API 索引**: https://www.tradingview.com/charting-library-docs/latest/api/

### 时间和导航
- **TimeScale API**: https://www.tradingview.com/charting-library-docs/latest/api/interfaces/Charting_Library.ITimeScaleApi
- **Visible Range**: https://www.tradingview.com/charting-library-docs/latest/api/interfaces/Charting_Library.SetVisibleRangeOptions
- **Timezone API**: https://www.tradingview.com/charting-library-docs/latest/api/interfaces/Charting_Library.ITimezoneApi

### 绘图和指标
- **Drawings API**: https://www.tradingview.com/charting-library-docs/latest/ui_elements/drawings/drawings-api
- **Studies (Indicators) API**: https://www.tradingview.com/charting-library-docs/latest/api/interfaces/Charting_Library.IStudyApi
- **Custom Studies**: https://www.tradingview.com/charting-library-docs/latest/custom_studies

### 交易相关 (Trading Terminal)
- **Broker API**: https://www.tradingview.com/charting-library-docs/latest/trading_terminal/Broker-API
- **Account Manager**: https://www.tradingview.com/charting-library-docs/latest/trading_terminal/Account-Manager
- **Order Ticket**: https://www.tradingview.com/charting-library-docs/latest/trading_terminal/order-ticket
- **Positions**: https://www.tradingview.com/charting-library-docs/latest/trading_terminal/trading-concepts/positions
- **Watch List**: https://www.tradingview.com/charting-library-docs/latest/trading_terminal/Watch-List
- **Depth of Market (DOM)**: https://www.tradingview.com/charting-library-docs/latest/trading_terminal/depth-of-market

---

## 数据连接

### Datafeed
- **Datafeed 概述**: https://www.tradingview.com/charting-library-docs/latest/connecting_data
- **UDF (Universal Data Format)**: https://www.tradingview.com/charting-library-docs/latest/connecting_data/UDF
- **Trading Sessions**: https://www.tradingview.com/charting-library-docs/latest/connecting_data/Trading-Sessions
- **Symbology**: https://www.tradingview.com/charting-library-docs/latest/connecting_data/Symbology

### 实时数据
- **Real-time Updates**: https://www.tradingview.com/charting-library-docs/latest/connecting_data/datafeed-api#real-time-updates
- **Quotes**: https://www.tradingview.com/charting-library-docs/latest/connecting_data/datafeed-api#quotes

---

## 定制化

### 外观和主题
- **Custom Themes**: https://www.tradingview.com/charting-library-docs/latest/customization/styles/custom-themes
- **CSS Color Themes**: https://www.tradingview.com/charting-library-docs/latest/customization/styles/CSS-Color-Themes
- **Overrides**: https://www.tradingview.com/charting-library-docs/latest/customization/overrides

### 功能控制
- **Featuresets**: https://www.tradingview.com/charting-library-docs/latest/customization/Featuresets
- **Disabled Features**: https://www.tradingview.com/charting-library-docs/latest/customization/Disabled-Features
- **Enabled Features**: https://www.tradingview.com/charting-library-docs/latest/customization/Enabled-Features

### UI 元素
- **Legend**: https://www.tradingview.com/charting-library-docs/latest/ui_elements/Legend
- **Toolbars**: https://www.tradingview.com/charting-library-docs/latest/ui_elements/toolbars
- **Market Status**: https://www.tradingview.com/charting-library-docs/latest/ui_elements/market-status
- **UI Elements 概述**: https://www.tradingview.com/charting-library-docs/latest/ui_elements

---

## 教程和指南

### 实用指南
- **Enable Debug Mode**: https://www.tradingview.com/charting-library-docs/latest/tutorials/enable-debug-mode
- **Create Custom Page in Account Manager**: https://www.tradingview.com/charting-library-docs/latest/tutorials/create-custom-page-in-account-manager
- **Migrating from Lightweight Charts**: https://www.tradingview.com/charting-library-docs/latest/tutorials/migrate-from-lwc

### 示例代码
- **Charting Library Tutorial (GitHub)**: https://github.com/tradingview/charting-library-tutorial
- **Examples (官方)**: https://www.tradingview.com/charting-library-docs/latest/getting_started/examples

---

## 版本和更新

### 当前版本
- **版本**: v28.5.0
- **发布日期**: 2024-12-18
- **Changelog**: [见项目 changelog.md](../charting_library-master-v28.5/changelog.md)

### 版本检测
```javascript
// 在浏览器控制台执行
TradingView.version()
```

### 近期更新亮点
- **v28.5**: Column series baseline position, Moving Average Double "Another symbol" input
- **v28.4**: `includeOHLCValuesForSingleValuePlots` 导出选项, 增强日志
- **v28.3**: Symbol logos, Watchlist/Details 组件改进
- **v28.2**: Rank Correlation Index 指标, 秒级 K 线支持

---

## 常用场景文档

### 图表操作
- **缩放和滚动**: https://www.tradingview.com/charting-library-docs/latest/api/interfaces/Charting_Library.IChartWidgetApi#setvisiblerange
- **导出数据**: https://www.tradingview.com/charting-library-docs/latest/api/interfaces/Charting_Library.IChartWidgetApi#exportdata
- **保存和加载布局**: https://www.tradingview.com/charting-library-docs/latest/saving_loading

### 事件订阅
- **Subscribe/Unsubscribe**: https://www.tradingview.com/charting-library-docs/latest/api/interfaces/Charting_Library.ISubscription
- **Events Map**: https://www.tradingview.com/charting-library-docs/latest/api/interfaces/Charting_Library.SubscribeEventsMap

### 性能优化
- **Best Practices**: https://www.tradingview.com/charting-library-docs/latest/getting_started/Best-Practices
- **Memory Management**: https://www.tradingview.com/charting-library-docs/latest/getting_started/Best-Practices#memory-management

---

## 支持和问题反馈

### 获取帮助
1. 先查阅 [官方文档](https://www.tradingview.com/charting-library-docs/)
2. 查看 [Best Practices](https://www.tradingview.com/charting-library-docs/latest/getting_started/Best-Practices) 避免常见问题
3. 加入 [Discord 社区](https://discord.gg/UC7cGkvn4U) 寻求帮助
4. 在 [GitHub Issues](https://github.com/tradingview/charting_library/issues) 提交 bug 或功能请求

### 文档反馈
- **Twitter**: [@tv_charts](https://twitter.com/tv_charts)
- **GitHub Issues**: https://github.com/tradingview/charting_library/issues

---

## 本地文档

项目中的相关文档：
- **组件功能说明**: [TradingView_ChartingLibrary_Components.md](./TradingView_ChartingLibrary_Components.md)
- **Changelog**: [../charting_library-master-v28.5/changelog.md](../../charting_library-master-v28.5/changelog.md)
- **README**: [../charting_library-master-v28.5/README.md](../../charting_library-master-v28.5/README.md)
- **类型定义**: [../charting_library-master-v28.5/charting_library.d.ts](../../charting_library-master-v28.5/charting_library.d.ts)

---

**文档维护**: TradeDev 项目组  
**最后更新**: 2026-05-04
