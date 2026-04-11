"""
SQLite bar storage — scalable schema for any symbol and timeframe.

Table: bars(symbol, timeframe, ts, open, high, low, close, volume)
  PRIMARY KEY (symbol, timeframe, ts)

Designed to be extended: add equities, crypto, different timeframes, etc.
just by passing different symbol/timeframe strings.

Usage:
    import db
    db.init_db()
    db.insert_bars("MES", "5min", list_of_bar_dicts)
    bars = db.get_bars("MES", "5min", from_ts=1700000000)
    latest = db.get_latest_ts("MES", "5min")
"""
import sqlite3
import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent / "data" / "tradedev.db"


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")    # concurrent reads while writing
    conn.execute("PRAGMA synchronous=NORMAL")  # safe but faster than FULL
    return conn


def init_db() -> None:
    """Create tables and indexes if they don't exist."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bars (
                symbol    TEXT    NOT NULL,
                timeframe TEXT    NOT NULL,
                ts        INTEGER NOT NULL,
                open      REAL    NOT NULL,
                high      REAL    NOT NULL,
                low       REAL    NOT NULL,
                close     REAL    NOT NULL,
                volume    REAL    NOT NULL,
                PRIMARY KEY (symbol, timeframe, ts)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bars_sym_tf_ts "
            "ON bars (symbol, timeframe, ts)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chart_layouts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT    NOT NULL,
                symbol    TEXT    NOT NULL DEFAULT '',
                resolution TEXT   NOT NULL DEFAULT '',
                content   TEXT    NOT NULL,
                timestamp INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS study_templates (
                name    TEXT PRIMARY KEY,
                content TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS drawing_templates (
                tool_name     TEXT NOT NULL,
                template_name TEXT NOT NULL,
                content       TEXT NOT NULL,
                PRIMARY KEY (tool_name, template_name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chart_templates (
                name    TEXT PRIMARY KEY,
                content TEXT NOT NULL
            )
        """)
    logger.info("Database ready: %s", _DB_PATH)


def insert_bars(symbol: str, timeframe: str, bars: List[dict]) -> int:
    """Insert or replace bars. Returns number of rows upserted."""
    if not bars:
        return 0
    rows = [
        (symbol, timeframe,
         b["time"], b["open"], b["high"], b["low"], b["close"], b["volume"])
        for b in bars
    ]
    with _conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO bars "
            "(symbol, timeframe, ts, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def get_bars(
    symbol: str,
    timeframe: str,
    from_ts: int = 0,
    to_ts: int = 9_999_999_999,
    limit: Optional[int] = None,
) -> List[dict]:
    """Return bars in [from_ts, to_ts] sorted ascending by timestamp."""
    sql = (
        "SELECT ts, open, high, low, close, volume FROM bars "
        "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=? "
        "ORDER BY ts"
    )
    params: list = [symbol, timeframe, from_ts, to_ts]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {"time": r[0], "open": r[1], "high": r[2],
         "low": r[3], "close": r[4], "volume": r[5]}
        for r in rows
    ]


def get_latest_ts(symbol: str, timeframe: str) -> Optional[int]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT MAX(ts) FROM bars WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()
    return row[0]


def get_earliest_ts(symbol: str, timeframe: str) -> Optional[int]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT MIN(ts) FROM bars WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()
    return row[0]


def get_latest_ts_before(symbol: str, timeframe: str, before_ts: int) -> Optional[int]:
    """Return the largest bar timestamp strictly before before_ts, or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT MAX(ts) FROM bars WHERE symbol=? AND timeframe=? AND ts<?",
            (symbol, timeframe, before_ts),
        ).fetchone()
    return row[0]


def count_bars(symbol: str, timeframe: str) -> int:
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM bars WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()[0]


# ─── Chart Layout CRUD ────────────────────────────────────────────────────────

def get_all_charts() -> List[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, name, symbol, resolution, timestamp FROM chart_layouts ORDER BY timestamp DESC"
        ).fetchall()
    return [{"id": r[0], "name": r[1], "symbol": r[2], "resolution": r[3], "timestamp": r[4]} for r in rows]


def save_chart(chart_id: Optional[int], name: str, symbol: str, resolution: str, content: str, timestamp: int) -> int:
    with _conn() as conn:
        if chart_id:
            conn.execute(
                "UPDATE chart_layouts SET name=?, symbol=?, resolution=?, content=?, timestamp=? WHERE id=?",
                (name, symbol, resolution, content, timestamp, chart_id),
            )
            return chart_id
        else:
            cur = conn.execute(
                "INSERT INTO chart_layouts (name, symbol, resolution, content, timestamp) VALUES (?,?,?,?,?)",
                (name, symbol, resolution, content, timestamp),
            )
            return cur.lastrowid


def get_chart_content(chart_id: int) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute("SELECT content FROM chart_layouts WHERE id=?", (chart_id,)).fetchone()
    return row[0] if row else None


def remove_chart(chart_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM chart_layouts WHERE id=?", (chart_id,))


# ─── Study Templates CRUD ─────────────────────────────────────────────────────

def get_all_study_templates() -> List[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT name FROM study_templates").fetchall()
    return [{"name": r[0]} for r in rows]


def save_study_template(name: str, content: str) -> None:
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO study_templates (name, content) VALUES (?,?)", (name, content))


def get_study_template_content(name: str) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute("SELECT content FROM study_templates WHERE name=?", (name,)).fetchone()
    return row[0] if row else None


def remove_study_template(name: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM study_templates WHERE name=?", (name,))


# ─── Drawing Templates CRUD ──────────────────────────────────────────────────

def get_drawing_templates(tool_name: str) -> List[str]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT template_name FROM drawing_templates WHERE tool_name=?", (tool_name,)
        ).fetchall()
    return [r[0] for r in rows]


def save_drawing_template(tool_name: str, template_name: str, content: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO drawing_templates (tool_name, template_name, content) VALUES (?,?,?)",
            (tool_name, template_name, content),
        )


def load_drawing_template(tool_name: str, template_name: str) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT content FROM drawing_templates WHERE tool_name=? AND template_name=?",
            (tool_name, template_name),
        ).fetchone()
    return row[0] if row else None


def remove_drawing_template(tool_name: str, template_name: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM drawing_templates WHERE tool_name=? AND template_name=?",
            (tool_name, template_name),
        )


# ─── Chart Templates CRUD ────────────────────────────────────────────────────

def get_all_chart_templates() -> List[str]:
    with _conn() as conn:
        rows = conn.execute("SELECT name FROM chart_templates").fetchall()
    return [r[0] for r in rows]


def save_chart_template(name: str, content: str) -> None:
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO chart_templates (name, content) VALUES (?,?)", (name, content))


def get_chart_template_content(name: str) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute("SELECT content FROM chart_templates WHERE name=?", (name,)).fetchone()
    return row[0] if row else None


def remove_chart_template(name: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM chart_templates WHERE name=?", (name,))
