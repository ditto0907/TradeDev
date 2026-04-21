#!/usr/bin/env python3
"""
Schema / data migration for the v2 refactor.

What v2 introduces
------------------

The v2 refactor ("dataManager / dataValidator / realtimeBuilder / IBfetch
as the sole write proxy") does **not** require any breaking schema
changes to the ``bars`` / ``ib_fetch_cache`` / ``realtime_bars`` tables.
What it *does* introduce is a new ``contract_calendar.active_contract``
lookup which differs from the legacy "day <= 10" heuristic — so any
``bars.contract_month`` values that were tagged by the old code near a
rollover boundary may now be stale.  This script re-derives
``contract_month`` for every row in the ``bars`` table using the new
rule set and rewrites it in-place when the new value differs.

The migration is:

  * **Idempotent** — re-running is a no-op.
  * **Versioned** — uses ``PRAGMA user_version`` so the runner knows
    what has / has not been applied.
  * **Reversible** — the only mutation is to ``bars.contract_month``;
    running ``--downgrade`` restores the legacy day-10 derivation.

Usage
-----

Apply (default)::

    python scripts/migrate_v2.py

Force a re-run even if user_version is already >= 2::

    python scripts/migrate_v2.py --force

Revert to the legacy day-10 heuristic::

    python scripts/migrate_v2.py --downgrade

Dry run (report differences, write nothing)::

    python scripts/migrate_v2.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make ``priceaction`` importable regardless of invocation directory.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import contract_calendar as cc  # noqa: E402

DB_PATH = ROOT / "data" / "tradedev.db"
SCHEMA_VERSION_V2 = 2
SCHEMA_VERSION_V1 = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("migrate_v2")


# ─── Legacy day-10 derivation (for --downgrade) ───────────────────────────────

def _legacy_contract_month(ts: int, symbol: str) -> str:
    """Reproduce the pre-v2 heuristic for reverse migration."""
    inst = config.INSTRUMENTS.get(symbol) or {}
    months = inst.get("contract_months", [3, 6, 9, 12])
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    y, m, d = dt.year, dt.month, dt.day
    for qm in months:
        if m < qm or (m == qm and d <= 10):
            return f"{y}{qm:02d}"
    return f"{y + 1}{months[0]:02d}"


# ─── Migration primitives ─────────────────────────────────────────────────────

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _get_user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA statements cannot use ? parameters; integer is sanitised.
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _walk_bars(conn: sqlite3.Connection):
    """Yield every row needed to recompute contract_month."""
    yield from conn.execute(
        "SELECT symbol, timeframe, ts, contract_month FROM bars"
    )


def _rewrite_contract_months(
    conn: sqlite3.Connection,
    derive_fn,
    dry_run: bool,
) -> dict:
    """Recompute ``contract_month`` for every bar.  Returns counters dict."""
    updates: list[tuple] = []
    total = 0
    changed = 0
    for row in _walk_bars(conn):
        total += 1
        sym = row["symbol"]
        ts = row["ts"]
        tf = row["timeframe"]
        current = row["contract_month"] or ""
        try:
            new_val = derive_fn(ts, sym)
        except Exception as e:
            logger.debug("skip %s/%s ts=%s: %s", sym, tf, ts, e)
            continue
        if new_val and new_val != current:
            changed += 1
            updates.append((new_val, sym, tf, ts))

    logger.info("Scanned %d bars, %d need update.", total, changed)
    if dry_run or not updates:
        return {"scanned": total, "changed": changed, "written": 0}

    conn.executemany(
        "UPDATE bars SET contract_month=? WHERE symbol=? AND timeframe=? AND ts=?",
        updates,
    )
    conn.commit()
    return {"scanned": total, "changed": changed, "written": len(updates)}


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DB_PATH), help="Path to SQLite DB")
    ap.add_argument("--force", action="store_true",
                    help="Re-run even if user_version is already >= 2")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report changes without writing")
    ap.add_argument("--downgrade", action="store_true",
                    help="Revert to the legacy day-10 heuristic")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        logger.error("DB not found at %s — nothing to migrate.", db_path)
        return 1

    conn = _connect(db_path)
    try:
        current_version = _get_user_version(conn)
        logger.info("DB: %s  (current user_version=%d)", db_path, current_version)

        if args.downgrade:
            logger.info("Downgrade: rewriting contract_month using legacy day-10 heuristic")
            stats = _rewrite_contract_months(conn, _legacy_contract_month, args.dry_run)
            if not args.dry_run:
                _set_user_version(conn, SCHEMA_VERSION_V1)
                conn.commit()
            logger.info("Downgrade complete: %s", stats)
            return 0

        if current_version >= SCHEMA_VERSION_V2 and not args.force:
            logger.info("Already at v%d — nothing to do (use --force to re-run).",
                        SCHEMA_VERSION_V2)
            return 0

        logger.info("Applying v2 migration: rewrite contract_month "
                    "using contract_calendar.active_contract")
        stats = _rewrite_contract_months(
            conn, cc.active_contract, args.dry_run,
        )
        if not args.dry_run:
            _set_user_version(conn, SCHEMA_VERSION_V2)
            conn.commit()
        logger.info("v2 migration complete: %s", stats)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
