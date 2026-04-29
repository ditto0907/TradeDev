# Optimization Analysis — 2-Bar Close-at-Extreme Strategy

**Backtest period**: 2026-04-15 to 2026-04-28 (10 trading days, MES M5 RTH 09:30-12:00 ET)

## 一、回测结果摘要

| 指标 | 值 | 说明 |
|------|-----|------|
| 总信号数 | 7 | 平均每日 0.7 次，频率偏低 |
| 胜率（决出胜负） | **28.6%** | 远低于 1:1 R:R 的盈亏平衡线 50% |
| 净点数 | **-39.0 pts** | 期望 -5.57 pts/笔，**净亏损** |
| EARLY 时段 (09:30-10:30) | 1W / 5L = **16.7%** | 主战场全部亏损 |
| MID 时段 (10:30-12:00) | 1W / 0L | 样本太小但暗示晚段更可靠 |

> **结论**：用户对"开盘 2h 处于震荡"的判断完全正确。原始策略**胜率仅 28.6%**，1:1 R:R 模式下 -39 pts 净亏，**不具备开盘前 2h 直接使用的可行性**。

---

## 二、Al Brooks 框架下的失败原因诊断

### 失败模式 1：在 TR 内部追"高潮"
绝大多数信号（7/7 = 100%）发生在 **IN_PDR**（开盘价处于前一交易日范围内 = 震荡背景），其中 6/7 出现在 09:30-10:30 的开盘震荡区，胜率 16.7%。  
**Al Brooks 解读**：TR 内部的 2 根连续极端收盘 K 线，**更像是 leg 末端的力竭**而非 BO 的开始 —— 卖在 TR 顶、买在 TR 底，**违反 TR 黄金法则**："**Buy Low, Sell High, Scalp**"。

### 失败模式 2：早盘 1 小时内的"假突破"
EARLY 时段 6 笔仅 1 笔盈利（04-17）。MES 09:30 开盘后通常需要 **30-60 min 形成 OR**，期间高低反复测试，2-bar 极端收盘信号不断诱多/诱空。

### 失败模式 3：逆向 D1 趋势的 Counter-trend 单
4 笔 COUNTER 信号 → 1W/3L (25%)，2 笔 WITH-trend → 0W/2L (0%)，1 笔 NEUTRAL → 1W/0L。  
样本过小不能下结论，但提示 **不分背景一律入场**是问题核心。

### 失败模式 4：1:1 R:R + 紧密止损
止损放在 2 根信号 K 线 swing low/high 外 1 tick，平均 risk = 11 pts。MES 在 OR 内噪声常超 10 pts，止损被频繁触发后 1:1 目标尚未达成。

---

## 三、优化建议（按优先级排序）

### ★★★ 优先级 1：时间过滤 — 跳过 09:30-10:30 OR 形成期

**修改**：禁止在前 12 根 K 线（09:30-10:30）入场。

**依据**：
- EARLY 6 笔 → 1W/5L，期望 -7.25 pts/笔
- MID 1 笔 → 1W/0L
- 与"开盘 2h 处于震荡"的市场观察一致

**实施**：将 `ENTRY_START` 调整为 `dtime(10, 30)`，或要求信号必须发生在 **OR 形成完毕**之后。

---

### ★★★ 优先级 2：背景过滤 — 仅在 OR/TR BO 后入场

**修改**：信号仅在以下情况成立时入场：
1. 当前 K 线是 **OR breakout**（收盘越过 09:30-10:00 的 OR 高/低）
2. 或当前 2 根 K 线**完全脱离**前 5 根 K 线的重叠区（脱离 TR）

**依据**：
- IN_PDR 6 笔仅 1W → 16.7%（开盘在前日区间内 = 震荡概率高）
- Al Brooks 核心规则：**Strong BO 入场，TR 内 scalp 反向**
- 2-bar close-on-extreme 在 BO 之后是 FT 信号，在 TR 内部是力竭信号

**实施伪代码**：
```python
or_high = max(b['high'] for b in day_bars[:6])  # 09:30-10:00 OR
or_low  = min(b['low']  for b in day_bars[:6])
if direction == "LONG" and entry < or_high: continue   # 必须 OR BO 之上
if direction == "SHORT" and entry > or_low: continue
```

---

### ★★ 优先级 3：D1 趋势对齐过滤

**修改**：只在 **D1 NEUTRAL** 或 **D1 与信号方向一致** 时入场，跳过 COUNTER 单。

**依据**：
- 当前样本 COUNTER 4 笔 → 1W/3L
- 即使仅 1 根 NEUTRAL 全胜不能绝对化，但与 Al Brooks "**Trade only in trend direction**" 原则一致

**实施**：
```python
if align == "COUNTER":
    continue  # 跳过反趋势信号
```

---

### ★★ 优先级 4：调整 R:R — 用 MM 目标替代 1:1

**修改**：
- 入场后第一目标 = 1×R（取部分仓位）
- 第二目标 = **MM**（前一腿长度的等长投影）
- 整体加权目标 ≈ 1.5R

**依据**：
- 1:1 R:R 在低胜率策略下不能盈利（需 ≥ 50%）
- 当前 28.6% 胜率下，**需 R:R ≥ 2.5** 才能盈亏平衡
- Al Brooks 推荐 BO 信号优先取 MM 目标

**计算**：
- 若胜率维持 28.6%，需 R:R ≥ 2.5：win 71.4 / loss 28.6 → 0.286 × 2.5 = 0.715，刚好覆盖 0.286 损失
- 若过滤后胜率提到 50%，1:1 R:R 即可盈亏平衡，1.5:1 即正期望

---

### ★ 优先级 5：信号棒尺寸过滤

**修改**：仅当 2 根信号 K 线**实体之和 > 当日 ATR 的 30%** 时入场，过滤 doji 和小实体。

**依据**：
- 2 bar close-on-extreme 在大实体时是真信号（强 urgency）
- 在小实体时是噪声（接近 doji，多空均衡）

---

### ★ 优先级 6：避开重要 S/R 反向信号

**修改**：信号方向若指向最近的强 S/R（PDH/PDL/round number/MM target）且距离 < 5 pts，跳过 —— 价格大概率在该 S/R 拒绝。

---

## 四、综合优化策略（V2）

```
入场前必须全部满足：
1. 时间窗口   : 10:30-12:00 ET（跳过 OR 形成期）
2. 背景过滤   : 信号 K 线收盘已 BO OR 或脱离前 5 根 TR
3. 趋势对齐   : D1 bias 与信号方向一致 OR D1 中性
4. 信号强度   : 2 根信号 K 线实体 > 当日 ATR × 30%
5. 不撞 S/R  : 目标方向最近 S/R 距离 > 5 pts

入场：信号 K 线收盘后下一根 K 线 open
止损：2 根信号 K 线 swing 外 1 tick
目标：分两段
  - T1 = entry ± 1×R (50% 仓位)
  - T2 = entry ± MM (50% 仓位)
```

**预期改善**：
- 时间过滤可将信号数减少约 60%（去掉 EARLY 6 笔），但保留质量
- 背景过滤进一步剔除 IN_PDR 低质信号
- 趋势对齐降低 COUNTER 单亏损
- 加权 R:R ≈ 1.5 提升期望

---

## 五、回测样本局限性

- 仅 10 个交易日，样本量小（n=7），统计意义有限
- 4 月下半月行情背景（MES TR 7121-7185 区间内震荡反复）使 EARLY 时段策略表现尤其差
- 建议**扩展回测周期至最少 60 个交易日**（约 3 个月），且包含 trending day 与 range day 的混合样本

## 六、下一步行动建议

1. ✅ 已完成：基础回测与背景标签
2. ⏳ 实施 V2 优化策略（时间 + 背景 + 趋势 + 信号强度 4 重过滤）
3. ⏳ 扩展回测至 2026-01-01 起至今（~80 个交易日）
4. ⏳ 对每笔信号进行 Al Brooks 价格行为人工标注，比较"机器规则"与"人工解读"差异
5. ⏳ 加入 trailing stop（跟随 EMA20）以捕捉 trending day 的额外利润

---

## 七、文件清单

- [`backtest_2bar_close_extreme.py`](backtest_2bar_close_extreme.py) — 回测脚本
- [`backtest_results.csv`](backtest_results.csv) — 逐笔交易明细
- [`backtest_report.md`](backtest_report.md) — 回测原始报告（按维度分组）
- [`optimization_analysis.md`](optimization_analysis.md) — 本文件，优化建议
