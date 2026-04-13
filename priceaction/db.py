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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_cycle_analyses (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol     TEXT    NOT NULL,
                timeframe  TEXT    NOT NULL,
                session    TEXT    NOT NULL DEFAULT 'RTH',
                created_at TEXT    NOT NULL,
                bar_from   INTEGER NOT NULL,
                bar_to     INTEGER NOT NULL,
                summary    TEXT    NOT NULL DEFAULT '',
                annotations TEXT   NOT NULL DEFAULT '[]',
                active     INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mca_sym_tf "
            "ON market_cycle_analyses (symbol, timeframe)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_backtests (
                id           TEXT PRIMARY KEY,
                symbol       TEXT    NOT NULL,
                timeframe    TEXT    NOT NULL,
                from_ts      INTEGER NOT NULL,
                to_ts        INTEGER NOT NULL,
                created_at   TEXT    NOT NULL,
                params_json  TEXT    NOT NULL DEFAULT '{}',
                summary_json TEXT    NOT NULL DEFAULT '{}',
                trade_count  INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_trades (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                backtest_id    TEXT    NOT NULL,
                symbol         TEXT    NOT NULL,
                timeframe      TEXT    NOT NULL,
                direction      TEXT    NOT NULL,
                contracts      INTEGER NOT NULL DEFAULT 1,
                entry_time     INTEGER NOT NULL,
                entry_price    REAL    NOT NULL,
                exit_time      INTEGER,
                exit_price     REAL,
                stop_price     REAL    NOT NULL,
                target_price   REAL    NOT NULL,
                pnl            REAL,
                outcome        TEXT    NOT NULL DEFAULT 'open',
                bars_held      INTEGER NOT NULL DEFAULT 0,
                signal_ibs     REAL    NOT NULL,
                context_pass   INTEGER NOT NULL DEFAULT 1,
                context_reason TEXT    NOT NULL DEFAULT '',
                created_at     TEXT    NOT NULL,
                FOREIGN KEY (backtest_id) REFERENCES strategy_backtests(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_st_backtest_id "
            "ON strategy_trades (backtest_id)"
        )
        # ── Migrate: add contracts column to existing databases ───────────────
        cursor = conn.execute("PRAGMA table_info(strategy_trades)")
        columns = {row[1] for row in cursor.fetchall()}
        if "contracts" not in columns:
            conn.execute(
                "ALTER TABLE strategy_trades ADD COLUMN contracts INTEGER NOT NULL DEFAULT 1"
            )
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


def get_coverage() -> List[dict]:
    """Return min/max timestamps and bar count for every (symbol, timeframe) pair."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT symbol, timeframe, MIN(ts), MAX(ts), COUNT(*) "
            "FROM bars GROUP BY symbol, timeframe ORDER BY symbol, timeframe"
        ).fetchall()
    return [
        {"symbol": r[0], "timeframe": r[1], "min_ts": r[2], "max_ts": r[3], "count": r[4]}
        for r in rows
    ]


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


# ─── Market Cycle Analysis CRUD ──────────────────────────────────────────────

def save_analysis(symbol: str, timeframe: str, session: str,
                  created_at: str, bar_from: int, bar_to: int,
                  summary: str, annotations: str) -> int:
    """Insert a new analysis record. Returns the new row id."""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO market_cycle_analyses "
            "(symbol, timeframe, session, created_at, bar_from, bar_to, summary, annotations, active) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (symbol, timeframe, session, created_at, bar_from, bar_to, summary, annotations),
        )
        return cur.lastrowid


def get_analyses(symbol: str = None, timeframe: str = None,
                 active_only: bool = False) -> List[dict]:
    """Return analysis records, optionally filtered."""
    clauses, params = [], []
    if symbol:
        clauses.append("symbol=?"); params.append(symbol)
    if timeframe:
        clauses.append("timeframe=?"); params.append(timeframe)
    if active_only:
        clauses.append("active=1")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM market_cycle_analyses {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_analysis_by_id(analysis_id: int) -> Optional[dict]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM market_cycle_analyses WHERE id=?", (analysis_id,)
        ).fetchone()
    return dict(row) if row else None


def update_analysis_active(analysis_id: int, active: bool) -> bool:
    """Toggle active flag. Returns True if row was found."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE market_cycle_analyses SET active=? WHERE id=?",
            (1 if active else 0, analysis_id),
        )
    return cur.rowcount > 0


def delete_analysis(analysis_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM market_cycle_analyses WHERE id=?", (analysis_id,),
        )
    return cur.rowcount > 0


# ─── Strategy Backtest CRUD ──────────────────────────────────────────────────

def save_backtest(backtest_id: str, symbol: str, timeframe: str,
                  from_ts: int, to_ts: int, created_at: str,
                  params_json: str, summary_json: str, trade_count: int) -> None:
    """Insert or replace a backtest run record."""
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO strategy_backtests "
            "(id, symbol, timeframe, from_ts, to_ts, created_at, params_json, summary_json, trade_count) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (backtest_id, symbol, timeframe, from_ts, to_ts, created_at,
             params_json, summary_json, trade_count),
        )


def get_all_backtests() -> List[dict]:
    """Return all backtest run records, newest first."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM strategy_backtests ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_backtest_by_id(backtest_id: str) -> Optional[dict]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM strategy_backtests WHERE id=?", (backtest_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_backtest(backtest_id: str) -> bool:
    """Delete a backtest and all its trades (CASCADE)."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM strategy_backtests WHERE id=?", (backtest_id,)
        )
    return cur.rowcount > 0


def save_strategy_trades(trades: List[dict]) -> None:
    """Bulk-insert trade records for a backtest run."""
    if not trades:
        return
    rows = [
        (
            t["backtest_id"], t["symbol"], t["timeframe"], t["direction"],
            t.get("contracts", 1),
            t["entry_time"], t["entry_price"], t.get("exit_time"),
            t.get("exit_price"), t["stop_price"], t["target_price"],
            t.get("pnl"), t["outcome"], t["bars_held"], t["signal_ibs"],
            t["context_pass"], t["context_reason"], t["created_at"],
        )
        for t in trades
    ]
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO strategy_trades "
            "(backtest_id, symbol, timeframe, direction, contracts, entry_time, entry_price, "
            "exit_time, exit_price, stop_price, target_price, pnl, outcome, "
            "bars_held, signal_ibs, context_pass, context_reason, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def get_trades_for_backtest(backtest_id: str) -> List[dict]:
    """Return all trades for a backtest run, sorted by entry_time."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM strategy_trades WHERE backtest_id=? ORDER BY entry_time",
            (backtest_id,),
        ).fetchall()
    return [dict(r) for r in rows]
