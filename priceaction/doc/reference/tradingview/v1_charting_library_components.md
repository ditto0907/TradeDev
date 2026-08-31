# TradingView Charting Library v28.5 组件功能文档

## 概述

本项目使用 **TradingView Advanced Charts (Charting Library)** v28.5 作为前端图表组件库。

- **官方文档**: https://www.tradingview.com/charting-library-docs/
- **官方演示**: https://charting-library.tradingview.com/
- **GitHub**: https://github.com/tradingview/charting_library
- **版本**: v28.5.0 (2024-12-18)

---

## 日历/日期相关组件

TradingView Charting Library **没有独立的日历组件**，但提供了多个日期导航和时间范围管理的 API：

### 1. Go To Date 对话框

**功能**: 内置的"跳转到指定日期"对话框

**触发方式**:
```javascript
// 通过命令系统触发
widget.executeActionById('ChartDialogsShowGoToDate');
```

**事件标识**:
```typescript
ChartDialogsShowGoToDate = "Chart.Dialogs.ShowGoToDate"
```

---

### 2. setVisibleRange API

**功能**: 编程式设置图表可见时间范围

**接口定义**:
```typescript
interface VisibleTimeRange {
  /** UNIX 时间戳（秒）- 起始时间 */
  from: number;
  /** UNIX 时间戳（秒）- 结束时间 */
  to: number;
}

interface SetVisibleRangeOptions {
  /** 应用默认右侧边距 */
  applyDefaultRightMargin?: boolean;
  /** 应用百分比右侧边距 */
  percentRightMargin?: number;
}
```

**使用示例**:
```javascript
// 设置可见范围为 2026-01-01 到 2026-12-31
widget.activeChart().setVisibleRange(
  { 
    from: 1735689600,  // 2026-01-01 00:00:00 UTC
    to: 1767225600     // 2026-12-31 00:00:00 UTC
  },
  { percentRightMargin: 20 }  // 右侧保留 20% 边距
).then(() => console.log('时间范围已应用'));
```

**注意事项**:
- 时间戳单位是**秒**，不是毫秒
- 设置 `applyDefaultRightMargin` 或 `percentRightMargin` 时，`to` 值会被忽略，改用最新 K 线时间

---

### 3. Time Scale API

**功能**: 时间轴操作和监听

**接口定义**:
```typescript
interface ITimeScaleApi {
  /** 坐标转时间戳 */
  coordinateToTime(x: number): number | null;
  
  /** 监听缩放变化 */
  barSpacingChanged(): ISubscription<(newBarSpacing: number) => void>;
  
  /** 监听滚动变化 */
  rightOffsetChanged(): ISubscription<(rightOffset: number) => void>;
  
  /** 设置右侧偏移量（K线数量） */
  setRightOffset(offset: number): void;
  
  /** 设置 K 线间距 */
  setBarSpacing(newBarSpacing: number): void;
  
  /** 获取当前 K 线间距 */
  barSpacing(): number;
  
  /** 获取当前右侧偏移量 */
  rightOffset(): number;
  
  /** 获取图表宽度（像素） */
  width(): number;
  
  /** 默认右侧偏移量（K线数） */
  defaultRightOffset(): IWatchedValue<number>;
  
  /** 默认右侧偏移量（百分比） */
  defaultRightOffsetPercentage(): IWatchedValue<number>;
  
  /** 是否使用百分比模式 */
  usePercentageRightOffset(): IWatchedValue<boolean>;
}
```

**使用示例**:
```javascript
// 获取 TimeScale API
const timeScale = widget.activeChart().getTimeScale();

// 坐标转时间
const timestamp = timeScale.coordinateToTime(100);  // 距离左侧 100px 对应的时间

// 监听滚动事件
timeScale.rightOffsetChanged().subscribe(
  null,
  (offset) => console.log(`右侧偏移: ${offset} 根 K 线`),
  true
);

// 监听缩放事件
timeScale.barSpacingChanged().subscribe(
  null,
  (spacing) => console.log(`K线间距: ${spacing}px`),
  true
);
```

---

### 4. Order Duration DatePicker (仅 Trading Terminal)

**功能**: 订单有效期的日期/时间选择器

**接口定义**:
```typescript
interface OrderDurationMetaInfo {
  /** 显示日期选择器 */
  hasDatePicker?: boolean;
  
  /** 显示时间选择器 */
  hasTimePicker?: boolean;
  
  /** 是否为默认选项 */
  default?: boolean;
  
  /** 本地化标题 */
  name: string;
  
  /** 持续时间标识 */
  value: string;
  
  /** 支持的订单类型列表 */
  supportedOrderTypes?: OrderType[];
}
```

**使用场景**:
- Trading Terminal 模式下的订单下单界面
- 设置订单有效期（GTC、DAY、GTD 等）
- GTD (Good Till Date) 订单需要日期选择器

---

## 其他相关时间功能

### 5. CrossHair 时间显示

**功能**: 十字光标移动时获取当前时间和价格

```javascript
widget.activeChart().crossHairMoved().subscribe(
  null,
  ({ time, price }) => {
    console.log('时间:', new Date(time * 1000));
    console.log('价格:', price);
  }
);
```

### 6. Bar Time Conversion

**功能**: K 线时间戳转换

```javascript
// 获取周期结束时间
const endTime = widget.activeChart().barTimeToEndOfPeriod(unixTime);

// 周期结束时间转 K 线时间
const barTime = widget.activeChart().endOfPeriodToBarTime(unixTime);
```

---

## 集成示例

### 自定义日期导航按钮

```javascript
// 添加一个"今天"按钮
document.getElementById('goto-today').addEventListener('click', () => {
  const now = Math.floor(Date.now() / 1000);
  const oneDayAgo = now - 86400;
  
  widget.activeChart().setVisibleRange(
    { from: oneDayAgo, to: now },
    { applyDefaultRightMargin: true }
  );
});

// 添加"上周"按钮
document.getElementById('goto-last-week').addEventListener('click', () => {
  const now = Math.floor(Date.now() / 1000);
  const oneWeekAgo = now - 7 * 86400;
  
  widget.activeChart().setVisibleRange(
    { from: oneWeekAgo, to: now },
    { percentRightMargin: 5 }
  );
});
```

### 监听用户滚动并记录位置

```javascript
const timeScale = widget.activeChart().getTimeScale();

// 监听右侧偏移变化
timeScale.rightOffsetChanged().subscribe(null, (offset) => {
  localStorage.setItem('chart_right_offset', offset);
}, true);

// 恢复用户位置
widget.onChartReady(() => {
  const savedOffset = localStorage.getItem('chart_right_offset');
  if (savedOffset) {
    timeScale.setRightOffset(parseInt(savedOffset));
  }
});
```

---

## 限制和注意事项

1. **无独立日历组件**: TradingView 没有提供 UI 日历选择器组件，需要配合第三方日历库（如 Flatpickr、react-datepicker）实现自定义日期选择界面

2. **时间戳单位**: 
   - `setVisibleRange` 使用**秒级**时间戳
   - JavaScript `Date.now()` 返回**毫秒**，需要除以 1000

3. **交易时段**: 图表会根据交易时段自动过滤显示，需要在 Datafeed 中正确配置 `session` 和 `has_intraday`

4. **时区**: 图表时区可通过 Timezone API 设置，影响时间显示但不影响数据存储

---

## 常见问题

### Q: 如何实现日期范围选择器？
**A**: 使用第三方日历组件（如 Flatpickr）获取用户选择的日期，然后调用 `setVisibleRange` API：

```javascript
import flatpickr from "flatpickr";

flatpickr("#date-range", {
  mode: "range",
  onChange: (selectedDates) => {
    if (selectedDates.length === 2) {
      const from = Math.floor(selectedDates[0].getTime() / 1000);
      const to = Math.floor(selectedDates[1].getTime() / 1000);
      widget.activeChart().setVisibleRange({ from, to });
    }
  }
});
```

### Q: 如何滚动到最新数据？
**A**: 设置右侧偏移为 0：

```javascript
widget.activeChart().getTimeScale().setRightOffset(0);
```

### Q: 如何自动滚动到特定交易时间？
**A**: 结合 `setVisibleRange` 和市场开盘时间：

```javascript
// 假设市场在 2026-05-04 09:30 开盘
const marketOpen = new Date('2026-05-04T09:30:00Z').getTime() / 1000;
const now = Date.now() / 1000;

widget.activeChart().setVisibleRange(
  { from: marketOpen, to: now },
  { applyDefaultRightMargin: true }
);
```

---

## 相关资源

- [TradingView Charting Library 官方文档](https://www.tradingview.com/charting-library-docs/)
- [Best Practices](https://www.tradingview.com/charting-library-docs/latest/getting_started/Best-Practices)
- [API 参考](https://www.tradingview.com/charting-library-docs/latest/api/)
- [Discord 社区](https://discord.gg/UC7cGkvn4U)

---

**文档生成时间**: 2026-05-04  
**组件库版本**: TradingView Charting Library v28.5.0  
**项目**: TradeDev Price Action Trading System
