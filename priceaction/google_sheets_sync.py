"""
Google Sheets Sync — writes MES 5min OHLCV bars to Google Sheets.

Setup (one-time):
  1. Go to https://console.cloud.google.com and create a project
  2. Enable "Google Sheets API" and "Google Drive API"
  3. Create a Service Account → Actions → Manage Keys → Add Key → JSON
  4. Download the JSON key → place at priceaction/credentials/service_account.json
  5. Create a Google Sheet named "MES_KLine_Data"
  6. Share the sheet with the service account email (found in the JSON under "client_email")
     and give it Editor access

Usage:
    sync = GoogleSheetsSync()
    sync.authenticate()
    sync.initial_upload(bars_5min)
    sync.append_new_bar("5min", bar_dict)  # call on each new bar
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import config

logger = logging.getLogger(__name__)

HEADERS = ["Datetime", "Open", "High", "Low", "Close", "Volume"]


def _bar_to_row(bar: dict) -> list:
    """Convert a bar dict to a Google Sheets row."""
    dt = datetime.fromtimestamp(bar["time"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return [dt, bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]]


class GoogleSheetsSync:
    """
    Manages writing OHLCV bars to a Google Sheet.

    One worksheet is maintained:
        - "5min"  — 5-minute bars

    Real-time bars are buffered and flushed every SHEETS_WRITE_INTERVAL_SECONDS
    seconds to stay within Google's rate limits.
    """

    def __init__(self):
        self._gc = None            # gspread client
        self._sheet = None         # Spreadsheet object
        self._worksheets: Dict[str, object] = {}
        self._buffer: Dict[str, List[dict]] = {"5min": []}
        self._last_flush = 0.0

    def authenticate(self) -> bool:
        """
        Authenticate with Google Sheets using a service account JSON.
        Returns True on success, False if credentials file is missing.
        """
        creds_path = config.GOOGLE_CREDENTIALS_PATH
        if not Path(creds_path).exists():
            logger.warning(
                "Google credentials not found at %s. "
                "Google Sheets sync is DISABLED. "
                "See google_sheets_sync.py docstring for setup instructions.",
                creds_path,
            )
            return False

        try:
            import gspread
            from google.oauth2.service_account import Credentials

            scopes = [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
            ]
            creds = Credentials.from_service_account_file(str(creds_path), scopes=scopes)
            self._gc = gspread.authorize(creds)

            if config.GOOGLE_SHEET_ID:
                self._sheet = self._gc.open_by_key(config.GOOGLE_SHEET_ID)
            else:
                self._sheet = self._gc.open(config.GOOGLE_SHEET_NAME)

            self._ensure_worksheets()
            logger.info("Google Sheets authenticated. Sheet: %s", self._sheet.title)
            return True
        except Exception as e:
            logger.error("Google Sheets authentication failed: %s", e)
            return False

    def _ensure_worksheets(self):
        """Create worksheets if they don't exist and set headers."""
        existing = {ws.title: ws for ws in self._sheet.worksheets()}
        for name in (config.WORKSHEET_5MIN,):
            if name not in existing:
                ws = self._sheet.add_worksheet(title=name, rows=10000, cols=6)
                ws.append_row(HEADERS)
                logger.info("Created worksheet '%s'", name)
            else:
                ws = existing[name]
            self._worksheets[name] = ws

    def _ws(self, bar_size_key: str):
        """Return the worksheet for bar size key '5min'."""
        name = config.WORKSHEET_5MIN
        return self._worksheets.get(name)

    # ─── Initial Upload ───────────────────────────────────────────────────────

    def initial_upload(self, bars_5min: List[dict]):
        """
        Upload all historical bars. Clears existing data and rewrites from scratch.
        Uses batch updates to minimize API calls.
        """
        if not self._gc:
            return
        for key, bars in [("5min", bars_5min)]:
            ws = self._ws(key)
            if not ws or not bars:
                continue
            logger.info("Uploading %d %s bars to Google Sheets...", len(bars), key)
            try:
                # Clear existing data (keep header)
                ws.clear()
                # Write header + all data in one batch call
                rows = [HEADERS] + [_bar_to_row(b) for b in bars]
                ws.update("A1", rows)
                logger.info("Uploaded %d %s bars", len(bars), key)
            except Exception as e:
                logger.error("Initial upload failed for %s: %s", key, e)

    # ─── Real-time Buffered Append ─────────────────────────────────────────────

    def buffer_bar(self, bar_size_key: str, bar: dict):
        """
        Buffer a new bar. Call flush_buffer() periodically to write to Sheets.
        This avoids exceeding Google's 300 req/min rate limit.
        """
        if not self._gc:
            return
        self._buffer[bar_size_key].append(bar)
        now = time.time()
        if now - self._last_flush >= config.SHEETS_WRITE_INTERVAL_SECONDS:
            self.flush_buffer()

    def flush_buffer(self):
        """Write all buffered bars to Google Sheets and clear the buffer."""
        if not self._gc:
            return
        for key in ("5min",):
            bars = self._buffer[key]
            if not bars:
                continue
            ws = self._ws(key)
            if not ws:
                continue
            try:
                rows = [_bar_to_row(b) for b in bars]
                ws.append_rows(rows, value_input_option="USER_ENTERED")
                logger.debug("Flushed %d %s bars to Google Sheets", len(rows), key)
                self._buffer[key] = []
            except Exception as e:
                logger.error("Flush failed for %s: %s", key, e)
        self._last_flush = time.time()


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sync = GoogleSheetsSync()
    ok = sync.authenticate()
    if ok:
        # Write a few sample bars
        sample_bars = [
            {"time": 1700000000, "open": 5800.0, "high": 5810.0, "low": 5795.0, "close": 5807.0, "volume": 1234},
            {"time": 1700000300, "open": 5807.0, "high": 5815.0, "low": 5803.0, "close": 5812.0, "volume": 987},
        ]
        sync.initial_upload([], sample_bars)
        print("Test upload complete. Check your Google Sheet.")
    else:
        print("Authentication failed. See the setup instructions in google_sheets_sync.py.")
