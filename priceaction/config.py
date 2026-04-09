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

# ─── MES Contract ────────────────────────────────────────────────────────────
MES_SYMBOL = "MES"
MES_SEC_TYPE = "CONTFUT"   # Continuous front-month contract (auto-rolls)
MES_EXCHANGE = "CME"
MES_CURRENCY = "USD"

# Historical data fetch window on startup
HISTORY_DURATION_1MIN = "2 D"   # last 2 days of 1-min bars
HISTORY_DURATION_5MIN = "5 D"   # last 5 days of 5-min bars
MAX_BARS_IN_MEMORY = 5000        # cap per bar size

# ─── Google Sheets ────────────────────────────────────────────────────────────
GOOGLE_CREDENTIALS_PATH = BASE_DIR / "credentials" / "service_account.json"
GOOGLE_SHEET_NAME = "MES_KLine_Data"   # Name of your Google Sheet (must share with service account)
GOOGLE_SHEET_ID = ""                   # Optional: set spreadsheet ID directly to skip name search
WORKSHEET_1MIN = "1min"
WORKSHEET_5MIN = "5min"
SHEETS_WRITE_INTERVAL_SECONDS = 30    # Buffer real-time bars and flush every N seconds

# ─── FastAPI Server ───────────────────────────────────────────────────────────
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000

# ─── Price Action Analysis ────────────────────────────────────────────────────
SR_LOOKBACK = 5           # bars on each side for swing high/low detection
SR_MERGE_PCT = 0.15       # merge S/R levels within 0.15% of each other
SR_MIN_TOUCHES = 2        # minimum touches to count as a level
ANALYSIS_BAR_SIZE = "5min"  # run analysis on 5-min bars
