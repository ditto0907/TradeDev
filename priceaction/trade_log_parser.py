"""
Trade log parser — auto-detects broker (IB / Topstep / Lucid) from CSV
content and extracts per-trade records.

For brokers that only log open/close executions (e.g. IB), opens are
matched into round-trip trades using a configurable strategy:
    MATCH_STRATEGY = "FILO"   (default, last-in-first-out)
    MATCH_STRATEGY = "FIFO"   (first-in-first-out)

You can also override at runtime by setting the env var
    TRADE_MATCH_STRATEGY=FIFO

Each trade dict produced by `parse_csv_content()` / `load_all_trades()`:
  {
    "id":            int,
    "broker":        "ib" | "topstep" | "lucid",
    "symbol":        "MES" | "MNQ" | "NK225" | "MGC" | …,
    "contract":      "MESM6" | …          # raw contract code (best effort)
    "direction":     "long" | "short",
    "qty":           int,
    "entry_time":    int   (UTC epoch sec),
    "exit_time":     int | None,
    "entry_price":   float,
    "exit_price":    float | None,
    "bars":          int,                  # 5min bars held (estimate)
    "pnl":           float | None,
    "points":        float | None,
    "currency":      "USD" | "JPY" | …,
    "source_file":   str,
    "date":          "YYYY-MM-DD"          # entry date (UTC)
    "trade_key":     str                   # stable de-dup key
  }
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

# ─── Matching strategy ───────────────────────────────────────────────────────
# How to pair Open executions with Close executions for IB-style brokers.
MATCH_STRATEGY: str = os.environ.get("TRADE_MATCH_STRATEGY", "FILO").upper()
if MATCH_STRATEGY not in ("FILO", "FIFO"):
    logger.warning("Invalid TRADE_MATCH_STRATEGY=%r — falling back to FILO",
                   MATCH_STRATEGY)
    MATCH_STRATEGY = "FILO"

# ─── Contract specs ──────────────────────────────────────────────────────────
# Multiplier = $/JPY value per point.  Used to compute pnl from price diff.
CONTRACT_SPECS = {
    "MES":   {"multiplier": 5.0,   "currency": "USD"},
    "MNQ":   {"multiplier": 2.0,   "currency": "USD"},
    "ES":    {"multiplier": 50.0,  "currency": "USD"},
    "NQ":    {"multiplier": 20.0,  "currency": "USD"},
    "MGC":   {"multiplier": 10.0,  "currency": "USD"},
    "GC":    {"multiplier": 100.0, "currency": "USD"},
    "NK225": {"multiplier": 100.0, "currency": "JPY"},  # OSE mini Nikkei = ¥100/pt
}

# IB conid → base symbol map (extend as needed)
IB_CONID_SYMBOL = {
    "161030023": "NK225",     # Mini Nikkei 225 (OSE)
}


def _normalize_symbol(raw: str, currency: str = "USD") -> Tuple[str, str]:
    """Return (base_symbol, raw_contract).

    "MESM6"  -> ("MES",  "MESM6")
    "MNQH26" -> ("MNQ",  "MNQH26")
    "161030023" + JPY -> ("NK225", "161030023")
    """
    raw = (raw or "").strip()
    if not raw:
        return ("UNKNOWN", "")
    if raw.isdigit():
        sym = IB_CONID_SYMBOL.get(raw)
        if sym:
            return (sym, raw)
        if currency == "JPY":
            return ("NK225", raw)
        return (f"CONID:{raw}", raw)
    m = re.match(r"^([A-Z]{1,5})[FGHJKMNQUVXZ]\d{1,2}$", raw)
    if m:
        return (m.group(1), raw)
    return (raw, raw)


# ─── Datetime helpers ────────────────────────────────────────────────────────

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

_TZ_OFFSET_RE = re.compile(r"([+-])(\d{2}):?(\d{2})$")


def _parse_dt(s: str, default_tz_offset_h: float = 0) -> Optional[int]:
    """Parse a datetime to UTC epoch seconds.

    *default_tz_offset_h* applies only if the string itself does not carry an
    explicit ±HH:MM offset suffix.
    """
    if not s:
        return None
    s = s.strip().replace(";", " ").replace(",", " ")
    s = re.sub(r"\s+", " ", s)

    tz_offset_h: Optional[float] = None
    m = _TZ_OFFSET_RE.search(s)
    if m:
        sign, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
        tz_offset_h = (hh + mm / 60.0) * (1 if sign == "+" else -1)
        s = _TZ_OFFSET_RE.sub("", s).strip()

    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            offset = tz_offset_h if tz_offset_h is not None else default_tz_offset_h
            return int(dt.replace(tzinfo=timezone.utc).timestamp() - offset * 3600)
        except ValueError:
            continue
    logger.debug("Cannot parse datetime: %r", s)
    return None


def _parse_float(s) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if not s:
        return None
    try:
        return float(s.replace(",", "").replace("$", "").replace("¥", ""))
    except ValueError:
        return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ─── FILO / FIFO position matching ───────────────────────────────────────────

def _match_legs(legs: List[dict], strategy: str = "") -> List[dict]:
    """Pair Open legs with Close legs to produce round-trip trades.

    Each leg dict must contain: ts, price, qty (>0), is_open (bool), side
    ("buy"/"sell"), symbol, contract, currency, source_file.
    """
    strategy = (strategy or MATCH_STRATEGY).upper()
    legs = sorted(legs, key=lambda x: x["ts"])
    opens: List[dict] = []
    trades: List[dict] = []

    for leg in legs:
        if leg["is_open"]:
            opens.append(dict(leg, _remaining=leg["qty"]))
            continue

        remaining = leg["qty"]
        opp = "sell" if leg["side"] == "buy" else "buy"
        while remaining > 0 and opens:
            if strategy == "FIFO":
                idx = next((i for i, o in enumerate(opens)
                            if o["side"] == opp and o["symbol"] == leg["symbol"]), -1)
            else:  # FILO / LIFO
                idx = next((i for i in range(len(opens) - 1, -1, -1)
                            if opens[i]["side"] == opp and opens[i]["symbol"] == leg["symbol"]),
                           -1)
            if idx < 0:
                logger.debug("Close leg without matching open: %s", leg)
                break
            o = opens[idx]
            take = min(o["_remaining"], remaining)
            direction = "long" if o["side"] == "buy" else "short"
            sign = 1.0 if direction == "long" else -1.0
            points = (leg["price"] - o["price"]) * sign

            spec = CONTRACT_SPECS.get(o["symbol"], {"multiplier": 1.0, "currency": "USD"})
            mult = spec["multiplier"]
            currency = leg.get("currency") or spec["currency"]
            pnl = round(points * take * mult, 2)

            trades.append({
                "broker":      o["broker"],
                "symbol":      o["symbol"],
                "contract":    o["contract"],
                "direction":   direction,
                "qty":         int(take),
                "entry_time":  o["ts"],
                "exit_time":   leg["ts"],
                "entry_price": o["price"],
                "exit_price":  leg["price"],
                "points":      round(points, 4),
                "pnl":         pnl,
                "currency":    currency,
                "source_file": o.get("source_file", ""),
            })
            o["_remaining"] -= take
            remaining     -= take
            if o["_remaining"] <= 0:
                opens.pop(idx)

    for o in opens:
        direction = "long" if o["side"] == "buy" else "short"
        spec = CONTRACT_SPECS.get(o["symbol"], {"currency": "USD"})
        trades.append({
            "broker":      o["broker"],
            "symbol":      o["symbol"],
            "contract":    o["contract"],
            "direction":   direction,
            "qty":         int(o["_remaining"]),
            "entry_time":  o["ts"],
            "exit_time":   None,
            "entry_price": o["price"],
            "exit_price":  None,
            "points":      None,
            "pnl":         None,
            "currency":    o.get("currency") or spec.get("currency", "USD"),
            "source_file": o.get("source_file", ""),
        })

    return trades


# ─── IB Activity Statement (English / Chinese) ───────────────────────────────

# Map normalized header → canonical key
_IB_HEADER_MAP = {
    "datadiscriminator": "_disc",
    "assetcategory":     "asset",
    "currency":          "currency",
    "symbol":            "symbol",
    "datetime":          "datetime",
    "quantity":          "qty",
    "tprice":            "price",
    "realizedpl":        "pnl",
    "code":              "code",
    # Chinese
    "资产分类":          "asset",
    "货币":              "currency",
    "代码":              "symbol",
    "日期时间":          "datetime",
    "日期/时间":         "datetime",
    "数量":              "qty",
    "交易价格":          "price",
    "已实现的损益":      "pnl",
}


def _parse_ib(text: str, source_file: str = "") -> List[dict]:
    """Parse IB Activity Statement (English "Trades,..." or Chinese "交易,...").

    Recognises both header variants and properly handles quoted commas
    inside the Date/Time field.
    """
    if "Trades,Header" not in text and "交易,Header" not in text:
        return []

    is_chinese = "交易,Header" in text
    section_token = "交易" if is_chinese else "Trades"

    legs: List[dict] = []
    header: Optional[List[str]] = None
    reader = csv.reader(io.StringIO(text))

    for parts in reader:
        if len(parts) < 3:
            continue
        if parts[0] != section_token:
            continue
        kind = parts[1]
        rest = parts[2:]
        if kind == "Header":
            header = [p.strip() for p in rest]
            continue
        if kind != "Data" or header is None:
            continue
        # Manually map — handle the duplicate "代码" header (symbol vs. code)
        row_norm: dict = {}
        for i, h in enumerate(header):
            if i >= len(rest):
                break
            # Try raw header first (for Chinese), then normalized (for English)
            key = _IB_HEADER_MAP.get(h) or _IB_HEADER_MAP.get(_norm(h)) or h
            if key in row_norm:
                # Second occurrence of duplicate header → use as Open/Close code
                key = "code"
            row_norm[key] = rest[i].strip()

        asset = row_norm.get("asset", "")
        if asset and ("Future" not in asset and "期货" not in asset):
            continue

        qty   = _parse_float(row_norm.get("qty"))
        price = _parse_float(row_norm.get("price"))
        ts    = _parse_dt(row_norm.get("datetime", ""))
        code  = (row_norm.get("code") or "").strip()
        currency = (row_norm.get("currency") or "USD").strip() or "USD"
        raw_sym  = (row_norm.get("symbol") or "").strip()

        if qty is None or price is None or ts is None or not code:
            continue

        symbol, contract = _normalize_symbol(raw_sym, currency=currency)
        is_open = ("O" in code) and ("C" not in code)

        legs.append({
            "broker":      "ib",
            "ts":          ts,
            "price":       price,
            "qty":         abs(qty),
            "side":        "buy" if qty > 0 else "sell",
            "is_open":     is_open,
            "symbol":      symbol,
            "contract":    contract,
            "currency":    currency,
            "source_file": source_file,
        })

    trades = _match_legs(legs)
    logger.info("IB %s: %d legs → %d trades (%s)",
                source_file or "<text>", len(legs), len(trades), MATCH_STRATEGY)
    return trades


# ─── Topstep / generic per-trade CSV ─────────────────────────────────────────

_TS_ENTRY_TIME  = {"entry", "entrytime", "opentime", "opendate", "entrydatetime", "enteredat"}
_TS_EXIT_TIME   = {"exit", "exittime", "closetime", "closedate", "exitdatetime", "exitedat"}
_TS_DIRECTION   = {"direction", "side", "type", "tradetype"}
_TS_ENTRY_PRICE = {"entryprice", "openprice", "avgentryprice"}
_TS_EXIT_PRICE  = {"exitprice", "closeprice", "avgexitprice"}
_TS_QTY         = {"qty", "quantity", "size", "contracts"}
_TS_PNL         = {"pl", "pnl", "netpl", "profitloss", "profit"}
_TS_SYMBOL      = {"symbol", "contractname", "ticker", "instrument"}


def _parse_topstep(text: str, source_file: str = "") -> List[dict]:
    trades: List[dict] = []
    try:
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t|;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            return []
        norm_keys = {k: _norm(k) for k in reader.fieldnames if k}

        def get(row: dict, aliases) -> str:
            for raw, n in norm_keys.items():
                if n in aliases:
                    return (row.get(raw) or "").strip()
            return ""

        for raw_row in reader:
            row = {k: (v or "").strip() for k, v in raw_row.items() if k}
            entry_raw  = get(row, _TS_ENTRY_TIME)
            ep_raw     = get(row, _TS_ENTRY_PRICE)
            if not entry_raw or not ep_raw:
                continue
            dir_raw = get(row, _TS_DIRECTION).lower()
            direction = "long" if any(w == dir_raw for w in ("long", "buy", "b")) else "short"
            ts_in  = _parse_dt(entry_raw)
            ts_out = _parse_dt(get(row, _TS_EXIT_TIME))
            if ts_in is None:
                continue

            raw_sym = get(row, _TS_SYMBOL) or "MES"
            symbol, contract = _normalize_symbol(raw_sym, "USD")
            qty   = int(abs(_parse_float(get(row, _TS_QTY)) or 1))
            ep    = _parse_float(ep_raw)
            xp    = _parse_float(get(row, _TS_EXIT_PRICE))
            pnl   = _parse_float(get(row, _TS_PNL))
            pts   = None
            if ep is not None and xp is not None:
                pts = round((xp - ep) * (1 if direction == "long" else -1), 4)
            spec = CONTRACT_SPECS.get(symbol, {"currency": "USD"})

            trades.append({
                "broker":      "topstep",
                "symbol":      symbol,
                "contract":    contract,
                "direction":   direction,
                "qty":         qty,
                "entry_time":  ts_in,
                "exit_time":   ts_out,
                "entry_price": ep,
                "exit_price":  xp,
                "points":      pts,
                "pnl":         pnl,
                "currency":    spec.get("currency", "USD"),
                "source_file": source_file,
            })
    except Exception as exc:
        logger.warning("Topstep parse error in %s: %s", source_file, exc)
    logger.info("Topstep %s: %d trades", source_file or "<text>", len(trades))
    return trades


# ─── Lucid (TopstepX) Orders CSV ─────────────────────────────────────────────

def _parse_lucid(text: str, source_file: str = "") -> List[dict]:
    raw_legs: List[dict] = []
    try:
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t|;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            return []
        for row in reader:
            row = {k: (v or "").strip() for k, v in row.items() if k}
            if row.get("Status", "").strip() != "Filled":
                continue
            side  = row.get("B/S", "").strip().lower()
            ts    = _parse_dt(row.get("Fill Time", ""), default_tz_offset_h=8)
            price = _parse_float(row.get("avgPrice"))
            qty   = int(abs(_parse_float(row.get("filledQty")) or 1))
            raw_sym  = (row.get("Contract") or row.get("Product") or "MES").strip()
            symbol, contract = _normalize_symbol(raw_sym, "USD")
            if not side or ts is None or price is None:
                continue
            raw_legs.append({
                "broker":      "lucid",
                "ts":          ts,
                "price":       price,
                "qty":         qty,
                "side":        side,
                "symbol":      symbol,
                "contract":    contract,
                "currency":    "USD",
                "source_file": source_file,
            })
    except Exception as exc:
        logger.warning("Lucid parse error in %s: %s", source_file, exc)
        return []

    # Tag each leg as open/close via running net position per symbol.
    raw_legs.sort(key=lambda x: x["ts"])
    final_legs: List[dict] = []
    pos_by_sym: dict = {}
    for leg in raw_legs:
        sym  = leg["symbol"]
        pos  = pos_by_sym.get(sym, 0)
        sign = 1 if leg["side"] == "buy" else -1
        new_pos = pos + sign * leg["qty"]

        if pos == 0 or (pos > 0 and sign > 0) or (pos < 0 and sign < 0):
            # Same direction or starting fresh — pure open
            final_legs.append({**leg, "is_open": True})
        elif (pos > 0 and new_pos >= 0) or (pos < 0 and new_pos <= 0):
            # Reducing or flat — pure close
            final_legs.append({**leg, "is_open": False})
        else:
            # Flipping: split into close (|pos|) + open (qty - |pos|)
            close_qty = abs(pos)
            open_qty  = leg["qty"] - close_qty
            final_legs.append({**leg, "is_open": False, "qty": close_qty})
            final_legs.append({**leg, "is_open": True,  "qty": open_qty,
                               "ts": leg["ts"] + 1})
        pos_by_sym[sym] = new_pos

    trades = _match_legs(final_legs)
    logger.info("Lucid %s: %d legs → %d trades", source_file or "<text>", len(final_legs), len(trades))
    return trades


# ─── Auto-detect & dispatch ──────────────────────────────────────────────────

def _detect_broker(text: str) -> str:
    head = text[:8192]
    head_lower = head.lower()
    first_line = head.split("\n", 1)[0]
    first_norm = _norm(first_line)
    if "trades,header" in head_lower or "交易,header" in head_lower or "交易,Header" in head:
        return "ib"
    if "bs" in first_norm and "avgprice" in first_norm:
        return "lucid"
    if "enteredat" in first_norm or "contractname" in first_norm:
        return "topstep"
    return "topstep"


def parse_csv_content(text: str, source_file: str = "") -> List[dict]:
    """Auto-detect format and parse."""
    broker = _detect_broker(text)
    if broker == "ib":
        trades = _parse_ib(text, source_file)
    elif broker == "lucid":
        trades = _parse_lucid(text, source_file)
    else:
        trades = _parse_topstep(text, source_file)
    return _finalize(trades)


def _finalize(trades: List[dict]) -> List[dict]:
    """Sort, assign IDs, derive date/bars/trade_key."""
    trades.sort(key=lambda t: t.get("entry_time") or 0)
    for i, t in enumerate(trades, 1):
        t["id"] = i
        if t.get("entry_time"):
            dt = datetime.fromtimestamp(t["entry_time"], tz=timezone.utc)
            t["date"] = dt.strftime("%Y-%m-%d")
        else:
            t["date"] = ""
        if t.get("entry_time") and t.get("exit_time"):
            t["bars"] = max(1, int((t["exit_time"] - t["entry_time"]) // 300))
        else:
            t["bars"] = 0
        t["trade_key"] = (
            f"{t['broker']}|{t.get('contract') or t['symbol']}|"
            f"{t['entry_time']}|{t['direction']}|{t['qty']}|"
            f"{t.get('exit_time') or 0}"
        )
    return trades


def load_all_trades() -> List[dict]:
    """Load and merge trades from every recognised log file in data/."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    all_trades: List[dict] = []
    seen: set = set()
    for pat in ("trade_log_*", "trades_log_*"):
        for fp in sorted(DATA_DIR.glob(pat)):
            if fp.name in seen or not fp.is_file():
                continue
            if fp.suffix and fp.suffix.lower() not in (".csv", ""):
                continue
            seen.add(fp.name)
            try:
                text = fp.read_text(encoding="utf-8-sig", errors="replace")
            except Exception as exc:
                logger.warning("Cannot read %s: %s", fp.name, exc)
                continue
            broker = _detect_broker(text)
            if broker == "ib":
                trades = _parse_ib(text, fp.name)
            elif broker == "lucid":
                trades = _parse_lucid(text, fp.name)
            else:
                trades = _parse_topstep(text, fp.name)
            all_trades.extend(trades)
    result = _finalize(all_trades)
    logger.info("load_all_trades: %d trades total (%s)", len(result), MATCH_STRATEGY)
    return result


def set_match_strategy(strategy: str) -> None:
    """Override matching strategy at runtime."""
    global MATCH_STRATEGY
    s = (strategy or "").upper()
    if s in ("FILO", "FIFO"):
        MATCH_STRATEGY = s
        logger.info("Trade match strategy set to %s", s)
