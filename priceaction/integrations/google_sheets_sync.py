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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from core import config

logger = logging.getLogger(__name__)

HEADERS = ["Datetime", "Open", "High", "Low", "Close", "Volume"]

# Drop the oldest buffered bars beyond this cap if Google Sheets is
# unreachable for a long time, so the buffer can't grow without bound.
_MAX_BUFFER_BARS = 5000


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
        self._buffer_lock = threading.Lock()
        self._flushing = False     # guards against overlapping flush threads

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
            # gspread.APIError stringifies as the HTTP Response repr (e.g.
            # "<Response [200]>"), which hides the real cause.  Dig into the
            # response body / attributes so the log actually says what went
            # wrong (permissions, quota, malformed JSON, etc.).
            detail = repr(e)
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    body = resp.text
                except Exception:
                    body = "<unreadable>"
                detail = (
                    f"{type(e).__name__} status={getattr(resp, 'status_code', '?')} "
                    f"body={body[:500]}"
                )
            logger.error(
                "Google Sheets authentication failed: %s",
                detail,
                exc_info=True,
            )
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
        Buffer a new bar. Flushing happens on a background thread so the
        caller (the asyncio event-loop tick handler) is never blocked on
        synchronous gspread network IO.
        This avoids exceeding Google's 300 req/min rate limit.
        """
        if not self._gc:
            return
        with self._buffer_lock:
            self._buffer[bar_size_key].append(bar)
            # Bound the buffer: drop oldest if Google has been unreachable.
            buf = self._buffer[bar_size_key]
            if len(buf) > _MAX_BUFFER_BARS:
                overflow = len(buf) - _MAX_BUFFER_BARS
                del buf[:overflow]
                logger.warning("Sheets buffer over cap — dropped %d oldest %s bars",
                               overflow, bar_size_key)
        now = time.time()
        if now - self._last_flush >= config.SHEETS_WRITE_INTERVAL_SECONDS:
            self.flush_async()

    def flush_async(self):
        """Run flush_buffer() on a daemon thread (non-blocking)."""
        if not self._gc or self._flushing:
            return
        self._flushing = True
        self._last_flush = time.time()  # reserve the window immediately
        t = threading.Thread(target=self._flush_worker, daemon=True)
        t.start()

    def _flush_worker(self):
        try:
            self.flush_buffer()
        finally:
            self._flushing = False

    def flush_buffer(self):
        """Write all buffered bars to Google Sheets and clear the buffer.

        Safe to call directly (e.g. at shutdown) — performs synchronous
        gspread IO, so do NOT call from the event-loop thread; use
        flush_async() for that.
        """
        if not self._gc:
            return
        for key in ("5min",):
            with self._buffer_lock:
                bars = self._buffer[key]
                if not bars:
                    continue
                # Take a snapshot and clear under the lock; restore on failure.
                pending = list(bars)
                self._buffer[key] = []
            ws = self._ws(key)
            if not ws:
                # Put them back so they aren't lost.
                with self._buffer_lock:
                    self._buffer[key] = pending + self._buffer[key]
                continue
            try:
                rows = [_bar_to_row(b) for b in pending]
                ws.append_rows(rows, value_input_option="USER_ENTERED")
                logger.debug("Flushed %d %s bars to Google Sheets", len(rows), key)
            except Exception as e:
                logger.error("Flush failed for %s: %s", key, e)
                # Re-buffer the unsent bars (respect the cap).
                with self._buffer_lock:
                    merged = pending + self._buffer[key]
                    self._buffer[key] = merged[-_MAX_BUFFER_BARS:]
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
        sync.initial_upload(sample_bars)
        print("Test upload complete. Check your Google Sheet.")
    else:
        print("Authentication failed. See the setup instructions in google_sheets_sync.py.")
