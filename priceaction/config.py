"""
Configuration for the Price Action trading system.

Before running:
1. Set GOOGLE_SHEET_ID or GOOGLE_SHEET_NAME to your sheet
2. Place your service account JSON at credentials/service_account.json
3. Make sure IB TWS/Gateway is running on the configured host/port
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# ─── Interactive Brokers ──────────────────────────────────────────────────────
IB_HOST = "127.0.0.1"
IB_PORT = 7497         # TWS paper/live. Use 7496 for live, 4001 for IB Gateway paper
IB_CLIENT_ID = 10      # Use 10 to avoid conflicts with RiskControl (uses 0)

# ─── MES Contract (legacy — kept for backward compatibility) ─────────────────
MES_SYMBOL = "MES"
MES_SEC_TYPE = "CONTFUT"   # Continuous front-month contract (auto-rolls)
MES_EXCHANGE = "CME"
MES_CURRENCY = "USD"

# Historical data fetch window on startup
HISTORY_DURATION_5MIN = "5 D"   # last 5 days of 5-min bars
HISTORY_DURATION_1D   = "2 Y"   # 2 years of daily bars
MAX_BARS_IN_MEMORY = 5000        # cap per bar size

# Extra symbols to prefetch on startup (if DB has no data)
# ib_symbol: IB contract symbol, symbol: our display/DB key
EXTRA_SYMBOLS = [
    {"symbol": "MNQ",     "ib_symbol": "MNQ", "exchange": "CME",   "currency": "USD"},
    {"symbol": "NK225MC", "ib_symbol": "N225MC", "exchange": "OSE.JPN", "currency": "JPY"},
    {"symbol": "MGC",     "ib_symbol": "MGC", "exchange": "COMEX", "currency": "USD"},
]

# ─── Unified Instrument Registry ─────────────────────────────────────────────
# Central source-of-truth for all supported symbols.
# Keys: our display/DB symbol name.
INSTRUMENTS = {
    "MES": {
        "ib_symbol": "MES",
        "exchange": "CME",
        "currency": "USD",
        "timezone": "America/New_York",
        "contract_type": "quarterly",          # quarterly rollover
        "contract_months": [3, 6, 9, 12],      # H M U Z
        "rth_start": (9, 30),                   # local-time RTH window
        "rth_end":   (16, 0),
        # CME equity-index rollover: 8th business day of the contract
        # month (published as the CME "Quarterly Roll" date).
        "rollover_rule": {"type": "nth_business_day", "n": 8},
    },
    "MNQ": {
        "ib_symbol": "MNQ",
        "exchange": "CME",
        "currency": "USD",
        "timezone": "America/New_York",
        "contract_type": "quarterly",
        "contract_months": [3, 6, 9, 12],
        "rth_start": (9, 30),
        "rth_end":   (16, 0),
        "rollover_rule": {"type": "nth_business_day", "n": 8},
    },
    "NK225MC": {
        "ib_symbol": "N225MC",
        "exchange": "OSE.JPN",
        "currency": "JPY",
        "timezone": "Asia/Tokyo",
        "contract_type": "monthly",            # monthly rollover
        "contract_months": list(range(1, 13)),  # every month
        "rth_start": (8, 45),                   # JST RTH window
        "rth_end":   (15, 45),
        # OSE monthly: Special Quotation is the 2nd Friday; the front
        # month rolls one business day before SQ.
        "rollover_rule": {"type": "second_friday", "offset_bdays": -1},
    },
    "MGC": {
        "ib_symbol": "MGC",
        "exchange": "COMEX",
        "currency": "USD",
        "timezone": "America/New_York",
        "contract_type": "bi-monthly",         # Feb,Apr,Jun,Aug,Oct,Dec
        "contract_months": [2, 4, 6, 8, 10, 12],
        "rth_start": (9, 30),
        "rth_end":   (17, 0),
        # COMEX metal: roll one business day before Last Trading Day
        # (which is itself the 3rd-to-last business day of the month).
        "rollover_rule": {"type": "n_bdays_before_ltd", "n": 1},
    },
}

# ─── Google Sheets ────────────────────────────────────────────────────────────
GOOGLE_CREDENTIALS_PATH = BASE_DIR / "credentials" / "service_account.json"
GOOGLE_SHEET_NAME = "MES_KLine_Data"   # Name of your Google Sheet (must share with service account)
GOOGLE_SHEET_ID = ""                   # Optional: set spreadsheet ID directly to skip name search
WORKSHEET_5MIN = "5min"
SHEETS_WRITE_INTERVAL_SECONDS = 30    # Buffer real-time bars and flush every N seconds

# ─── FastAPI Server ───────────────────────────────────────────────────────────
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000

# ─── Price Action Analysis ────────────────────────────────────────────────────
SR_LOOKBACK = 5           # bars on each side for swing high/low detection
SR_MERGE_PCT = 0.15       # merge S/R levels within 0.15% of each other
SR_MIN_TOUCHES = 2        # minimum touches to count as a level
SR_MAX_LEVELS_PER_SIDE = 4   # keep only major levels per side
SR_MAX_DISTANCE_PCT = 1.2    # ignore levels too far from current price
ANALYSIS_BAR_SIZE = "5min"  # run analysis on 5-min bars

# ─── IBS 2-Bar Strategy ───────────────────────────────────────────────────────
IBS_THRESHOLD        = 0.70    # IBS ≥ threshold → long; IBS ≤ (1-threshold) → short
IBS_SR_PROXIMITY_PCT = 0.30    # % distance to consider "near" an S/R level
IBS_CONTEXT_LOOKBACK = 200     # rolling bars for S/R context (prevents look-ahead bias)
MES_TICK_VALUE       = 5.0     # USD per point for MES ($5/pt)
MAX_STOP_LOSS        = 200.0   # max USD risk per trade; contracts = floor(max_stop / (stop_dist * tick_value))
