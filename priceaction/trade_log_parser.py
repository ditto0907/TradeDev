"""
Trade log parser — reads Topstep and IB trade log files from the data/ directory.

Supported file names (place in priceaction/data/):
  trade_log_topstep          — Topstep / Rithmic / Tradovate CSV export
  trade_log_IB_*             — IB Activity Statement or simple executions CSV

Output (per trade):
  {
    "id":           int,         # sequential ID
    "source":       "topstep" | "ib",
    "symbol":       "MES",
    "direction":    "long" | "short",
    "qty":          int,
    "entry_time":   int,         # UTC epoch seconds
    "entry_price":  float,
    "exit_time":    int | None,  # None = still open
    "exit_price":   float | None,
    "pnl":          float | None,
  }

Topstep expected column names (case-insensitive, any delimiter):
  Entry/Open Time, Exit/Close Time, Direction/Side, Qty/Quantity,
  Entry/Open Price, Exit/Close Price, P/L or Net P/L

IB Activity Statement:
  Section rows starting with "Trades,Data,Order,Futures,..." are parsed.
  Columns: Symbol, Date/Time, Quantity, T. Price, Realized P/L, Code
  Code "O" = open (entry), "C" = close (exit)
  Buys/sells are matched into round-trip trades.

IB simple executions CSV:
  Headers: Symbol, Date/Time, Quantity, Price
  Positive quantity = buy, negative = sell
  Matched into round-trips.
"""

import csv
import glob
import io
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

# ─── Datetime helpers ─────────────────────────────────────────────────────────

_DT_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%y %H:%M:%S",
    "%m/%d/%y %H:%M",
    "%Y%m%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y",
]


def _parse_dt(s: str, tz_offset_h: int = 0) -> Optional[int]:
    """Parse datetime string to UTC epoch seconds.
    tz_offset_h: the timezone offset of the source data in hours (e.g. 8 for UTC+8).
    """
    if not s:
        return None
    s = s.strip().replace(";", " ")   # IB uses semicolons between date and time
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            utc_ts = int(dt.replace(tzinfo=timezone.utc).timestamp()) - tz_offset_h * 3600
            return utc_ts
        except ValueError:
            continue
    logger.debug("Cannot parse datetime: %r", s)
    return None


def _parse_float(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        return float(s.replace(",", "").replace("$", "").strip())
    except (ValueError, AttributeError):
        return None


def _norm(s: str) -> str:
    """Normalise a column header for matching."""
    return re.sub(r"[^a-z0-9]", "", s.lower())

# ─── Topstep / generic per-trade CSV ─────────────────────────────────────────

_TOPSTEP_ENTRY_TIME  = {"entry", "entrytime", "opentime", "opendate", "entrydatetime"}
_TOPSTEP_EXIT_TIME   = {"exit",  "exittime",  "closetime","closedate","exitdatetime"}
_TOPSTEP_DIRECTION   = {"direction", "side", "type", "tradetype"}
_TOPSTEP_ENTRY_PRICE = {"entryprice", "openprice", "avgentryprice"}
_TOPSTEP_EXIT_PRICE  = {"exitprice",  "closeprice","avgexitprice"}
_TOPSTEP_QTY         = {"qty", "quantity", "size", "contracts"}
_TOPSTEP_PNL         = {"pl", "pnl", "netpl", "profitloss", "profit"}


def _find_col(norm_key: str, row_keys: dict) -> Optional[str]:
    """Return the raw key whose normalised name matches one of the target aliases."""
    for raw_key, norm in row_keys.items():
        if norm in norm_key:
            return raw_key
    return None


def _parse_topstep(filepath: Path) -> List[dict]:
    trades = []
    try:
        text = filepath.read_text(encoding="utf-8-sig", errors="replace")
        # Detect delimiter
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t|;")
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        # Normalise header keys
        if reader.fieldnames is None:
            return []
        norm_keys = {k: _norm(k) for k in reader.fieldnames if k}

        for raw_row in reader:
            row = {k: (v or "").strip() for k, v in raw_row.items() if k}
            try:
                def get(aliases):
                    for raw, norm in norm_keys.items():
                        if norm in aliases:
                            return row.get(raw, "")
                    return ""

                entry_raw  = get(_TOPSTEP_ENTRY_TIME)
                exit_raw   = get(_TOPSTEP_EXIT_TIME)
                dir_raw    = get(_TOPSTEP_DIRECTION).lower()
                ep_raw     = get(_TOPSTEP_ENTRY_PRICE)
                xp_raw     = get(_TOPSTEP_EXIT_PRICE)
                qty_raw    = get(_TOPSTEP_QTY)
                pnl_raw    = get(_TOPSTEP_PNL)

                if not entry_raw or not ep_raw:
                    continue

                direction = "long" if any(w in dir_raw for w in ("long", "buy", "b")) else "short"
                entry_ts  = _parse_dt(entry_raw)
                if entry_ts is None:
                    continue

                trades.append({
                    "source":       "topstep",
                    "symbol":       "MES",
                    "direction":    direction,
                    "qty":          int(abs(_parse_float(qty_raw) or 1)),
                    "entry_time":   entry_ts,
                    "entry_price":  _parse_float(ep_raw),
                    "exit_time":    _parse_dt(exit_raw),
                    "exit_price":   _parse_float(xp_raw),
                    "pnl":          _parse_float(pnl_raw),
                })
            except Exception as exc:
                logger.debug("Topstep row skip: %s", exc)

    except Exception as exc:
        logger.warning("Cannot parse Topstep file %s: %s", filepath.name, exc)

    logger.info("Topstep %s: parsed %d trades", filepath.name, len(trades))
    return trades

# ─── IB Activity Statement ────────────────────────────────────────────────────

def _parse_ib_activity(filepath: Path) -> List[dict]:
    """
    IB Flex Query / Activity Statement format.
    Sections start with: Trades,Header,...  Trades,Data,...
    Matches open (Code=O) and close (Code=C/Cx) executions into round-trips.
    """
    trades = []
    opens: List[dict]  = []     # pending open legs
    closes: List[dict] = []     # pending close legs

    try:
        text = filepath.read_text(encoding="utf-8-sig", errors="replace")

        # Check if this is an IB Activity Statement
        if "Trades,Header" not in text and "Trades,Data" not in text:
            return []

        header = None
        for line in text.splitlines():
            parts = line.split(",")
            if len(parts) < 3:
                continue
            if parts[0] == "Trades" and parts[1] == "Header":
                header = [p.strip() for p in parts[2:]]
                continue
            if parts[0] == "Trades" and parts[1] == "Data" and header:
                row = dict(zip(header, [p.strip() for p in parts[2:]]))
                # Only futures MES rows
                asset = row.get("Asset Category", "").strip()
                sym   = row.get("Symbol", "").strip()
                if "Future" not in asset:
                    continue
                if "MES" not in sym:
                    continue

                qty     = _parse_float(row.get("Quantity", ""))
                price   = _parse_float(row.get("T. Price", ""))
                ts      = _parse_dt(row.get("Date/Time", ""))
                pnl     = _parse_float(row.get("Realized P/L", "0"))
                code    = row.get("Code", "")

                if qty is None or price is None or ts is None:
                    continue

                leg = {"ts": ts, "price": price, "qty": abs(qty),
                       "side": "buy" if qty > 0 else "sell", "pnl": pnl}

                # Code "O" = opening, "C" or contains "C" = closing
                if "O" in code and "C" not in code:
                    opens.append(leg)
                else:
                    closes.append(leg)

        # Match opens to closes chronologically
        for o in sorted(opens, key=lambda x: x["ts"]):
            matching = [c for c in closes if abs(c["ts"] - o["ts"]) < 86400 * 5]
            if matching:
                c = min(matching, key=lambda x: abs(x["ts"] - o["ts"]) if x["ts"] >= o["ts"] else float("inf"))
                closes.remove(c)
                direction = "long" if o["side"] == "buy" else "short"
                trades.append({
                    "source":      "ib",
                    "symbol":      "MES",
                    "direction":   direction,
                    "qty":         int(o["qty"]),
                    "entry_time":  o["ts"],
                    "entry_price": o["price"],
                    "exit_time":   c["ts"],
                    "exit_price":  c["price"],
                    "pnl":         c.get("pnl"),
                })
            else:
                direction = "long" if o["side"] == "buy" else "short"
                trades.append({
                    "source":      "ib",
                    "symbol":      "MES",
                    "direction":   direction,
                    "qty":         int(o["qty"]),
                    "entry_time":  o["ts"],
                    "entry_price": o["price"],
                    "exit_time":   None,
                    "exit_price":  None,
                    "pnl":         None,
                })

    except Exception as exc:
        logger.warning("Cannot parse IB activity file %s: %s", filepath.name, exc)

    logger.info("IB Activity %s: parsed %d trades", filepath.name, len(trades))
    return trades


def _parse_ib_simple(filepath: Path) -> List[dict]:
    """
    Simple IB executions CSV.
    Headers: Symbol, Date/Time, Quantity, Price
    Match consecutive buys/sells into round-trips.
    """
    trades = []
    try:
        text = filepath.read_text(encoding="utf-8-sig", errors="replace")
        if "Trades,Header" in text:
            return []   # handled by _parse_ib_activity

        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t|;")
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if reader.fieldnames is None:
            return []

        execs = []
        for row in reader:
            row = {k: (v or "").strip() for k, v in row.items() if k}
            norm_row = {_norm(k): v for k, v in row.items()}
            sym = norm_row.get("symbol", "")
            if "MES" not in sym.upper():
                continue
            ts  = _parse_dt(norm_row.get("datetime") or norm_row.get("date") or "")
            qty = _parse_float(norm_row.get("quantity") or norm_row.get("qty") or "")
            px  = _parse_float(norm_row.get("price") or norm_row.get("tprice") or "")
            if ts is None or qty is None or px is None:
                continue
            execs.append({"ts": ts, "qty": qty, "price": px})

        # Simple FIFO matching
        position = 0.0
        avg_entry = 0.0
        entry_ts  = None
        for e in sorted(execs, key=lambda x: x["ts"]):
            if position == 0:
                position  = e["qty"]
                avg_entry = e["price"]
                entry_ts  = e["ts"]
            else:
                direction = "long" if position > 0 else "short"
                pnl_per   = (e["price"] - avg_entry) * (1 if direction == "long" else -1)
                pnl       = pnl_per * abs(position) * 5   # MES multiplier = 5
                trades.append({
                    "source":      "ib",
                    "symbol":      "MES",
                    "direction":   direction,
                    "qty":         int(abs(position)),
                    "entry_time":  entry_ts,
                    "entry_price": avg_entry,
                    "exit_time":   e["ts"],
                    "exit_price":  e["price"],
                    "pnl":         round(pnl, 2),
                })
                position  = 0.0
                avg_entry = 0.0
                entry_ts  = None

    except Exception as exc:
        logger.warning("Cannot parse IB simple file %s: %s", filepath.name, exc)

    logger.info("IB Simple %s: parsed %d trades", filepath.name, len(trades))
    return trades

# ─── Lucid (TopstepX) Orders CSV ──────────────────────────────────────────────

def _parse_lucid_text(text: str) -> List[dict]:
    """Parse Lucid CSV text content into round-trip trades via FIFO matching."""
    trades = []
    try:
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t|;")
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if reader.fieldnames is None:
            return []

        # Collect filled executions
        fills = []
        for row in reader:
            row = {k: (v or "").strip() for k, v in row.items() if k}
            status = row.get("Status", "").strip()
            if status != "Filled":
                continue
            side      = row.get("B/S", "").strip().lower()   # "buy" or "sell"
            fill_time = _parse_dt(row.get("Fill Time", ""), tz_offset_h=8)
            avg_price = _parse_float(row.get("avgPrice", ""))
            filled_qty = _parse_float(row.get("filledQty", "")) or 1
            product   = row.get("Product", "MES").strip()

            if not side or fill_time is None or avg_price is None:
                continue

            fills.append({
                "ts":    fill_time,
                "side":  side,     # "buy" or "sell"
                "price": avg_price,
                "qty":   int(abs(filled_qty)),
                "symbol": product,
            })

        # Sort by fill time and match via FIFO position tracking
        fills.sort(key=lambda x: x["ts"])
        position  = 0
        entry_leg = None

        for f in fills:
            sign = 1 if f["side"] == "buy" else -1
            new_pos = position + sign * f["qty"]

            if position == 0:
                # Opening a new position
                entry_leg = f
                position  = new_pos
            elif (position > 0 and new_pos <= 0) or (position < 0 and new_pos >= 0):
                # Closing (or flipping) the position
                direction = "long" if position > 0 else "short"
                mult = 5 if entry_leg["symbol"] == "MES" else 5  # MES multiplier
                pnl_per = (f["price"] - entry_leg["price"]) * (1 if direction == "long" else -1)
                pnl = round(pnl_per * entry_leg["qty"] * mult, 2)
                trades.append({
                    "source":      "lucid",
                    "symbol":      entry_leg["symbol"],
                    "direction":   direction,
                    "qty":         entry_leg["qty"],
                    "entry_time":  entry_leg["ts"],
                    "entry_price": entry_leg["price"],
                    "exit_time":   f["ts"],
                    "exit_price":  f["price"],
                    "pnl":         pnl,
                })
                position = new_pos
                if new_pos != 0:
                    entry_leg = f
                else:
                    entry_leg = None
            else:
                # Adding to position (same direction) — update avg
                position = new_pos

        # If still open, record as open trade
        if position != 0 and entry_leg:
            direction = "long" if position > 0 else "short"
            trades.append({
                "source":      "lucid",
                "symbol":      entry_leg["symbol"],
                "direction":   direction,
                "qty":         entry_leg["qty"],
                "entry_time":  entry_leg["ts"],
                "entry_price": entry_leg["price"],
                "exit_time":   None,
                "exit_price":  None,
                "pnl":         None,
            })

    except Exception as exc:
        logger.warning("Cannot parse Lucid CSV content: %s", exc)

    logger.info("Lucid text: parsed %d trades", len(trades))
    return trades


def _parse_lucid(filepath: Path) -> List[dict]:
    """Parse Lucid CSV file."""
    try:
        text = filepath.read_text(encoding="utf-8-sig", errors="replace")
        return _parse_lucid_text(text)
    except Exception as exc:
        logger.warning("Cannot read Lucid file %s: %s", filepath.name, exc)
        return []

# ─── Public interface ─────────────────────────────────────────────────────────

def load_all_trades() -> List[dict]:
    """Load and merge trades from all recognised log files in data/."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    all_trades: List[dict] = []

    # Find Topstep files
    for fp in sorted(DATA_DIR.glob("trade_log_topstep*")):
        all_trades.extend(_parse_topstep(fp))

    # Find IB files
    for fp in sorted(DATA_DIR.glob("trade_log_IB*")):
        parsed = _parse_ib_activity(fp)
        if not parsed:
            parsed = _parse_ib_simple(fp)
        all_trades.extend(parsed)

    # Find Lucid files
    for fp in sorted(DATA_DIR.glob("trade_log_lucid*")):
        all_trades.extend(_parse_lucid(fp))

    # Sort by entry time and assign IDs
    all_trades.sort(key=lambda t: t.get("entry_time") or 0)
    for i, t in enumerate(all_trades):
        t["id"] = i + 1

    logger.info("Total trades loaded: %d", len(all_trades))
    return all_trades


def parse_csv_content(text: str) -> List[dict]:
    """Parse raw CSV text, auto-detecting format (Lucid, Topstep, IB).
    Returns a list of trade dicts with sequential IDs assigned.
    """
    trades: List[dict] = []

    # Detect format by header keywords
    first_line = text.split("\n", 1)[0].lower()

    if "b/s" in first_line and "avgprice" in first_line:
        # Lucid format
        trades = _parse_lucid_text(text)
    elif "trades,header" in text[:4096]:
        # IB Activity Statement
        from pathlib import Path
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(text)
            tmp = Path(f.name)
        trades = _parse_ib_activity(tmp)
        tmp.unlink(missing_ok=True)
    else:
        # Try Topstep / generic per-trade CSV
        from pathlib import Path
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(text)
            tmp = Path(f.name)
        trades = _parse_topstep(tmp)
        if not trades:
            trades = _parse_ib_simple(tmp)
        tmp.unlink(missing_ok=True)

    trades.sort(key=lambda t: t.get("entry_time") or 0)
    for i, t in enumerate(trades):
        t["id"] = i + 1

    logger.info("CSV content parsed: %d trades", len(trades))
    return trades
