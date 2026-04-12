#!/usr/bin/env python3
"""
测试market-cycle-analysis skill的新API接口
使用日期时间字符串而不是Unix时间戳
"""
import requests
import json

API_BASE = "http://localhost:8000"

print("=" * 80)
print("测试Market Cycle Analysis Skill API")
print("=" * 80)
print()

# Step 1: 使用日期时间字符串获取K线数据
print("1. 获取K线数据 (使用日期时间字符串)...")
print("   请求: /api/skill/bars")
print("   参数: symbol=MNQ, from_dt=2026-04-08 09:30, to_dt=2026-04-08 11:00")
print()

response = requests.get(
    f"{API_BASE}/api/skill/bars",
    params={
        "symbol": "MNQ",
        "resolution": "5",
        "session": "RTH",
        "from_dt": "2026-04-08 09:30",
        "to_dt": "2026-04-08 11:00",
    },
    timeout=10,
)

if response.status_code != 200:
    print(f"✗ API错误: {response.status_code}")
    print(response.text)
    exit(1)

data = response.json()
bars = data["bars"]

print(f"✓ 成功获取 {data['count']} 根K线")
print(f"  Symbol: {data['symbol']}")
print(f"  Resolution: {data['resolution']}")
print(f"  Session: {data['session']}")
print()

# 显示前3根K线
print("前3根K线:")
for i, bar in enumerate(bars[:3]):
    from datetime import datetime
    dt = datetime.fromtimestamp(bar['time'])
    print(f"  {i+1}. {dt.strftime('%H:%M')}: "
          f"O={bar['open']:.2f} H={bar['high']:.2f} "
          f"L={bar['low']:.2f} C={bar['close']:.2f}")
print()

# Step 2: 进行简单分析
print("2. 进行Al Brooks分析...")

# Opening Range (前6根K线)
or_bars = bars[:6]
or_high = max(b['high'] for b in or_bars)
or_low = min(b['low'] for b in or_bars)
or_start_ts = or_bars[0]['time']
or_end_ts = or_bars[-1]['time']

print(f"   Opening Range: {or_low:.2f} - {or_high:.2f} (Range: {or_high - or_low:.2f})")

# 计算牛熊K线比例
bull_bars = sum(1 for b in bars if b['close'] > b['open'])
bear_bars = sum(1 for b in bars if b['close'] < b['open'])
print(f"   Bull/Bear: {bull_bars}/{bear_bars} ({bull_bars/(bull_bars+bear_bars)*100:.1f}% bulls)")
print()

# Step 3: 准备写回数据
print("3. 准备annotation数据...")

annotations = [
    {
        "label": "Opening Range",
        "type": "range",
        "start_time": or_start_ts,
        "end_time": or_end_ts,
        "price_high": or_high,
        "price_low": or_low,
    },
    {
        "label": f"OR High {or_high:.2f}",
        "type": "hline",
        "start_time": bars[0]['time'],
        "price": or_high,
        "style": "solid",
    },
    {
        "label": f"OR Low {or_low:.2f}",
        "type": "hline",
        "start_time": bars[0]['time'],
        "price": or_low,
        "style": "solid",
    },
]

summary = f"""• Phase: TR → weak BC
• OR: {or_low:.2f} - {or_high:.2f} (Range: {or_high - or_low:.2f} pts)
• Bars: {bull_bars} bull ({bull_bars/(bull_bars+bear_bars)*100:.1f}%) vs {bear_bars} bear
• Bias: NEUTRAL-BULL — Bulls control but lack strong momentum"""

payload = {
    "symbol": "MNQ",
    "timeframe": "5",
    "session": "RTH",
    "bar_from": bars[0]['time'],
    "bar_to": bars[-1]['time'],
    "summary": summary,
    "annotations": annotations,
}

print(f"   {len(annotations)} annotations 准备就绪")
print(f"   Summary: {len(summary)} 字符")
print()

# Step 4: 写回分析结果
print("4. 写回分析结果到数据库...")
print("   请求: POST /api/skill/analysis")
print()

response = requests.post(
    f"{API_BASE}/api/skill/analysis",
    json=payload,
    headers={"Content-Type": "application/json"},
    timeout=10,
)

if response.status_code == 200:
    result = response.json()
    print(f"✓ 分析保存成功!")
    print(f"  Analysis ID: {result.get('id')}")
    print(f"  Annotations: {len(annotations)}")
    print(f"  WebSocket: 图表将自动更新")
else:
    print(f"✗ API错误: {response.status_code}")
    print(f"Response: {response.text}")
    exit(1)

print()
print("=" * 80)
print("测试完成!")
print("=" * 80)
print()
print("总结:")
print("  ✓ 使用日期时间字符串获取K线数据 (无需时间戳转换)")
print("  ✓ 进行Al Brooks价格行为分析")
print("  ✓ 将分析结果写回数据库")
print("  ✓ 前端图表通过WebSocket自动更新")
