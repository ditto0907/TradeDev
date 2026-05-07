# Interactive Brokers Tick 数据指南

## 概述

Interactive Brokers (IB) 通过其 TWS (Trader Workstation) API 提供**多种粒度**的实时数据，包括毫秒级的 tick-by-tick 数据。

---

## IB 提供的实时数据类型

### 1. 聚合 Market Data (`reqMktData`)

**当前项目使用的方法** ✅

**特点**:
- 聚合的 tick 数据（快照模式）
- 更新频率：约 **250-300ms** 一次
- 数据内容：last price, bid/ask, volume
- 带宽占用低
- 适合图表显示和常规交易

**代码实现** (priceaction/ib_data_fetcher.py):
```python
# Line 716
ticker = self.ib.reqMktData(contract, "", False, False)

# Line 706: 配置更新间隔
_TICK_BROADCAST_INTERVAL = 0.25  # 250ms
```

**优点**:
- 实现简单
- 资源占用少
- 满足大多数交易需求

**缺点**:
- 无法获取每笔真实成交
- 时间精度仅到秒级
- 错过高频微观结构数据

---

### 2. Real-Time Bars (`reqRealTimeBars`)

**5秒聚合 K 线**

**特点**:
- IB 预聚合的 5 秒 OHLCV 数据
- 每 5 秒推送一次
- 数据延迟极低（<100ms）
- 适合短线交易监控

**代码实现** (priceaction/ib_data_fetcher.py):
```python
# Line 683
rt_bars = self.ib.reqRealTimeBars(contract, 5, "TRADES", False)
```

**用途**:
- 当前项目用于聚合 5 分钟 K 线
- 从 5 秒 bar 向上聚合，避免 tick 处理开销

---

### 3. Tick-by-Tick Data (`reqTickByTickData`) 🎯

**毫秒级逐笔数据** — IB 支持但**本项目未使用**

#### 3.1 数据类型

IB 提供 **4 种** tick-by-tick 数据流：

| 类型 | 说明 | 时间精度 | 数据内容 |
|------|------|----------|----------|
| **Last** | 最新成交 | **毫秒** | price, size, exchange |
| **AllLast** | 所有成交 | **毫秒** | 包含每笔成交的详细信息 |
| **BidAsk** | 盘口报价 | **毫秒** | bid price/size, ask price/size |
| **MidPoint** | 中间价 | **毫秒** | (bid + ask) / 2 |

#### 3.2 API 接口 (ib_insync)

```python
from ib_insync import IB, Stock, Future

ib = IB()
ib.connect('127.0.0.1', 7497, clientId=1)

contract = Future('MES', '202606', 'CME')

# 订阅逐笔成交数据
ticker = ib.reqTickByTickData(contract, 'Last')

def on_tick_by_tick(ticks):
    for tick in ticks:
        print(f"Time: {tick.time}")         # datetime with microseconds
        print(f"Price: {tick.price}")
        print(f"Size: {tick.size}")
        print(f"Exchange: {tick.exchange}")

ticker.updateEvent += on_tick_by_tick
```

#### 3.3 数据结构

**Last Tick**:
```python
TickByTickLast(
    time=datetime(2026, 5, 4, 9, 30, 0, 123456),  # 微秒精度!
    tickType=1,
    price=5123.25,
    size=2,
    exchange='CME',
    specialConditions='',
    pastLimit=False,
    unreported=False
)
```

**BidAsk Tick**:
```python
TickByTickBidAsk(
    time=datetime(2026, 5, 4, 9, 30, 0, 234567),
    bidPrice=5123.00,
    askPrice=5123.25,
    bidSize=10,
    askSize=5,
    bidPastLow=False,
    askPastHigh=False
)
```

#### 3.4 性能和限制

**优势**:
- ✅ 真正的逐笔成交数据
- ✅ 微秒级时间戳（精度 0.000001 秒）
- ✅ 完整的市场深度信息
- ✅ 适合高频分析、订单流分析、市场微观结构研究

**限制**:
- ⚠️ **数据量巨大**: MES 活跃时段每秒数百笔成交
- ⚠️ **带宽要求高**: 需要稳定的低延迟网络
- ⚠️ **IB 订阅限制**: 
  - 免费账户可能有限制
  - 同时订阅数量有上限（通常 3-5 个品种）
- ⚠️ **历史数据**: tick-by-tick 历史数据仅保留 **极短时间**（几小时到几天）
- ⚠️ **CPU 占用**: 实时处理高频 tick 需要高性能

**IB 官方文档**:
- [Tick-by-Tick Data](https://interactivebrokers.github.io/tws-api/tick_data.html)
- [ib_insync reqTickByTickData](https://ib-insync.readthedocs.io/api.html#ib_insync.ib.IB.reqTickByTickData)

---

## 当前项目的实时数据架构

### 数据流

```
┌─────────────────────────────────────────────────────────────┐
│  IB TWS/Gateway                                              │
│  - 接收交易所实时数据                                          │
└────────────────────┬────────────────────────────────────────┘
                     │
          ┌──────────┴──────────┐
          │                     │
    reqMktData          reqRealTimeBars
    (250ms 快照)         (5秒 OHLCV)
          │                     │
          │                     │
          ▼                     ▼
┌─────────────────────────────────────────────────────────────┐
│  ib_data_fetcher.py                                          │
│  - _on_tick_unified()  聚合 tick → 5min bar                 │
│  - _process_tick()     更新 _rt_current                      │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
        WebSocket 推送 (250ms 节流)
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  前端 (TradingView Charting Library)                         │
│  - 实时更新图表                                               │
└─────────────────────────────────────────────────────────────┘
```

### 实现细节

**1. Tick 聚合逻辑** (ib_data_fetcher.py, line 878-950):
```python
def _process_tick(self, symbol, tf_key, interval, ts, o, h, l, c, v, is_bar=False):
    """
    Unified tick handler for all symbols and timeframes.
    
    Args:
        is_bar: True = 来自 reqRealTimeBars (5秒预聚合)
                False = 来自 reqMktData (逐笔 tick)
    """
    # 判断是否需要完成当前 bar
    if ts >= rt["end"]:
        # 完成 bar，存入 DB
        completed_bar = {...}
        db.insert_bars(...)
        
        # 开始新 bar
        rt["start"] = new_start
        rt["end"] = new_end
        rt["open"] = c
        # ...
    else:
        # 更新当前 bar 的 OHLCV
        rt["high"] = max(rt["high"], h)
        rt["low"] = min(rt["low"], l)
        rt["close"] = c
        rt["volume"] += v
```

**2. WebSocket 推送节流** (ib_data_fetcher.py, line 706):
```python
_TICK_BROADCAST_INTERVAL = 0.25  # 250ms

# 避免每个 tick 都推送到前端，降低带宽占用
# 前端每秒最多收到 4 次更新
```

**3. 为什么不使用 tick-by-tick？**

对于本项目的用途（Al Brooks Price Action 分析、5分钟图表交易）：
- ✅ 250ms 精度已足够（5分钟 = 300秒，250ms 是 1/1200）
- ✅ 降低系统复杂度和资源占用
- ✅ 避免 IB 订阅费用和限制
- ✅ 5秒聚合 bar 比 tick 聚合更高效

---

## 如何在项目中启用 Tick-by-Tick 数据

### 场景 1: 研究订单流 / 市场微观结构

**实现步骤**:

1. **创建独立的 tick 数据采集器**:

```python
# priceaction/tick_collector.py
import asyncio
from datetime import datetime
from ib_insync import IB, Future
import db

class TickCollector:
    def __init__(self):
        self.ib = IB()
        self.ticks_buffer = []
    
    async def connect(self):
        await self.ib.connectAsync('127.0.0.1', 7497, clientId=20)
    
    async def subscribe_ticks(self, symbol='MES', contract_month='202606'):
        contract = Future(symbol, contract_month, 'CME')
        await self.ib.qualifyContractsAsync(contract)
        
        # 订阅逐笔成交
        ticker = self.ib.reqTickByTickData(contract, 'AllLast')
        
        def on_tick(ticks):
            for tick in ticks:
                self.ticks_buffer.append({
                    'symbol': symbol,
                    'time': tick.time.timestamp(),  # 浮点数，包含微秒
                    'price': tick.price,
                    'size': tick.size,
                    'exchange': tick.exchange,
                })
                
                # 批量写入 DB（每 1000 笔）
                if len(self.ticks_buffer) >= 1000:
                    self._flush_ticks()
        
        ticker.updateEvent += on_tick
        print(f"[{symbol}] Subscribed to tick-by-tick data")
    
    def _flush_ticks(self):
        if not self.ticks_buffer:
            return
        
        # 写入专用的 tick 数据表
        # db.insert_ticks(self.ticks_buffer)
        print(f"Flushed {len(self.ticks_buffer)} ticks to DB")
        self.ticks_buffer.clear()
```

2. **创建 tick 数据表**:

```sql
-- priceaction/db.py 中添加
CREATE TABLE ticks (
    symbol         TEXT    NOT NULL,
    contract_month TEXT    NOT NULL,
    ts             REAL    NOT NULL,  -- 浮点数存储微秒时间戳
    price          REAL    NOT NULL,
    size           INTEGER NOT NULL,
    exchange       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (symbol, contract_month, ts)
);

CREATE INDEX idx_ticks_lookup ON ticks (symbol, ts);
```

3. **独立运行采集器**:

```bash
# scripts/collect_ticks.py
import asyncio
from tick_collector import TickCollector

async def main():
    collector = TickCollector()
    await collector.connect()
    
    # 同时采集多个品种
    await collector.subscribe_ticks('MES', '202606')
    await collector.subscribe_ticks('MNQ', '202606')
    
    # 保持运行
    while True:
        await asyncio.sleep(60)
        collector._flush_ticks()  # 定期刷新

asyncio.run(main())
```

---

### 场景 2: 构建秒级 K 线（从 tick 聚合）

**利用 tick-by-tick 数据构建 1秒、5秒、10秒 K 线**:

```python
# 在 tick_collector.py 中添加
class TickToBarAggregator:
    def __init__(self, interval_sec=1):
        self.interval = interval_sec
        self.current_bar = None
    
    def on_tick(self, tick):
        ts = int(tick.time.timestamp())
        bar_start = (ts // self.interval) * self.interval
        
        if self.current_bar is None or self.current_bar['start'] != bar_start:
            # 完成上一根 bar
            if self.current_bar:
                self._emit_bar(self.current_bar)
            
            # 开始新 bar
            self.current_bar = {
                'start': bar_start,
                'open': tick.price,
                'high': tick.price,
                'low': tick.price,
                'close': tick.price,
                'volume': tick.size,
            }
        else:
            # 更新当前 bar
            self.current_bar['high'] = max(self.current_bar['high'], tick.price)
            self.current_bar['low'] = min(self.current_bar['low'], tick.price)
            self.current_bar['close'] = tick.price
            self.current_bar['volume'] += tick.size
    
    def _emit_bar(self, bar):
        print(f"1-sec bar: {bar}")
        # db.insert_bars('MES', '1', [bar], source='tick_aggregated')
```

---

### 场景 3: 订单流分析（Order Flow / Footprint Charts）

**分析每个价格档位的成交量**:

```python
from collections import defaultdict

class OrderFlowAnalyzer:
    def __init__(self, price_step=0.25):
        self.price_step = price_step  # MES tick size
        self.volume_profile = defaultdict(lambda: {'bid_vol': 0, 'ask_vol': 0})
    
    def on_tick(self, tick):
        # 四舍五入到最近的 tick
        price_level = round(tick.price / self.price_step) * self.price_step
        
        # 简化判断：基于 tick 的 uptick/downtick 规则
        # 实际需要 BidAsk tick 配合判断主动买/卖
        if tick.price > self.last_price:
            self.volume_profile[price_level]['ask_vol'] += tick.size
        else:
            self.volume_profile[price_level]['bid_vol'] += tick.size
        
        self.last_price = tick.price
    
    def get_footprint(self):
        # 返回每个价格档位的买卖量
        return dict(self.volume_profile)
```

**可视化**:
- 在 TradingView 图表上绘制 Delta (买量 - 卖量)
- 识别大单成交的价格档位
- 支撑/阻力强度分析

---

## 数据存储考量

### Tick 数据存储挑战

**数据量估算** (MES 为例):
- 活跃时段：**200-500 ticks/秒**
- 每日交易时段：23 小时（电子盘）
- **每日 tick 数**: 500 × 60 × 60 × 23 ≈ **4100 万笔**

**存储方案**:

| 方案 | 优点 | 缺点 | 适用场景 |
|------|------|------|----------|
| **SQLite** | 简单、本地 | 写入性能瓶颈（>1000 ticks/s） | 研究、回测（离线） |
| **时序数据库** (InfluxDB, TimescaleDB) | 高性能写入/查询 | 部署复杂 | 生产环境、实时分析 |
| **内存缓存** + 定期落盘 | 极低延迟 | 数据易丢失 | 日内交易（不需长期保存） |
| **压缩存储** (Parquet, HDF5) | 节省空间 | 不适合实时写入 | 历史数据归档 |

**推荐架构** (生产环境):
```
实时 tick → 内存环形缓冲区 (最近 1 小时)
              ↓
         每 5 分钟批量写入 SQLite/TimescaleDB
              ↓
         每日归档到 Parquet 文件
```

---

## 成本和订阅要求

### IB 市场数据订阅

| 数据类型 | 是否需要额外订阅 | 费用 |
|----------|------------------|------|
| **reqMktData** (聚合 tick) | ✅ 需要 | CME 实时数据：约 $1.50/月 |
| **reqRealTimeBars** (5秒 bar) | ✅ 需要 | 同上 |
| **reqTickByTickData** (逐笔) | ✅ 需要 | **可能更高**（取决于交易所） |

**注意**:
- IB 账户需要满足交易所的数据订阅要求
- 专业用户（非散户）费用更高
- Delayed Data (15分钟延迟) 通常免费

**检查订阅状态**:
TWS → Account → Market Data Subscriptions

---

## 总结和建议

### ✅ IB 提供毫秒级 tick 数据

**答案**: **是的**，IB 通过 `reqTickByTickData` API 提供**微秒精度**的逐笔数据。

### 📊 当前项目使用情况

- **当前**: `reqMktData` (250ms 聚合快照)
- **未来**: 可选升级到 `reqTickByTickData` (微秒级 tick)

### 🎯 何时需要 Tick-by-Tick 数据？

**推荐使用场景**:
- ✅ 订单流分析 (Order Flow / Footprint Charts)
- ✅ 市场微观结构研究
- ✅ 高频交易策略开发
- ✅ 精确的成交量分析（VWAP、POC）
- ✅ 秒级或亚秒级 K 线构建

**不推荐场景**:
- ❌ 5分钟以上周期的图表交易（当前 250ms 已足够）
- ❌ 日线、周线分析
- ❌ 趋势跟踪策略（不需要微观数据）

### 🔧 实现建议

1. **先评估需求**: 是否真的需要微秒级精度？
2. **分离采集**: tick 采集器独立运行，不影响主系统
3. **优化存储**: 使用时序数据库或批量写入
4. **控制成本**: 仅订阅必要的品种和时段
5. **测试性能**: 先用 Paper Trading 账户测试

### 📚 进一步学习

- **IB API 文档**: https://interactivebrokers.github.io/tws-api/
- **ib_insync 文档**: https://ib-insync.readthedocs.io/
- **Time & Sales 数据**: IB TWS 内置的 Time & Sales 窗口可预览 tick 数据
- **市场微观结构**: 推荐阅读 *Trading and Exchanges* by Larry Harris

---

## 相关文件

### 当前实现
- [ib_data_fetcher.py](../ib_data_fetcher.py) — 实时数据订阅和聚合
- [realtime_builder.py](../realtime_builder.py) — Tick → Bar 聚合逻辑
- [server.py](../server.py) — WebSocket 推送

### 配置
- [config.py](../config.py) — 品种配置、tick size
- [requirements.txt](../requirements.txt) — ib_insync 版本

---

**维护**: TradeDev 项目组  
**最后更新**: 2026-05-04  
**ib_insync 版本**: ≥0.9.86
