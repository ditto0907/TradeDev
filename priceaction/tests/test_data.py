"""
Synthetic MES OHLCV test data generator.

Produces realistic-looking Micro E-mini S&P 500 (MES) bar data using a
geometric Brownian motion model with intraday volatility patterns and
occasional trend/range regimes.

Used as a fallback when IB TWS is not running, so the frontend chart and
price-action analysis can be tested without a live data connection.

Usage:
    from test_data import generate_bars
    bars_5min = generate_bars(n=500, bar_minutes=5)
    bars_1min = generate_bars(n=500, bar_minutes=1)
"""
import math
import random
import time
from datetime import datetime, timedelta, timezone
from typing import List

# ── Parameters ────────────────────────────────────────────────────────────────

MES_START_PRICE   = 5_320.00   # realistic MES level
MES_ANNUAL_VOL    = 0.18       # ~18% annualised volatility (S&P ~16-20%)
MES_TICK          = 0.25       # minimum price increment
TRADING_DAYS_YEAR = 252
MINUTES_PER_DAY   = 390        # regular RTH session (9:30–16:00 ET)

# Extended-hours trading (CME Globex: ~23h/day Mon–Fri)
GLOBEX_MINUTES_DAY = 1_380

# ── Helpers ───────────────────────────────────────────────────────────────────

def _round_tick(price: float, tick: float = MES_TICK) -> float:
    return round(round(price / tick) * tick, 4)


def _intraday_vol_multiplier(minute_of_day: int) -> float:
    """
    Intraday volatility U-shape: higher at open (9:30), mid-day dip, spike at
    close (15:45–16:00). minute_of_day is minutes since midnight ET.
    """
    # Map to session minutes (0 = 9:30 open, 390 = 16:00 close)
    session_open  = 9 * 60 + 30
    session_close = 16 * 60
    m = minute_of_day - session_open

    if m < 0 or m > (session_close - session_open):
        # Pre/post market — lower vol
        return 0.55

    # Normalize to [0, 1]
    frac = m / (session_close - session_open)

    # U-shape: high at 0 and 1, low in middle
    u = 1.0 - 2.8 * frac * (1.0 - frac)  # parabola dipping at frac=0.5
    return max(0.5, min(1.8, 1.0 + 0.55 * u))


def _is_trading_time(dt: datetime) -> bool:
    """True if dt falls in CME Globex hours (Sun 17:00 – Fri 16:00 CT ≈ Sun 18:00 – Fri 17:00 ET)."""
    # Simplified: skip Saturdays and Sunday before 18:00 ET
    weekday = dt.weekday()  # 0=Mon … 6=Sun
    if weekday == 5:         # Saturday — no trading
        return False
    if weekday == 6 and dt.hour < 18:
        return False
    return True


# ── Regime Engine ─────────────────────────────────────────────────────────────

class _RegimeEngine:
    """
    Randomly alternates between three market regimes to produce realistic
    trend/range/reversal structures that the price-action analyzer can detect.
    """
    REGIMES = [
        {"name": "markup",       "drift": +0.0003,  "vol_mult": 1.0,  "min_bars": 30, "max_bars": 80},
        {"name": "markdown",     "drift": -0.0003,  "vol_mult": 1.1,  "min_bars": 25, "max_bars": 70},
        {"name": "accumulation", "drift": +0.00005, "vol_mult": 0.55, "min_bars": 20, "max_bars": 60},
        {"name": "distribution", "drift": -0.00005, "vol_mult": 0.50, "min_bars": 20, "max_bars": 55},
    ]

    def __init__(self, rng: random.Random):
        self._rng   = rng
        self._regime = self._rng.choice(self.REGIMES)
        self._bars_left = self._rng.randint(
            self._regime["min_bars"], self._regime["max_bars"]
        )

    def step(self):
        self._bars_left -= 1
        if self._bars_left <= 0:
            # Transition: markup → distribution or accumulation; markdown → markup etc.
            transitions = {
                "markup":       ["distribution", "accumulation"],
                "markdown":     ["accumulation", "markup"],
                "accumulation": ["markup", "markup", "markdown"],
                "distribution": ["markdown", "markdown", "accumulation"],
            }
            next_name = self._rng.choice(transitions[self._regime["name"]])
            self._regime = next(r for r in self.REGIMES if r["name"] == next_name)
            self._bars_left = self._rng.randint(
                self._regime["min_bars"], self._regime["max_bars"]
            )

    @property
    def drift(self) -> float:
        return self._regime["drift"]

    @property
    def vol_mult(self) -> float:
        return self._regime["vol_mult"]


# ── Main Generator ────────────────────────────────────────────────────────────

def generate_bars(
    n: int = 500,
    bar_minutes: int = 5,
    end_time: datetime = None,
    seed: int = 42,
) -> List[dict]:
    """
    Generate `n` synthetic MES OHLCV bars ending at `end_time` (UTC).

    Each bar is a dict compatible with the server's bar format:
        {"time": int (unix seconds), "open": float, "high": float,
         "low": float, "close": float, "volume": int}

    Parameters
    ----------
    n           : Number of bars to generate (500 recommended for test).
    bar_minutes : Bar size in minutes (1 or 5).
    end_time    : End datetime (UTC). Defaults to current time rounded down.
    seed        : Random seed for reproducibility.
    """
    rng = random.Random(seed)

    if end_time is None:
        now = datetime.now(tz=timezone.utc)
        # Round down to nearest bar boundary
        rounded_minute = (now.minute // bar_minutes) * bar_minutes
        end_time = now.replace(minute=rounded_minute, second=0, microsecond=0)

    # Bar timestamps (go backwards from end_time, skip non-trading slots)
    timestamps = []
    dt = end_time
    while len(timestamps) < n:
        if _is_trading_time(dt):
            timestamps.append(dt)
        dt -= timedelta(minutes=bar_minutes)

    timestamps.reverse()  # chronological order

    # Per-bar volatility: σ per bar
    bars_per_year = TRADING_DAYS_YEAR * MINUTES_PER_DAY / bar_minutes
    sigma_per_bar = MES_ANNUAL_VOL / math.sqrt(bars_per_year)

    regime  = _RegimeEngine(rng)
    price   = MES_START_PRICE
    bars    = []

    for dt in timestamps:
        minute_of_day = dt.hour * 60 + dt.minute
        iv_mult = _intraday_vol_multiplier(minute_of_day)

        # Effective per-bar σ adjusted by regime and intraday vol
        sigma = price * sigma_per_bar * regime.vol_mult * iv_mult
        drift = price * regime.drift

        o = _round_tick(price)

        # Simulate intra-bar path with 4 sub-steps
        sub_prices = [price]
        for _ in range(4):
            sub_prices.append(sub_prices[-1] + drift / 4 + rng.gauss(0, sigma / 2))

        c = _round_tick(sub_prices[-1])
        h = _round_tick(max(sub_prices) + abs(rng.gauss(0, sigma * 0.3)))
        l = _round_tick(min(sub_prices) - abs(rng.gauss(0, sigma * 0.3)))

        # Sanity: high ≥ max(o,c), low ≤ min(o,c)
        h = max(h, o, c)
        l = min(l, o, c)

        # Volume: higher during RTH open/close, lower pre/post market
        session_open = 9 * 60 + 30
        session_min  = minute_of_day - session_open
        if 0 <= session_min <= 390:
            base_vol = rng.randint(300, 1_400)
            if session_min < 30 or session_min > 350:   # open/close spike
                base_vol = rng.randint(800, 3_000)
        else:
            base_vol = rng.randint(50, 300)             # overnight

        bars.append({
            "time":   int(dt.timestamp()),
            "open":   o,
            "high":   h,
            "low":    l,
            "close":  c,
            "volume": base_vol,
        })

        price = c
        regime.step()

    return bars


# ── Standalone preview ────────────────────────────────────────────────────────

if __name__ == "__main__":
    bars5 = generate_bars(n=500, bar_minutes=5)
    bars1 = generate_bars(n=500, bar_minutes=1)

    print(f"Generated {len(bars5)} 5-min bars  |  {len(bars1)} 1-min bars")
    print("\nFirst 5min bar:", bars5[0])
    print("Last  5min bar:", bars5[-1])

    prices = [b["close"] for b in bars5]
    print(f"\nPrice range: {min(prices):.2f} – {max(prices):.2f}")
    print(f"Start: {prices[0]:.2f}   End: {prices[-1]:.2f}")

    from datetime import datetime, timezone
    print("\nSample bars:")
    for b in bars5[::50]:
        dt = datetime.fromtimestamp(b["time"], tz=timezone.utc)
        print(f"  {dt.strftime('%Y-%m-%d %H:%M')}  O={b['open']:8.2f}  H={b['high']:8.2f}"
              f"  L={b['low']:8.2f}  C={b['close']:8.2f}  V={b['volume']:5d}")
