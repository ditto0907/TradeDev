"""Google Sheets writer for the strategy research workflow.

Wraps the gspread client and exposes high-level operations:
  - append_signals(records)         → writes B–I columns, returns row indices
  - update_analysis(row_idx, ...)   → writes J–O columns (LLM analysis)
  - read_all_signals()              → returns all data rows for inspection

Sheet layout (see doc/strategy_research_v1.md §4.2):
  Row 1: category headers (PB, Pattern, Context...)
  Row 2: sub-headers (Date, Bar Cnt, ...)
  Row 3+: data rows
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────
DEFAULT_SHEET_ID = "1rzRkEaFUum6omr2xNd36CI0brq-UifsnTRhT0tSBjhs"
DEFAULT_WORKSHEET_GID = 622455454
DEFAULT_CREDS_PATH = (
    Path(__file__).resolve().parent.parent / "credentials" / "service_account.json"
)
HEADER_ROWS = 2
DATA_START_ROW = 3

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# Column letters (A=1)
COL = {
    "Date":            "B",
    "Bar Cnt":         "C",
    "Signal Strength": "D",
    "COH":             "E",
    "Overlapping":     "F",
    "PB Bars":         "G",
    "PB Strength":     "H",
    "FT":              "I",
    "Pattern":         "J",
    "Minor/Major":     "K",
    "Leg Cnt":         "L",
    "Context":         "M",
    "SR":              "N",
    "SR Detail":       "O",
    "背景支持":         "P",
    "支持理由":         "Q",
    "1:1":              "R",
    "2:1":              "S",
}


class StrategySheet:
    """Thin wrapper over a gspread Worksheet."""

    def __init__(
        self,
        sheet_id: str = DEFAULT_SHEET_ID,
        worksheet_gid: int = DEFAULT_WORKSHEET_GID,
        creds_path: Optional[Path] = None,
    ):
        self.sheet_id = sheet_id
        self.worksheet_gid = worksheet_gid
        self.creds_path = Path(creds_path) if creds_path else DEFAULT_CREDS_PATH
        self._gc: Optional[gspread.Client] = None
        self._ws: Optional[gspread.Worksheet] = None

    def authenticate(self) -> None:
        if not self.creds_path.exists():
            raise FileNotFoundError(
                f"service account JSON not found: {self.creds_path}"
            )
        creds = Credentials.from_service_account_file(
            str(self.creds_path), scopes=SCOPES
        )
        self._gc = gspread.authorize(creds)
        sh = self._gc.open_by_key(self.sheet_id)
        ws = next(
            (w for w in sh.worksheets() if w.id == self.worksheet_gid), None
        )
        if ws is None:
            raise RuntimeError(
                f"worksheet gid={self.worksheet_gid} not found in "
                f"spreadsheet {self.sheet_id}"
            )
        self._ws = ws
        logger.info(
            "[StrategySheet] connected to %r / %r", sh.title, ws.title
        )

    @property
    def ws(self) -> gspread.Worksheet:
        if self._ws is None:
            self.authenticate()
        assert self._ws is not None
        return self._ws

    # ─── Read ─────────────────────────────────────────────────────────────
    def read_all_signals(self) -> list[dict]:
        """Return all data rows (row >= 3) as dicts keyed by sub-header column.

        Includes a synthetic ``_row`` field with the absolute sheet row number.
        """
        all_rows = self.ws.get_all_values()
        if len(all_rows) <= HEADER_ROWS:
            return []
        sub_headers = all_rows[1]  # row 2 (0-indexed)
        out = []
        for i, row in enumerate(all_rows[HEADER_ROWS:], start=DATA_START_ROW):
            if not any(c.strip() for c in row):
                continue  # skip blank rows
            entry = {"_row": i}
            for j, h in enumerate(sub_headers):
                if h:
                    entry[h] = row[j] if j < len(row) else ""
            out.append(entry)
        return out

    def find_next_data_row(self) -> int:
        """First empty row at or after DATA_START_ROW."""
        rows = self.ws.get_all_values()
        for i in range(HEADER_ROWS, len(rows)):
            if not any(c.strip() for c in rows[i]):
                return i + 1  # 1-indexed
        return len(rows) + 1

    # ─── Write ────────────────────────────────────────────────────────────
    def append_signals(self, records: Iterable[dict]) -> list[int]:
        """Append signal records to the sheet (only the detector-filled columns).

        Each *record* must have keys:
          date, bar_cnt, signal_strength, coh_l, overlapping,
          pb_bars, pb_strength, ft

        Returns the list of absolute row numbers written.
        """
        records = list(records)
        if not records:
            return []
        # Find first empty row
        start_row = self.find_next_data_row()
        rows_payload = []
        for r in records:
            row = ["" for _ in range(17)]  # cols A..Q (17 cols)
            row[1]  = r["date"]               # B
            row[2]  = r["bar_cnt"]            # C
            row[3]  = r["signal_strength"]    # D
            row[4]  = f"{r['coh_l']:.2f}"     # E
            row[5]  = r["overlapping"]        # F
            row[6]  = r["pb_bars"]            # G
            row[7]  = r["pb_strength"]        # H
            row[8]  = r["ft"]                 # I
            rows_payload.append(row)

        end_row = start_row + len(rows_payload) - 1
        rng = f"A{start_row}:Q{end_row}"
        self.ws.update(rng, rows_payload, value_input_option="USER_ENTERED")
        logger.info(
            "[StrategySheet] appended %d signal rows at %s",
            len(rows_payload), rng,
        )
        return list(range(start_row, end_row + 1))

    def update_analysis(
        self,
        row: int,
        pattern: Optional[str] = None,
        minor_major: Optional[str] = None,
        leg_cnt: Optional[str] = None,
        context: Optional[str] = None,
        sr: Optional[str] = None,
        sr_detail: Optional[str] = None,
    ) -> None:
        """Update the LLM-analysis columns J–O for a single existing row."""
        self.bulk_update_analysis([{
            "row": row,
            "pattern": pattern,
            "minor_major": minor_major,
            "leg_cnt": leg_cnt,
            "context": context,
            "sr": sr,
            "sr_detail": sr_detail,
        }])

    def bulk_update_analysis(self, items: list[dict]) -> None:
        """Update J–O columns for multiple rows in a single API call.

        Each item is a dict with keys:
          row, pattern, minor_major, leg_cnt, context, sr, sr_detail
        (any key may be absent or None — skipped)
        """
        field_map = {
            "pattern":     COL["Pattern"],
            "minor_major": COL["Minor/Major"],
            "leg_cnt":     COL["Leg Cnt"],
            "context":     COL["Context"],
            "sr":          COL["SR"],
            "sr_detail":   COL["SR Detail"],
        }
        data = []
        for item in items:
            row = item["row"]
            for key, col in field_map.items():
                val = item.get(key)
                if val is not None:
                    data.append({"range": f"{col}{row}", "values": [[val]]})
        if not data:
            return
        self.ws.batch_update(data, value_input_option="USER_ENTERED")
        logger.info(
            "[StrategySheet] bulk_update: %d cell writes across %d rows",
            len(data), len(items),
        )


    def write_outcome_headers(self) -> None:
        """Write sub-headers for outcome columns R and S in row 2."""
        self.ws.batch_update(
            [
                {"range": "R2", "values": [["1:1"]]},
                {"range": "S2", "values": [["2:1"]]},
            ],
            value_input_option="USER_ENTERED",
        )
        logger.info("[StrategySheet] wrote outcome headers R2/S2")

    def bulk_update_outcomes(self, items: list[dict]) -> None:
        """Write 1:1 and 2:1 outcome (Y/N) columns R and S in a single API call.

        Each item: {"row": int, "r1": "Y"|"N", "r2": "Y"|"N"}
        """
        data = []
        for item in items:
            row = item["row"]
            if "r1" in item and item["r1"] is not None:
                data.append({"range": f"R{row}", "values": [[item["r1"]]]})
            if "r2" in item and item["r2"] is not None:
                data.append({"range": f"S{row}", "values": [[item["r2"]]]})
        if not data:
            return
        self.ws.batch_update(data, value_input_option="USER_ENTERED")
        logger.info(
            "[StrategySheet] bulk_update_outcomes: %d cells, %d rows",
            len(data), len(items),
        )


__all__ = ["StrategySheet"]
