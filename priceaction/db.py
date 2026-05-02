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
from typing import Dict, List, Optional, Tuple
from queue import Queue, Empty
from contextlib import contextmanager
import threading

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent / "data" / "tradedev.db"

# Sentinel value for "no upper bound" in timestamp queries.
# Represents a far-future Unix timestamp (~2286).
MAX_TIMESTAMP = 9_999_999_999

# ── v3 source rank table ─────────────────────────────────────────────────────
# Higher rank = more authoritative.  ``insert_bars`` refuses to overwrite an
# existing row whose source has a higher rank than the incoming bar's source.
# ``ib_continuous`` is rank 0 because ContFuture data is back-adjusted by IB
# every rollover and must NOT be persisted to ``bars`` — see doc/data_redesign_v3.md.
SOURCE_RANK = {
    "ib_validated":     100,
    "ib_monthly":        80,
    "ib_historical":     60,
    "realtime_completed": 20,
    "ib_continuous":      0,
    "unknown":            0,
}


def source_rank(source: str) -> int:
    """Return the rank for *source*; unknown sources rank as 0."""
    return SOURCE_RANK.get(source, 0)


def _set_db_path_for_testing(path) -> None:
    """Override the database path and reset the connection pool.
    Test-only helper; production code must not call this.
    """
    global _DB_PATH, _pool, _pool_initialized
    # Drain the pool
    try:
        while True:
            c = _pool.get_nowait()
            try:
                c.close()
            except Exception:
                pass
    except Empty:
        pass
    _DB_PATH = Path(path)
    _pool = Queue(maxsize=10)
    _pool_initialized = False

# ── Connection Pool ───────────────────────────────────────────────────────────
# Reuse connections to avoid "too many open files" with high-frequency operations.
# SQLite WAL mode allows multiple readers + one writer concurrently.

_pool: Queue = Queue(maxsize=10)
_pool_lock = threading.Lock()
_pool_initialized = False


def _create_connection() -> sqlite3.Connection:
    """Create a new database connection."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")    # concurrent reads while writing
    conn.execute("PRAGMA synchronous=NORMAL")  # safe but faster than FULL
    return conn


def _init_pool():
    """Initialize connection pool with 5 connections."""
    global _pool_initialized
    with _pool_lock:
        if _pool_initialized:
            return
        for _ in range(5):
            try:
                _pool.put(_create_connection(), block=False)
            except Exception as e:
                logger.warning("Failed to create pool connection: %s", e)
        _pool_initialized = True
        logger.info("DB connection pool initialized with %d connections", _pool.qsize())


@contextmanager
def _conn():
    """Get a connection from pool (or create new if pool empty), return to pool after use."""
    if not _pool_initialized:
        _init_pool()
    
    conn = None
    try:
        # Try to get from pool (non-blocking)
        conn = _pool.get(block=False)
    except Empty:
        # Pool empty, create temporary connection
        logger.debug("Pool exhausted, creating temporary connection")
        conn = _create_connection()
        temp_conn = True
    else:
        temp_conn = False
    
    try:
        yield conn
        conn.commit()  # Auto-commit on successful exit
    except Exception:
        conn.rollback()
        raise
    finally:
        # Return to pool if it was from pool and pool not full
        if not temp_conn:
            try:
                _pool.put(conn, block=False)
            except:
                # Pool full, close this connection
                conn.close()
        else:
            # Temporary connection, close it
            conn.close()


def init_db() -> None:
    """Create tables and indexes if they don't exist.

    v3 schema notes
    ---------------
    The four bar-related tables (``bars``, ``realtime_bars``,
    ``ib_fetch_cache``, ``validated_ranges``) are recreated on every startup
    if they predate v3 (i.e. lack the v3 columns).  This is safe because the
    redesign assumes a fresh start — see ``doc/data_redesign_v3.md``.
    """
    with _conn() as conn:
        # ── v3 migration: drop pre-v3 bar/cache tables that lack v3 columns ──
        for tbl, marker_col in (
            ("bars",           "source_rank"),
            ("realtime_bars",  "contract_month"),
            ("ib_fetch_cache", "contract_token"),
            ("validated_ranges", "contract_month"),
        ):
            try:
                cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
            except sqlite3.Error:
                cols = set()
            if cols and marker_col not in cols:
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
                logger.info("v3 init: dropped pre-v3 %s table", tbl)

        # ── bars: per-contract immutable facts ───────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bars (
                symbol         TEXT    NOT NULL,
                contract_month TEXT    NOT NULL,
                timeframe      TEXT    NOT NULL,
                ts             INTEGER NOT NULL,
                open           REAL    NOT NULL,
                high           REAL    NOT NULL,
                low            REAL    NOT NULL,
                close          REAL    NOT NULL,
                volume         REAL    NOT NULL,
                source         TEXT    NOT NULL,
                source_rank    INTEGER NOT NULL,
                fetched_at     INTEGER NOT NULL,
                PRIMARY KEY (symbol, contract_month, timeframe, ts)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bars_lookup "
            "ON bars (symbol, timeframe, ts, contract_month)"
        )
        # ── bar_revisions: audit trail for any change to a ``bars`` row ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bar_revisions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol         TEXT    NOT NULL,
                contract_month TEXT    NOT NULL,
                timeframe      TEXT    NOT NULL,
                ts             INTEGER NOT NULL,
                prev_source    TEXT    NOT NULL,
                prev_rank      INTEGER NOT NULL,
                prev_open      REAL,
                prev_high      REAL,
                prev_low       REAL,
                prev_close     REAL,
                prev_volume    REAL,
                new_source     TEXT    NOT NULL,
                new_rank       INTEGER NOT NULL,
                diff_summary   TEXT    NOT NULL DEFAULT '',
                revised_at     INTEGER NOT NULL,
                reason         TEXT    NOT NULL DEFAULT ''
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bar_rev_lookup "
            "ON bar_revisions (symbol, contract_month, timeframe, ts)"
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
        # ── Realtime bars table — one in-progress bar per (symbol, contract_month, timeframe) ─
        # v3: holds only the current unfinished bar; completed bars are NEVER
        # promoted to ``bars`` directly — instead we trigger an IB pull and
        # let ``insert_bars`` write the authoritative row.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS realtime_bars (
                symbol         TEXT    NOT NULL,
                contract_month TEXT    NOT NULL,
                timeframe      TEXT    NOT NULL,
                ts             INTEGER NOT NULL,
                open           REAL    NOT NULL,
                high           REAL    NOT NULL,
                low            REAL    NOT NULL,
                close          REAL    NOT NULL,
                volume         REAL    NOT NULL,
                updated_at     INTEGER NOT NULL,
                PRIMARY KEY (symbol, contract_month, timeframe)
            )
        """)
        # ── IB fetch cache: mirror of IB historical responses ─────────────
        # v3: ``contract_token`` distinguishes 'MONTH:YYYYMM' from 'CONT'
        # (continuous-contract data, kept in cache only — never in ``bars``).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ib_fetch_cache (
                symbol         TEXT    NOT NULL,
                contract_token TEXT    NOT NULL,
                timeframe      TEXT    NOT NULL,
                ts             INTEGER NOT NULL,
                open           REAL    NOT NULL,
                high           REAL    NOT NULL,
                low            REAL    NOT NULL,
                close          REAL    NOT NULL,
                volume         REAL    NOT NULL,
                fetched_at     INTEGER NOT NULL,
                PRIMARY KEY (symbol, contract_token, timeframe, ts)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ib_cache_lookup "
            "ON ib_fetch_cache (symbol, timeframe, ts, contract_token)"
        )
        # ── Validated ranges — tracks already-checked time ranges per contract ─
        # v3: ``contract_month`` is part of the natural key, so the same
        # timeframe window can be validated independently per contract.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS validated_ranges (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol         TEXT    NOT NULL,
                contract_month TEXT    NOT NULL,
                timeframe      TEXT    NOT NULL,
                from_ts        INTEGER NOT NULL,
                to_ts          INTEGER NOT NULL,
                checked_at     TEXT    NOT NULL,
                mismatches     INTEGER NOT NULL DEFAULT 0,
                fixed          INTEGER NOT NULL DEFAULT 0,
                UNIQUE(symbol, contract_month, timeframe, from_ts, to_ts)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vr_lookup "
            "ON validated_ranges (symbol, timeframe, contract_month)"
        )
        # ── Trade logs — parsed broker trade history with user annotations ─
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_key    TEXT    NOT NULL UNIQUE,
                date         TEXT    NOT NULL DEFAULT '',
                broker       TEXT    NOT NULL,
                symbol       TEXT    NOT NULL,
                contract     TEXT    NOT NULL DEFAULT '',
                direction    TEXT    NOT NULL,
                qty          INTEGER NOT NULL DEFAULT 1,
                entry_time   INTEGER NOT NULL,
                exit_time    INTEGER,
                entry_price  REAL,
                exit_price   REAL,
                bars         INTEGER NOT NULL DEFAULT 0,
                pnl          REAL,
                points       REAL,
                currency     TEXT    NOT NULL DEFAULT 'USD',
                source_file  TEXT    NOT NULL DEFAULT '',
                trade_type   TEXT    NOT NULL DEFAULT '',
                entry_reason TEXT    NOT NULL DEFAULT '',
                market_cycle TEXT    NOT NULL DEFAULT '',
                sup_res      TEXT    NOT NULL DEFAULT '',
                notes        TEXT    NOT NULL DEFAULT '',
                created_at   TEXT    NOT NULL,
                updated_at   TEXT    NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tl_date "
            "ON trade_logs (date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tl_broker_sym "
            "ON trade_logs (broker, symbol)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tl_entry_time "
            "ON trade_logs (entry_time)"
        )
    logger.info("Database ready: %s", _DB_PATH)


def insert_bars(
    symbol: str,
    timeframe: str,
    bars: List[dict],
    source: Optional[str] = None,
    reason: str = "",
) -> dict:
    """v3 insert: write per-contract bars to the ``bars`` table with
    rank-guard and audit-trail enforcement.

    Each input bar dict MUST contain:
      * ``time`` (int)
      * ``open`` / ``high`` / ``low`` / ``close`` (float)
      * ``volume`` (float)
      * ``contract_month`` (non-empty 'YYYYMM' string)
      * ``source`` (string in :data:`SOURCE_RANK`) — or pass ``source=`` arg

    Rules:
      * ``contract_month`` is required and must be non-empty.
      * ``source='ib_continuous'`` is rejected — ContFuture data goes to
        ``ib_fetch_cache`` only.  ContFuture is back-adjusted by IB on every
        rollover, so it is not a stable fact.
      * If a row already exists for the same key and the existing
        ``source_rank`` is higher than the incoming bar's rank, the write is
        rejected (logged at WARNING).
      * If a row exists with values different from the incoming bar (any
        OHLCV diff or different source), a row is appended to
        ``bar_revisions`` before the overwrite.

    Returns a dict with counters::

        {"inserted":int, "replaced":int, "rejected_rank":int,
         "rejected_validation":int, "revisions":int}
    """
    out = {"inserted": 0, "replaced": 0,
           "rejected_rank": 0, "rejected_validation": 0,
           "revisions": 0}
    if not bars:
        return out
    import time as _time
    now_ts = int(_time.time())

    # ── Pre-validate every bar ───────────────────────────────────────────
    cleaned: List[Tuple] = []
    for b in bars:
        bar_source = b.get("source", source)
        if not bar_source:
            logger.warning(
                "insert_bars %s/%s ts=%s: missing source — rejected",
                symbol, timeframe, b.get("time"),
            )
            out["rejected_validation"] += 1
            continue
        if bar_source == "ib_continuous":
            logger.warning(
                "insert_bars %s/%s ts=%s: source='ib_continuous' is not "
                "allowed in bars table — rejected",
                symbol, timeframe, b.get("time"),
            )
            out["rejected_validation"] += 1
            continue
        cm = b.get("contract_month", "")
        if not cm:
            logger.warning(
                "insert_bars %s/%s ts=%s: empty contract_month — rejected",
                symbol, timeframe, b.get("time"),
            )
            out["rejected_validation"] += 1
            continue
        try:
            o = float(b["open"]); h = float(b["high"])
            l = float(b["low"]);  c = float(b["close"])
            v = float(b["volume"])
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "insert_bars %s/%s ts=%s: missing/non-numeric OHLCV — rejected",
                symbol, timeframe, b.get("time"),
            )
            out["rejected_validation"] += 1
            continue
        if h < l:
            logger.warning(
                "insert_bars %s/%s ts=%s: high (%.4f) < low (%.4f) — rejected",
                symbol, timeframe, b["time"], h, l,
            )
            out["rejected_validation"] += 1
            continue
        if any(p <= 0 for p in (o, h, l, c)):
            logger.warning(
                "insert_bars %s/%s ts=%s: non-positive OHLC — rejected",
                symbol, timeframe, b["time"],
            )
            out["rejected_validation"] += 1
            continue
        if v < 0:
            logger.warning(
                "insert_bars %s/%s ts=%s: negative volume — rejected",
                symbol, timeframe, b["time"],
            )
            out["rejected_validation"] += 1
            continue
        new_rank = source_rank(bar_source)
        cleaned.append((symbol, cm, timeframe, int(b["time"]),
                        o, h, l, c, v, bar_source, new_rank, now_ts))

    if not cleaned:
        return out

    # ── Rank-guard + revision audit, applied row by row ──────────────────
    # Rank-guard summary counters: (sym, cm, tf, old_src, new_src) -> {ohlcv_diff: int, same: int}
    _rank_guard_summary: Dict[Tuple, Dict[str, int]] = {}

    revisions: List[Tuple] = []
    accepted: List[Tuple] = []
    with _conn() as conn:
        # Pre-fetch existing rows in one query
        keys = [(r[0], r[1], r[2], r[3]) for r in cleaned]
        existing: Dict[Tuple, Tuple] = {}
        # SQLite has a ~999 parameter limit; chunk.
        CHUNK = 200
        for i in range(0, len(keys), CHUNK):
            sub = keys[i:i + CHUNK]
            placeholders = ",".join(["(?,?,?,?)"] * len(sub))
            flat = [v for k in sub for v in k]
            rows = conn.execute(
                "SELECT symbol, contract_month, timeframe, ts, "
                "open, high, low, close, volume, source, source_rank "
                "FROM bars WHERE (symbol, contract_month, timeframe, ts) IN "
                f"(VALUES {placeholders})",
                flat,
            ).fetchall()
            for r in rows:
                existing[(r[0], r[1], r[2], r[3])] = r

        for row in cleaned:
            sym, cm, tf, ts, o, h, l, c, v, src, new_rank, fetched = row
            old = existing.get((sym, cm, tf, ts))
            if old is None:
                accepted.append(row)
                continue
            old_o, old_h, old_l, old_c, old_v = old[4], old[5], old[6], old[7], old[8]
            old_src, old_rank = old[9], old[10]
            ohlcv_diff = (
                abs(old_o - o) > 1e-9 or abs(old_h - h) > 1e-9 or
                abs(old_l - l) > 1e-9 or abs(old_c - c) > 1e-9 or
                abs(old_v - v) > 1e-9
            )
            src_diff = (old_src != src)
            if not ohlcv_diff and not src_diff:
                # Identical — silent no-op (still counts as replace=0).
                continue
            if new_rank < old_rank:
                _key = (sym, cm, tf, old_src, src)
                if _key not in _rank_guard_summary:
                    _rank_guard_summary[_key] = {"ohlcv_diff": 0, "same": 0}
                if ohlcv_diff:
                    _rank_guard_summary[_key]["ohlcv_diff"] += 1
                else:
                    _rank_guard_summary[_key]["same"] += 1
                out["rejected_rank"] += 1
                continue
            # Audit row
            diff_parts = []
            if abs(old_o - o) > 1e-9: diff_parts.append(f"o:{old_o}->{o}")
            if abs(old_h - h) > 1e-9: diff_parts.append(f"h:{old_h}->{h}")
            if abs(old_l - l) > 1e-9: diff_parts.append(f"l:{old_l}->{l}")
            if abs(old_c - c) > 1e-9: diff_parts.append(f"c:{old_c}->{c}")
            if abs(old_v - v) > 1e-9: diff_parts.append(f"v:{old_v}->{v}")
            if src_diff: diff_parts.append(f"src:{old_src}->{src}")
            revisions.append((
                sym, cm, tf, ts,
                old_src, old_rank, old_o, old_h, old_l, old_c, old_v,
                src, new_rank, ",".join(diff_parts), now_ts, reason or "",
            ))
            accepted.append(row)

        # Emit aggregated rank-guard summary (one line per group instead of per bar)
        for (g_sym, g_cm, g_tf, g_old_src, g_new_src), counts in _rank_guard_summary.items():
            n_diff = counts["ohlcv_diff"]
            n_same = counts["same"]
            parts = []
            if n_diff:
                parts.append(f"{n_diff} bars with DIFFERENT OHLCV")
            if n_same:
                parts.append(f"{n_same} bars same OHLCV")
            log_fn = logger.warning if n_diff else logger.debug
            log_fn(
                "insert_bars %s/%s/%s: rank guard blocked %d bars (%s) [old=%s → new=%s]",
                g_sym, g_cm, g_tf, n_diff + n_same, ", ".join(parts), g_old_src, g_new_src,
            )

        if revisions:
            conn.executemany(
                "INSERT INTO bar_revisions "
                "(symbol, contract_month, timeframe, ts, "
                " prev_source, prev_rank, prev_open, prev_high, prev_low, prev_close, prev_volume, "
                " new_source, new_rank, diff_summary, revised_at, reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                revisions,
            )
            out["revisions"] = len(revisions)

        if accepted:
            conn.executemany(
                "INSERT OR REPLACE INTO bars "
                "(symbol, contract_month, timeframe, ts, "
                " open, high, low, close, volume, source, source_rank, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                accepted,
            )
            # accepted = inserted (no existing) + replaced (existing differed and rank ok)
            out["replaced"] = len(revisions)
            out["inserted"] = len(accepted) - len(revisions)

    return out


def upsert_realtime_bar(symbol: str, timeframe: str, bar: dict,
                         contract_month: Optional[str] = None) -> None:
    """Upsert the current in-progress realtime bar.

    v3: keyed by (symbol, contract_month, timeframe).  ``contract_month``
    must be supplied either via the *bar* dict or the keyword argument.
    """
    import time as _time
    cm = bar.get("contract_month") or contract_month
    if not cm:
        raise ValueError("realtime_bar requires non-empty contract_month")
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO realtime_bars "
            "(symbol, contract_month, timeframe, ts, "
            " open, high, low, close, volume, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (symbol, cm, timeframe,
             int(bar["time"]), float(bar["open"]), float(bar["high"]),
             float(bar["low"]), float(bar["close"]), float(bar["volume"]),
             int(_time.time())),
        )


def delete_realtime_bar(symbol: str, contract_month: str, timeframe: str) -> int:
    """Remove the realtime row for a (symbol, contract_month, timeframe).
    Used after a completed realtime bar has been successfully promoted via
    an authoritative IB pull."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM realtime_bars "
            "WHERE symbol=? AND contract_month=? AND timeframe=?",
            (symbol, contract_month, timeframe),
        )
        return cur.rowcount


def get_all_realtime_bars() -> List[dict]:
    """Return all in-progress realtime bars (one per
    symbol/contract_month/timeframe)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT symbol, contract_month, timeframe, ts, "
            "open, high, low, close, volume FROM realtime_bars"
        ).fetchall()
    return [
        {"symbol": r[0], "contract_month": r[1], "timeframe": r[2],
         "time": r[3], "open": r[4], "high": r[5],
         "low": r[6], "close": r[7], "volume": r[8]}
        for r in rows
    ]


def get_realtime_bar(symbol: str, contract_month: str,
                     timeframe: str) -> Optional[dict]:
    """Return the current realtime bar for (symbol, contract_month, timeframe)
    or None if none exists."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM realtime_bars "
            "WHERE symbol=? AND contract_month=? AND timeframe=?",
            (symbol, contract_month, timeframe),
        ).fetchone()
    if not row:
        return None
    return {"time": row[0], "open": row[1], "high": row[2],
            "low": row[3], "close": row[4], "volume": row[5],
            "symbol": symbol, "contract_month": contract_month,
            "timeframe": timeframe}


def get_bars(
    symbol: str,
    timeframe: str,
    from_ts: int = 0,
    to_ts: int = MAX_TIMESTAMP,
    limit: Optional[int] = None,
    contract_month: Optional[str] = None,
) -> List[dict]:
    """Return bars in [from_ts, to_ts] sorted ascending by timestamp.

    v3 note: bars are stored per-contract.  When *contract_month* is omitted,
    rows from all contracts are returned (mainly for diagnostics) — chart
    rendering should always supply a specific contract_month or use the
    continuous_view module.
    """
    sql = (
        "SELECT ts, open, high, low, close, volume, source, source_rank, "
        "       contract_month, fetched_at "
        "FROM bars "
        "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=?"
    )
    params: list = [symbol, timeframe, from_ts, to_ts]
    if contract_month is not None:
        sql += " AND contract_month=?"
        params.append(contract_month)
    sql += " ORDER BY ts"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {"time": r[0], "open": r[1], "high": r[2],
         "low": r[3], "close": r[4], "volume": r[5],
         "source": r[6], "source_rank": r[7],
         "contract_month": r[8], "fetched_at": r[9]}
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


def delete_bars_by_source(source: str) -> int:
    """Delete all bars with the given source. Returns count deleted."""
    with _conn() as conn:
        cursor = conn.execute(
            "DELETE FROM bars WHERE source=?", (source,)
        )
        return cursor.rowcount


def get_coverage() -> List[dict]:
    """Return min/max timestamps and bar count for every (symbol, timeframe) pair,
    including the list of distinct contract months stored for each pair."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT symbol, timeframe, MIN(ts), MAX(ts), COUNT(*) "
            "FROM bars GROUP BY symbol, timeframe ORDER BY symbol, timeframe"
        ).fetchall()
        # Gather distinct contract months per (symbol, timeframe)
        contract_rows = conn.execute(
            "SELECT symbol, timeframe, contract_month, COUNT(*) "
            "FROM bars WHERE contract_month != '' "
            "GROUP BY symbol, timeframe, contract_month "
            "ORDER BY symbol, timeframe, contract_month"
        ).fetchall()

    # Build contract_months map: (symbol, tf) -> [{contract_month, count}]
    contracts_map: dict = {}
    for r in contract_rows:
        key = (r[0], r[1])
        contracts_map.setdefault(key, []).append({"contract_month": r[2], "count": r[3]})

    return [
        {
            "symbol": r[0], "timeframe": r[1],
            "min_ts": r[2], "max_ts": r[3], "count": r[4],
            "contracts": contracts_map.get((r[0], r[1]), []),
        }
        for r in rows
    ]


def get_distinct_contract_months(symbol: str, timeframe: str) -> List[str]:
    """Return sorted list of distinct non-empty contract months for a (symbol, timeframe) pair."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT contract_month FROM bars "
            "WHERE symbol=? AND timeframe=? AND contract_month != '' "
            "ORDER BY contract_month",
            (symbol, timeframe),
        ).fetchall()
    return [r[0] for r in rows]


def get_bar_revisions(
    symbol: Optional[str] = None,
    contract_month: Optional[str] = None,
    timeframe: Optional[str] = None,
    ts: Optional[int] = None,
    limit: int = 100,
) -> List[dict]:
    """Return audit-trail rows from ``bar_revisions`` (newest first)."""
    where = []
    params: list = []
    if symbol:
        where.append("symbol=?"); params.append(symbol)
    if contract_month:
        where.append("contract_month=?"); params.append(contract_month)
    if timeframe:
        where.append("timeframe=?"); params.append(timeframe)
    if ts is not None:
        where.append("ts=?"); params.append(ts)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, symbol, contract_month, timeframe, ts, "
            " prev_source, prev_rank, prev_open, prev_high, prev_low, "
            " prev_close, prev_volume, "
            " new_source, new_rank, diff_summary, revised_at, reason "
            f"FROM bar_revisions{where_sql} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [
        {"id": r[0], "symbol": r[1], "contract_month": r[2], "timeframe": r[3],
         "ts": r[4],
         "prev_source": r[5], "prev_rank": r[6],
         "prev_open": r[7], "prev_high": r[8], "prev_low": r[9],
         "prev_close": r[10], "prev_volume": r[11],
         "new_source": r[12], "new_rank": r[13],
         "diff_summary": r[14], "revised_at": r[15], "reason": r[16]}
        for r in rows
    ]


# ─── Validated Ranges ─────────────────────────────────────────────────────────
# Tracks time ranges that have been validated by the background task.

def insert_validated_range(
    symbol: str, timeframe: str, from_ts: int, to_ts: int,
    mismatches: int = 0, fixed: int = 0,
    contract_month: str = "",
) -> int:
    """Record that a time range has been validated for a specific contract.

    v3: ``contract_month`` participates in the natural key.
    Upserts on (symbol, contract_month, timeframe, from_ts, to_ts).
    """
    from datetime import datetime, timezone as _tz
    checked_at = datetime.now(_tz.utc).isoformat()
    with _conn() as conn:
        cursor = conn.execute(
            "INSERT INTO validated_ranges "
            "(symbol, contract_month, timeframe, from_ts, to_ts, "
            " checked_at, mismatches, fixed) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol, contract_month, timeframe, from_ts, to_ts) "
            "DO UPDATE SET "
            "checked_at=excluded.checked_at, "
            "mismatches=excluded.mismatches, "
            "fixed=excluded.fixed",
            (symbol, contract_month, timeframe, from_ts, to_ts,
             checked_at, mismatches, fixed),
        )
        return cursor.lastrowid


def get_validated_ranges(
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    contract_month: Optional[str] = None,
) -> List[dict]:
    """Return validated ranges, optionally filtered.  Sorted by from_ts desc."""
    where_clauses = []
    params: list = []
    if symbol:
        where_clauses.append("symbol=?"); params.append(symbol)
    if timeframe:
        where_clauses.append("timeframe=?"); params.append(timeframe)
    if contract_month is not None:
        where_clauses.append("contract_month=?"); params.append(contract_month)
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT id, symbol, contract_month, timeframe, from_ts, to_ts, "
            f"       checked_at, mismatches, fixed "
            f"FROM validated_ranges{where_sql} ORDER BY from_ts DESC",
            params,
        ).fetchall()
    return [
        {"id": r[0], "symbol": r[1], "contract_month": r[2], "timeframe": r[3],
         "from_ts": r[4], "to_ts": r[5], "checked_at": r[6],
         "mismatches": r[7], "fixed": r[8]}
        for r in rows
    ]


def get_merged_validated_ranges(
    symbol: str,
    timeframe: str,
    contract_month: str,
) -> List[dict]:
    """Return merged (non-overlapping) **clean** validated ranges for a
    (symbol, contract_month, timeframe).  Only rows with ``mismatches=0``
    count as clean.  Adjacent and overlapping ranges are merged.
    """
    try:
        from ib_data_fetcher import _key_to_ib
        _, interval = _key_to_ib(timeframe)
    except Exception:
        interval = 1
    with _conn() as conn:
        rows = conn.execute(
            "SELECT from_ts, to_ts FROM validated_ranges "
            "WHERE symbol=? AND contract_month=? AND timeframe=? "
            "AND mismatches=0 ORDER BY from_ts",
            (symbol, contract_month, timeframe),
        ).fetchall()
    if not rows:
        return []
    merged: list = []
    cur_from, cur_to = rows[0]
    for r_from, r_to in rows[1:]:
        if r_from <= cur_to + interval:
            cur_to = max(cur_to, r_to)
        else:
            merged.append({"from_ts": cur_from, "to_ts": cur_to})
            cur_from, cur_to = r_from, r_to
    merged.append({"from_ts": cur_from, "to_ts": cur_to})
    return merged


def is_range_validated(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    contract_month: str = "",
) -> bool:
    """Check if [from_ts, to_ts] is fully covered by clean validated ranges
    for the given (symbol, contract_month, timeframe)."""
    merged = get_merged_validated_ranges(symbol, timeframe, contract_month)
    if not merged:
        return False
    for rng in merged:
        if rng["from_ts"] <= from_ts and rng["to_ts"] >= to_ts:
            return True
    return False


def get_unchecked_ranges(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    contract_month: str = "",
) -> List[dict]:
    """Return sub-ranges of [from_ts, to_ts] that have NOT been validated yet
    for (symbol, contract_month, timeframe)."""
    merged = get_merged_validated_ranges(symbol, timeframe, contract_month)
    if not merged:
        return [{"from_ts": from_ts, "to_ts": to_ts}]
    unchecked: list = []
    cursor = from_ts
    for rng in merged:
        if rng["to_ts"] < cursor:
            continue
        if rng["from_ts"] > cursor:
            unchecked.append({"from_ts": cursor, "to_ts": min(rng["from_ts"] - 1, to_ts)})
        cursor = max(cursor, rng["to_ts"] + 1)
        if cursor > to_ts:
            break
    if cursor <= to_ts:
        unchecked.append({"from_ts": cursor, "to_ts": to_ts})
    return unchecked
# Raw bars fetched from IB, kept as a local cache to avoid redundant IB requests.
# Only used for caching purposes — never written to by any other logic.

def insert_ib_cache_bars(symbol: str, timeframe: str, bars: List[dict],
                          contract_token: Optional[str] = None) -> int:
    """Insert or replace bars into the IB fetch cache with validation.

    v3: each row is keyed by ``contract_token`` — either ``'MONTH:YYYYMM'``
    for a specific month-future or ``'CONT'`` for a continuous-contract
    response.  ContFuture data lives ONLY here, never in the ``bars`` table.

    The token can be supplied per bar (``bar['contract_token']``) or as the
    *contract_token* keyword argument applied to all bars.
    """
    if not bars:
        return 0
    import time as _time
    now_ts = int(_time.time())
    valid_rows = []
    for b in bars:
        token = b.get("contract_token") or contract_token
        if not token:
            logger.warning(
                "ib_cache: bar %s/%s ts=%s missing contract_token — skipped",
                symbol, timeframe, b.get("time"),
            )
            continue
        try:
            o = float(b["open"]); h = float(b["high"])
            l = float(b["low"]);  c = float(b["close"])
            v = float(b["volume"])
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "ib_cache: bar %s/%s ts=%s missing/non-numeric OHLCV — skipped",
                symbol, timeframe, b.get("time"),
            )
            continue
        if h < l or any(p <= 0 for p in (o, h, l, c)) or v < 0:
            logger.warning(
                "ib_cache: skipping invalid bar %s/%s ts=%s "
                "O=%.4f H=%.4f L=%.4f C=%.4f V=%.1f",
                symbol, timeframe, b["time"], o, h, l, c, v,
            )
            continue
        valid_rows.append(
            (symbol, token, timeframe, int(b["time"]),
             o, h, l, c, v, now_ts)
        )
    if not valid_rows:
        return 0
    with _conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO ib_fetch_cache "
            "(symbol, contract_token, timeframe, ts, "
            " open, high, low, close, volume, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            valid_rows,
        )
    return len(valid_rows)


def get_ib_cache_bars(
    symbol: str,
    timeframe: str,
    from_ts: int = 0,
    to_ts: int = MAX_TIMESTAMP,
    contract_token: Optional[str] = None,
) -> List[dict]:
    """Return cached IB bars in [from_ts, to_ts] sorted ascending.

    *contract_token* is required for v3 reads (None → all tokens, returns
    rows annotated with their token).
    """
    sql = (
        "SELECT ts, open, high, low, close, volume, contract_token, fetched_at "
        "FROM ib_fetch_cache "
        "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=?"
    )
    params: list = [symbol, timeframe, from_ts, to_ts]
    if contract_token is not None:
        sql += " AND contract_token=?"
        params.append(contract_token)
    sql += " ORDER BY ts"
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    # Derive contract_month from the token so callers (e.g. data_validator
    # writing fixed bars back via insert_bars) get a tag insert_bars accepts.
    # MONTH:YYYYMM → YYYYMM ; CONT → derive front-month from ts.
    def _cm_from_token(tok: str, ts: int) -> str:
        if tok and tok.startswith("MONTH:") and len(tok) >= 12:
            return tok[6:]
        if tok == "CONT":
            try:
                from contract_calendar import active_contract
                return active_contract(int(ts), symbol)
            except Exception:
                return ""
        return ""

    return [
        {"time": r[0], "open": r[1], "high": r[2],
         "low": r[3], "close": r[4], "volume": r[5],
         "contract_token": r[6], "fetched_at": r[7],
         "contract_month": _cm_from_token(r[6], r[0])}
        for r in rows
    ]


def get_ib_cache_coverage(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    contract_token: Optional[str] = None,
) -> List[int]:
    """Return sorted list of cached timestamps for (symbol, timeframe[, token])
    in range."""
    sql = ("SELECT ts FROM ib_fetch_cache "
           "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=?")
    params: list = [symbol, timeframe, from_ts, to_ts]
    if contract_token is not None:
        sql += " AND contract_token=?"
        params.append(contract_token)
    sql += " ORDER BY ts"
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [r[0] for r in rows]


def delete_ib_cache_bars(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    contract_token: Optional[str] = None,
) -> int:
    """Delete IB-cache rows in [from_ts, to_ts] (inclusive), optionally
    scoped to a single contract_token.  Returns rows deleted."""
    sql = ("DELETE FROM ib_fetch_cache "
           "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=?")
    params: list = [symbol, timeframe, from_ts, to_ts]
    if contract_token is not None:
        sql += " AND contract_token=?"
        params.append(contract_token)
    with _conn() as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount


def find_gaps(
    symbol: str,
    timeframe: str,
    expected_interval: int,
    max_acceptable_gap: int = None,
) -> List[dict]:
    """
    Detect gaps in bar data using the trading session calendar.

    Uses ``TradingCalendar`` for the symbol to classify gaps accurately
    based on the instrument's actual trading schedule rather than ad-hoc
    weekday/hour heuristics.

    Returns a list of gap dicts::

        {
            "gap_start":    int,   # timestamp of last bar before the gap
            "gap_end":      int,   # timestamp of first bar after the gap
            "gap_seconds":  int,   # duration in seconds
            "gap_type":     str,   # "weekend" | "holiday" | "maintenance" | "data_gap" | "normal"
            "expected_bars": int,  # how many bars we'd expect in this gap
        }
    """
    if max_acceptable_gap is None:
        # Default thresholds: 4 h for intraday, 4 days for daily
        max_acceptable_gap = 14400 if expected_interval < 86400 else 345600

    with _conn() as conn:
        rows = conn.execute(
            "SELECT ts FROM bars WHERE symbol=? AND timeframe=? ORDER BY ts",
            (symbol, timeframe),
        ).fetchall()

    if len(rows) < 2:
        return []

    # Use trading calendar for classification
    try:
        from trading_calendar import get_calendar
        cal = get_calendar(symbol)
    except Exception:
        cal = None

    gaps: List[dict] = []
    for i in range(1, len(rows)):
        t1 = rows[i - 1][0]
        t2 = rows[i][0]
        gap = t2 - t1
        if gap <= max_acceptable_gap:
            continue

        if cal:
            gap_type = cal.classify_gap(t1, t2)
        else:
            # Fallback to basic heuristics when calendar unavailable
            from datetime import datetime, timezone, timedelta
            from market_holidays import spans_us_holiday
            _et = timezone(timedelta(hours=-4))
            d1_et = datetime.fromtimestamp(t1, tz=timezone.utc).astimezone(_et)
            d2_et = datetime.fromtimestamp(t2, tz=timezone.utc).astimezone(_et)
            is_normal_weekend = (
                d1_et.weekday() == 4 and d2_et.weekday() in (0, 6)
                and d1_et.hour >= 16 and gap < 201600
            )
            is_maintenance = (
                d1_et.hour >= 16 and d2_et.hour <= 19 and gap < 14400
            )
            spans_holiday = spans_us_holiday(d1_et.date(), d2_et.date())
            if is_normal_weekend:
                gap_type = "weekend"
            elif is_maintenance:
                gap_type = "maintenance"
            elif spans_holiday and gap < 259200:
                gap_type = "holiday"
            else:
                gap_type = "data_gap"

        gaps.append({
            "gap_start": t1,
            "gap_end": t2,
            "gap_seconds": gap,
            "gap_type": gap_type,
            "expected_bars": max(0, gap // expected_interval - 1),
        })

    return gaps


# ─── Data Maintenance Tools ──────────────────────────────────────────────────
# Standard data operations for inspecting, fixing, and cleaning bar data.
# These power the /api/data/* endpoints and the datavalid.html UI.

def delete_bars_range(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
) -> int:
    """Delete bars in a time range. Returns count deleted."""
    with _conn() as conn:
        cursor = conn.execute(
            "DELETE FROM bars WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=?",
            (symbol, timeframe, from_ts, to_ts),
        )
        return cursor.rowcount


def delete_bars_by_timestamps(
    symbol: str,
    timeframe: str,
    timestamps: List[int],
) -> int:
    """Delete specific bars by their timestamps. Returns count deleted."""
    if not timestamps:
        return 0
    placeholders = ",".join("?" * len(timestamps))
    with _conn() as conn:
        cursor = conn.execute(
            f"DELETE FROM bars WHERE symbol=? AND timeframe=? AND ts IN ({placeholders})",
            [symbol, timeframe] + timestamps,
        )
        return cursor.rowcount


def get_bar_at(symbol: str, timeframe: str, ts: int) -> Optional[dict]:
    """Get a single bar at exact timestamp. For point inspection."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT ts, open, high, low, close, volume, source, contract_month FROM bars "
            "WHERE symbol=? AND timeframe=? AND ts=?",
            (symbol, timeframe, ts),
        ).fetchone()
    if not row:
        return None
    return {
        "time": row[0], "open": row[1], "high": row[2],
        "low": row[3], "close": row[4], "volume": row[5],
        "source": row[6], "contract_month": row[7],
    }


def get_integrity_report(symbol: str, timeframe: str,
                         from_ts: int = 0, to_ts: int = MAX_TIMESTAMP) -> dict:
    """Generate a data integrity report: counts, source breakdown, OHLCV violations."""
    with _conn() as conn:
        # Total count
        total = conn.execute(
            "SELECT COUNT(*) FROM bars WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=?",
            (symbol, timeframe, from_ts, to_ts),
        ).fetchone()[0]

        # Source breakdown
        source_rows = conn.execute(
            "SELECT source, COUNT(*) FROM bars "
            "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=? "
            "GROUP BY source ORDER BY source",
            (symbol, timeframe, from_ts, to_ts),
        ).fetchall()
        sources = {r[0]: r[1] for r in source_rows}

        # OHLCV violations
        violations = conn.execute(
            "SELECT COUNT(*) FROM bars "
            "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=? "
            "AND (high < low OR open > high OR open < low OR close > high OR close < low "
            "     OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0 OR volume < 0)",
            (symbol, timeframe, from_ts, to_ts),
        ).fetchone()[0]

        # Duplicate check (should be 0 due to PK, but verify)
        dup_check = conn.execute(
            "SELECT ts, COUNT(*) as cnt FROM bars "
            "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=? "
            "GROUP BY ts HAVING cnt > 1",
            (symbol, timeframe, from_ts, to_ts),
        ).fetchall()

        # Time range
        range_row = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM bars "
            "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=?",
            (symbol, timeframe, from_ts, to_ts),
        ).fetchone()

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "total_bars": total,
        "sources": sources,
        "ohlcv_violations": violations,
        "duplicates": len(dup_check),
        "earliest_ts": range_row[0],
        "latest_ts": range_row[1],
    }


def fix_ohlcv_violations(symbol: str, timeframe: str,
                         from_ts: int = 0, to_ts: int = MAX_TIMESTAMP) -> int:
    """Fix bars where high < low by swapping. Delete bars with non-positive prices.
    Returns count of bars fixed or deleted."""
    fixed = 0
    with _conn() as conn:
        # Fix high < low (swap)
        cursor = conn.execute(
            "UPDATE bars SET high = low, low = high "
            "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=? AND high < low",
            (symbol, timeframe, from_ts, to_ts),
        )
        fixed += cursor.rowcount

        # Delete non-positive prices
        cursor = conn.execute(
            "DELETE FROM bars "
            "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=? "
            "AND (open <= 0 OR high <= 0 OR low <= 0 OR close <= 0)",
            (symbol, timeframe, from_ts, to_ts),
        )
        fixed += cursor.rowcount

        # Fix open/close outside [low, high]
        cursor = conn.execute(
            "UPDATE bars SET open = CASE "
            "  WHEN open > high THEN high "
            "  WHEN open < low THEN low "
            "  ELSE open END, "
            "close = CASE "
            "  WHEN close > high THEN high "
            "  WHEN close < low THEN low "
            "  ELSE close END "
            "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=? "
            "AND (open > high OR open < low OR close > high OR close < low)",
            (symbol, timeframe, from_ts, to_ts),
        )
        fixed += cursor.rowcount

    return fixed


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


# ─── Trade Logs (broker history) ────────────────────────────────────────────

# Fields the user is allowed to edit via the UI.
TRADE_LOG_USER_FIELDS = ("trade_type", "entry_reason", "market_cycle",
                         "sup_res", "notes")


def upsert_trade_logs(trades: List[dict]) -> int:
    """Insert parsed trades into trade_logs, preserving existing user
    annotations.  Matches existing rows by `trade_key`.
    Returns count of rows touched.
    """
    if not trades:
        return 0
    from datetime import datetime as _dt
    now_iso = _dt.utcnow().isoformat()
    count = 0
    with _conn() as conn:
        for t in trades:
            key = t.get("trade_key")
            if not key or not t.get("entry_time"):
                continue
            existing = conn.execute(
                "SELECT id FROM trade_logs WHERE trade_key=?", (key,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE trade_logs SET "
                    "  date=?, broker=?, symbol=?, contract=?, direction=?, qty=?,"
                    "  entry_time=?, exit_time=?, entry_price=?, exit_price=?,"
                    "  bars=?, pnl=?, points=?, currency=?, source_file=?, updated_at=? "
                    "WHERE trade_key=?",
                    (t.get("date", ""), t["broker"], t["symbol"],
                     t.get("contract", ""), t["direction"], t["qty"],
                     t["entry_time"], t.get("exit_time"),
                     t.get("entry_price"), t.get("exit_price"),
                     t.get("bars", 0), t.get("pnl"), t.get("points"),
                     t.get("currency", "USD"), t.get("source_file", ""),
                     now_iso, key),
                )
            else:
                conn.execute(
                    "INSERT INTO trade_logs "
                    "(trade_key, date, broker, symbol, contract, direction, qty,"
                    " entry_time, exit_time, entry_price, exit_price, bars, pnl,"
                    " points, currency, source_file, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, t.get("date", ""), t["broker"], t["symbol"],
                     t.get("contract", ""), t["direction"], t["qty"],
                     t["entry_time"], t.get("exit_time"),
                     t.get("entry_price"), t.get("exit_price"),
                     t.get("bars", 0), t.get("pnl"), t.get("points"),
                     t.get("currency", "USD"), t.get("source_file", ""),
                     now_iso, now_iso),
                )
            count += 1
    return count


def list_trade_logs(
    broker: Optional[str] = None,
    symbol: Optional[str] = None,
    date_from: Optional[str] = None,    # YYYY-MM-DD inclusive
    date_to: Optional[str] = None,
    trade_type: Optional[str] = None,
    entry_reason: Optional[str] = None,
    market_cycle: Optional[str] = None,
    sup_res: Optional[str] = None,
    source_file: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[dict]:
    """Return trade_logs rows filtered by any combination of fields."""
    sql = "SELECT * FROM trade_logs WHERE 1=1"
    params: list = []
    if broker:
        sql += " AND broker=?";          params.append(broker)
    if symbol:
        sql += " AND symbol=?";          params.append(symbol)
    if date_from:
        sql += " AND date>=?";           params.append(date_from)
    if date_to:
        sql += " AND date<=?";           params.append(date_to)
    if trade_type:
        sql += " AND trade_type=?";      params.append(trade_type)
    if entry_reason:
        sql += " AND entry_reason=?";    params.append(entry_reason)
    if market_cycle:
        sql += " AND market_cycle=?";    params.append(market_cycle)
    if sup_res:
        sql += " AND sup_res=?";         params.append(sup_res)
    if source_file:
        sql += " AND source_file=?";     params.append(source_file)
    sql += " ORDER BY entry_time DESC"
    if limit:
        sql += " LIMIT ?";               params.append(limit)
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def update_trade_log(trade_id: int, fields: dict) -> bool:
    """Patch user-input fields on a trade_log row."""
    from datetime import datetime as _dt
    cols = [c for c in TRADE_LOG_USER_FIELDS if c in fields]
    if not cols:
        return False
    set_sql = ", ".join(f"{c}=?" for c in cols) + ", updated_at=?"
    params = [fields[c] for c in cols] + [_dt.utcnow().isoformat(), trade_id]
    with _conn() as conn:
        cur = conn.execute(f"UPDATE trade_logs SET {set_sql} WHERE id=?", params)
        return cur.rowcount > 0


def delete_trade_log(trade_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM trade_logs WHERE id=?", (trade_id,))
        return cur.rowcount > 0


def delete_trade_logs_by_source(source_file: str) -> int:
    """Delete all trade_logs originating from a given source CSV file."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM trade_logs WHERE source_file=?", (source_file,))
        return cur.rowcount


def trade_log_distinct(field: str) -> List[str]:
    """Return distinct values for a column (used to build filter dropdowns)."""
    if field not in {"broker", "symbol", "trade_type", "entry_reason",
                     "market_cycle", "sup_res", "source_file"}:
        return []
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {field} FROM trade_logs "
            f"WHERE {field} IS NOT NULL AND {field}!='' "
            f"ORDER BY {field}"
        ).fetchall()
    return [r[0] for r in rows]
